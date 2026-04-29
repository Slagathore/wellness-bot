"""
SQLite-backed reminder repository.

Assumes table `reminders` with columns:
id, user_id, kind, payload (JSON), next_run_at (ISO), last_delivered_at, enabled, cadence_cron.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List

from app.domain.reminders.commands import CreateReminderCommand
from app.domain.reminders.service import ReminderLike
from app.infra.db.session import db_ro, db_rw
from app.utils.time_utils import normalize_operator, operator_now


@dataclass(slots=True)
class ReminderRecord:
    id: str
    user_id: str
    kind: str
    text: str
    due_at: datetime
    timezone: str | None
    metadata: dict | None
    cadence_cron: str | None = None
    enabled: bool = True
    last_delivered_at: datetime | None = None
    chat_id: int | None = None


class SqliteReminderRepository:
    """Minimal reminder repo using existing reminders table."""

    @staticmethod
    def _naive_operator_str(ts: datetime) -> str:
        """Normalize any datetime to a naive operator-time ISO string.

        Strips timezone suffix so SQLite lexical comparison works
        consistently regardless of how the stored value was formatted.
        """
        from app.utils.time_utils import normalize_operator
        return normalize_operator(ts).strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def _parse_operator_datetime(raw_value: str | None) -> datetime | None:
        if not raw_value:
            return None
        parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
        return normalize_operator(parsed)

    def due_before(self, ts: datetime, limit: int = 100) -> Iterable[ReminderLike]:
        ts_str = self._naive_operator_str(ts)
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT r.id, r.user_id, r.kind, r.payload, r.next_run_at, r.cadence_cron,
                       r.enabled, r.last_delivered_at, u.telegram_user_id
                FROM reminders r
                LEFT JOIN users u ON u.id = r.user_id
                WHERE r.enabled = 1 AND SUBSTR(r.next_run_at, 1, 19) <= ?
                ORDER BY r.next_run_at ASC
                LIMIT ?
                """,
                (ts_str, limit),
            ).fetchall()
        reminders: List[ReminderRecord] = []
        for row in rows:
            payload = {}
            text = ""
            timezone = None
            if row["payload"]:
                try:
                    payload = json.loads(row["payload"])
                    text = payload.get("text") or payload.get("reminder_text") or ""
                    timezone = payload.get("timezone")
                    if row["cadence_cron"] and not payload.get("cadence_cron"):
                        payload["cadence_cron"] = row["cadence_cron"]
                    if row["enabled"] is not None and "enabled" not in payload:
                        payload["enabled"] = bool(row["enabled"])
                except Exception:
                    payload = {"raw": row["payload"]}
            reminders.append(
                ReminderRecord(
                    id=str(row["id"]),
                    user_id=str(row["user_id"]),
                    kind=str(row["kind"]),
                    text=text,
                    due_at=self._parse_operator_datetime(row["next_run_at"])
                    or normalize_operator(datetime.fromisoformat(str(row["next_run_at"]))),
                    timezone=timezone,
                    metadata=payload,
                    cadence_cron=row["cadence_cron"],
                    enabled=bool(row["enabled"]),
                    last_delivered_at=(
                        self._parse_operator_datetime(row["last_delivered_at"])
                        if row["last_delivered_at"]
                        else None
                    ),
                    chat_id=(
                        row["telegram_user_id"]
                        if "telegram_user_id" in row.keys()
                        else None
                    ),
                )
            )
        return reminders

    def mark_sent(self, reminder_id: str, sent_at: datetime) -> None:
        with db_rw() as conn:
            conn.execute(
                "UPDATE reminders SET last_delivered_at = ?, enabled = 0 WHERE id = ?",
                (self._naive_operator_str(sent_at), reminder_id),
            )

    def delete(self, reminder_id: str) -> None:
        with db_rw() as conn:
            conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))

    def list_for_user(self, user_id: str, limit: int = 25) -> Iterable[ReminderLike]:
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT r.id, r.user_id, r.kind, r.payload, r.next_run_at, r.cadence_cron,
                       r.enabled, r.last_delivered_at, u.telegram_user_id
                FROM reminders r
                LEFT JOIN users u ON u.id = r.user_id
                WHERE r.user_id = ?
                ORDER BY r.enabled DESC, r.next_run_at ASC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()

        reminders: List[ReminderRecord] = []
        for row in rows:
            payload = {}
            text = ""
            timezone = None
            if row["payload"]:
                try:
                    payload = json.loads(row["payload"])
                    text = payload.get("text") or payload.get("reminder_text") or ""
                    timezone = payload.get("timezone")
                    if row["cadence_cron"] and not payload.get("cadence_cron"):
                        payload["cadence_cron"] = row["cadence_cron"]
                    if row["enabled"] is not None and "enabled" not in payload:
                        payload["enabled"] = bool(row["enabled"])
                except Exception:
                    payload = {"raw": row["payload"]}
            reminders.append(
                ReminderRecord(
                    id=str(row["id"]),
                    user_id=str(row["user_id"]),
                    kind=str(row["kind"]),
                    text=text,
                    due_at=self._parse_operator_datetime(row["next_run_at"])
                    or normalize_operator(datetime.fromisoformat(str(row["next_run_at"]))),
                    timezone=timezone,
                    metadata=payload,
                    cadence_cron=row["cadence_cron"],
                    enabled=bool(row["enabled"]),
                    last_delivered_at=(
                        self._parse_operator_datetime(row["last_delivered_at"])
                        if row["last_delivered_at"]
                        else None
                    ),
                    chat_id=(
                        row["telegram_user_id"]
                        if "telegram_user_id" in row.keys()
                        else None
                    ),
                )
            )
        return reminders

    def disable(self, reminder_id: str) -> None:
        with db_rw() as conn:
            conn.execute(
                "UPDATE reminders SET enabled = 0 WHERE id = ?", (reminder_id,)
            )

    def disable_all_for_user(self, user_id: str) -> int:
        with db_rw() as conn:
            cur = conn.execute(
                "UPDATE reminders SET enabled = 0 WHERE user_id = ?", (user_id,)
            )
            return cur.rowcount

    def update_sent(self, reminder_id: str, next_send_time: datetime | None) -> None:
        now_str = self._naive_operator_str(operator_now())
        with db_rw() as conn:
            if next_send_time is None:
                conn.execute(
                    """
                    UPDATE reminders
                    SET last_delivered_at = ?, enabled = 0
                    WHERE id = ?
                    """,
                    (now_str, reminder_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE reminders
                    SET last_delivered_at = ?, next_run_at = ?
                    WHERE id = ?
                    """,
                    (
                        now_str,
                        self._naive_operator_str(next_send_time),
                        reminder_id,
                    ),
                )

    def reschedule(self, reminder_id: str, next_send_time: datetime) -> None:
        with db_rw() as conn:
            conn.execute(
                """
                UPDATE reminders
                SET next_run_at = ?
                WHERE id = ?
                """,
                (self._naive_operator_str(next_send_time), reminder_id),
            )

    def create(self, cmd: CreateReminderCommand) -> str:
        payload = cmd.metadata or {}
        payload.setdefault("text", cmd.text)
        if cmd.timezone:
            payload.setdefault("timezone", cmd.timezone)
        if cmd.cadence_cron:
            payload.setdefault("cadence_cron", cmd.cadence_cron)
        payload.setdefault("enabled", bool(cmd.enabled))
        payload_json = json.dumps(payload)
        with db_rw() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (user_id, kind, payload, next_run_at, cadence_cron, enabled)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cmd.user_id,
                    cmd.kind,
                    payload_json,
                    self._naive_operator_str(cmd.next_run_at) if cmd.next_run_at else None,
                    cmd.cadence_cron,
                    1 if cmd.enabled else 0,
                ),
            )
            return str(cursor.lastrowid)
