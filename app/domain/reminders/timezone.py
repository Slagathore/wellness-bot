"""
Timezone resolution for reminders.

Bridges per-user timezone offsets (stored in profile_context) with the
operator-time scheduling used by the reminder scanner.  All reminder
storage remains in operator time; this module handles the user ↔ operator
conversion so reminders fire at the right moment for each user.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.utils.time_utils import (
    normalize_operator,
    operator_now,
    to_operator_time,
    to_user_time,
)

logger = logging.getLogger(__name__)
_WAKE_BUFFER_MINUTES = 45


def _parse_clock_time(raw: str | None) -> tuple[int, int] | None:
    if not raw:
        return None
    text = str(raw).strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except Exception:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def resolve_user_tz_offset(user_id: int) -> int | None:
    """Return the user's timezone offset in minutes east of zero, or None."""
    try:
        from app.db import db_ro
        with db_ro() as conn:
            row = conn.execute(
                "SELECT value FROM profile_context "
                "WHERE user_id = ? AND key = 'timezone_offset_minutes'",
                (user_id,),
            ).fetchone()
        if row:
            raw = row[0] if isinstance(row, tuple) else row["value"]
            return int(raw)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not resolve tz offset for user %s: %s", user_id, exc)
    return None


def user_now(user_id: int) -> datetime:
    """Return the current time in the user's local timezone (naive)."""
    offset = resolve_user_tz_offset(user_id)
    if offset is None:
        # Fall back to operator time
        return operator_now().replace(tzinfo=None)
    op = operator_now()
    return to_user_time(op, offset)


def user_time_to_operator(user_local: datetime, user_id: int) -> datetime:
    """Convert a naive user-local datetime to aware operator time for storage."""
    offset = resolve_user_tz_offset(user_id)
    if offset is None:
        # No offset stored — assume operator time
        return normalize_operator(user_local)
    return to_operator_time(user_local, offset)


def resolve_user_sleep_window(
    user_id: int,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Return the user's usual bedtime and wake time as (hour, minute) pairs."""
    try:
        from app.db import db_ro

        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT key, value
                FROM profile_context
                WHERE user_id = ?
                  AND key IN ('usual_bedtime', 'usual_wake_time')
                """,
                (user_id,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not resolve sleep window for user %s: %s", user_id, exc)
        return None

    values = {str(row["key"]): str(row["value"]) for row in rows if row["value"]}
    bedtime = _parse_clock_time(values.get("usual_bedtime"))
    wake_time = _parse_clock_time(values.get("usual_wake_time"))
    if bedtime and wake_time:
        return bedtime, wake_time
    return None


def normalize_user_local_reminder_time(
    candidate_local: datetime,
    *,
    reference_local: datetime,
    time_of_day: str | None = None,
    sleep_window: tuple[tuple[int, int], tuple[int, int]] | None = None,
    min_lead_minutes: int = 30,
) -> datetime:
    """Shift a user-local reminder time out of "too soon" and sleep-window ranges."""
    adjusted = candidate_local.replace(second=0, microsecond=0)
    min_allowed = reference_local.replace(second=0, microsecond=0) + timedelta(
        minutes=max(0, int(min_lead_minutes))
    )
    if adjusted < min_allowed:
        adjusted = min_allowed

    if not sleep_window:
        return adjusted

    bedtime, wake_time = sleep_window
    for _ in range(4):
        if not _is_within_sleep_window(adjusted, bedtime, wake_time):
            break
        adjusted = _next_awake_time(adjusted, bedtime, wake_time, time_of_day=time_of_day)
        if adjusted < min_allowed:
            adjusted = min_allowed
    return adjusted


def normalize_operator_reminder_time_for_user(
    candidate_operator: datetime,
    user_id: int,
    *,
    time_of_day: str | None = None,
    min_lead_minutes: int = 30,
    respect_sleep_window: bool = True,
) -> datetime:
    """Normalize an operator-time reminder candidate using the user's local clock."""
    operator_dt = normalize_operator(candidate_operator)
    local_reference = user_now(user_id)
    local_candidate = to_user_time(operator_dt, resolve_user_tz_offset(user_id), reference=operator_dt)
    sleep_window = resolve_user_sleep_window(user_id) if respect_sleep_window else None
    adjusted_local = normalize_user_local_reminder_time(
        local_candidate,
        reference_local=local_reference,
        time_of_day=time_of_day,
        sleep_window=sleep_window,
        min_lead_minutes=min_lead_minutes,
    )
    return user_time_to_operator(adjusted_local, user_id)


def operator_to_user_display(
    operator_dt: datetime, user_id: int, fmt: str = "%b %d at %I:%M %p"
) -> str:
    """Format an operator datetime as a human-readable string in the user's timezone."""
    offset = resolve_user_tz_offset(user_id)
    if offset is None:
        dt = normalize_operator(operator_dt)
    else:
        dt = to_user_time(operator_dt, offset)
    return dt.strftime(fmt)


def _is_within_sleep_window(
    candidate_local: datetime,
    bedtime: tuple[int, int],
    wake_time: tuple[int, int],
) -> bool:
    minutes = candidate_local.hour * 60 + candidate_local.minute
    bedtime_minutes = bedtime[0] * 60 + bedtime[1]
    wake_minutes = wake_time[0] * 60 + wake_time[1]
    if bedtime_minutes == wake_minutes:
        return False
    if bedtime_minutes < wake_minutes:
        return bedtime_minutes <= minutes < wake_minutes
    return minutes >= bedtime_minutes or minutes < wake_minutes


def _next_awake_time(
    candidate_local: datetime,
    bedtime: tuple[int, int],
    wake_time: tuple[int, int],
    *,
    time_of_day: str | None = None,
) -> datetime:
    wake_dt = candidate_local.replace(
        hour=wake_time[0],
        minute=wake_time[1],
        second=0,
        microsecond=0,
    )
    bedtime_minutes = bedtime[0] * 60 + bedtime[1]
    wake_minutes = wake_time[0] * 60 + wake_time[1]
    candidate_minutes = candidate_local.hour * 60 + candidate_local.minute

    crosses_midnight = bedtime_minutes > wake_minutes
    if crosses_midnight:
        if candidate_minutes >= bedtime_minutes:
            wake_dt += timedelta(days=1)
    elif candidate_minutes >= wake_minutes:
        wake_dt += timedelta(days=1)

    adjusted = wake_dt + timedelta(minutes=_WAKE_BUFFER_MINUTES)
    if str(time_of_day or "").strip().lower() == "afternoon" and adjusted.hour < 13:
        adjusted = adjusted.replace(hour=13, minute=0)
    return adjusted
