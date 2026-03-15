"""Flask sentiment-analysis API powered by Ollama via LangChain.

Endpoints:
  POST /analyze  – accepts JSON {"text": "<string>"}, returns sentiment label.
  GET  /metrics  – Prometheus metrics (via prometheus_client).
  GET  /health   – liveness probe.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from flask import Flask, jsonify, request
from prometheus_client import make_wsgi_app
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from fault_injector import fault_injector
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


@app.get("/health")
def health() -> Any:
    """Liveness probe."""
    return jsonify({"status": "ok"})


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
