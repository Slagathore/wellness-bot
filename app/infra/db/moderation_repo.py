"""Moderation/crisis event repository."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from app.infra.db.session import db_rw

logger = logging.getLogger(__name__)


class ModerationRepository:
    """Persist moderation/crisis events into moderation_events table."""

    def __init__(self) -> None:
        self._ensure_schema()

    def add_event(
        self,
        user_id: int | str,
        event_type: str,
        severity: int,
        details: Dict[str, Any],
    ) -> None:
        try:
            with db_rw() as conn:
                conn.execute(
                    """
                    INSERT INTO moderation_events(user_id, event_type, severity, details, resolved, timestamp)
                    VALUES (?, ?, ?, ?, 0, datetime('now'))
                    """,
                    (user_id, event_type, severity, json.dumps(details)),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to insert moderation event: %s", exc)

    def _ensure_schema(self) -> None:
        try:
            with db_rw() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS moderation_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT,
                        event_type TEXT,
                        severity INTEGER,
                        details TEXT,
                        resolved INTEGER DEFAULT 0,
                        resolved_at TEXT,
                        resolved_by TEXT,
                        timestamp TEXT DEFAULT (datetime('now'))
                    )
                    """
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to ensure moderation_events schema: %s", exc)
