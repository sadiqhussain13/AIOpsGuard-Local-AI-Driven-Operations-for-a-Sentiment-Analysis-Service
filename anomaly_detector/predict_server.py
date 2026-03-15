"""Anomaly-detector prediction server.

Loads the DVC-tracked IsolationForest model and exposes:

  POST /predict  – accepts JSON array of metric values, returns anomaly flag.
  GET  /health   – liveness probe.
  GET  /metrics  – Prometheus metrics.
"""

from __future__ import annotations

import logging
import os
import pickle
import time
from pathlib import Path
from typing import Any

import numpy as np
from flask import Flask, jsonify, request
from prometheus_client import Counter, Histogram, make_wsgi_app
from werkzeug.middleware.dispatcher import DispatcherMiddleware

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
PREDICT_TOTAL = Counter("anomaly_predict_total", "Total prediction requests")
PREDICT_ANOMALY = Counter("anomaly_predict_anomaly_total", "Predictions marked anomaly")
PREDICT_LATENCY = Histogram(
    "anomaly_predict_latency_seconds",
    "Prediction latency",
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0],
)

# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------
MODEL_PATH: str = os.environ.get("MODEL_PATH", "model/anomaly_model.pkl")
_model: Any | None = None


def _load_model() -> Any | None:
    """Load the pickled IsolationForest pipeline from disk.

    Reads ``MODEL_PATH`` at call time so that environment-variable overrides
    (e.g. in tests) are respected even after module import.

    Returns:
        Loaded sklearn pipeline, or None if the file does not exist.
    """
    path = Path(os.environ.get("MODEL_PATH", MODEL_PATH))
    if not path.exists():
        logger.warning("Model file not found at %s; predictions will fail", path)
        return None
    with open(path, "rb") as fh:
        model = pickle.load(fh)  # noqa: S301
    logger.info("Model loaded from %s", path)
    return model


def get_model() -> Any | None:
    """Lazy-load and cache the model."""
    global _model  # noqa: PLW0603
    if _model is None:
        _model = _load_model()
    return _model


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------
app = Flask(__name__)

# Expected number of features (must match training)
N_FEATURES: int = 5  # response_time_ms, cpu_usage_pct, memory_usage_pct, error_rate, request_count


@app.get("/health")
def health() -> Any:
    """Liveness probe."""
    return jsonify({"status": "ok", "model_loaded": get_model() is not None})


@app.post("/predict")
def predict() -> Any:
    """Predict whether a metric vector is anomalous.

    Request body (JSON)::

        [<response_time_ms>, <cpu_usage_pct>, <memory_usage_pct>,
         <error_rate>, <request_count>]

    Returns:
        JSON with ``anomaly`` (bool) and ``score`` (float).
    """
    PREDICT_TOTAL.inc()
    start = time.perf_counter()

    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, list) or len(payload) != N_FEATURES:
            return (
                jsonify(
                    {
                        "error": (
                            f"Expected a JSON array of {N_FEATURES} numeric values"
                        )
                    }
                ),
                400,
            )

        model = get_model()
        if model is None:
            return jsonify({"error": "Model not loaded"}), 503

        X = np.array(payload, dtype=float).reshape(1, -1)
        pred: int = int(model.predict(X)[0])  # 1=normal, -1=anomaly
        score: float = float(
            model.named_steps["iso_forest"].score_samples(
                model.named_steps["scaler"].transform(X)
            )[0]
        )
        is_anomaly = pred == -1
        if is_anomaly:
            PREDICT_ANOMALY.inc()

        logger.info("predict anomaly=%s score=%.4f", is_anomaly, score)
        return jsonify({"anomaly": is_anomaly, "score": round(score, 6)})

    except Exception as exc:
        logger.exception("Prediction error: %s", exc)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        PREDICT_LATENCY.observe(time.perf_counter() - start)


# ---------------------------------------------------------------------------
# WSGI app with Prometheus /metrics endpoint
# ---------------------------------------------------------------------------
application = DispatcherMiddleware(app, {"/metrics": make_wsgi_app()})

if __name__ == "__main__":
    import uvicorn
    from asgiref.wsgi import WsgiToAsgi  # type: ignore[import]

    asgi_app = WsgiToAsgi(application)
    uvicorn.run(
        asgi_app,
        host="0.0.0.0",  # noqa: S104
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )
