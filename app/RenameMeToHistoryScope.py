# Rename this file to history_scope.py to use it.
"""History scope helpers for separating standard and roleplay data."""

from __future__ import annotations

from functools import lru_cache

from app.db import db_ro

HISTORY_SCOPE_STANDARD = "standard"
HISTORY_SCOPE_ROLEPLAY = "roleplay"

VALID_HISTORY_SCOPES = {
    HISTORY_SCOPE_STANDARD,
    HISTORY_SCOPE_ROLEPLAY,
}


def normalize_history_scope(value: str | None) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in VALID_HISTORY_SCOPES:
        return lowered
    return HISTORY_SCOPE_STANDARD


def history_scope_for_personality(personality_name: str | None) -> str:
    lowered = str(personality_name or "").strip().lower()
    if lowered == HISTORY_SCOPE_ROLEPLAY:
        return HISTORY_SCOPE_ROLEPLAY
    return HISTORY_SCOPE_STANDARD


def history_scope_for_user(user_id: int) -> str:
    try:
        with db_ro() as conn:
            row = conn.execute(
                "SELECT personality FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
    except Exception:
        return HISTORY_SCOPE_STANDARD
    personality = row["personality"] if row and row["personality"] else None
    return history_scope_for_personality(personality)


def is_standard_history_scope(scope: str | None) -> bool:
    return normalize_history_scope(scope) == HISTORY_SCOPE_STANDARD


@lru_cache(maxsize=32)
def table_has_column(table_name: str, column_name: str) -> bool:
    """Best-effort schema probe used for compatibility with older DB files."""

    try:
        with db_ro() as conn:
            rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    except Exception:
        return False
    return any(str(row["name"] if hasattr(row, "keys") else row[1]) == column_name for row in rows)


def inferred_history_scope_for_message(
    *,
    message_id: int | None = None,
    session_id: int | None = None,
    user_id: int | None = None,
) -> str:
    """Infer history scope from the richest available source.

    Priority:
    1. `messages.scope` when present
    2. `sessions.scope` when present
    3. current `users.personality`
    4. `standard`
    """

    try:
        with db_ro() as conn:
            if message_id is not None and table_has_column("messages", "scope"):
                row = conn.execute(
                    "SELECT scope FROM messages WHERE id = ?",
                    (message_id,),
                ).fetchone()
                if row and row["scope"]:
                    return normalize_history_scope(row["scope"])

            if session_id is not None and table_has_column("sessions", "scope"):
                row = conn.execute(
                    "SELECT scope FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row and row["scope"]:
                    return normalize_history_scope(row["scope"])

            if (
                message_id is not None
                and table_has_column("sessions", "scope")
                and table_has_column("messages", "session_id")
            ):
                row = conn.execute(
                    """
                    SELECT s.scope
                    FROM messages AS m
                    JOIN sessions AS s ON s.id = m.session_id
                    WHERE m.id = ?
                    """,
                    (message_id,),
                ).fetchone()
                if row and row["scope"]:
                    return normalize_history_scope(row["scope"])
    except Exception:
        pass

    if user_id is not None:
        return history_scope_for_user(user_id)
    return HISTORY_SCOPE_STANDARD


def automated_moderation_allowed_for_scope(scope: str | None) -> bool:
    """Automated crisis alerting is only enabled for standard scope."""

    return is_standard_history_scope(scope)
