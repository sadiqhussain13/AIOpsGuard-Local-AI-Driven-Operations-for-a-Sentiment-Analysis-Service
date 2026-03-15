"""Locust load-test script for the AIOpsGuard sentiment-analysis API.

Run with:
    locust -f locustfile.py --host http://localhost:30080

Or headless:
    locust -f locustfile.py --host http://localhost:30080 \
           --headless -u 50 -r 5 -t 2m
"""

from __future__ import annotations

import random

from locust import HttpUser, between, task

SHORT_SENTENCES: list[str] = [
    "Great product!",
    "Terrible experience.",
    "It was okay.",
    "Absolutely love it.",
    "Not worth the money.",
    "Neutral opinion here.",
    "Best thing ever!",
    "Worst service I've used.",
    "Pretty average overall.",
    "Could not be happier.",
]

LONG_SENTENCES: list[str] = [
    (
        "I have been using this service for several months now and I must say "
        "that the quality has consistently exceeded my expectations in every "
        "possible way imaginable."
    ),
    (
        "The customer support team was incredibly unhelpful and rude during "
        "my entire interaction, making an already frustrating situation much "
        "worse than it needed to be."
    ),
    (
        "While the product itself works as advertised, the shipping was "
        "delayed by two weeks and the packaging was damaged upon arrival, "
        "leaving me feeling somewhat dissatisfied with the overall experience."
    ),
    (
        "After careful consideration of all available alternatives I have "
        "concluded that this is neither the best nor the worst option and "
        "represents a reasonable middle-ground for most use cases."
    ),
]


class SentimentUser(HttpUser):
    """Simulated user hitting the /analyze endpoint with mixed text lengths."""

    wait_time = between(0.5, 2.0)

    @task(weight=7)
    def analyze_short(self) -> None:
        """Send a short sentence to /analyze."""
        text = random.choice(SHORT_SENTENCES)
        self.client.post(
            "/analyze",
            json={"text": text},
            name="/analyze [short]",
        )

    @task(weight=3)
    def analyze_long(self) -> None:
        """Send a longer sentence to /analyze."""
        text = random.choice(LONG_SENTENCES)
        self.client.post(
            "/analyze",
            json={"text": text},
            name="/analyze [long]",
        )

    @task(weight=1)
    def health_check(self) -> None:
        """Ping the /health endpoint."""
        self.client.get("/health", name="/health")
