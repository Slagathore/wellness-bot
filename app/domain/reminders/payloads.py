"""
Helper to build reminder payload metadata consistent with legacy fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ReminderPayload:
    text: str
    frequency: str
    time_of_day: Optional[str]
    allow_jitter: bool
    base_hour: int
    base_minute: int
    specific_hour: Optional[int]
    specific_minute: Optional[int]
    extra: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "text": self.text,
            "frequency": self.frequency,
            "time_of_day": self.time_of_day,
            "allow_jitter": self.allow_jitter,
            "base_hour": self.base_hour,
            "base_minute": self.base_minute,
            "specific_hour": self.specific_hour,
            "specific_minute": self.specific_minute,
        }
        payload.update(self.extra)
        return payload


def build_payload(
    *,
    text: str,
    frequency: str,
    time_of_day: str | None,
    allow_jitter: bool,
    base_hour: int | None,
    base_minute: int | None,
    specific_hour: int | None,
    specific_minute: int | None,
    metadata: dict | None = None,
) -> ReminderPayload:
    base_hour_val = base_hour
    base_minute_val = base_minute
    if base_hour_val is None:
        if time_of_day:
            base_hour_val = {
                "morning": 9,
                "afternoon": 15,
                "evening": 20,
                "night": 3,
            }.get(time_of_day.lower(), 9)
        else:
            base_hour_val = 9
    if base_minute_val is None:
        base_minute_val = specific_minute if specific_minute is not None else 0

    return ReminderPayload(
        text=text,
        frequency=frequency,
        time_of_day=time_of_day,
        allow_jitter=allow_jitter,
        base_hour=base_hour_val,
        base_minute=base_minute_val,
        specific_hour=(
            None
            if allow_jitter
            else (specific_hour if specific_hour is not None else base_hour_val)
        ),
        specific_minute=None if allow_jitter else base_minute_val,
        extra=metadata or {},
    )
