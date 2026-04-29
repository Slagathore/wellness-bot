from __future__ import annotations

from datetime import datetime
from typing import Any

import app.personality.modes as personality_modes
from app.core.container import container
from app.db import db_ro, db_rw
from app.domain.reminders.auto_followups import maybe_create_followup_for_message
from app.domain.reminders.service import ReminderService
from app.infra.db.reminders_repo import SqliteReminderRepository


def _reminder_service() -> ReminderService:
    return ReminderService(SqliteReminderRepository())


def _insert_session(user_id: int) -> int:
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO sessions(user_id, status, ctx_token_budget) VALUES(?, 'active', 1024)",
            (user_id,),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _insert_user_message(user_id: int, session_id: int, text: str) -> int:
    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO messages(session_id, user_id, role, content, timestamp)
            VALUES (?, ?, 'user', ?, CURRENT_TIMESTAMP)
            """,
            (session_id, user_id, text),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def test_neutral_message_does_not_always_create_followup(test_config, test_user, monkeypatch) -> None:
    user_id, _ = test_user
    monkeypatch.setattr(container, "resolve", lambda name: _reminder_service())

    reminder_id = maybe_create_followup_for_message(
        user_id=user_id,
        session_id=None,
        message_id=None,
        text="I made pasta and watched a show after work.",
        message_timestamp=datetime.utcnow().isoformat(),
    )

    assert reminder_id is None
    with db_ro() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM reminders WHERE user_id = ?", (user_id,)).fetchone()["c"]
    assert count == 0


def test_scheduled_event_with_day_anchor_creates_best_guess_followup(
    test_config, test_user, monkeypatch
) -> None:
    user_id, telegram_user_id = test_user
    sent_events: list[dict[str, Any]] = []

    monkeypatch.setattr(container, "resolve", lambda name: _reminder_service())

    def _capture(event_name: str, payload: dict[str, Any], **_: Any) -> None:
        sent_events.append({"event": event_name, "payload": payload})

    import app.core.events

    monkeypatch.setattr(app.core.events.event_bus, "publish", _capture)

    reminder_id = maybe_create_followup_for_message(
        user_id=user_id,
        session_id=None,
        message_id=101,
        text="I'm really nervous about my interview tomorrow.",
        message_timestamp=datetime.utcnow().isoformat(),
    )

    assert telegram_user_id is not None
    assert reminder_id is not None
    assert sent_events == []
    with db_ro() as conn:
        pending = conn.execute(
            "SELECT value FROM profile_context WHERE user_id = ? AND key = 'pending_followup_clarification'",
            (user_id,),
        ).fetchone()
        reminder = conn.execute(
            "SELECT payload FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    assert pending is None
    assert reminder is not None
    assert "check how the interview went" in str(reminder["payload"])


def test_pending_clarification_resolves_into_reminder(test_config, test_user, monkeypatch) -> None:
    user_id, _ = test_user
    sent_events: list[dict[str, Any]] = []
    monkeypatch.setattr(container, "resolve", lambda name: _reminder_service())

    import app.core.events

    monkeypatch.setattr(
        app.core.events.event_bus,
        "publish",
        lambda event_name, payload, **_: sent_events.append({"event": event_name, "payload": payload}),
    )

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO profile_context(user_id, key, value, updated_at)
            VALUES (?, 'pending_followup_clarification', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, key)
            DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                '{"origin_message_id": 201, "origin_session_id": 22, '
                '"reason_text": "check how court went", '
                '"followup_kind": "event_followup", '
                '"trigger_label": "future_event", '
                '"energy_score": 6, '
                '"valence_score": -0.4}',
            ),
        )

    reminder_id = maybe_create_followup_for_message(
        user_id=user_id,
        session_id=22,
        message_id=202,
        text="It should be over around 3 pm tomorrow.",
        message_timestamp=datetime.utcnow().isoformat(),
    )

    assert reminder_id is not None
    assert any("check in after that" in item["payload"]["text"] for item in sent_events)
    with db_ro() as conn:
        pending = conn.execute(
            "SELECT value FROM profile_context WHERE user_id = ? AND key = 'pending_followup_clarification'",
            (user_id,),
        ).fetchone()
        reminder = conn.execute(
            "SELECT payload FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    assert pending is None
    assert reminder is not None
    assert "clarified_event_timing" in str(reminder["payload"])


def test_generic_future_activity_does_not_create_followup(
    test_config, test_user, monkeypatch
) -> None:
    user_id, _ = test_user
    monkeypatch.setattr(container, "resolve", lambda name: _reminder_service())

    reminder_id = maybe_create_followup_for_message(
        user_id=user_id,
        session_id=None,
        message_id=303,
        text="I have work tomorrow.",
        message_timestamp=datetime.utcnow().isoformat(),
    )

    assert reminder_id is None
    with db_ro() as conn:
        pending = conn.execute(
            "SELECT value FROM profile_context WHERE user_id = ? AND key = 'pending_followup_clarification'",
            (user_id,),
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM reminders WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
    assert pending is None
    assert count == 0


def test_followups_no_longer_merge_at_creation_time(test_config, test_user, monkeypatch) -> None:
    user_id, _ = test_user
    monkeypatch.setattr(container, "resolve", lambda name: _reminder_service())
    session_id = _insert_session(user_id)

    first_message_id = _insert_user_message(user_id, session_id, "I feel sick and wiped out today.")
    second_message_id = _insert_user_message(user_id, session_id, "Still feeling sick and wiped out today.")

    first = maybe_create_followup_for_message(
        user_id=user_id,
        session_id=session_id,
        message_id=first_message_id,
        text="I feel sick and wiped out today.",
        message_timestamp=datetime.utcnow().isoformat(),
    )
    second = maybe_create_followup_for_message(
        user_id=user_id,
        session_id=session_id,
        message_id=second_message_id,
        text="Still feeling sick and wiped out today.",
        message_timestamp=datetime.utcnow().isoformat(),
    )

    assert first is not None
    assert second is not None
    assert first != second
    with db_ro() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM reminders WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
    assert count == 2


def test_sentiment_row_can_trigger_followup_when_heuristics_are_soft(
    test_config, test_user, monkeypatch
) -> None:
    user_id, _ = test_user
    monkeypatch.setattr(container, "resolve", lambda name: _reminder_service())
    session_id = _insert_session(user_id)
    message_id = _insert_user_message(user_id, session_id, "I guess things are happening.")
    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO sentiments(message_id, valence, arousal, dominance, emotion_label, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, -0.8, 0.95, 0.2, "fear", 0.92),
        )

    reminder_id = maybe_create_followup_for_message(
        user_id=user_id,
        session_id=session_id,
        message_id=message_id,
        text="I guess things are happening.",
        message_timestamp=datetime.utcnow().isoformat(),
    )

    assert reminder_id is not None
    with db_ro() as conn:
        payload = conn.execute(
            "SELECT payload FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()["payload"]
    assert '"sentiment_source": "message"' in payload


def test_custom_character_mode_blocks_followup_clarifications(
    test_config, test_user, monkeypatch
) -> None:
    user_id, _ = test_user
    monkeypatch.setattr(container, "resolve", lambda name: _reminder_service())
    monkeypatch.setattr(
        "app.domain.reminders.auto_followups.get_user_personality_name",
        lambda _user_id: "custom:42",
    )
    monkeypatch.setattr(
        personality_modes,
        "load_custom_character_config",
        lambda personality_name: {
            "name": "Test Character",
            "emoji": "🎭",
            "temperature": 1.0,
            "top_p": 0.9,
            "repeat_penalty": 1.0,
            "system_prompt": "Roleplay character",
            "enable_reminders": False,
            "psych_profile_weight": 0.0,
        }
        if personality_name == "custom:42"
        else None,
    )

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO profile_context(user_id, key, value, updated_at)
            VALUES (?, 'pending_followup_clarification', ?, CURRENT_TIMESTAMP)
            """,
            (
                user_id,
                '{"origin_message_id": 1, "reason_text": "check how work went"}',
            ),
        )

    reminder_id = maybe_create_followup_for_message(
        user_id=user_id,
        session_id=99,
        message_id=301,
        text="Work should wrap around 4 pm.",
        message_timestamp=datetime.utcnow().isoformat(),
    )

    assert reminder_id is None
    with db_ro() as conn:
        pending = conn.execute(
            "SELECT value FROM profile_context WHERE user_id = ? AND key = 'pending_followup_clarification'",
            (user_id,),
        ).fetchone()
        reminder_count = conn.execute(
            "SELECT COUNT(*) AS c FROM reminders WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
    assert pending is None
    assert reminder_count == 0
