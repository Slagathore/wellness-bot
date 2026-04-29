from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.domain.reminders.commands import CreateReminderCommand
from app.domain.reminders.service import ReminderService


@dataclass
class FakeReminderRecord:
    id: str
    user_id: str
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
                text=cmd.text,
                metadata=cmd.metadata or {},
                due_at=cmd.next_run_at,
            )
        )
        return rid

    def due_before(self, ts, limit: int = 100):
        return []

    def mark_sent(self, reminder_id: str, sent_at):
        return None

    def delete(self, reminder_id: str):
        return None

    def list_for_user(self, user_id: str, limit: int = 25):
        return [r for r in self.records if r.user_id == user_id][:limit]

    def disable(self, reminder_id: str):
        return None

    def disable_all_for_user(self, user_id: str):
        return 0

    def update_sent(self, reminder_id: str, next_send_time):
        return None

    def reschedule(self, reminder_id: str, next_send_time):
        return None


def test_create_reminder_inserts_record():
    repo = FakeRepo()
    svc = ReminderService(repo)

    rid = svc.create(
        CreateReminderCommand(
            user_id="1",
            text="Drink water",
            next_run_at=datetime.now(timezone.utc),
            cadence_cron="0 9 * * *",
            enabled=True,
            timezone="UTC",
            metadata={"frequency": "daily"},
        )
    )
    assert rid.isdigit()
    assert repo.records[0].user_id == "1"
