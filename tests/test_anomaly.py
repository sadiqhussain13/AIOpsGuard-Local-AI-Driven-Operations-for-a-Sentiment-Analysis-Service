"""Unit tests for the anomaly-detector model training and prediction server."""

from __future__ import annotations

import os
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Allow importing from anomaly_detector/
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "anomaly_detector"),
)

from train_anomaly_model import (  # noqa: E402
    FEATURE_COLS,
    build_pipeline,
    evaluate,
    load_data,
)


# ---------------------------------------------------------------------------
# Training helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_csv(tmp_path: Path) -> Path:
    """Create a small synthetic CSV for testing."""
    rng = np.random.default_rng(42)
    n = 200
    df = pd.DataFrame(
        {
            "response_time_ms": rng.normal(200, 30, n),
            "cpu_usage_pct": rng.normal(40, 10, n),
            "memory_usage_pct": rng.normal(50, 10, n),
            "error_rate": rng.uniform(0, 0.05, n),
            "request_count": rng.integers(50, 500, n).astype(float),
        }
    )
    csv_file = tmp_path / "logs.csv"
    df.to_csv(csv_file, index=False)
    return csv_file


def test_load_data_returns_dataframe(sample_csv: Path) -> None:
    """load_data should return a DataFrame with expected columns."""
    df = load_data(str(sample_csv))
    assert set(df.columns) == set(FEATURE_COLS)
    assert len(df) > 0


def test_load_data_missing_column(tmp_path: Path) -> None:
    """load_data should raise ValueError when a required column is missing."""
    bad_csv = tmp_path / "bad.csv"
    pd.DataFrame({"col1": [1, 2, 3]}).to_csv(bad_csv, index=False)
    with pytest.raises(ValueError, match="missing required columns"):
        load_data(str(bad_csv))


def test_build_pipeline_fits(sample_csv: Path) -> None:
    """Pipeline should fit without errors and produce predictions."""
    df = load_data(str(sample_csv))
    X = df.values
    pipeline = build_pipeline(contamination=0.05)
    pipeline.fit(X)
    preds = pipeline.predict(X)
    assert set(preds).issubset({1, -1})


def test_evaluate_returns_dict(sample_csv: Path) -> None:
    """evaluate() should return a dict with expected keys."""
    df = load_data(str(sample_csv))
    X = df.values
    pipeline = build_pipeline(contamination=0.05)
    pipeline.fit(X)
    metrics = evaluate(pipeline, X)
    assert "anomaly_count" in metrics
    assert "anomaly_rate" in metrics
    assert 0.0 <= metrics["anomaly_rate"] <= 1.0


# ---------------------------------------------------------------------------
# Prediction server
# ---------------------------------------------------------------------------


@pytest.fixture()
def model_file(tmp_path: Path, sample_csv: Path) -> Path:
    """Train a small model and return the path to the pickled file."""
    df = load_data(str(sample_csv))
    X = df.values
    pipeline = build_pipeline(contamination=0.05)
    pipeline.fit(X)
    pkl = tmp_path / "anomaly_model.pkl"
    with open(pkl, "wb") as fh:
        pickle.dump(pipeline, fh)
    return pkl


def test_predict_server_health(model_file: Path) -> None:
    """GET /health should return 200."""
    os.environ["MODEL_PATH"] = str(model_file)
    import predict_server  # noqa: PLC0415

    predict_server._model = None  # reset cached model
    flask_app = predict_server.app
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["model_loaded"] is True


def test_predict_server_normal_input(model_file: Path) -> None:
    """POST /predict with normal-range values should return anomaly=False (usually)."""
    os.environ["MODEL_PATH"] = str(model_file)
    import predict_server  # noqa: PLC0415

    predict_server._model = None
    flask_app = predict_server.app
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        resp = client.post(
            "/predict",
            json=[200.0, 40.0, 50.0, 0.01, 300.0],
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "anomaly" in data
        assert isinstance(data["anomaly"], bool)
        assert "score" in data


def test_predict_server_bad_input(model_file: Path) -> None:
    """POST /predict with wrong number of features should return 400."""
    os.environ["MODEL_PATH"] = str(model_file)
    import predict_server  # noqa: PLC0415

    predict_server._model = None
    flask_app = predict_server.app
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        resp = client.post("/predict", json=[1.0, 2.0])
        assert resp.status_code == 400


def test_predict_server_missing_model(tmp_path: Path) -> None:
    """POST /predict when model file is missing should return 503."""
    os.environ["MODEL_PATH"] = str(tmp_path / "nonexistent.pkl")
    import predict_server  # noqa: PLC0415

    predict_server._model = None
    flask_app = predict_server.app
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        resp = client.post("/predict", json=[200.0, 40.0, 50.0, 0.01, 300.0])
        assert resp.status_code == 503
