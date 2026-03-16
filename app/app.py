"""Flask sentiment-analysis API powered by Ollama via LangChain.

Endpoints:
  POST /analyze  – accepts JSON {"text": "<string>"}, returns sentiment label.
  GET  /metrics  – Prometheus metrics (via prometheus_client).
  GET  /health   – liveness probe.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

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

_llm: Any | None = None


def _get_llm() -> Any:
    """Lazy-load the Ollama LLM wrapper."""
    global _llm  # noqa: PLW0603
    if _llm is None:
        try:
            from langchain_ollama import OllamaLLM  # type: ignore[import]

            _llm = OllamaLLM(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL)
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
        return float(result[0]["value"][1])
    except Exception:
        return None


def _recent_loki_logs(limit: int = 20) -> list[str]:
    """Return recent log lines from Loki, when available."""
    params = urllib.parse.urlencode(
        {
            "query": "{job=~\".*\"}",
            "limit": str(limit),
            "start": str(int((time.time() - 300) * 1e9)),
        }
    )
    ok, _, _, data = _http_json(f"{LOKI_URL}/loki/api/v1/query_range?{params}")
    if not ok:
        return [f"Loki unavailable at {LOKI_URL}"]

    lines: list[str] = []
    for stream in data.get("data", {}).get("result", []):
        for _, message in stream.get("values", []):
            lines.append(message.strip())
    if not lines:
        return ["No logs found in Loki for the last 5 minutes."]
    return lines[-limit:]


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

    llm = _get_llm() if INCIDENT_USE_LLM else None
    if llm is None:
        return {
            "source": "heuristic",
            "root_cause": "No critical signal detected" if not error_rps else "Elevated request errors",
            "reasoning": (
                "Fallback heuristic used because LLM is unavailable. "
                f"Snapshot: {json.dumps(snapshot)}"
            ),
            "remediation_script": "kubectl rollout restart deployment/sentiment-app -n default",
            "snapshot": snapshot,
        }

    prompt = (
        "You are an SRE assistant. Given this JSON snapshot, return valid JSON only with keys "
        "root_cause, reasoning, remediation_script, severity. Keep remediation_script to kubectl commands only.\n\n"
        f"snapshot={json.dumps(snapshot)}"
    )
    try:
        raw = str(llm.invoke(prompt)).strip()
        parsed = json.loads(raw)
        parsed["source"] = "llm"
        parsed["snapshot"] = snapshot
        return parsed
    except Exception:
        return {
            "source": "llm-fallback",
            "root_cause": "Model response parsing failed",
            "reasoning": "LLM returned non-JSON output; falling back to safe default remediation.",
            "remediation_script": "kubectl rollout restart deployment/sentiment-app -n default",
            "snapshot": snapshot,
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
def ui_incident_assistant() -> Any:
    """Return incident triage summary with suggested remediation script."""
    return jsonify(_build_incident_response())


@app.post("/ui/api/failure-rate")
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
