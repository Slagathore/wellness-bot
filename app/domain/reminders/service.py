"""Mission Statement:
Empower the Wellness Bot domain layer to schedule, describe, and dispatch reminders that keep
users on-track with their mental-health rituals. This module receives normalized reminder
instructions, enforces scheduling invariants, and fans out events so downstream workers can
deliver empathetic nudges without duplicating business logic.

Responsibilities:
- Accept reminder create/update/delete commands.
- Determine due reminders and emit events to deliver.
- Enforce idempotency and scheduling constraints.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Protocol

from app.core.events import event_bus
from app.domain import events
from app.domain.reminders.commands import CreateReminderCommand
from app.domain.reminders.payloads import build_payload
from app.domain.reminders.timezone import normalize_operator_reminder_time_for_user
from app.utils.time_utils import normalize_operator

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Reminder:
    id: str
    user_id: str
    text: str
    due_at: datetime
    timezone: str | None = None
    recurring: bool = False
    recurrence: str | None = None
    metadata: dict | None = None
    chat_id: int | None = None


# todo: Surface delivery-priority metadata so multi-channel reminder routing stays deterministic.


class ReminderLike(Protocol):
    """Shape of reminder records consumed by the service layer."""

    id: str
    user_id: str
    text: str
    due_at: datetime
    timezone: str | None
    metadata: dict | None
    chat_id: int | None
    cadence_cron: str | None
    enabled: bool
    last_delivered_at: datetime | None


# todo: Surface delivery-priority metadata so multi-channel reminder routing stays deterministic.


class ReminderRepository(Protocol):
    """Abstraction for reminder persistence."""

    def due_before(
        self, ts: datetime, limit: int = 100
    ) -> Iterable[ReminderLike]:  # pragma: no cover - interface
        ...

    def mark_sent(
        self, reminder_id: str, sent_at: datetime
    ) -> None:  # pragma: no cover - interface
        ...

    def delete(self, reminder_id: str) -> None:  # pragma: no cover - interface
        ...

    def list_for_user(
        self, user_id: str, limit: int = 25
    ) -> Iterable[ReminderLike]:  # pragma: no cover - interface
        ...

    def disable(self, reminder_id: str) -> None:  # pragma: no cover - interface
        ...

    def disable_all_for_user(self, user_id: str) -> int:  # pragma: no cover - interface
        ...

    def create(self, cmd: CreateReminderCommand) -> str:  # pragma: no cover - interface
        ...

    def update_sent(
        self, reminder_id: str, next_send_time: datetime | None
    ) -> None:  # pragma: no cover - interface
        ...

    def reschedule(
        self, reminder_id: str, next_send_time: datetime
    ) -> None:  # pragma: no cover - interface
        ...


class ReminderService:
    """Coordinates reminder scheduling and delivery."""

    def __init__(self, repo: ReminderRepository) -> None:
        self._repo = repo

    def process_due(self, *, now: datetime) -> int:
        """Find due reminders and emit events; returns count emitted."""
        now = normalize_operator(now)
        horizon = now + timedelta(hours=2)
        emitted = 0
        reminders = list(self._repo.due_before(horizon, limit=250))
        by_user: dict[str, list[ReminderLike]] = {}
        for reminder in reminders:
            by_user.setdefault(str(reminder.user_id), []).append(reminder)
        for user_reminders in by_user.values():
            emitted += self._emit_user_batches(user_reminders, now=now)
        return emitted

    def _emit_user_batches(
        self, reminders: list[ReminderLike], *, now: datetime
    ) -> int:
        now = normalize_operator(now)
        emitted = 0
        ordered = sorted(reminders, key=self._normalized_due_at)
        consumed: set[str] = set()

        overdue = [item for item in ordered if self._normalized_due_at(item) < now]
        if len(overdue) > 1:
            self._publish_batch(self._build_batch(overdue, now=now, overdue_merge=True))
            consumed.update(str(item.id) for item in overdue)
            emitted += 1

        fixed = [
            item
            for item in ordered
            if str(item.id) not in consumed and self._is_exact_fixed_time(item)
        ]

        for item in ordered:
            item_id = str(item.id)
            item_due = self._normalized_due_at(item)
            if item_id in consumed or item_due > now or self._is_exact_fixed_time(item):
                continue
            anchor = self._nearest_fixed_anchor(item, fixed)
            anchor_due = self._normalized_due_at(anchor) if anchor is not None else None
            if anchor is not None and anchor_due is not None and anchor_due > now:
                self._repo.reschedule(item_id, anchor_due)
                consumed.add(item_id)
                logger.info(
                    "[REMINDER-TELEMETRY] deferred reminder_id=%s user_id=%s anchor_id=%s anchor_at=%s",
                    item_id,
                    item.user_id,
                    anchor.id,
                    anchor_due.isoformat(),
                )

        for anchor in fixed:
            anchor_id = str(anchor.id)
            anchor_due = self._normalized_due_at(anchor)
            if anchor_id in consumed or anchor_due > now:
                continue
            same_time_fixed = [
                item
                for item in fixed
                if str(item.id) not in consumed
                and self._normalized_due_at(item) == anchor_due
            ]
            same_time_ids = {str(item.id) for item in same_time_fixed}
            members: list[ReminderLike] = list(same_time_fixed)
            for item in ordered:
                item_id = str(item.id)
                if item_id in consumed or self._is_exact_fixed_time(item):
                    continue
                nearest = self._nearest_fixed_anchor(item, fixed)
                if nearest is not None and str(nearest.id) in same_time_ids:
                    members.append(item)
            if members:
                self._publish_batch(self._build_batch(members, now=now, overdue_merge=False))
                consumed.update(str(item.id) for item in members)
                emitted += 1

        loose_due = [
            item
            for item in ordered
            if str(item.id) not in consumed
            and not self._is_exact_fixed_time(item)
            and self._normalized_due_at(item) <= now
        ]
        while loose_due:
            anchor = loose_due[0]
            anchor_due = self._normalized_due_at(anchor)
            members = [
                item
                for item in ordered
                if str(item.id) not in consumed
                and not self._is_exact_fixed_time(item)
                and abs(
                    (
                        self._normalized_due_at(item) - anchor_due
                    ).total_seconds()
                )
                <= 7200
            ]
            if not members:
                break
            self._publish_batch(self._build_batch(members, now=now, overdue_merge=False))
            consumed.update(str(item.id) for item in members)
            emitted += 1
            loose_due = [
                item
                for item in ordered
                if str(item.id) not in consumed
                and not self._is_exact_fixed_time(item)
                and self._normalized_due_at(item) <= now
            ]
        return emitted

    def _publish_batch(self, payload: dict[str, object]) -> None:
        raw_metadata = payload.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        logger.info(
            "[REMINDER-TELEMETRY] emit reminder_id=%s user_id=%s chat_id=%s due_at=%s merged=%s overdue_merge=%s",
            payload.get("reminder_id"),
            payload.get("user_id"),
            payload.get("chat_id"),
            payload.get("due_at"),
            metadata.get("merged_count"),
            metadata.get("overdue_merge"),
        )
        event_bus.publish(events.EVENT_REMINDER_DUE, payload)

    def _build_batch(
        self,
        reminders: list[ReminderLike],
        *,
        now: datetime,
        overdue_merge: bool,
    ) -> dict[str, object]:
        ordered = sorted(reminders, key=self._normalized_due_at)
        primary = ordered[0]
        primary_metadata = dict(primary.metadata or {})
        primary_metadata.update(
            {
                "merged_count": len(ordered),
                "merged_reminders": [self._serialize_member(item) for item in ordered],
                "overdue_merge": overdue_merge,
                "merge_window_minutes": 120,
                "merge_generated_at": now.isoformat(),
            }
        )
        primary_text = (
            "check in after missed reminders"
            if overdue_merge
            else (primary.text or "combined reminder check-in")
        )
        return {
            "reminder_id": primary.id,
            "user_id": primary.user_id,
            "chat_id": primary.chat_id,
            "text": primary_text,
            "due_at": min(self._normalized_due_at(item) for item in ordered).isoformat(),
            "timezone": primary.timezone,
            "metadata": primary_metadata,
        }

    @staticmethod
    def _serialize_member(reminder: ReminderLike) -> dict[str, object]:
        metadata = dict(reminder.metadata or {})
        return {
            "id": reminder.id,
            "text": reminder.text,
            "due_at": ReminderService._normalized_due_at(reminder).isoformat(),
            "timezone": reminder.timezone,
            "cadence_cron": reminder.cadence_cron,
            "enabled": bool(reminder.enabled),
            "last_delivered_at": (
                normalize_operator(reminder.last_delivered_at).isoformat()
                if reminder.last_delivered_at is not None
                else None
            ),
            "metadata": metadata,
            "fixed_time": ReminderService._fixed_time_flag_from_metadata(metadata),
            "recurring": bool(
                reminder.cadence_cron and str(reminder.cadence_cron).lower() != "once"
            ),
        }

    @staticmethod
    def _fixed_time_flag_from_metadata(metadata: dict[str, object]) -> bool:
        if "fixed_time" in metadata:
            return bool(metadata.get("fixed_time"))
        if metadata.get("followup_kind"):
            return False
        allow_jitter = bool(metadata.get("allow_jitter") or metadata.get("fuzzy"))
        return (
            not allow_jitter
            and metadata.get("specific_hour") is not None
            and metadata.get("specific_minute") is not None
        )

    def _is_exact_fixed_time(self, reminder: ReminderLike) -> bool:
        return self._fixed_time_flag_from_metadata(dict(reminder.metadata or {}))

    @staticmethod
    def _nearest_fixed_anchor(
        reminder: ReminderLike, fixed: list[ReminderLike]
    ) -> ReminderLike | None:
        if not fixed:
            return None
        ranked = sorted(
            fixed,
            key=lambda anchor: (
                abs(
                    (
                        ReminderService._normalized_due_at(anchor)
                        - ReminderService._normalized_due_at(reminder)
                    ).total_seconds()
                ),
                ReminderService._normalized_due_at(anchor),
            ),
        )
        return ranked[0]

    @staticmethod
    def _normalized_due_at(reminder: ReminderLike) -> datetime:
        return normalize_operator(reminder.due_at)

    def mark_sent(self, reminder_id: str, *, sent_at: datetime) -> None:
        """Mark reminder as delivered."""
        self._repo.mark_sent(reminder_id, sent_at)

    def list_for_user(self, user_id: str, limit: int = 25) -> Iterable[ReminderLike]:
        """List reminders for a given user."""
        return self._repo.list_for_user(user_id, limit=limit)

    def disable(self, reminder_id: str) -> None:
        """Disable (pause) a specific reminder."""
        self._repo.disable(reminder_id)

    def disable_all_for_user(self, user_id: str) -> int:
        """Disable all reminders for a user; returns count updated."""
        return self._repo.disable_all_for_user(user_id)

    def create(self, cmd: CreateReminderCommand) -> str:
        """Create a new reminder and return its ID.

        The reminder is persisted and will be picked up by the periodic
        ``_scan_due`` job when ``next_run_at`` passes.  We no longer fire
        ``EVENT_REMINDER_DUE`` immediately on creation because that caused
        premature dispatch (before the reminder is actually due) and wasted
        LLM calls on cloud models.
        """
        reminder_id = self._repo.create(cmd)
        logger.info(
            "[REMINDER-TELEMETRY] create reminder_id=%s user_id=%s next_run_at=%s cadence=%s enabled=%s",
            reminder_id,
            cmd.user_id,
            cmd.next_run_at.isoformat() if cmd.next_run_at else None,
            cmd.cadence_cron,
            cmd.enabled,
        )
        #todo: Optionally emit a "reminder.created" informational event for analytics
        return reminder_id

    def mark_sent_and_schedule_next(
        self, reminder_id: str, next_send_time: datetime | None
    ) -> None:
        """Mark a reminder as sent and optionally set the next run."""
        self._repo.update_sent(reminder_id, next_send_time)

    def create_custom_reminder(
        self,
        *,
        user_id: str,
        text: str,
        next_run_at: datetime,
        frequency: str,
        time_of_day: str | None = None,
        allow_jitter: bool = True,
        base_hour: int | None = None,
        base_minute: int | None = None,
        specific_hour: int | None = None,
        specific_minute: int | None = None,
        metadata: dict | None = None,
        timezone: str | None = None,
    ) -> str:
        """
        Convenience wrapper mirroring legacy payload structure for custom reminders.

        The payload stores scheduling hints (time_of_day, jitter, base/specific times)
        used by the reminder sender to randomize or pin times.
        """
        payload_data = build_payload(
            text=text,
            frequency=frequency,
            time_of_day=time_of_day,
            allow_jitter=allow_jitter,
            base_hour=base_hour,
            base_minute=base_minute,
            specific_hour=specific_hour,
            specific_minute=specific_minute,
            metadata=metadata,
        ).to_dict()
        payload_data.setdefault("respect_sleep_window", True)
        payload_data.setdefault(
            "fixed_time",
            bool(
                not allow_jitter
                and specific_hour is not None
                and specific_minute is not None
                and not payload_data.get("followup_kind")
            ),
        )

        adjusted_next_run_at = next_run_at
        try:
            adjusted_next_run_at = normalize_operator_reminder_time_for_user(
                next_run_at,
                int(user_id),
                time_of_day=time_of_day,
                min_lead_minutes=30,
                respect_sleep_window=bool(
                    payload_data.get("respect_sleep_window", True)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Reminder normalization skipped for user %s text=%r: %s",
                user_id,
                text[:80],
                exc,
            )

        cmd = CreateReminderCommand(
            user_id=user_id,
            text=text,
            next_run_at=adjusted_next_run_at,
            cadence_cron=frequency,
            enabled=True,
            timezone=timezone,
            metadata=payload_data,
        )
        return self.create(cmd)
