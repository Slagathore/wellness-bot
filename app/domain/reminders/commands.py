from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class CreateReminderCommand:
    user_id: str
    text: str
    kind: str = "custom_reminder"
    next_run_at: datetime | None = None
    cadence_cron: str | None = None
    enabled: bool = True
    timezone: str | None = None
    metadata: dict | None = None
