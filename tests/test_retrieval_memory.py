from __future__ import annotations

import json

from app.db import db_ro, db_rw
from app.infra.db.schema_bootstrap import ensure_schema_current
from app.memory.conversation import ConversationMemoryIndexer
from app.memory.conversation import _build_embedding_text, _prioritize_user_results
from app.orchestrator.context_builder import _merge_memory_results


def test_build_embedding_text_includes_summary_topics_and_context() -> None:
    text = _build_embedding_text(
        text="I have a job interview tomorrow morning and I am nervous about it.",
        summary="User has an interview tomorrow morning and feels anxious.",
        topics=["job interview", "anxiety"],
        context_window="assistant: We talked about prep questions.\nuser: I want to practice tonight.",
    )

    assert "Summary: User has an interview tomorrow morning and feels anxious." in text
    assert "Topics: job interview, anxiety" in text
    assert "Recent context:" in text
    assert "practice tonight" in text


def test_prioritize_user_results_keeps_user_memories_first() -> None:
    rows = [
        {"message_id": 100, "role": "assistant", "rank_score": 0.96},
        {"message_id": 101, "role": "user", "rank_score": 0.93},
        {"message_id": 102, "role": "assistant", "rank_score": 0.91},
        {"message_id": 103, "role": "user", "rank_score": 0.90},
    ]

    top = _prioritize_user_results(rows, 3)

    assert [row["message_id"] for row in top] == [101, 103, 100]


def test_merge_memory_results_prefers_user_items_and_rewards_hybrid_hits() -> None:
    lexical = [
        {
            "message_id": 10,
            "role": "assistant",
            "rank_score": 0.84,
            "lexical_score": 0.84,
            "retrieval_source": "lexical",
        },
        {
            "message_id": 11,
            "role": "user",
            "rank_score": 0.79,
            "lexical_score": 0.79,
            "retrieval_source": "lexical",
        },
    ]
    semantic = [
        {
            "message_id": 10,
            "role": "assistant",
            "rank_score": 0.82,
            "semantic_score": 0.82,
            "retrieval_source": "semantic",
        },
        {
            "message_id": 12,
            "role": "user",
            "rank_score": 0.81,
            "semantic_score": 0.81,
            "retrieval_source": "semantic",
        },
    ]

    merged = _merge_memory_results(lexical=lexical, semantic=semantic, k=3)

    assert [row["message_id"] for row in merged] == [12, 11, 10]
    hybrid = next(row for row in merged if row["message_id"] == 10)
    assert hybrid["retrieval_source"] == "hybrid"
    assert float(hybrid["rank_score"]) > 0.84


def test_index_message_persists_enriched_embedding_fields(
    test_session, monkeypatch
) -> None:
    user_id, session_id = test_session
    ensure_schema_current(force=True)

    class _DummyBackend:
        def upsert(self, message_id, embedding, metadata):
            return None

    monkeypatch.setattr(
        "app.memory.conversation.generate",
        lambda *args, **kwargs: {
            "text": json.dumps(
                {
                    "summary": "User has a therapy appointment tomorrow morning.",
                    "topics": ["therapy", "appointment"],
                }
            )
        },
    )
    monkeypatch.setattr(
        "app.memory.conversation.embed_text",
        lambda text: [0.25, 0.5, 0.75],
    )
    monkeypatch.setattr("app.memory.conversation.enabled", lambda flag: True)
    monkeypatch.setattr("app.memory.conversation.get_backend", lambda: _DummyBackend())

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO messages(user_id, session_id, scope, role, content)
            VALUES(?, ?, 'standard', 'assistant', ?)
            """,
            (user_id, session_id, "We should check in after your therapy session."),
        )
        conn.execute(
            """
            INSERT INTO messages(user_id, session_id, scope, role, content)
            VALUES(?, ?, 'standard', 'user', ?)
            """,
            (
                user_id,
                session_id,
                "I have a therapy appointment tomorrow morning and I want you to remember it.",
            ),
        )
        message_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    ConversationMemoryIndexer().index_message(
        message_id=message_id,
        user_id=user_id,
        scope="standard",
        role="user",
        content="I have a therapy appointment tomorrow morning and I want you to remember it.",
    )

    with db_ro() as conn:
        row = conn.execute(
            """
            SELECT summary, topics, context_window, emotional_salience, user_value_score, context_score
            FROM conversation_embeddings
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()

    assert row is not None
    assert "therapy appointment" in str(row["summary"]).lower()
    assert "therapy" in str(row["topics"]).lower()
    assert "assistant:" in str(row["context_window"]).lower()
    assert float(row["user_value_score"]) > 0.5
    assert float(row["context_score"]) > 0.3
    assert float(row["emotional_salience"]) >= 0.0
