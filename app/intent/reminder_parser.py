from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Dict, Optional

from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime.interfaces import UnifiedWellnessBot


@dataclass
class _TaskMeta:
    """Metadata tracked for each scheduled reminder parse."""

    chat_id: int
    user_id: int
    session_id: int
    context: ContextTypes.DEFAULT_TYPE
    username: str
    message_preview: str
    message_timestamp: datetime
    consumed: bool = False


class ReminderIntentScheduler:
    """Run reminder intent parsing without blocking message handling."""

    def __init__(self, bot: "UnifiedWellnessBot") -> None:
        self._bot = bot
        self._pending: Dict[asyncio.Task, _TaskMeta] = {}
        self._lock = threading.Lock()
        self._logger = logging.getLogger(f"{__name__}.ReminderIntentScheduler")
        self._timeout_seconds = 30.0
        self._max_pending_tasks = 50
        self._max_consecutive_errors = 5
        self._consecutive_errors = 0
        self._disabled = False

    def schedule(
        self,
        *,
        update_user,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        chat_id: int,
        session_id: int,
        message_text: str,
        message_timestamp: datetime,
    ) -> Optional[asyncio.Task]:
        """Schedule reminder intent parsing in the background.

        Returns the asyncio Task so callers can optionally inspect the result.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._logger.warning("Reminder scheduling skipped: no running loop")
            return None

        with self._lock:
            if self._disabled:
                self._logger.warning(
                    "Async reminder parsing disabled after repeated failures; falling back to synchronous handling."
                )
                return None
            pending_count = len(self._pending)
            if pending_count >= self._max_pending_tasks:
                self._logger.warning(
                    "Too many pending reminder intent tasks (%s >= %s); skipping new async parse.",
                    pending_count,
                    self._max_pending_tasks,
                )
                return None

        task = loop.create_task(
            self._run_parse(
                user_id=user_id,
                message_text=message_text,
                reference_time=message_timestamp,
            )
        )
        username = (
            getattr(update_user, "username", None)
            or getattr(update_user, "first_name", None)
            or "user"
        )
        preview = (
            (message_text[:60] + "...") if len(message_text) > 60 else message_text
        )

        meta = _TaskMeta(
            chat_id=chat_id,
            user_id=user_id,
            session_id=session_id,
            context=context,
            username=username,
            message_preview=preview,
            message_timestamp=message_timestamp,
        )

        with self._lock:
            self._pending[task] = meta

        task.add_done_callback(self._on_task_done)
        return task

    def mark_consumed(self, task: asyncio.Task) -> None:
        """Mark the task result as consumed by the main message handler."""
        with self._lock:
            meta = self._pending.get(task)
            if meta:
                meta.consumed = True

    async def _run_parse(
        self, *, user_id: int, message_text: str, reference_time: datetime
    ) -> Optional[dict]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._bot._parse_reminder_intent,
                    user_id,
                    message_text,
                    reference_time,
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._logger.error(
                "Reminder intent parsing timed out after %.1f seconds",
                self._timeout_seconds,
            )
            raise

    def _on_task_done(self, task: asyncio.Task) -> None:
        with self._lock:
            meta = self._pending.get(task)

        if not meta:
            return

        try:
            result = task.result()
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.error("Reminder intent parsing failed: %s", exc, exc_info=True)
            self._handle_failure(task)
            return

        if not result:
            with self._lock:
                self._pending.pop(task, None)
                self._consecutive_errors = 0
            return
        if meta.consumed:
            with self._lock:
                self._pending.pop(task, None)
                self._consecutive_errors = 0
            return

        with self._lock:
            self._consecutive_errors = 0

        loop = asyncio.get_running_loop()
        follow_up_task = loop.create_task(self._send_follow_up(task, meta, result))
        # Hold a strong reference so the GC cannot collect the task before it
        # finishes and runs the finally-block that removes the entry from _pending.
        with self._lock:
            self._pending[follow_up_task] = meta

    async def _send_follow_up(
        self, task: asyncio.Task, meta: _TaskMeta, result: dict
    ) -> None:
        try:
            if meta.consumed:
                return

            message = self._format_follow_up(result)
            if not message:
                return

            await meta.context.bot.send_message(chat_id=meta.chat_id, text=message)
            self._logger.info(
                "Sent reminder follow-up to %s (%s)",
                meta.username,
                meta.message_preview,
            )
        except Exception as exc:  # pragma: no cover - network failure guard
            self._logger.error(
                "Failed to send reminder follow-up: %s", exc, exc_info=True
            )
        finally:
            with self._lock:
                self._pending.pop(task, None)

    def _handle_failure(self, task: asyncio.Task) -> None:
        disabled_triggered = False
        with self._lock:
            self._pending.pop(task, None)
            self._consecutive_errors += 1
            error_count = self._consecutive_errors
            if error_count >= self._max_consecutive_errors and not self._disabled:
                self._disabled = True
                disabled_triggered = True

        if disabled_triggered:
            self._logger.error(
                "Disabling async reminder parsing after %d consecutive failures.",
                error_count,
            )
            try:
                self._bot.log(
                    "Async reminder parsing disabled after repeated failures; falling back to synchronous handling."
                )
            except Exception:
                pass
        else:
            self._logger.warning(
                "Reminder intent parsing failed (%d/%d consecutive failures).",
                error_count,
                self._max_consecutive_errors,
            )

    def _format_follow_up(self, result: dict) -> Optional[str]:
        created = result.get("created")
        needs_clarification = result.get("needs_clarification")

        if created == "reminder":
            reminder_text = result.get("text") or "your reminder"
            frequency = result.get("frequency")
            next_time = result.get("next_time")

            if needs_clarification:
                return (
                    f"I started setting up a reminder about '{reminder_text}', "
                    "but I need a bit more detail on the timing."
                )

            parts = [f"Reminder saved for '{reminder_text}'."]
            if frequency:
                parts.append(f"Schedule: {frequency}.")
            if next_time:
                parts.append(f"Next run (CST): {next_time}.")
            return " ".join(parts)

        if created == "checkin":
            frequency = result.get("frequency")
            if needs_clarification:
                return (
                    "I'm ready to set those check-ins - let me know what time of day "
                    "you want them."
                )

            if frequency:
                return f"Check-ins configured for a {frequency} cadence."
            return "Check-ins are all set."

        return None
