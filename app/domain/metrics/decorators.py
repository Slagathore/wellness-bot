"""
Decorator to record message metrics per platform.
"""

from __future__ import annotations

import functools
import time

from app.monitoring import MESSAGE_LATENCY, MESSAGE_TOTAL


def message_metrics(platform: str):
    """Decorator to track total count and latency for handlers."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
                return result
            finally:
                duration = time.perf_counter() - start
                MESSAGE_TOTAL.labels(platform=platform, direction="inbound").inc()
                MESSAGE_LATENCY.labels(platform=platform).observe(duration)

        return wrapper

    return decorator
