from __future__ import annotations

import logging
from datetime import datetime

from app.core.container import container
from app.domain.workfocus.service import WorkFocusService

logger = logging.getLogger(__name__)


def run_checkins(now: datetime | None = None) -> int:
    """Scan and emit due check-ins."""
    try:
        service: WorkFocusService = container.resolve("workfocus_service")
    except Exception:
        logger.warning("WorkFocusService not registered; skipping checkins scan.")
        return 0
    now_str = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:00")
    count = service.emit_due_checkins(now_str)
    if count:
        logger.info("Emitted %s check-in events", count)
    return count
