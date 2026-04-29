"""Work Focus monitor service."""

from __future__ import annotations

import logging
import threading

from datetime import datetime, timezone

from app.db import db_ro
from app.monitoring import WORKER_ERRORS
from app.utils.time_utils import operator_now

logger = logging.getLogger(__name__)


class WorkfocusService:
    """Background monitor that checks Work Focus mode inactivity."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.thread: threading.Thread | None = None
        self.running = False

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="workfocus-monitor",
        )
        self.thread.start()
        logger.info("[WorkFocus] Monitor started")

    def stop(self) -> None:
        self.running = False

    def join(self) -> None:
        self.bot._join_thread(self.thread, "workfocus")
        self.thread = None

    def _loop(self) -> None:
        try:
            while self.running and not self.bot.shutdown_event.is_set():
                try:
                    with db_ro() as conn:
                        workfocus_users = conn.execute(
                            """SELECT u.id, u.telegram_user_id, u.display_name,
                                  MAX(m.timestamp) as last_message
                               FROM users u
                               LEFT JOIN messages m ON u.id = m.user_id AND m.role = 'user'
                               WHERE u.personality = 'workfocus'
                               GROUP BY u.id""",
                        ).fetchall()
                    now = operator_now()
                    for user in workfocus_users:
                        last_message = user["last_message"]
                        if not last_message:
                            continue
                        try:
                            last_dt = datetime.fromisoformat(str(last_message))
                        except ValueError:
                            continue
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        last_dt = last_dt.astimezone(now.tzinfo)
                        minutes_idle = (now - last_dt).total_seconds() / 60
                        if minutes_idle >= 10:
                            self.bot._send_workfocus_checkin(
                                user["telegram_user_id"], user["id"]
                            )
                    if self.bot.shutdown_event.wait(timeout=60):
                        break
                except Exception as exc:  # noqa: BLE001
                    WORKER_ERRORS.labels(component="workfocus_monitor").inc()
                    logger.error("[WorkFocus] Monitor error: %s", exc, exc_info=True)
                    if self.bot.shutdown_event.wait(timeout=30):
                        break
        finally:
            self.running = False
            self.thread = None
            logger.info("[WorkFocus] Monitor stopped")
