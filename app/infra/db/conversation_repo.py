"""
SQLite-backed conversation repository.

Stores user/assistant messages in the `messages` table. If the table is absent,
it will be created with a minimal schema compatible with existing joins.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.feature_flags import enabled
from app.history_scope import history_scope_for_user
from app.domain.conversation.service import ConversationRepository, UserMessage
from app.infra.db.session import db_rw
from app.memory import ConversationMemoryIndexer

logger = logging.getLogger(__name__)


class SqliteConversationRepository(ConversationRepository):
    """Persist conversation turns to SQLite."""

    def __init__(self) -> None:
        self._ensure_schema()

    def append(
        self, message: UserMessage, reply: Optional[str] = None
    ) -> dict[str, object]:
        now = datetime.now(timezone.utc).isoformat()
        db_user_id = message.db_user_id or int(message.user_id)
        inserted_rows: list[tuple[int, int, str, str]] = []
        user_message_id: int | None = None
        assistant_message_id: int | None = None
        with db_rw() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
            session_id = self.get_session_id(db_user_id) if "session_id" in cols else None
            session_scope = history_scope_for_user(db_user_id)
            if session_id is not None and "scope" in cols:
                scope_row = conn.execute(
                    "SELECT scope FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if scope_row and scope_row["scope"]:
                    session_scope = str(scope_row["scope"])

            to_insert = [("user", message.text)]
            if reply is not None:
                to_insert.append(("assistant", reply))

            for role, content in to_insert:
                row_data = {
                    "role": role,
                    "user_id": db_user_id,
                    "content": content,
                }
                if "session_id" in cols:
                    row_data["session_id"] = session_id
                if "scope" in cols:
                    row_data["scope"] = session_scope
                if "timestamp" in cols:
                    row_data["timestamp"] = now
                if "created_at" in cols:
                    row_data["created_at"] = now
                if "correlation_id" in cols:
                    row_data["correlation_id"] = message.correlation_id

                columns = ", ".join(row_data.keys())
                placeholders = ", ".join("?" for _ in row_data)
                cursor = conn.execute(
                    f"INSERT INTO messages ({columns}) VALUES ({placeholders})",
                    tuple(row_data.values()),
                )
                if cursor.lastrowid is not None:
                    inserted_id = int(cursor.lastrowid)
                    if role == "user":
                        user_message_id = inserted_id
                    elif role == "assistant":
                        assistant_message_id = inserted_id
                    inserted_rows.append(
                        (inserted_id, db_user_id, session_scope, role)
                    )

        if inserted_rows and enabled("conversation_memory_v2"):
            indexer = ConversationMemoryIndexer()
            for message_id, user_id, scope, role in inserted_rows:
                content = message.text if role == "user" else (reply or "")
                try:
                    indexer.index_message(
                        message_id=message_id,
                        user_id=user_id,
                        scope=scope,
                        role=role,
                        content=content,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Real-time memory indexing failed for message %s: %s",
                        message_id,
                        exc,
                    )
        return {
            "session_id": session_id,
            "timestamp": now,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
        }

    def get_session_id(self, db_user_id: int) -> int:
        """Fetch or create an active session for a given internal user id."""
        target_scope = history_scope_for_user(db_user_id)
        with db_rw() as conn:
            sess = conn.execute(
                "SELECT id, scope FROM sessions WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (db_user_id,),
            ).fetchone()
            if sess and str(sess["scope"] or "standard") == target_scope:
                return int(sess["id"])
            if sess:
                conn.execute(
                    "UPDATE sessions SET status = 'archived' WHERE user_id = ? AND status = 'active'",
                    (db_user_id,),
                )
            cursor = conn.execute(
                """
                INSERT INTO sessions (user_id, scope, status, started_at)
                VALUES (?, ?, 'active', datetime('now'))
                """,
                (db_user_id, target_scope),
            )
            last_id = cursor.lastrowid
            if last_id is None:
                raise RuntimeError("Failed to create session")
            return int(last_id)

    def _ensure_schema(self) -> None:
        """Create minimal messages table if missing."""
        try:
            with db_rw() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        role TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        content TEXT,
                        correlation_id TEXT,
                        created_at TEXT DEFAULT (datetime('now'))
                    )
                    """
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to ensure messages schema: %s", exc)
