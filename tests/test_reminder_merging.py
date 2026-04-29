from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from app.core.events import event_bus
from app.domain import events
from app.domain.reminders.service import ReminderService


@dataclass
class FakeReminder:
    id: str
    user_id: str
    text: str
    due_at: datetime
    metadata: dict
    timezone: str | None = None
    chat_id: int | None = 123
    cadence_cron: str | None = "once"
    enabled: bool = True
    last_delivered_at: datetime | None = None


class FakeRepo:
    def __init__(self, reminders: list[FakeReminder]):
        self.reminders = reminders
        self.rescheduled: list[tuple[str, datetime]] = []

    def due_before(self, ts: datetime, limit: int = 100):
        rows = [r for r in self.reminders if r.enabled and r.due_at <= ts]
        rows.sort(key=lambda r: r.due_at)
        return rows[:limit]

    def mark_sent(self, reminder_id: str, sent_at: datetime):
        return None

    def delete(self, reminder_id: str):
        return None

    def list_for_user(self, user_id: str, limit: int = 25):
        return [r for r in self.reminders if r.user_id == user_id][:limit]

    def disable(self, reminder_id: str):
        return None

    def disable_all_for_user(self, user_id: str):
        return 0

    def create(self, cmd):
        return "1"

    def update_sent(self, reminder_id: str, next_send_time):
        return None

    def reschedule(self, reminder_id: str, next_send_time: datetime):
        self.rescheduled.append((reminder_id, next_send_time))
        for reminder in self.reminders:
            if reminder.id == reminder_id:
                reminder.due_at = next_send_time
                break


def test_due_jitter_defers_to_future_fixed_anchor(monkeypatch):
    now = datetime(2026, 3, 21, 8, 0, tzinfo=timezone.utc)
    fixed = FakeReminder(
        id="fixed-1",
        user_id="u1",
        text="Doctor appointment",
        due_at=now + timedelta(minutes=90),
        metadata={"fixed_time": True, "specific_hour": 9, "specific_minute": 30},
    )
    jitter = FakeReminder(
        id="jit-1",
        user_id="u1",
        text="How are you feeling?",
        due_at=now,
        metadata={"allow_jitter": True, "time_of_day": "morning"},
    )
    repo = FakeRepo([jitter, fixed])
    svc = ReminderService(cast(Any, repo))
    published: list[dict] = []

    monkeypatch.setattr(
        event_bus,
        "publish",
        lambda event_name, payload, **kwargs: published.append(payload)
        if event_name == events.EVENT_REMINDER_DUE
        else None,
    )

    emitted = svc.process_due(now=now)

    assert emitted == 0
    assert repo.rescheduled == [("jit-1", fixed.due_at)]


def test_multiple_overdue_reminders_merge_into_single_batch(monkeypatch):
    now = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
    reminders = [
        FakeReminder(
            id="1",
            user_id="u1",
            text="Drink water",
            due_at=now - timedelta(hours=3),
            metadata={"allow_jitter": True},
        ),
        FakeReminder(
            id="2",
            user_id="u1",
            text="Stretch",
            due_at=now - timedelta(minutes=40),
            metadata={"allow_jitter": True},
        ),
        FakeReminder(
            id="3",
            user_id="u1",
            text="Wake up",
            due_at=now - timedelta(minutes=5),
            metadata={"fixed_time": True, "specific_hour": 7, "specific_minute": 0},
            cadence_cron="daily",
        ),
    ]
    repo = FakeRepo(reminders)
    svc = ReminderService(cast(Any, repo))
    published: list[dict] = []

    def _capture(event_name, payload, **kwargs):
        if event_name == events.EVENT_REMINDER_DUE:
            published.append(payload)

    monkeypatch.setattr(event_bus, "publish", _capture)

    emitted = svc.process_due(now=now)

    assert emitted == 1
    assert len(published) == 1
    meta = published[0]["metadata"]
    assert meta["overdue_merge"] is True
    assert meta["merged_count"] == 3
    assert len(meta["merged_reminders"]) == 3


def test_mixed_naive_and_aware_due_datetimes_do_not_crash(monkeypatch):
    now = datetime(2026, 3, 29, 12, 7, tzinfo=timezone.utc)
    reminders = [
        FakeReminder(
            id="aware",
            user_id="u1",
            text="Take meds",
            due_at=now - timedelta(minutes=10),
            metadata={"allow_jitter": True},
        ),
        FakeReminder(
            id="naive",
            user_id="u1",
            text="Drink water",
            due_at=datetime(2026, 3, 29, 7, 0),
            metadata={"allow_jitter": True},
        ),
    ]

    class MixedRepo(FakeRepo):
        def due_before(self, ts: datetime, limit: int = 100):
            return self.reminders[:limit]

    repo = MixedRepo(reminders)
    svc = ReminderService(cast(Any, repo))
    published: list[dict] = []

    monkeypatch.setattr(
        event_bus,
        "publish",
        lambda event_name, payload, **kwargs: published.append(payload)
        if event_name == events.EVENT_REMINDER_DUE
        else None,
    )

    emitted = svc.process_due(now=now)

    assert emitted == 1
    assert len(published) == 1
    due_at = str(published[0]["due_at"])
    assert "T" in due_at
