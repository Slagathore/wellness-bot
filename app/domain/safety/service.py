"""
Safety/moderation service: detect crisis keywords, log events, and emit an alert.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.core.events import event_bus
from app.domain import events
from app.domain.safety.filter import matches_crisis
from app.history_scope import history_scope_for_user
from app.infra.db.moderation_repo import ModerationRepository

logger = logging.getLogger(__name__)


class SafetyService:
    def __init__(self, repo: ModerationRepository) -> None:
        self._repo = repo

    def inspect_message(
        self, *, user_id: str | int, chat_id: str | int | None, text: str
    ) -> bool:
        """Detect crisis keywords, log a moderation event, and publish an alert.

        Runs in **every** history scope — crisis detection is never suppressed
        for roleplay/downbad conversations. The scope is recorded in the event
        details so admins keep the context. Returns True if a crisis was flagged.
        """
        if not matches_crisis(text):
            return False

        try:
            scope = history_scope_for_user(int(user_id))
        except Exception:
            scope = None

        # 5 is the max the moderation_events CHECK constraint allows
        # (severity BETWEEN 1 AND 5) and matches the batch crisis path in the
        # nightly/sentiment workers. Using 7 here silently failed the insert.
        severity = 5
        details: Dict[str, Any] = {
            "message": text[:500],
            "chat_id": chat_id,
            "source": "keyword_filter",
            "scope": scope or "standard",
        }
        self._repo.add_event(user_id, "crisis_detected", severity, details)
        event_bus.publish(
            events.EVENT_CRISIS_DETECTED,
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "severity": severity,
                "details": details,
            },
        )
        return True
