"""E2E reminder test aligned with modular dispatcher."""

from __future__ import annotations

import json
from datetime import datetime

from app.config import settings
from app.core.events import event_bus
from app.db import db_rw
from app.domain import events
from app.domain.reminders.dispatcher import ReminderDispatcher
from app.domain.reminders.service import ReminderService
from app.infra.db.reminders_repo import SqliteReminderRepository
from app.infra.llm.client import LLMClient
from app.runtime.services.user_sessions import UserSessionStore


class DummyLLM(LLMClient):
    def chat(self, messages, *, model=None, **kwargs):  # type: ignore[override]
        return "Hydration reminder: have a glass of water."


def test_reminder_dispatch_triggers_send_reply(monkeypatch, test_config, test_session):
    user_id, session_id = test_session
    cfg = settings()
    store = UserSessionStore(
        data_root=cfg.data_root, ctx_token_budget=cfg.ctx_token_budget
    )
    reminder_service = ReminderService(SqliteReminderRepository())
    dispatcher = ReminderDispatcher(reminder_service, DummyLLM(), store)

    reminder_payload = {"text": "Drink Water", "frequency": "once"}
    now = datetime.utcnow().isoformat(sep=" ")

    with db_rw() as conn:
        user = conn.execute(
            "SELECT telegram_user_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        telegram_chat_id = user["telegram_user_id"]
        reminder_id = conn.execute(
            "INSERT INTO reminders(user_id, kind, payload, next_run_at, enabled, cadence_cron) VALUES(?, 'hydration', ?, ?, 1, ?)",
            (user_id, json.dumps(reminder_payload), now, "0 * * * *"),
        ).lastrowid

    sent_events = []
    orig_publish = event_bus.publish

    def fake_publish(event_name, payload, **kwargs):
        if event_name == events.EVENT_SEND_REPLY:
            sent_events.append(payload)
        return orig_publish(event_name, payload, **kwargs)

    monkeypatch.setattr(event_bus, "publish", fake_publish)

    dispatcher.handle_due(
        {
            "reminder_id": reminder_id,
            "user_id": user_id,
            "chat_id": telegram_chat_id,
            "text": reminder_payload["text"],
            "metadata": reminder_payload,
        }
    )

    assert sent_events, "No reminder reply emitted"
    assert "water" in sent_events[0]["text"].lower()
