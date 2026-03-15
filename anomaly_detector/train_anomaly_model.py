"""Synthetic log anomaly-detection model training script.

Uses scikit-learn IsolationForest trained on synthetic log metrics.
Logs the model artefact to MLflow and saves it for DVC tracking.

Usage::

    python train_anomaly_model.py [--data data/logs.csv] [--output model/anomaly_model.pkl]
"""

from __future__ import annotations

import argparse
import logging
import os
import pickle
from pathlib import Path
from typing import Any

import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature columns expected in the CSV
# ---------------------------------------------------------------------------
FEATURE_COLS: list[str] = [
    "response_time_ms",
    "cpu_usage_pct",
    "memory_usage_pct",
    "error_rate",
    "request_count",
]

MLFLOW_TRACKING_URI: str = os.environ.get(
    "MLFLOW_TRACKING_URI", "http://localhost:5001"
)
MLFLOW_EXPERIMENT: str = os.environ.get(
    "MLFLOW_EXPERIMENT", "aiopsguard-anomaly-detection"
)


def load_data(csv_path: str) -> pd.DataFrame:
    """Load feature data from a CSV file.

    Args:
        csv_path: Path to the CSV containing log metrics.

    Returns:
        DataFrame with expected feature columns.
    """
    df = pd.read_csv(csv_path)
    missing = set(FEATURE_COLS) - set(df.columns)
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    return df[FEATURE_COLS].dropna()


def build_pipeline(contamination: float = 0.05) -> Pipeline:
    """Build the scikit-learn pipeline.

    Args:
        contamination: Expected proportion of anomalies in the dataset.

    Returns:
        sklearn Pipeline with scaler and IsolationForest.
    """
    return Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "iso_forest",
                IsolationForest(
                    n_estimators=200,
                    contamination=contamination,
                    random_state=42,
                    n_jobs=-1,
                ),
            ),
        ]
    )


def evaluate(pipeline: Pipeline, X: np.ndarray) -> dict[str, Any]:
    """Compute basic statistics on anomaly scores.

    Args:
        pipeline: Fitted pipeline.
        X: Feature matrix.

    Returns:
        Dictionary of evaluation metrics.
    """
    scores: np.ndarray = pipeline.named_steps["iso_forest"].score_samples(
        pipeline.named_steps["scaler"].transform(X)
    )
    preds: np.ndarray = pipeline.predict(X)
    anomaly_count = int((preds == -1).sum())
    return {
        "anomaly_count": anomaly_count,
        "anomaly_rate": round(anomaly_count / len(preds), 4),
        "score_mean": round(float(scores.mean()), 4),
        "score_std": round(float(scores.std()), 4),
    }


def train(data_path: str, output_path: str) -> None:
    """Train the anomaly detection model and log it to MLflow.

    Args:
        data_path: Path to the input CSV.
        output_path: Path where the pickled model will be saved.
    """
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    mlflow.set_experiment(MLFLOW_EXPERIMENT)

    logger.info("Loading data from %s", data_path)
    df = load_data(data_path)
    X: np.ndarray = df.values
    logger.info("Dataset shape: %s", X.shape)

    with mlflow.start_run(run_name="isolation-forest-training"):
        contamination = 0.05
        mlflow.log_param("n_estimators", 200)
        mlflow.log_param("contamination", contamination)
        mlflow.log_param("random_state", 42)
        mlflow.log_param("data_path", data_path)
        mlflow.log_param("n_samples", len(X))

        pipeline = build_pipeline(contamination)
        logger.info("Training IsolationForest …")
        pipeline.fit(X)

        metrics = evaluate(pipeline, X)
        mlflow.log_metrics(metrics)
        logger.info("Training metrics: %s", metrics)

        # Save artefact locally (tracked by DVC)
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "wb") as fh:
            pickle.dump(pipeline, fh)
        logger.info("Model saved to %s", output_file)

        # Log to MLflow model registry
        mlflow.sklearn.log_model(
            pipeline,
            artifact_path="anomaly_model",
            registered_model_name="AIOpsGuard-AnomalyDetector",
        )
        logger.info("Model logged to MLflow experiment=%s", MLFLOW_EXPERIMENT)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Train anomaly detection model")
    parser.add_argument(
        "--data",
        default="data/logs.csv",
        help="Path to synthetic log CSV (default: data/logs.csv)",
    )
    parser.add_argument(
        "--output",
        default="model/anomaly_model.pkl",
        help="Output path for pickled model (default: model/anomaly_model.pkl)",
    )
    args = parser.parse_args()
    train(args.data, args.output)


if __name__ == "__main__":
    main()
