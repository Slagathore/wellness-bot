"""E2E test for long conversation continuity."""

from __future__ import annotations

from app.db import db_ro, db_rw
from app.orchestrator.context_builder import (
    create_session_summary,
    recent_messages,
    should_create_summary,
)
from app.utils.text import embed_text
from app.vector_backends import get_backend


def test_conversation_continuity_50_messages(test_config, test_session, mock_ollama):
    user_id, session_id = test_session
    backend = get_backend()

    for idx in range(25):
        user_text = f"User message {idx}"
        with db_rw() as conn:
            conn.execute(
                "INSERT INTO messages(user_id, session_id, role, content) VALUES(?, ?, 'user', ?)",
                (user_id, session_id, user_text),
            )
            user_msg_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()[
                "id"
            ]
            # Update message_count and token_count (estimate ~50 tokens per message pair)
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1, token_count = COALESCE(token_count, 0) + 50 WHERE id = ?",
                (session_id,),
            )
        backend.upsert(user_msg_id, embed_text(user_text), {"user_id": user_id})

        bot_text = f"Response to {idx}"
        with db_rw() as conn:
            conn.execute(
                "INSERT INTO messages(user_id, session_id, role, content) VALUES(?, ?, 'assistant', ?)",
                (user_id, session_id, bot_text),
            )
            bot_msg_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()[
                "id"
            ]
            # Update message_count and token_count (estimate ~50 tokens per message pair)
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1, token_count = COALESCE(token_count, 0) + 50 WHERE id = ?",
                (session_id,),
            )
        backend.upsert(bot_msg_id, embed_text(bot_text), {"user_id": user_id})

    with db_ro() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        ).fetchone()["c"]
        assert total == 50

    assert should_create_summary(session_id)
    create_session_summary(session_id)

    with db_ro() as conn:
        summary = conn.execute(
            "SELECT summary FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()["summary"]
        assert summary

    recent = recent_messages(user_id, session_id, max_msgs=30)
    assert len(recent) == 30
