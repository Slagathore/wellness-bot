"""
Structured logging setup with correlation ID support.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, Optional


class CorrelationFilter(logging.Filter):
    """Inject correlation_id into log records if present on the logger."""

    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - minimal
        correlation_id = getattr(record, "correlation_id", None) or getattr(
            logging.LoggerAdapter, "correlation_id", None
        )
        setattr(record, "correlation_id", correlation_id)
        return True


class JsonFormatter(logging.Formatter):
    """Simple JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:  # pragma: no cover - formatting
        base: Dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)
        correlation_id = getattr(record, "correlation_id", None)
        if correlation_id:
            base["correlation_id"] = correlation_id
        if record.__dict__.get("extra_fields"):
            base.update(record.__dict__["extra_fields"])
        return json.dumps(base, ensure_ascii=True)


def configure_logging(*, level: str = "INFO", json_output: bool | None = None) -> None:
    """
    Configure root logger with optional JSON output.

    json_output defaults to env LOG_JSON (true/false).
    """

    if json_output is None:
        json_output = os.getenv("LOG_JSON", "false").lower() in {"1", "true", "yes"}

    handlers = []
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationFilter())
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s - %(message)s "
                "cid=%(correlation_id)s"
            )
        )
    handlers.append(handler)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )


def with_correlation(
    logger: logging.Logger, correlation_id: Optional[str]
) -> logging.LoggerAdapter:
    """Return an adapter that injects correlation_id."""
    return logging.LoggerAdapter(logger, {"correlation_id": correlation_id})
