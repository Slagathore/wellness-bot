"""Tests for the shared adventure lore/memory refresh."""

from __future__ import annotations

import json

import pytest

from app.db import db_ro, db_rw
from app.domain.adventure.lore import (lore_refresh_due,
                                       refresh_adventure_lore)


def _make_adventure(user_id: int) -> int:
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO adventures(user_id, title, lore, status, settings) "
            "VALUES (?, 'Quest', 'Initial lore.', 'active', '{}')",
            (user_id,),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _add_messages(adv_id: int, n: int) -> None:
    with db_rw() as conn:
        for i in range(n):
            role = "user" if i % 2 == 0 else "narrator"
            conn.execute(
                "INSERT INTO adventure_messages(adventure_id, role, content) VALUES (?, ?, ?)",
                (adv_id, role, f"line {i}"),
            )


def test_lore_refresh_due_threshold(test_user):
    user_id, _ = test_user
    adv_id = _make_adventure(user_id)
    assert lore_refresh_due(adv_id) is False
    _add_messages(adv_id, 5)
    assert lore_refresh_due(adv_id) is False
    _add_messages(adv_id, 1)  # now 6 total, >= threshold
    assert lore_refresh_due(adv_id) is True


@pytest.mark.asyncio
async def test_refresh_folds_messages_and_advances_pointer(test_user):
    user_id, _ = test_user
    adv_id = _make_adventure(user_id)
    _add_messages(adv_id, 6)

    calls = {}

    async def fake_chat(messages, options=None):
        calls["messages"] = messages
        return {"text": "PLAYER IDENTITY: hero\nESTABLISHED FACTS: it rained."}

    updated = await refresh_adventure_lore(adv_id, chat_fn=fake_chat, reason="test")
    assert updated is True

    with db_ro() as conn:
        row = conn.execute("SELECT lore, settings FROM adventures WHERE id = ?", (adv_id,)).fetchone()
    assert "ESTABLISHED FACTS: it rained." in row["lore"]
    assert json.loads(row["settings"])["last_lore_message_id"] > 0

    # Second refresh with no new messages is a no-op.
    assert await refresh_adventure_lore(adv_id, chat_fn=fake_chat, reason="test") is False


@pytest.mark.asyncio
async def test_refresh_falls_back_when_llm_empty(test_user):
    user_id, _ = test_user
    adv_id = _make_adventure(user_id)
    _add_messages(adv_id, 6)

    async def empty_chat(messages, options=None):
        return {"text": ""}

    assert await refresh_adventure_lore(adv_id, chat_fn=empty_chat, reason="t") is True
    with db_ro() as conn:
        lore = conn.execute("SELECT lore FROM adventures WHERE id = ?", (adv_id,)).fetchone()["lore"]
    # Fallback preserves existing lore + appends recent canon changes.
    assert "Initial lore." in lore
    assert "RECENT CANON CHANGES" in lore
