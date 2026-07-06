"""Regression tests for the reminder due-scan separator bug (Bug 1).

Onboarding writes next_run_at with a space separator ("YYYY-MM-DD HH:MM:SS")
while the scan compared it against a "T"-separated cutoff via a lexical SUBSTR.
Because ' ' < 'T', a same-day reminder scheduled for *later* compared as already
due, so onboarding reminders fired hours early. The scan now normalizes the
separator (REPLACE 'T' -> ' ') on both sides, which is separator-agnostic and
time-correct.
"""

from __future__ import annotations

from datetime import datetime

from app.db import db_rw
from app.infra.db.reminders_repo import SqliteReminderRepository


def _insert_reminder(user_id: int, next_run_at: str) -> None:
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO reminders(user_id, kind, payload, next_run_at, enabled) "
            "VALUES (?, 'custom', '{\"text\": \"hi\"}', ?, 1)",
            (user_id, next_run_at),
        )


def test_space_separated_future_reminder_is_not_due_early(test_user) -> None:
    user_id, _ = test_user
    # Scheduled later today, written with a space separator (onboarding style).
    day = "2026-07-05"
    _insert_reminder(user_id, f"{day} 23:00:00")

    repo = SqliteReminderRepository()
    cutoff = datetime(2026, 7, 5, 8, 0, 0)  # 8am — well before 11pm
    due_ids = [r.id for r in repo.due_before(cutoff)]

    assert due_ids == []  # must NOT be treated as due at 8am


def test_space_separated_past_reminder_is_due(test_user) -> None:
    user_id, _ = test_user
    _insert_reminder(user_id, "2026-07-05 06:00:00")

    repo = SqliteReminderRepository()
    cutoff = datetime(2026, 7, 5, 8, 0, 0)  # 8am — after 6am
    due_ids = [r.id for r in repo.due_before(cutoff)]

    assert len(due_ids) == 1


def test_t_separated_reminder_still_works(test_user) -> None:
    user_id, _ = test_user
    _insert_reminder(user_id, "2026-07-05T06:00:00")  # reschedule style

    repo = SqliteReminderRepository()
    due_ids = [r.id for r in repo.due_before(datetime(2026, 7, 5, 8, 0, 0))]

    assert len(due_ids) == 1
