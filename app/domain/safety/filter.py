"""Safety and rate-limit filter for inbound messages."""

from __future__ import annotations

import logging
import re

from app.history_scope import (automated_moderation_allowed_for_scope,
                               history_scope_for_user)
from app.monitoring import SAFETY_BLOCKS
from app.utils.rate_limit import check_and_enforce_rate_limit

logger = logging.getLogger(__name__)


class SafetyFilter:
    """Applies basic safety/rate-limit checks."""

    def allow(self, user_id: int, text: str) -> bool:
        try:
            if check_and_enforce_rate_limit(user_id):
                SAFETY_BLOCKS.labels(reason="rate_limit").inc()
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Safety check failed open for user %s: %s", user_id, exc)

        try:
            scope = history_scope_for_user(int(user_id))
        except Exception:
            scope = None
        if not automated_moderation_allowed_for_scope(scope):
            return True

        if self._matches_crisis(text):
            SAFETY_BLOCKS.labels(reason="crisis_keyword").inc()
            return False
        return True

    def _matches_crisis(self, text: str) -> bool:
        lowered = (text or "").lower()
        crisis_terms = [
            r"kill myself",
            r"suicide",
            r"end my life",
            r"want to die",
            r"hurt myself",
        ]
        return any(re.search(term, lowered) for term in crisis_terms)
