"""
Safety/moderation service to detect crisis keywords and log events.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from app.core.events import event_bus
from app.domain import events
from app.history_scope import (automated_moderation_allowed_for_scope,
                               history_scope_for_user)
from app.infra.db.moderation_repo import ModerationRepository

logger = logging.getLogger(__name__)


class SafetyService:
    def __init__(self, repo: ModerationRepository) -> None:
        self._repo = repo
        self._crisis_patterns = [
            r"kill myself",
            r"suicide",
            r"end my life",
            r"want to die",
            r"hurt myself",
        ]

    def inspect_message(
        self, *, user_id: str | int, chat_id: str | int | None, text: str
    ) -> None:
        """Detect crisis keywords and log to moderation events."""
        try:
            scope = history_scope_for_user(int(user_id))
        except Exception:
            scope = None
        if not automated_moderation_allowed_for_scope(scope):
            return
        if self._matches_crisis(text):
            severity = 7
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

    def _matches_crisis(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(re.search(pat, lowered) for pat in self._crisis_patterns)
