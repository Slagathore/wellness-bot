"""
AsyncIO scheduler wrapper (APScheduler) with graceful start/stop.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class Scheduler:
    """Thin wrapper to standardize job registration and lifecycle."""

    def __init__(self, *, timezone: str = "UTC") -> None:
        self._scheduler = AsyncIOScheduler(timezone=timezone)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._scheduler.start()
        self._started = True
        logger.info("Scheduler started")

    def shutdown(self, *, wait: bool = True) -> None:
        if not self._started:
            return
        try:
            self._scheduler.shutdown(wait=wait)
        except Exception:  # noqa: BLE001
            # Absorb errors during shutdown (e.g. CancelledError from
            # in-flight jobs when the event loop is already stopping).
            pass
        self._started = False
        logger.info("Scheduler stopped")

    def add_interval_job(
        self,
        func: Callable[..., Any],
        *,
        seconds: int | None = None,
        minutes: int | None = None,
        hours: int | None = None,
        id: str | None = None,
        max_instances: int = 1,
        coalesce: bool = True,
        misfire_grace_time: int = 60,
    ) -> None:
        # APScheduler requires numeric values; default to 0 if None
        seconds = seconds or 0
        minutes = minutes or 0
        hours = hours or 0
        trigger = IntervalTrigger(seconds=seconds, minutes=minutes, hours=hours)
        self._scheduler.add_job(
            func,
            trigger=trigger,
            id=id,
            max_instances=max_instances,
            coalesce=coalesce,
            misfire_grace_time=misfire_grace_time,
        )

    def add_cron_job(
        self,
        func: Callable[..., Any],
        *,
        cron: str | None = None,
        id: str | None = None,
        max_instances: int = 1,
        misfire_grace_time: int = 120,
    ) -> None:
        if not cron:
            raise ValueError("cron expression required")
        trigger = CronTrigger.from_crontab(cron)
        self._scheduler.add_job(
            func,
            trigger=trigger,
            id=id,
            max_instances=max_instances,
            misfire_grace_time=misfire_grace_time,
        )


def create_scheduler(*, timezone: str = "UTC") -> Scheduler:
    return Scheduler(timezone=timezone)
