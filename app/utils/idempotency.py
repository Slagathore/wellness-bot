"""Utilities to prevent duplicate processing of Telegram messages."""

from __future__ import annotations

from app.db import db_ro


def already_ingested(user_id: int, telegram_message_id: int) -> bool:
    """Return True if the message from Telegram has already been stored."""

    with db_ro() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM messages
            WHERE user_id = ? AND telegram_message_id = ?
            LIMIT 1
            """,
            (user_id, telegram_message_id),
        ).fetchone()
    return row is not None
