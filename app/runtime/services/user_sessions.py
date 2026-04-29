"""User/session persistence helpers."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from app.db import db_ro, db_rw
from app.feature_flags import enabled
from app.history_scope import history_scope_for_user
from app.memory import ConversationMemoryIndexer
from app.monitoring import ACTIVE_SESSIONS
from app.utils.time_utils import (
    format_operator_datetime,
    normalize_operator,
    operator_now,
)


def _format_timestamp(value) -> str:
    if value is None:
        return format_operator_datetime(operator_now())
    if isinstance(value, str):
        return value
    try:
        return format_operator_datetime(normalize_operator(value))
    except Exception:
        return format_operator_datetime(operator_now())


class UserSessionStore:
    """Encapsulates DB operations for users, sessions, and message persistence."""

    def __init__(
        self,
        *,
        data_root: str,
        ctx_token_budget: int,
        logger: logging.Logger | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.ctx_token_budget = ctx_token_budget
        self.logger = logger or logging.getLogger(__name__)

    def ensure_user(
        self, tg_id: int, username: str | None = None, name: str | None = None
    ) -> int:
        with db_rw() as conn:
            user = conn.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?",
                (tg_id,),
            ).fetchone()
            timestamp_now = _format_timestamp(None)
            if user:
                conn.execute(
                    "UPDATE users SET last_active_at = ? WHERE id = ?",
                    (timestamp_now, user["id"]),
                )
                user_id = user["id"]
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO users (
                        telegram_user_id,
                        telegram_username,
                        display_name,
                        onboarding_completed,
                        last_active_at,
                        personality
                    )
                    VALUES (?, ?, ?, 1, ?, 'friendly')
                    """,
                    (tg_id, username or "user", name or "User", timestamp_now),
                )
                user_id = cursor.lastrowid
        self._ensure_user_folders(tg_id)
        if user_id is None:
            raise RuntimeError("Failed to create user row")
        return int(user_id)

    def get_or_create_session(self, user_id: int) -> int:
        target_scope = history_scope_for_user(user_id)
        with db_rw() as conn:
            sess = conn.execute(
                "SELECT id, scope FROM sessions WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if sess and str(sess["scope"] or "standard") == target_scope:
                session_id = sess["id"]
            else:
                if sess:
                    conn.execute(
                        "UPDATE sessions SET status = 'archived' WHERE user_id = ? AND status = 'active'",
                        (user_id,),
                    )
                started_at = _format_timestamp(None)
                cursor = conn.execute(
                    """
                    INSERT INTO sessions (user_id, scope, status, started_at, ctx_token_budget)
                    VALUES (?, ?, 'active', ?, ?)
                    """,
                    (user_id, target_scope, started_at, self.ctx_token_budget),
                )
                session_id = cursor.lastrowid
            active_count = conn.execute(
                "SELECT COUNT(*) AS count FROM sessions WHERE status = 'active'"
            ).fetchone()["count"]
        ACTIVE_SESSIONS.set(active_count)
        if session_id is None:
            raise RuntimeError("Failed to create session row")
        return int(session_id)

    def get_active_session_id(self, user_id: int) -> int | None:
        with db_ro() as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])

    def get_latest_session_id(self, user_id: int) -> int | None:
        with db_ro() as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])

    def rotate_session(self, user_id: int, *, reason: str | None = None) -> int:
        """Complete any active session for the user and create a fresh one."""
        target_scope = history_scope_for_user(user_id)
        with db_rw() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'archived' WHERE user_id = ? AND status = 'active'",
                (user_id,),
            )
            started_at = _format_timestamp(None)
            cursor = conn.execute(
                """
                INSERT INTO sessions (user_id, scope, status, started_at, ctx_token_budget)
                VALUES (?, ?, 'active', ?, ?)
                """,
                (user_id, target_scope, started_at, self.ctx_token_budget),
            )
            session_id = cursor.lastrowid
            active_count = conn.execute(
                "SELECT COUNT(*) AS count FROM sessions WHERE status = 'active'"
            ).fetchone()["count"]
        ACTIVE_SESSIONS.set(active_count)
        self.logger.info(
            "Rotated session for user %s -> %s (%s)",
            user_id,
            session_id,
            reason or "no reason provided",
        )
        if session_id is None:
            raise RuntimeError("Failed to rotate session row")
        return int(session_id)

    def save_message(
        self,
        session_id: int,
        user_id: int,
        role: str,
        content: str,
        *,
        timestamp=None,
    ) -> int | None:
        timestamp_str = _format_timestamp(timestamp)
        max_retries = 10
        retry_delay = 0.2
        inserted_message_id: int | None = None
        scope_value: str | None = None
        for attempt in range(max_retries):
            try:
                with db_rw() as conn:
                    scope_row = conn.execute(
                        "SELECT scope FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    scope = (
                        str(scope_row["scope"])
                        if scope_row and scope_row["scope"]
                        else history_scope_for_user(user_id)
                    )
                    cursor = conn.execute(
                        """
                        INSERT INTO messages (session_id, user_id, scope, role, content, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (session_id, user_id, scope, role, content, timestamp_str),
                    )
                    inserted_message_id = (
                        int(cursor.lastrowid) if cursor.lastrowid is not None else None
                    )
                    scope_value = scope
                break
            except sqlite3.OperationalError as exc:
                if "database is locked" in str(exc) and attempt < max_retries - 1:
                    self.logger.warning(
                        "Database locked, retry %s/%s...", attempt + 1, max_retries
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 1.5
                    continue
                raise
        if (
            inserted_message_id is not None
            and scope_value is not None
            and enabled("conversation_memory_v2")
        ):
            try:
                ConversationMemoryIndexer().index_message(
                    message_id=inserted_message_id,
                    user_id=user_id,
                    scope=scope_value,
                    role=role,
                    content=content,
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.debug(
                    "Real-time memory indexing failed for message %s: %s",
                    inserted_message_id,
                    exc,
                )
        return inserted_message_id

    def get_user_id(self, tg_id: int | str | None) -> int | None:
        if tg_id is None or tg_id == "":
            return None
        try:
            with db_ro() as conn:
                row = conn.execute(
                    "SELECT id FROM users WHERE telegram_user_id = ?",
                    (int(tg_id),),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "Failed to resolve user id for telegram_id %s: %s", tg_id, exc
            )
            return None
        if not row:
            return None
        return row["id"] if isinstance(row, sqlite3.Row) else row[0]

    def _ensure_user_folders(self, tg_id: int) -> None:
        user_base = self.data_root / "users" / str(tg_id)
        folders = [
            user_base,
            user_base / "images",
            user_base / "documents",
            user_base / "voice_notes",
            user_base / "exports",
            user_base / "backups",
        ]
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
        profile_file = user_base / "profile.json"
        if profile_file.exists():
            return
        profile_data = {
            "telegram_id": tg_id,
            "created_at": operator_now().isoformat(),
            "folders_initialized": True,
            "preferences": {},
            "notes": [],
        }
        profile_file.write_text(json.dumps(profile_data, indent=2), encoding="utf-8")
