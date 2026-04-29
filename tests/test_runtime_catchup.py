from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.db import db_rw
from app.runtime.catchup import OfflineCatchupManager


class _DummyLLM:
    def chat(self, prompt):  # pragma: no cover - not used by this test
        return {"text": "ok"}


class _DummySessions:
    def ensure_user(self, tg_user_id: int, username=None, name=None) -> int:
        return int(tg_user_id)


def test_overdue_reminders_reads_payload_text(test_config, test_user) -> None:
    user_id, _ = test_user
    now = datetime.now(timezone.utc)
    due_at = now - timedelta(minutes=5)

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO reminders (user_id, kind, payload, next_run_at, enabled)
            VALUES (?, ?, ?, ?, 1)
            """,
            (
                user_id,
                "wellness",
                json.dumps({"text": "Drink water"}),
                due_at.isoformat(),
            ),
        )

    manager = OfflineCatchupManager(llm=_DummyLLM(), sessions=_DummySessions())
    manager.offline_start = now - timedelta(minutes=30)
    manager.offline_end = now + timedelta(seconds=1)

    overdue = manager._overdue_reminders(user_id)

    assert len(overdue) == 1
    assert overdue[0]["text"] == "Drink water"


def test_catchup_waits_for_idle_window_before_flushing(
    test_config, test_user
) -> None:
    user_id, telegram_user_id = test_user
    manager = OfflineCatchupManager(llm=_DummyLLM(), sessions=_DummySessions())

    now = datetime.now(timezone.utc)
    manager.offline_start = now - timedelta(minutes=20)
    manager.offline_end = now
    manager.started_at = now
    manager.active = True

    noted = manager.note_incoming_message(
        tg_user_id=telegram_user_id,
        chat_id=telegram_user_id,
        text="Hey, I sent this while you were offline.",
        msg_ts=now - timedelta(minutes=1),
        username="tester",
        name="Tester",
    )

    assert noted is True
    assert manager.ready_to_flush() is False

    manager._last_offline_note_at = now - timedelta(seconds=20)
    assert manager.ready_to_flush() is True
