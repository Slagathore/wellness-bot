"""
Helper to register baseline jobs on the scheduler.

This is intentionally small; real reminder/outbox/workfocus jobs will migrate here
as domain services are extracted.
"""

from __future__ import annotations

import logging
import time

from app.core.events import event_bus
from app.core.scheduler import Scheduler

logger = logging.getLogger(__name__)


def register_baseline_jobs(scheduler: Scheduler) -> None:
    """
    Register lightweight jobs to validate scheduler wiring.
    """

    scheduler.add_interval_job(
        emit_heartbeat, seconds=60, id="heartbeat", max_instances=1
    )
    logger.info("Baseline jobs registered")


def emit_heartbeat() -> None:
    """Emit a heartbeat event for monitoring."""
    event_bus.publish(
        "system.heartbeat",
        {"ts": time.time()},
    )


def register_checkin_job(scheduler: Scheduler) -> None:
    """Register workfocus check-in scanning job."""
    from app.workers.checkins import run_checkins

    scheduler.add_interval_job(
        run_checkins, minutes=1, id="checkins.scan_due", max_instances=1
    )
