"""
Minimal tracing helpers (noop with logging).
"""

from __future__ import annotations

import contextlib
import logging
import time

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def start_span(name: str):
    start = time.perf_counter()
    try:
        yield
    except Exception:
        logger.exception("Span %s failed", name)
        raise
    finally:
        duration = time.perf_counter() - start
        logger.debug("Span %s completed in %.3fs", name, duration)
