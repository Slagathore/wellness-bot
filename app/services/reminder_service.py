"""Reminder and outbox background services."""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

import requests

from app.db import db_ro, db_rw
from app.monitoring import WORKER_ERRORS
from app.utils.time_utils import get_current_time

logger = logging.getLogger(__name__)


class ReminderService:
    """Runs reminder scheduling and outbox delivery loops on background threads."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.scheduler_thread: threading.Thread | None = None
        self.outbox_thread: threading.Thread | None = None
        self.scheduler_running = False
        self.outbox_running = False

    # Public API -------------------------------------------------------------

    def start(self) -> None:
        """Start reminder scheduler and outbox sender threads."""

        if not self.scheduler_running:
            self.scheduler_running = True
            self.scheduler_thread = threading.Thread(
                target=self._scheduler_loop,
                daemon=True,
                name="reminder-scheduler",
            )
            self.scheduler_thread.start()

        if not self.outbox_running or not (
            self.outbox_thread and self.outbox_thread.is_alive()
        ):
            self.outbox_running = True
            self.outbox_thread = threading.Thread(
                target=self._outbox_loop,
                daemon=True,
                name="outbox-sender",
            )
            self.outbox_thread.start()
            self.bot.log("📤 Outbox sender thread started")

        self.bot.log("⏰ Reminder scheduler started")

    def stop(self) -> None:
        """Signal both threads to stop."""

        self.scheduler_running = False
        self.outbox_running = False

    def join(self) -> None:
        """Wait for background threads to exit."""

        self.bot._join_thread(self.scheduler_thread, "reminder", timeout=30.0)
        self.scheduler_thread = None
        self.bot._join_thread(self.outbox_thread, "outbox")
        self.outbox_thread = None

    # Internal loops --------------------------------------------------------

    def _scheduler_loop(self) -> None:
        """Background loop to check and send due reminders/check-ins."""

        try:
            while self.scheduler_running and not self.bot.shutdown_event.is_set():
                if self.bot.shutdown_event.is_set():
                    return
                self._process_due_reminders()
                self._process_due_checkins()
                self._process_inactive_users()
                if self.bot.shutdown_event.wait(timeout=60):
                    break
        finally:
            self.scheduler_running = False
            self.scheduler_thread = None
            self.bot.log("🛑 Reminder scheduler stopped")

    def _process_due_reminders(self) -> None:
        now_str = get_current_time().strftime("%Y-%m-%d %H:%M:00")
        try:
            with db_ro() as conn:
                due_reminders = conn.execute(
                    """SELECT r.id, r.user_id, r.kind, r.payload, u.telegram_user_id
                    FROM reminders r
                    JOIN users u ON r.user_id = u.id
                    WHERE r.enabled = 1 AND r.next_run_at <= ?""",
                    (now_str,),
                ).fetchall()
            for reminder in due_reminders:
                if self.bot.shutdown_event.is_set():
                    return
                payload = self.bot._safe_json_loads(
                    reminder["payload"], {}, context="reminder payload (scheduler)"
                )
                self.bot._send_reminder(
                    reminder["telegram_user_id"],
                    payload.get("text", "Reminder"),
                    reminder["id"],
                    payload.get("frequency", "once"),
                    payload,
                )
        except Exception as exc:  # noqa: BLE001
            WORKER_ERRORS.labels(component="reminder_scheduler").inc()
            if "no such table" not in str(exc) and "no such column" not in str(exc):
                logger.error("Reminder check error: %s", exc, exc_info=True)

    def _process_due_checkins(self) -> None:
        now_str = get_current_time().strftime("%Y-%m-%d %H:%M:00")
        try:
            with db_ro() as conn:
                due_checkins = conn.execute(
                    """SELECT c.user_id, c.personalized_prompt, c.frequency, u.telegram_user_id
                    FROM checkin_configs c
                    JOIN users u ON c.user_id = u.id
                    WHERE c.is_active = 1 AND c.next_checkin_at <= ?""",
                    (now_str,),
                ).fetchall()
            for checkin in due_checkins:
                if self.bot.shutdown_event.is_set():
                    return
                self.bot._send_checkin(
                    checkin["telegram_user_id"],
                    checkin["user_id"],
                    checkin["personalized_prompt"],
                    checkin["frequency"],
                )
        except Exception as exc:  # noqa: BLE001
            WORKER_ERRORS.labels(component="reminder_scheduler").inc()
            if "no such table" not in str(exc) and "no such column" not in str(exc):
                logger.error("Check-in check error: %s", exc, exc_info=True)

    def _process_inactive_users(self) -> None:
        three_days_ago = (get_current_time() - timedelta(days=3)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            with db_ro() as conn:
                inactive_users = conn.execute(
                    """SELECT DISTINCT u.id, u.telegram_user_id, u.display_name,
                          MAX(m.timestamp) as last_message_time
                       FROM users u
                       LEFT JOIN messages m ON u.id = m.user_id AND m.role = 'user'
                       WHERE u.onboarding_completed = 1
                       GROUP BY u.id
                       HAVING last_message_time < ? OR last_message_time IS NULL""",
                    (three_days_ago,),
                ).fetchall()
            for user in inactive_users:
                if self.bot.shutdown_event.is_set():
                    return
                with db_ro() as conn:
                    recent_wellness_check = conn.execute(
                        """SELECT 1 FROM reminders
                           WHERE user_id = ?
                           AND kind = 'wellness_check'
                           AND last_delivered_at > ?
                           LIMIT 1""",
                        (user["id"], three_days_ago),
                    ).fetchone()
                if not recent_wellness_check:
                    self.bot._send_wellness_check(user["telegram_user_id"], user["id"])
        except Exception as exc:  # noqa: BLE001
            WORKER_ERRORS.labels(component="reminder_scheduler").inc()
            if "no such table" not in str(exc) and "no such column" not in str(exc):
                logger.error("Inactive user check error: %s", exc, exc_info=True)

    def _outbox_loop(self) -> None:
        """Background loop to send queued broadcast messages."""

        bot_token = self.bot.cfg.telegram_bot_token
        endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"

        logger.info("[OutboxSender] Starting outbox sender loop...")

        try:
            while self.outbox_running and not self.bot.shutdown_event.is_set():
                try:
                    with db_ro() as conn:
                        row = conn.execute(
                            """SELECT id, user_id, chat_id, message_text
                               FROM telegram_outbox
                               WHERE sent = 0
                               ORDER BY id ASC
                               LIMIT 1""",
                        ).fetchone()

                    if not row:
                        if self.bot.shutdown_event.wait(timeout=2):
                            break
                        continue

                    try:
                        response = requests.post(
                            endpoint,
                            json={
                                "chat_id": row["chat_id"],
                                "text": row["message_text"],
                            },
                            timeout=30,
                        )

                        if response.ok:
                            message_id = (
                                response.json().get("result", {}).get("message_id")
                            )
                            with db_rw() as conn:
                                conn.execute(
                                    """UPDATE telegram_outbox
                                       SET sent = 1,
                                           sent_at = datetime('now'),
                                           telegram_message_id = ?
                                       WHERE id = ?""",
                                    (message_id, row["id"]),
                                )
                            logger.info(
                                "[OutboxSender] Sent message %s to chat %s",
                                row["id"],
                                row["chat_id"],
                            )
                        else:
                            logger.error(
                                "[OutboxSender] Failed to send message %s: %s - %s",
                                row["id"],
                                response.status_code,
                                response.text,
                            )
                            if self.bot.shutdown_event.wait(timeout=5):
                                break

                    except requests.exceptions.RequestException as exc:  # noqa: BLE001
                        WORKER_ERRORS.labels(component="outbox_sender").inc()
                        logger.error(
                            "[OutboxSender] Request error sending message %s: %s",
                            row["id"],
                            exc,
                        )
                        if self.bot.shutdown_event.wait(timeout=5):
                            break
                except Exception as exc:  # noqa: BLE001
                    WORKER_ERRORS.labels(component="outbox_sender").inc()
                    logger.error("[OutboxSender] Loop error: %s", exc, exc_info=True)
                    if self.bot.shutdown_event.wait(timeout=5):
                        break
        finally:
            self.outbox_running = False
            self.outbox_thread = None
            logger.info("[OutboxSender] Outbox sender loop stopped")
