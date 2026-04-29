from __future__ import annotations

from datetime import datetime, timezone

from dataclasses import dataclass

from app.domain.reminders.service import ReminderService
from app.domain.reminders.commands import CreateReminderCommand


@dataclass
class FakeReminderRecord:
    id: str
    user_id: str
    kind: str
    text: str
    metadata: dict
    due_at: object | None = None


class FakeRepo:
    def __init__(self):
        self.records = []
        self._counter = 0

    def create(self, cmd: CreateReminderCommand) -> str:
        self._counter += 1
        rid = str(self._counter)
        self.records.append(
            FakeReminderRecord(
                id=rid,
                user_id=cmd.user_id,
                kind=cmd.kind,
                text=cmd.text,
                metadata=cmd.metadata or {},
                due_at=cmd.next_run_at,
            )
        )
        return rid

    def list_for_user(self, user_id: str, limit: int = 25):
        return [r for r in self.records if r.user_id == user_id][:limit]

    def due_before(self, ts, limit: int = 100):
        return []

    def mark_sent(self, reminder_id: str, sent_at):
        return None

    def delete(self, reminder_id: str):
        return None

    def disable(self, reminder_id: str):
        return None

    def disable_all_for_user(self, user_id: str):
        return 0

    def update_sent(self, reminder_id: str, next_send_time):
        return None

    def reschedule(self, reminder_id: str, next_send_time):
        return None


def test_custom_reminder_payload_preserves_fields():
    repo = FakeRepo()
    svc = ReminderService(repo)

    rid = svc.create_custom_reminder(
        user_id="1",
        text="Test",
        next_run_at=datetime.now(timezone.utc),
        frequency="0 9 * * *",
        time_of_day="morning",
        allow_jitter=False,
        base_hour=8,
        base_minute=15,
        specific_hour=8,
        specific_minute=15,
        metadata={"foo": "bar"},
        timezone="UTC",
    )
    assert rid.isdigit()

    reminders = list(repo.list_for_user("1", limit=10))
    assert reminders and reminders[0].metadata.get("foo") == "bar"
    assert reminders[0].metadata.get("specific_hour") == 8
