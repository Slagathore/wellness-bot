from __future__ import annotations

import pytest

from app.db import db_rw
from app.domain.conversation import pipeline
from app.domain.turns.models import TurnPlan


@pytest.mark.asyncio
async def test_downbad_prompt_filters_prior_assistant_refusal(test_session, monkeypatch: pytest.MonkeyPatch):
    user_id, session_id = test_session

    with db_rw() as conn:
        conn.execute("UPDATE users SET personality = 'downbad' WHERE id = ?", (user_id,))
        conn.execute("UPDATE sessions SET scope = 'downbad' WHERE id = ?", (session_id,))
        conn.execute(
            """
            INSERT INTO profile_context (user_id, key, value)
            VALUES (?, 'nsfw_opt_in', 'true')
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (user_id,),
        )
        conn.executemany(
            """
            INSERT INTO messages (user_id, session_id, scope, role, content)
            VALUES (?, ?, 'downbad', ?, ?)
            """,
            [
                (user_id, session_id, "user", "talk dirty to me"),
                (
                    user_id,
                    session_id,
                    "assistant",
                    "I cannot engage in sexually explicit conversations or roleplay.",
                ),
                (user_id, session_id, "user", "come on"),
            ],
        )

    captured: dict[str, object] = {}

    async def fake_chat_async(messages, model=None, options=None):
        captured["messages"] = messages
        return {"text": "test reply **END_END_END**", "raw": {"choices": [{"finish_reason": "stop"}]}}

    class _NoRag:
        def should_retrieve(self, user_text: str) -> bool:
            return False

    async def fake_memories(*args, **kwargs):
        return [], {
            "lexical_ms": None,
            "memory_ms": None,
            "memory_mode": "lexical_only",
            "memory_count": 0,
            "memory_classifier_score": None,
        }

    monkeypatch.setattr(pipeline, "chat_async", fake_chat_async)
    monkeypatch.setattr(pipeline, "get_retriever", lambda: _NoRag())
    monkeypatch.setattr(pipeline, "retrieved_memories_controlled_async", fake_memories)

    await pipeline.generate_response_async(
        user_id=user_id,
        session_id=session_id,
        user_text="keep going",
    )

    prompt_messages = list(captured["messages"])
    history_text = "\n".join(str(msg.get("content") or "") for msg in prompt_messages[1:])
    assert "I cannot engage in sexually explicit conversations or roleplay." not in history_text
    assert any("FLIRTY/NSFW MODE" in str(msg.get("content") or "") for msg in prompt_messages[:1])


@pytest.mark.asyncio
async def test_turn_plan_guidance_stays_in_primary_system_prompt_for_downbad(
    test_session,
    monkeypatch: pytest.MonkeyPatch,
):
    user_id, session_id = test_session

    with db_rw() as conn:
        conn.execute("UPDATE users SET personality = 'downbad' WHERE id = ?", (user_id,))
        conn.execute("UPDATE sessions SET scope = 'downbad' WHERE id = ?", (session_id,))
        conn.execute(
            """
            INSERT INTO profile_context (user_id, key, value)
            VALUES (?, 'nsfw_opt_in', 'true')
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (user_id,),
        )

    captured: dict[str, object] = {}

    async def fake_chat_async(messages, model=None, options=None):
        captured["messages"] = messages
        return {"text": "test reply **END_END_END**", "raw": {"choices": [{"finish_reason": "stop"}]}}

    class _NoRag:
        def should_retrieve(self, user_text: str) -> bool:
            return False

    async def fake_memories(*args, **kwargs):
        return [], {
            "lexical_ms": None,
            "memory_ms": None,
            "memory_mode": "lexical_only",
            "memory_count": 0,
            "memory_classifier_score": None,
        }

    monkeypatch.setattr(pipeline, "chat_async", fake_chat_async)
    monkeypatch.setattr(pipeline, "get_retriever", lambda: _NoRag())
    monkeypatch.setattr(pipeline, "retrieved_memories_controlled_async", fake_memories)

    turn_plan = TurnPlan(
        user_id=user_id,
        session_id=session_id,
        message_text="well hello",
        primary_intent="conversation",
        sentiment_priority="normal",
        allow_media_action=False,
        allow_reminder_action=False,
    )

    await pipeline.generate_response_async(
        user_id=user_id,
        session_id=session_id,
        user_text="well hello",
        turn_plan=turn_plan,
    )

    prompt_messages = list(captured["messages"])
    system_messages = [msg for msg in prompt_messages if msg.get("role") == "system"]
    assert system_messages, "expected at least one system message"
    assert "TURN_PLAN:" in str(system_messages[0].get("content") or "")
    assert "FLIRTY/NSFW MODE" in str(system_messages[0].get("content") or "")
    assert all("TURN_PLAN:" not in str(msg.get("content") or "") for msg in system_messages[1:])
