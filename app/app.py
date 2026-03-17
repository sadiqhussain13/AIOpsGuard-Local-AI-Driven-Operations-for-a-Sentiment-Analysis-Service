"""Flask sentiment-analysis API powered by Ollama via LangChain.

Endpoints:
  POST /analyze  – accepts JSON {"text": "<string>"}, returns sentiment label.
  GET  /metrics  – Prometheus metrics (via prometheus_client).
  GET  /health   – liveness probe.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import sqlite3
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request
from prometheus_client import make_wsgi_app
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from fault_injector import _get_failure_rate, fault_injector
from metrics import REQUEST_ERROR, REQUEST_LATENCY, REQUEST_SUCCESS, REQUEST_TOTAL

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


class _LokiHandler(logging.Handler):
    """Lightweight log handler that pushes entries to Loki's HTTP push API.

    Flushes every message immediately to work correctly in
    pre-forked servers like gunicorn (daemon threads don't survive fork).
    """

    def __init__(self, url: str, labels: dict[str, str] | None = None):
        super().__init__()
        self._url = url.rstrip("/") + "/loki/api/v1/push"
        self._labels = labels or {"job": "sentiment-app"}

    def emit(self, record: logging.LogRecord) -> None:
        ts = str(int(record.created * 1e9))
        line = self.format(record)
        body = json.dumps(
            {"streams": [{"stream": self._labels, "values": [[ts, line]]}]}
        ).encode()
        req = urllib.request.Request(
            self._url, data=body, headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass  # best-effort; don't break the app if Loki is down

# ---------------------------------------------------------------------------
# LangChain / Ollama
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL: str = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.environ.get("OLLAMA_MODEL", "mistral")
PROMETHEUS_URL: str = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL: str = os.environ.get("LOKI_URL", "http://localhost:3100")
MLFLOW_URL: str = os.environ.get("MLFLOW_URL", "http://localhost:5001")
ANOMALY_DETECTOR_URL: str = os.environ.get(
    "ANOMALY_DETECTOR_URL", "http://localhost:8080"
)
INCIDENT_USE_LLM: bool = os.environ.get("INCIDENT_USE_LLM", "0") == "1"
DEPLOY_MODE: str = os.environ.get("DEPLOY_MODE", "compose")  # "compose" or "k8s"

# Attach Loki log handler so app logs are queryable in Loki
_loki_handler = _LokiHandler(LOKI_URL, labels={"job": "sentiment-app", "service": "sentiment-app"})
_loki_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().addHandler(_loki_handler)

# Auth
UI_API_KEY: str = os.environ.get("UI_API_KEY", "")

# Incident history DB
DB_PATH: str = os.environ.get("INCIDENT_DB_PATH", "/app/data/incidents.db")
_db_lock = threading.Lock()


def _init_db() -> None:
    """Create the incidents table if it does not yet exist."""
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp          TEXT NOT NULL,
                    root_cause         TEXT,
                    reasoning          TEXT,
                    remediation_script TEXT,
                    source             TEXT,
                    severity           TEXT
                )
                """
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not initialise incident DB at %s: %s", DB_PATH, exc)


def _save_incident(result: dict) -> None:
    """Persist an incident analysis record to SQLite."""
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with _db_lock, sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO incidents "
                "(timestamp, root_cause, reasoning, remediation_script, source, severity) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    result.get("root_cause", ""),
                    result.get("reasoning", ""),
                    result.get("remediation_script", ""),
                    result.get("source", ""),
                    result.get("severity", "unknown"),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not save incident: %s", exc)


def require_operator(f):
    """Decorator: enforce X-API-Key when UI_API_KEY env var is set."""
    @wraps(f)
    def _inner(*args, **kwargs):
        if UI_API_KEY:
            provided = request.headers.get("X-API-Key", "")
            if provided != UI_API_KEY:
                return (
                    jsonify({"error": "Unauthorized: valid X-API-Key header required"}),
                    401,
                )
        return f(*args, **kwargs)
    return _inner


_llm: Any | None = None


def _get_llm() -> Any:
    """Lazy-load the Ollama LLM wrapper."""
    global _llm  # noqa: PLW0603
    if _llm is None:
        try:
            from langchain_ollama import OllamaLLM  # type: ignore[import]

            _llm = OllamaLLM(
                model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL,
                timeout=30,
            )
            logger.info("Ollama LLM initialised (model=%s)", OLLAMA_MODEL)
        except Exception as exc:  # pragma: no cover
            logger.warning("Could not load Ollama LLM: %s", exc)
            _llm = None
    return _llm


SENTIMENT_PROMPT_TEMPLATE: str = (
    "Classify the sentiment of the following text as exactly one of: "
    "positive, negative, or neutral.\n\n"
    'Text: "{text}"\n\n'
    "Respond with a single word only."
)

VALID_LABELS: frozenset[str] = frozenset({"positive", "negative", "neutral"})
FEATURE_NAMES: tuple[str, ...] = (
    "response_time_ms",
    "cpu_usage_pct",
    "memory_usage_pct",
    "error_rate",
    "request_count",
)


def _http_json(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | list[Any] | None = None,
    timeout: float = 3.0,
) -> tuple[bool, int, float, Any]:
    """Execute an HTTP request and decode JSON when possible."""
    start = time.perf_counter()
    headers: dict[str, str] = {}
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url=url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            elapsed_ms = (time.perf_counter() - start) * 1000
            try:
                return True, int(resp.status), elapsed_ms, json.loads(raw)
            except json.JSONDecodeError:
                return True, int(resp.status), elapsed_ms, {"raw": raw}
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return False, int(exc.code), elapsed_ms, {"error": str(exc)}
    except Exception as exc:  # pragma: no cover
        elapsed_ms = (time.perf_counter() - start) * 1000
        return False, 0, elapsed_ms, {"error": str(exc)}


import math


def _prom_query_scalar(expression: str) -> float | None:
    """Query a scalar-like Prometheus expression and return a float."""
    qs = urllib.parse.urlencode({"query": expression})
    ok, _, _, data = _http_json(f"{PROMETHEUS_URL}/api/v1/query?{qs}")
    if not ok:
        return None
    try:
        result = data.get("data", {}).get("result", [])
        if not result:
            return None
        val = float(result[0]["value"][1])
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    except Exception:
        return None


def _recent_loki_logs(limit: int = 20) -> list[str]:
    """Return recent log lines from Loki, when available."""
    # First check if Loki is reachable at all
    ready_ok, _, _, _ = _http_json(f"{LOKI_URL}/ready")
    if not ready_ok:
        return [f"Loki unavailable at {LOKI_URL}"]

    params = urllib.parse.urlencode(
        {
            "query": '{job=~".+"}',
            "limit": str(limit),
            "start": str(int((time.time() - 300) * 1e9)),
        }
    )
    ok, _, _, data = _http_json(f"{LOKI_URL}/loki/api/v1/query_range?{params}")
    if not ok:
        return ["Loki is reachable but returned no log streams (check log shipping)."]

    lines: list[str] = []
    for stream in data.get("data", {}).get("result", []):
        for _, message in stream.get("values", []):
            lines.append(message.strip())
    if not lines:
        return ["No logs found in Loki for the last 5 minutes."]
    return lines[-limit:]


def _restart_command(service: str = "sentiment-app") -> str:
    """Return the correct restart command for the current deployment mode."""
    if DEPLOY_MODE == "k8s":
        return f"kubectl rollout restart deployment/{service} -n default"
    return f"docker compose restart {service}"


def _heuristic_analysis(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Produce a rule-based incident analysis from the telemetry snapshot."""
    issues: list[str] = []
    severity = "info"
    remediation_parts: list[str] = []

    # -- error rate ---------------------------------------------------------
    error_rps = snapshot.get("error_rps")
    if error_rps and error_rps > 0:
        issues.append(f"Elevated error rate ({error_rps:.4f} err/s)")
        severity = "warning" if error_rps < 1.0 else "critical"
        remediation_parts.append(_restart_command("sentiment-app"))

    # -- p95 latency -------------------------------------------------------
    p95 = snapshot.get("p95_latency_sec")
    if p95 is not None and p95 > 2.0:
        issues.append(f"High p95 latency ({p95:.2f}s)")
        if severity == "info":
            severity = "warning"

    # -- anomaly detector --------------------------------------------------
    anomaly = snapshot.get("anomaly") or {}
    if "error" in anomaly:
        issues.append(f"Anomaly detector error: {anomaly['error']}")
        remediation_parts.append(_restart_command("anomaly-detector"))
    elif anomaly.get("anomaly") is True:
        issues.append("Anomaly detector flagged current metrics as anomalous")
        severity = "critical" if severity != "critical" else severity

    # -- loki logs ---------------------------------------------------------
    logs = snapshot.get("logs", [])
    log_issues = [l for l in logs if "unavailable" in l.lower() or "error" in l.lower()]
    if log_issues:
        issues.append(f"{len(log_issues)} warning(s) in recent logs")

    root_cause = "; ".join(issues) if issues else "No critical signal detected"
    if not remediation_parts:
        remediation_parts.append("# No immediate action required")

    return {
        "source": "heuristic",
        "root_cause": root_cause,
        "severity": severity,
        "reasoning": f"Automated heuristic analysis. Snapshot: {json.dumps(snapshot)}",
        "remediation_script": "\n".join(remediation_parts),
        "snapshot": snapshot,
    }


def _build_incident_response() -> dict[str, Any]:
    """Generate a compact incident analysis from metrics, logs, and model output."""
    request_rps = _prom_query_scalar('sum(rate(request_total{endpoint="/analyze"}[5m]))')
    error_rps = _prom_query_scalar('sum(rate(request_error{endpoint="/analyze"}[5m]))')
    p95_latency = _prom_query_scalar(
        'histogram_quantile(0.95, sum by (le) (rate(request_latency_seconds_bucket{endpoint="/analyze"}[5m])))'
    )

    sample_vector = [200.0, 40.0, 50.0, 0.02, 300.0]
    _, _, _, anomaly_data = _http_json(
        f"{ANOMALY_DETECTOR_URL}/predict", method="POST", payload=sample_vector
    )

    logs = _recent_loki_logs()
    snapshot = {
        "request_rps": request_rps,
        "error_rps": error_rps,
        "p95_latency_sec": p95_latency,
        "anomaly": anomaly_data,
        "logs": logs[-8:],
    }

    # ---- heuristic path (LLM disabled or unavailable) --------------------
    llm = _get_llm() if INCIDENT_USE_LLM else None
    if llm is None:
        return _heuristic_analysis(snapshot)

    # ---- LLM path --------------------------------------------------------
    mode_hint = (
        "docker compose" if DEPLOY_MODE == "compose" else "kubectl"
    )
    prompt = (
        "You are an SRE assistant. Given this JSON snapshot, return valid JSON only with keys "
        f"root_cause, reasoning, remediation_script, severity. Use {mode_hint} commands for remediation_script.\n\n"
        f"snapshot={json.dumps(snapshot)}"
    )
    try:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(lambda: str(llm.invoke(prompt)).strip())
        raw = future.result(timeout=45)
        pool.shutdown(wait=False)
        parsed = json.loads(raw)
        parsed["source"] = "llm"
        parsed["snapshot"] = snapshot
        return parsed
    except (concurrent.futures.TimeoutError, TimeoutError):
        pool.shutdown(wait=False)
        logger.warning("LLM timed out; falling back to heuristic")
        return _heuristic_analysis(snapshot)
    except Exception:
        return {
            "source": "llm-fallback",
            "root_cause": "Model response parsing failed",
            "reasoning": "LLM returned non-JSON output; falling back to heuristic.",
            **_heuristic_analysis(snapshot),
        }


def _classify_sentiment(text: str) -> str:
    """Call the Ollama model and return a normalised sentiment label.

    Falls back to ``"neutral"`` when the LLM is unavailable or returns an
    unexpected value.

    Args:
        text: Input text to classify.

    Returns:
        One of ``"positive"``, ``"negative"``, or ``"neutral"``.
    """
    llm = _get_llm()
    if llm is None:
        logger.warning("LLM unavailable; defaulting to 'neutral'")
        return "neutral"

    prompt = SENTIMENT_PROMPT_TEMPLATE.format(text=text)
    try:
        raw: str = llm.invoke(prompt)
        label = raw.strip().lower().split()[0] if raw.strip() else "neutral"
        if label not in VALID_LABELS:
            logger.warning("Unexpected LLM output %r; defaulting to 'neutral'", raw)
            label = "neutral"
        return label
    except Exception as exc:  # pragma: no cover
        logger.error("LLM inference error: %s", exc)
        return "neutral"


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------
app = Flask(__name__)

# Initialise the incident history database at startup (best-effort).
_init_db()


@app.after_request
def _log_request(response):
    """Log every HTTP request so Loki has visibility into traffic."""
    logger.info("%s %s %s", request.method, request.path, response.status_code)
    return response


@app.get("/")
def index() -> Any:
    """Redirect root to the interactive operations dashboard."""
    return redirect("/ui")


@app.get("/ui")
def ui_dashboard() -> Any:
    """Serve the interactive AIOps dashboard."""
    return render_template("dashboard.html")


@app.get("/health")
def health() -> Any:
    """Liveness probe."""
    return jsonify({"status": "ok"})


@app.get("/ui/api/services")
def ui_services() -> Any:
    """Return live health status for all core dependencies."""
    checks: dict[str, tuple[str, str]] = {
        "app": ("GET", "http://localhost:5000/health"),
        "prometheus": ("GET", f"{PROMETHEUS_URL}/-/healthy"),
        "loki": ("GET", f"{LOKI_URL}/ready"),
        "mlflow": ("GET", f"{MLFLOW_URL}/"),
        "ollama": ("GET", f"{OLLAMA_BASE_URL}/api/tags"),
        "anomaly-detector": ("GET", f"{ANOMALY_DETECTOR_URL}/health"),
    }
    output: dict[str, Any] = {}
    for name, (method, url) in checks.items():
        ok, status_code, latency_ms, payload = _http_json(url=url, method=method)
        output[name] = {
            "ok": ok,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 2),
            "detail": payload,
        }
    output["failure_rate"] = _get_failure_rate()
    return jsonify(output)


@app.get("/ui/api/metrics")
def ui_metrics() -> Any:
    """Return key Prometheus metrics for dashboard summary cards."""
    summary = {
        "request_rps": _prom_query_scalar(
            'sum(rate(request_total{endpoint="/analyze"}[5m]))'
        ),
        "error_rps": _prom_query_scalar(
            'sum(rate(request_error{endpoint="/analyze"}[5m]))'
        ),
        "p95_latency_sec": _prom_query_scalar(
            'histogram_quantile(0.95, sum by (le) (rate(request_latency_seconds_bucket{endpoint="/analyze"}[5m])))'
        ),
        "anomaly_predict_rps": _prom_query_scalar(
            "sum(rate(anomaly_predict_total[5m]))"
        ),
        "app_up": _prom_query_scalar('up{job="sentiment-app"}'),
        "anomaly_up": _prom_query_scalar('up{job="anomaly-detector"}'),
    }
    return jsonify(summary)


@app.post("/ui/api/anomaly")
def ui_anomaly_predict() -> Any:
    """Proxy anomaly-prediction calls from the dashboard UI."""
    payload = request.get_json(silent=True) or {}
    features: list[float]
    if isinstance(payload, dict) and isinstance(payload.get("features"), list):
        features = [float(x) for x in payload["features"]]
    elif isinstance(payload, dict):
        features = [float(payload.get(name, 0.0)) for name in FEATURE_NAMES]
    else:
        return jsonify({"error": "Invalid payload"}), 400

    if len(features) != 5:
        return jsonify({"error": "Expected 5 feature values"}), 400

    ok, status_code, latency_ms, out = _http_json(
        f"{ANOMALY_DETECTOR_URL}/predict", method="POST", payload=features
    )
    if not ok:
        return (
            jsonify(
                {
                    "error": "Anomaly detector request failed",
                    "detail": out,
                    "latency_ms": round(latency_ms, 2),
                }
            ),
            max(500, status_code),
        )
    return jsonify({"latency_ms": round(latency_ms, 2), "result": out})


@app.get("/ui/api/incident")
@require_operator
def ui_incident_assistant() -> Any:
    """Return incident triage summary with suggested remediation script."""
    result = _build_incident_response()
    _save_incident(result)
    return jsonify(result)


@app.post("/ui/api/failure-rate")
@require_operator
def ui_set_failure_rate() -> Any:
    """Update runtime fault-injection failure rate (0.0 to 1.0)."""
    payload = request.get_json(silent=True) or {}
    try:
        rate = float(payload.get("failure_rate"))
    except (TypeError, ValueError):
        return jsonify({"error": "failure_rate must be a number"}), 400
    if rate < 0.0 or rate > 1.0:
        return jsonify({"error": "failure_rate must be between 0.0 and 1.0"}), 400

    os.environ["FAILURE_RATE"] = str(rate)
    return jsonify({"failure_rate": rate, "status": "updated"})


@app.get("/ui/api/incident/history")
def ui_incident_history() -> Any:
    """Return the last 50 incidents from the persistent store, newest first."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, timestamp, root_cause, source, severity "
                "FROM incidents ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as exc:
        return jsonify({"error": str(exc), "items": []}), 500


@app.post("/analyze")
@fault_injector
def analyze() -> Any:
    """Classify the sentiment of the provided text.

    Request body (JSON)::

        {"text": "<string>"}

    Returns:
        JSON response with ``sentiment`` key and optional ``error`` key.
    """
    endpoint = "/analyze"
    method = "POST"
    REQUEST_TOTAL.labels(method=method, endpoint=endpoint).inc()

    start = time.perf_counter()
    try:
        payload = request.get_json(silent=True)
        if not payload or "text" not in payload:
            REQUEST_ERROR.labels(
                method=method, endpoint=endpoint, status_code="400"
            ).inc()
            return jsonify({"error": "Missing 'text' field in JSON body"}), 400

        text: str = str(payload["text"])
        logger.info("Analysing text (len=%d)", len(text))

        sentiment = _classify_sentiment(text)

        REQUEST_SUCCESS.labels(method=method, endpoint=endpoint).inc()
        logger.info("Sentiment result: %s", sentiment)
        return jsonify({"sentiment": sentiment})

    except Exception as exc:  # pragma: no cover
        REQUEST_ERROR.labels(
            method=method, endpoint=endpoint, status_code="500"
        ).inc()
        logger.exception("Unhandled error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        elapsed = time.perf_counter() - start
        REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(elapsed)


# ---------------------------------------------------------------------------
# Prometheus metrics at /metrics (via WSGI middleware)
# ---------------------------------------------------------------------------
application = DispatcherMiddleware(
    app,
    {"/metrics": make_wsgi_app()},
)


def create_app() -> Flask:
    """Return the Flask application instance (used by tests)."""
    return app


if __name__ == "__main__":
    import uvicorn
    from asgiref.wsgi import WsgiToAsgi  # type: ignore[import]

    asgi_app = WsgiToAsgi(application)
    uvicorn.run(
        asgi_app,
        host="0.0.0.0",  # noqa: S104
        port=int(os.environ.get("PORT", "5000")),
        log_level="info",
    )
