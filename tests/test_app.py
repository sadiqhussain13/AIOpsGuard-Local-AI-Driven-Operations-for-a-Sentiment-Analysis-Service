"""Unit tests for the Flask sentiment-analysis app."""

from __future__ import annotations

import sys
import os

# Allow importing app module from the app/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest
from unittest.mock import MagicMock, patch

# Patch LangChain before importing app so we don't need Ollama installed
sys.modules.setdefault("langchain_ollama", MagicMock())

from app import create_app  # noqa: E402


@pytest.fixture()
def client():
    """Create a Flask test client."""
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

def test_health(client) -> None:
    """GET /health should return 200 with status ok."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# /analyze endpoint – input validation
# ---------------------------------------------------------------------------

def test_analyze_missing_body(client) -> None:
    """POST /analyze with no body should return 400."""
    resp = client.post("/analyze", content_type="application/json", data="")
    assert resp.status_code == 400


def test_analyze_missing_text_field(client) -> None:
    """POST /analyze without 'text' key should return 400."""
    resp = client.post(
        "/analyze",
        json={"message": "hello"},
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_analyze_returns_sentiment_field(client) -> None:
    """POST /analyze with valid payload should return a sentiment field."""
    with patch("app._classify_sentiment", return_value="positive"):
        resp = client.post("/analyze", json={"text": "I love this service!"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "sentiment" in data
    assert data["sentiment"] == "positive"


@pytest.mark.parametrize("label", ["positive", "negative", "neutral"])
def test_analyze_all_labels(client, label: str) -> None:
    """App should propagate all valid sentiment labels."""
    with patch("app._classify_sentiment", return_value=label):
        resp = client.post("/analyze", json={"text": "test"})
    assert resp.status_code == 200
    assert resp.get_json()["sentiment"] == label


# ---------------------------------------------------------------------------
# Fault injector
# ---------------------------------------------------------------------------

def test_fault_injector_no_failure(client) -> None:
    """With FAILURE_RATE=0.0 no faults should be injected."""
    with patch.dict(os.environ, {"FAILURE_RATE": "0.0"}):
        with patch("app._classify_sentiment", return_value="neutral"):
            resp = client.post("/analyze", json={"text": "test"})
    assert resp.status_code == 200


def test_fault_injector_full_failure(client) -> None:
    """With FAILURE_RATE=1.0 every request should return 500."""
    with patch.dict(os.environ, {"FAILURE_RATE": "1.0"}):
        resp = client.post("/analyze", json={"text": "test"})
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# _classify_sentiment fallback
# ---------------------------------------------------------------------------

def test_classify_sentiment_llm_unavailable() -> None:
    """_classify_sentiment should return 'neutral' when LLM is None."""
    import app as app_module
    original = app_module._llm
    app_module._llm = None
    with patch("app._get_llm", return_value=None):
        result = app_module._classify_sentiment("some text")
    app_module._llm = original
    assert result == "neutral"
