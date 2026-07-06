# Monitoring utilities and Prometheus metrics helpers.
from __future__ import annotations

import logging
import threading

try:
    from prometheus_client import (  # type: ignore[reportMissingImports]
        Counter,
        Gauge,
        Histogram,
        start_http_server,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency optional
    PROMETHEUS_AVAILABLE = False

    class _NoopMetric:
        def labels(self, *args, **kwargs):
            return self

        def observe(self, value):
            return None

        def inc(self, value: float = 1.0):
            return None

        def set(self, value):
            return None

        def time(self):
            class _Timer:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            return _Timer()

    def Counter(*args, **kwargs):  # type: ignore
        return _NoopMetric()

    def Gauge(*args, **kwargs):  # type: ignore
        return _NoopMetric()

    def Histogram(*args, **kwargs):  # type: ignore
        return _NoopMetric()

    def start_http_server(*args, **kwargs):  # type: ignore
        return None


logger = logging.getLogger(__name__)

MESSAGE_TOTAL = Counter(
    "wellness_messages_total",
    "Total messages processed by platform/direction.",
    ("platform", "direction"),
)

MESSAGE_LATENCY = Histogram(
    "wellness_message_latency_seconds",
    "Message processing latency in seconds.",
    ("platform",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
)

WORKER_ERRORS = Counter(
    "wellness_worker_errors_total",
    "Total background worker errors.",
    ("component",),
)

ACTIVE_SESSIONS = Gauge(
    "wellness_active_sessions",
    "Currently active conversation sessions.",
)

SAFETY_BLOCKS = Counter(
    "wellness_safety_blocks_total",
    "Count of messages blocked by safety/rate-limit filters.",
    ("reason",),
)

_METRICS_STARTED = False
_METRICS_LOCK = threading.Lock()


def start_metrics_server(port: int = 9103) -> None:
    """Start the Prometheus metrics server once."""

    global _METRICS_STARTED
    with _METRICS_LOCK:
        if _METRICS_STARTED:
            return
        if not PROMETHEUS_AVAILABLE:
            logger.warning("Prometheus client not installed; metrics disabled")
            _METRICS_STARTED = True
            return
        try:
            start_http_server(port)
            logger.info(
                "Prometheus metrics server started", extra={"metrics_port": port}
            )
            _METRICS_STARTED = True
        except (
            Exception
        ) as exc:  # pragma: no cover - startup failure should not crash app
            logger.error("Failed to start metrics server: %s", exc)
