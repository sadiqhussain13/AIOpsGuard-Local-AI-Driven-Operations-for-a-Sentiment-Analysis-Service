"""Fault-injection middleware for the Flask sentiment-analysis service.

Reads the ``FAILURE_RATE`` environment variable (0.0–1.0) and randomly
returns an HTTP 500 response to simulate transient failures.
"""

import os
import random
from functools import wraps
from typing import Any, Callable

from flask import jsonify


def _get_failure_rate() -> float:
    """Return the configured failure rate, clamped to [0.0, 1.0]."""
    try:
        rate = float(os.environ.get("FAILURE_RATE", "0.0"))
        return max(0.0, min(1.0, rate))
    except ValueError:
        return 0.0


def fault_injector(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that randomly injects HTTP 500 errors based on FAILURE_RATE.

    Args:
        func: The Flask view function to wrap.

    Returns:
        Wrapped function that may return a 500 error response.
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        rate = _get_failure_rate()
        if rate > 0.0 and random.random() < rate:
            return jsonify({"error": "Injected fault: simulated server error"}), 500
        return func(*args, **kwargs)

    return wrapper
