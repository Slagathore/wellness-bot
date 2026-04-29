"""Detect and store user personalization cues."""

from __future__ import annotations

import json
import re
from typing import Optional

from app.db import db_ro, db_rw
from app.utils.time_utils import operator_now

_MEMORY_REGEX = re.compile(
    r"\b(?:remember|save|note|keep track of)\b[^\n]*",
    re.IGNORECASE,
)


class PersonalizationAgent:
    """Lightweight detector that stores user facts upon request."""

    def __init__(self, log_callback):
        self._log = log_callback

    def process_message(self, user_id: int, message: str) -> Optional[str]:
        match = _MEMORY_REGEX.search(message)
        if not match:
            return None

        snippet = message.strip()
        note = self._extract_note(snippet)
        if not note:
            return None

        self._persist_note(user_id, note)
        self._log(f"Stored personalization note for user {user_id}: {note[:60]}...")
        return (
            "Got it - I'll remember that."
            if len(note) < 160
            else "I've saved that detail for later."
        )

    def _extract_note(self, message: str) -> str:
        lowered = message.lower()
        triggers = ["remember", "save", "note", "keep track of"]
        for trigger in triggers:
            if trigger in lowered:
                idx = lowered.index(trigger)
                fragment = message[idx + len(trigger) :].lstrip(": ,.-")
                if fragment:
                    return fragment.strip()
        return message.strip()

    def _persist_note(self, user_id: int, note: str) -> None:
        timestamp = operator_now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {"note": note, "timestamp": timestamp}

        try:
            with db_ro() as conn:
                row = conn.execute(
                    "SELECT value FROM profile_context WHERE user_id = ? AND key = 'memory_notes'",
                    (user_id,),
                ).fetchone()
            notes = []
            if row and row["value"]:
                try:
                    notes = json.loads(row["value"])
                except json.JSONDecodeError:
                    notes = []
            notes.append(entry)
            notes = notes[-50:]

            payload = json.dumps(notes)
            with db_rw() as conn:
                conn.execute(
                    """
                    INSERT INTO profile_context (user_id, key, value)
                    VALUES (?, 'memory_notes', ?)
                    ON CONFLICT(user_id, key)
                    DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                    """,
                    (user_id, payload),
                )
        except Exception as exc:  # noqa: BLE001
            self._log(f"Failed to persist personalization note: {exc}")
