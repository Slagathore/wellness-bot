# Rename this file to preferences.py to use it.
"""Preference helpers for follow-up configuration."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from app.db import db_ro, db_rw
from app.utils.time_utils import format_operator_datetime, operator_now


class PreferenceService:
    """Encapsulates preference lookups and caching."""

    def __init__(
        self, *, followup_cache_size: int = 1000, logger: logging.Logger | None = None
    ) -> None:
        self._followup_cache: OrderedDict[int, bool] = OrderedDict()
        self._followup_cache_size = followup_cache_size
        self._followup_lock = threading.Lock()
        self.logger = logger or logging.getLogger(__name__)

    # Follow-up preferences -------------------------------------------------

    def get_followup_pref(self, user_id: int) -> bool:
        cached = self._followup_cache_get(user_id)
        if cached is not None:
            return cached
        enabled = True
        try:
            with db_ro() as conn:
                row = conn.execute(
                    "SELECT value FROM profile_context WHERE user_id = ? AND key = 'followup_reminders_enabled'",
                    (user_id,),
                ).fetchone()
                if row:
                    enabled = str(row["value"]).lower() not in {
                        "0",
                        "false",
                        "off",
                        "no",
                    }
        except Exception:  # noqa: BLE001
            enabled = True
        self._followup_cache_set(user_id, enabled)
        return enabled

    def set_followup_pref(self, user_id: int, enabled: bool) -> None:
        value = "true" if enabled else "false"
        timestamp_now = format_operator_datetime(operator_now())
        try:
            with db_rw() as conn:
                conn.execute(
                    """
                    INSERT INTO profile_context (user_id, key, value)
                    VALUES (?, 'followup_reminders_enabled', ?)
                    ON CONFLICT(user_id, key)
                    DO UPDATE SET value = excluded.value, updated_at = ?
                    """,
                    (user_id, value, timestamp_now),
                )
        except Exception:  # noqa: BLE001
            pass
        self._followup_cache_set(user_id, enabled)

    def should_allow_followup(self, message_lower: str) -> bool:
        followup_phrases = [
            "remind me",
            "please remind",
            "follow up",
            "check in on me",
            "check on me",
            "check in later",
            "ping me",
            "reach out",
            "ask me later",
            "see how i am later",
            "keep me accountable",
            "can you check",
        ]
        return any(phrase in message_lower for phrase in followup_phrases)

    def should_disable_followups(self, message_lower: str) -> bool:
        disable_phrases = [
            "no more reminders",
            "stop reminders",
            "stop the reminders",
            "too many reminders",
            "dont remind me",
            "don't remind me",
            "no follow up",
            "stop following up",
            "pause the reminders",
            "pause reminders",
            "you shouldnt be having so many reminders",
            "you shouldn't be having so many reminders",
        ]
        return any(phrase in message_lower for phrase in disable_phrases)

    def should_enable_followups(self, message_lower: str) -> bool:
        enable_phrases = [
            "its okay to follow up",
            "it's okay to follow up",
            "you can follow up",
            "resume reminders",
            "its ok to remind me later",
            "it's ok to remind me later",
            "please check in on me later",
            "feel free to check on me later",
        ]
        return any(phrase in message_lower for phrase in enable_phrases)

    def clear_cache(self) -> None:
        with self._followup_lock:
            self._followup_cache.clear()

    # Internal helpers ------------------------------------------------------

    def _followup_cache_get(self, user_id: int) -> bool | None:
        with self._followup_lock:
            if user_id in self._followup_cache:
                value = self._followup_cache.pop(user_id)
                self._followup_cache[user_id] = value
                return value
            return None

    def _followup_cache_set(self, user_id: int, value: bool) -> None:
        with self._followup_lock:
            if user_id in self._followup_cache:
                self._followup_cache.pop(user_id)
            elif len(self._followup_cache) >= self._followup_cache_size:
                self._followup_cache.popitem(last=False)
            self._followup_cache[user_id] = value

    def _safe_json_loads(self, raw, default, *, context: str = ""):
        try:
            import json

            return json.loads(raw)
        except Exception:  # noqa: BLE001
            self.logger.debug("Failed to parse JSON for %s", context)
            return default
