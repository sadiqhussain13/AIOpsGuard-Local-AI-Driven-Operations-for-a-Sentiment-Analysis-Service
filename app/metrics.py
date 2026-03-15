"""Prometheus metrics definitions for the Flask sentiment-analysis service."""

from prometheus_client import Counter, Histogram, start_http_server

REQUEST_TOTAL: Counter = Counter(
    "request_total",
    "Total number of requests received",
    ["method", "endpoint"],
)

REQUEST_SUCCESS: Counter = Counter(
    "request_success",
    "Total number of successful requests",
    ["method", "endpoint"],
)

REQUEST_ERROR: Counter = Counter(
    "request_error",
    "Total number of failed requests",
    ["method", "endpoint", "status_code"],
)

REQUEST_LATENCY: Histogram = Histogram(
    "request_latency_seconds",
    "Request latency in seconds",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

__all__ = [
    "REQUEST_TOTAL",
    "REQUEST_SUCCESS",
    "REQUEST_ERROR",
    "REQUEST_LATENCY",
    "start_http_server",
]
