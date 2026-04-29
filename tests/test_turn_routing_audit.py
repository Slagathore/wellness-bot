from __future__ import annotations

import json
from typing import Any, cast

import pytest

from app.core.events import Event
from app.db import db_ro, db_rw
from app.domain.conversation.handler import ConversationEventHandler
from app.domain.onboarding.gate import OnboardingGate


class _StubOnboardingService:
    def handle_message(self, tg_user_id: int, db_user_id: int, text: str) -> str:
        return "Let's finish onboarding first."


class _StubSessionStore:
    def __init__(self, user_id: int) -> None:
        self._user_id = user_id

    def ensure_user(self, tg_user_id: int, username=None, name=None) -> int:
        return self._user_id


class _NeverCalledConversationService:
    async def process_user_message_async(self, msg, *, record_timing_now: bool = False):
        raise AssertionError("conversation service should not run for throttled messages")


class _DenyAllSafetyFilter:
    def allow(self, user_id: int, text: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_onboarding_gate_creates_audit_for_early_reply(
    test_user,
    monkeypatch: pytest.MonkeyPatch,
):
    user_id, telegram_user_id = test_user
    with db_rw() as conn:
        conn.execute(
            "UPDATE users SET onboarding_completed = 0 WHERE id = ?",
            (user_id,),
        )

    published: list[tuple[str, dict, str | None]] = []
    monkeypatch.setattr(
        "app.domain.onboarding.gate.event_bus.publish",
        lambda name, payload, correlation_id=None: published.append((name, payload, correlation_id)),
    )

    gate = OnboardingGate(
        cast(Any, _StubOnboardingService()),
        cast(Any, _StubSessionStore(user_id)),
    )
    await gate.handle(
        Event(
            name="user.message",
            payload={
                "user_id": telegram_user_id,
                "chat_id": 222,
                "text": "hey",
                "username": "tester",
                "first_name": "Test",
            },
            correlation_id="corr-onboarding-audit",
        )
    )

    assert published
    with db_ro() as conn:
        row = conn.execute(
            "SELECT status, route_json FROM turn_audit_log WHERE correlation_id = ?",
            ("corr-onboarding-audit",),
        ).fetchone()

    assert row is not None
    assert row["status"] == "reply_dispatched"
    stages = [item["stage"] for item in json.loads(str(row["route_json"]))]
    assert "onboarding.gate.onboarding_required" in stages
    assert "onboarding.gate.send_reply_published" in stages


@pytest.mark.asyncio
async def test_conversation_handler_creates_audit_for_safety_throttle(
    test_user,
    monkeypatch: pytest.MonkeyPatch,
):
    user_id, telegram_user_id = test_user
    published: list[tuple[str, dict, str | None]] = []
    monkeypatch.setattr(
        "app.domain.conversation.handler.event_bus.publish",
        lambda name, payload, correlation_id=None: published.append((name, payload, correlation_id)),
    )

    handler = ConversationEventHandler(
        cast(Any, _NeverCalledConversationService()),
        cast(Any, _StubSessionStore(user_id)),
        cast(Any, _DenyAllSafetyFilter()),
    )
    await handler.handle(
        Event(
            name="conversation.message",
            payload={
                "user_id": str(telegram_user_id),
                "db_user_id": user_id,
                "chat_id": 999,
                "text": "hello again",
            },
            correlation_id="corr-safety-audit",
        )
    )

    assert published
    with db_ro() as conn:
        row = conn.execute(
            "SELECT status, route_json FROM turn_audit_log WHERE correlation_id = ?",
            ("corr-safety-audit",),
        ).fetchone()

    assert row is not None
    assert row["status"] == "reply_dispatched"
    stages = [item["stage"] for item in json.loads(str(row["route_json"]))]
    assert "conversation.handler.safety_throttled" in stages
    assert "conversation.handler.safety_reply_published" in stages
