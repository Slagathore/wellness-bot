"""
Shared time utilities for the wellness bot.

All scheduling and storage is normalized to the operator timezone
(America/Chicago). These helpers keep calculations anchored to the operator
clock and work in terms of relative offsets.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from typing import Callable, Optional

ZoneInfoType: Callable[[str], tzinfo] | None
try:
    from zoneinfo import ZoneInfo as ZoneInfoType
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfoType = None

OPERATOR_TZ_NAME = "America/Chicago"

if ZoneInfoType is not None:
    OPERATOR_TZ = ZoneInfoType(OPERATOR_TZ_NAME)
else:  # pragma: no cover - zoneinfo unavailable fallback
    OPERATOR_TZ = dt_timezone(-timedelta(hours=6))


def operator_now() -> datetime:
    """Return the current operator time (aware)."""

    return datetime.now(OPERATOR_TZ)


def get_current_time() -> datetime:
    """
    Return the canonical “current time” for UI components.

    Historically this helper lived inside ``unified_bot``; exporting it keeps
    legacy callers working while centralizing time utilities.
    """

    return operator_now()


def operator_now_str() -> str:
    """Return the current operator time formatted for SQLite storage."""

    return operator_now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_operator(dt_obj: datetime | None) -> datetime:
    """
    Ensure a datetime is timezone-aware in the operator timezone.

    Accepts naive or aware datetimes and always returns an aware datetime
    anchored to the operator clock.
    """

    if dt_obj is None:
        return operator_now()
    if dt_obj.tzinfo is None:
        return dt_obj.replace(tzinfo=OPERATOR_TZ)
    return dt_obj.astimezone(OPERATOR_TZ)


def operator_offset_minutes(reference: Optional[datetime] = None) -> int:
    """Return the operator offset (minutes east of zero) at a given moment."""

    reference_dt = normalize_operator(reference)
    offset = reference_dt.utcoffset() or timedelta(0)
    return int(offset.total_seconds() // 60)


def offset_delta(
    user_offset_minutes: int | None, reference: Optional[datetime] = None
) -> int:
    """
    Return the minute delta between the user offset and the operator clock.

    Arguments:
        user_offset_minutes: minutes east of zero (the traditional offset).
        reference: operator timestamp to evaluate DST-aware offset.
    """

    if user_offset_minutes is None:
        return 0
    operator_offset = operator_offset_minutes(reference)
    return user_offset_minutes - operator_offset


def to_operator_time(
    user_local: datetime,
    user_offset_minutes: int | None,
    *,
    reference: Optional[datetime] = None,
) -> datetime:
    """
    Convert a user-local naive datetime into an operator-aware datetime.
    """

    if user_local.tzinfo is not None:
        raise ValueError("user_local must be naive (no timezone information)")
    delta = offset_delta(user_offset_minutes, reference)
    operator_naive = user_local - timedelta(minutes=delta)
    return operator_naive.replace(tzinfo=OPERATOR_TZ)


def to_operator_string(
    user_local: datetime,
    user_offset_minutes: int | None,
    *,
    reference: Optional[datetime] = None,
) -> str:
    """
    Convert a user-local naive datetime into operator time string.
    """

    operator_dt = to_operator_time(user_local, user_offset_minutes, reference=reference)
    return operator_dt.strftime("%Y-%m-%d %H:%M:%S")


def to_user_time(
    operator_dt: datetime,
    user_offset_minutes: int | None,
    *,
    reference: Optional[datetime] = None,
) -> datetime:
    """
    Convert an operator datetime (aware or naive) into a naive user-local datetime.
    """

    base_operator = normalize_operator(operator_dt)
    delta = offset_delta(user_offset_minutes, reference or base_operator)
    user_dt = base_operator + timedelta(minutes=delta)
    return user_dt.replace(tzinfo=None)


def format_operator_datetime(dt_obj: datetime | None) -> str:
    """
    Format a datetime for storage/display using the operator clock.
    """

    return normalize_operator(dt_obj).strftime("%Y-%m-%d %H:%M:%S")
