"""
Reminder dispatcher: consumes reminder.due payloads, generates personalized text,
publishes send-reply events, marks reminders sent, and schedules next run.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any

from croniter import croniter

from app.core.events import event_bus
from app.domain import events
from app.domain.reminders.service import ReminderService
from app.infra.llm.client import LLMClient
from app.monitoring import MESSAGE_LATENCY, MESSAGE_TOTAL
from app.monitoring_tracing import start_span
from app.runtime.services.user_sessions import UserSessionStore

logger = logging.getLogger(__name__)


class ReminderDispatcher:
    def __init__(
        self,
        reminders: ReminderService,
        llm: LLMClient,
        sessions: UserSessionStore,
    ) -> None:
        self._reminders = reminders
        self._llm = llm
        self._sessions = sessions

    def handle_due(self, payload: dict[str, Any]) -> None:
        with start_span("reminder.dispatch"):
            chat_id = payload.get("chat_id")
            user_id = payload.get("user_id")
            reminder_id = payload.get("reminder_id")
            text = payload.get("text") or "Reminder"
            metadata = payload.get("metadata") or {}
            merged_members = (
                metadata.get("merged_reminders")
                if isinstance(metadata.get("merged_reminders"), list)
                else []
            )
            if chat_id is None:
                logger.warning(
                    "[REMINDER-TELEMETRY] missing_chat reminder_id=%s user=%s text=%r",
                    reminder_id,
                    user_id,
                    text[:120],
                )
                # Still mark sent to prevent infinite retry loop
                self._mark_members_sent(
                    reminder_id=reminder_id,
                    metadata=metadata,
                    user_id=user_id,
                )
                return

            start = time.perf_counter()
            # Generate personalized message; fall back to plain text if LLM fails
            # so that mark_sent_and_schedule_next ALWAYS executes.
            try:
                message = self._generate_message(text, metadata, user_id=user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LLM failed for reminder %s; using fallback text: %s",
                    reminder_id, exc,
                )
                message = f"\U0001f514 Reminder: {text}"

            event_bus.publish(
                events.EVENT_SEND_REPLY,
                {"user_id": user_id, "chat_id": chat_id, "text": message},
                correlation_id=payload.get("correlation_id"),
            )
            logger.info(
                "[REMINDER-TELEMETRY] dispatched reminder_id=%s user_id=%s chat_id=%s merged=%s next_run=%s text_len=%d",
                reminder_id,
                user_id,
                chat_id,
                len(merged_members) if isinstance(merged_members, list) and merged_members else 1,
                next_run_at.isoformat()
                if (next_run_at := self._compute_next_run(metadata, user_id=user_id))
                else None,
                len(message or ""),
            )
            duration = time.perf_counter() - start
            try:
                MESSAGE_TOTAL.labels(platform="telegram", direction="outbound").inc()
                MESSAGE_LATENCY.labels(platform="telegram").observe(duration)
            except Exception:  # pragma: no cover - metrics optional
                pass

        # Save to conversation history if possible
        self._persist_message(chat_id, user_id, message)

        # ALWAYS mark sent and compute next occurrence — prevents infinite
        # retry loops where a stuck-due reminder re-fires every scanner cycle.
        self._mark_members_sent(
            reminder_id=reminder_id,
            metadata=metadata,
            user_id=user_id,
        )

    def _generate_message(
        self, reminder_text: str, metadata: dict[str, Any], *, user_id: str | int | None = None
    ) -> str:
        from app.config import settings as get_settings
        from app.orchestrator.context_builder import user_profile_context

        worker_model = get_settings().worker_model
        time_of_day = metadata.get("time_of_day")
        recent_context = self._recent_context(user_id, scope=metadata.get("scope"))
        origin_context = self._origin_context(metadata)
        semantic_context = self._semantic_context(
            reminder_text=reminder_text,
            metadata=metadata,
            user_id=user_id,
        )
        profile_context = ""
        try:
            if user_id is not None:
                profile_context = (user_profile_context(int(user_id)) or "").strip()[:700]
        except Exception:
            profile_context = ""
        system_prompt = (
            "You are Mira, a wellness companion. Generate a friendly, brief reminder (2-3 sentences). "
            "Be warm, personal, and natural. It should feel like a caring follow-up, not a hard-coded notification. "
            "Use the provided context only when it genuinely helps. Include a relevant emoji."
        )
        user_prompt = f"Reminder topic: {reminder_text}"
        if time_of_day:
            user_prompt += f"\nTime of day: {time_of_day}"
        if metadata.get("followup_kind"):
            user_prompt += f"\nReminder kind: {metadata.get('followup_kind')}"
        merged_lines = self._merged_member_lines(metadata)
        if merged_lines:
            user_prompt += f"\nMerged reminder items:\n{merged_lines}"
        if metadata.get("overdue_merge"):
            user_prompt += (
                "\nThese reminders were missed while the bot was offline or delayed. "
                "Make this feel like a caring catch-up or wellness check."
            )
        if origin_context:
            user_prompt += f"\nOrigin conversation context:\n{origin_context}"
        if recent_context:
            user_prompt += f"\nRecent context:\n{recent_context}"
        if semantic_context:
            user_prompt += f"\nSemantically relevant history:\n{semantic_context}"
        if profile_context:
            user_prompt += f"\nLow-priority user profile context:\n{profile_context}"
        resp = self._llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=worker_model,
        )
        if isinstance(resp, dict):
            text = (
                resp.get("text")
                or resp.get("message", {}).get("content")
                or resp.get("content")
                or resp.get("response")
            )
        elif isinstance(resp, str):
            text = resp
        else:
            text = str(resp)
        text = (text or "").strip()
        if not text:
            text = f"🔔 Reminder: {reminder_text}"
        # Strip admin-style command lines
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("!")]
        filtered = "\n".join(lines).strip()
        return filtered or text

    def _merged_member_lines(self, metadata: dict[str, Any]) -> str:
        members = (
            metadata.get("merged_reminders")
            if isinstance(metadata.get("merged_reminders"), list)
            else []
        )
        if not members:
            return ""
        lines: list[str] = []
        for member in members[:8]:
            if not isinstance(member, dict):
                continue
            if (
                metadata.get("overdue_merge")
                and member.get("fixed_time")
                and member.get("recurring")
            ):
                continue
            text = " ".join(str(member.get("text") or "").split())
            if not text:
                continue
            due_at = str(member.get("due_at") or "")
            clock = due_at[11:16] if len(due_at) >= 16 else ""
            lines.append(f"- {clock} {text}".strip())
        if lines:
            return "\n".join(lines)
        if metadata.get("overdue_merge"):
            return "- Several recurring reminders were missed; focus on checking in naturally rather than listing each one."
        return ""

    def _recent_context(
        self, user_id: str | int | None, *, scope: str | None = None
    ) -> str:
        if user_id is None:
            return ""
        try:
            from app.db import db_ro

            uid = int(user_id)
            with db_ro() as conn:
                rows = conn.execute(
                    """
                    SELECT role, content
                    FROM messages
                    WHERE user_id = ?
                      AND role IN ('user', 'assistant')
                      AND (? IS NULL OR scope = ?)
                    ORDER BY id DESC
                    LIMIT 4
                    """,
                    (uid, scope, scope),
                ).fetchall()
        except Exception:
            return ""

        if not rows:
            return ""
        lines: list[str] = []
        for row in reversed(rows):
            content = " ".join(str(row["content"] or "").split())
            if not content:
                continue
            lines.append(f"{row['role']}: {content[:180]}")
        return "\n".join(lines)

    def _origin_context(self, metadata: dict[str, Any]) -> str:
        origin_message_id = metadata.get("origin_message_id")
        if not origin_message_id:
            excerpt = str(metadata.get("origin_excerpt") or "").strip()
            return excerpt[:500]
        try:
            from app.db import db_ro

            with db_ro() as conn:
                origin_row = conn.execute(
                    """
                    SELECT session_id, role, content
                    FROM messages
                    WHERE id = ?
                    """,
                    (origin_message_id,),
                ).fetchone()
                if not origin_row or not origin_row["session_id"]:
                    excerpt = str(metadata.get("origin_excerpt") or "").strip()
                    return excerpt[:500]
                before = conn.execute(
                    """
                    SELECT role, content
                    FROM messages
                    WHERE session_id = ?
                      AND id < ?
                      AND role IN ('user', 'assistant')
                    ORDER BY id DESC
                    LIMIT 4
                    """,
                    (origin_row["session_id"], origin_message_id),
                ).fetchall()
                after = conn.execute(
                    """
                    SELECT role, content
                    FROM messages
                    WHERE session_id = ?
                      AND id > ?
                      AND role IN ('user', 'assistant')
                    ORDER BY id ASC
                    LIMIT 4
                    """,
                    (origin_row["session_id"], origin_message_id),
                ).fetchall()
        except Exception:
            excerpt = str(metadata.get("origin_excerpt") or "").strip()
            return excerpt[:500]

        lines: list[str] = []
        for row in reversed(before):
            content = " ".join(str(row["content"] or "").split())
            if content:
                lines.append(f"{row['role']}: {content[:180]}")
        origin_content = " ".join(str(origin_row["content"] or "").split())
        if origin_content:
            lines.append(f"origin_{origin_row['role']}: {origin_content[:220]}")
        for row in after:
            content = " ".join(str(row["content"] or "").split())
            if content:
                lines.append(f"{row['role']}: {content[:180]}")
        return "\n".join(lines)

    def _semantic_context(
        self,
        *,
        reminder_text: str,
        metadata: dict[str, Any],
        user_id: str | int | None,
    ) -> str:
        if user_id is None:
            return ""
        try:
            from app.memory import ConversationMemoryRetriever

            uid = int(user_id)
            query = reminder_text.strip()
            origin_excerpt = str(metadata.get("origin_excerpt") or "").strip()
            if origin_excerpt:
                query = f"{query}\n{origin_excerpt}".strip()
            rows = ConversationMemoryRetriever().search(
                user_id=uid,
                query=query,
                top_k=3,
                scope_filter=metadata.get("scope"),
            )
        except Exception:
            return ""
        if not rows:
            return ""
        lines: list[str] = []
        for row in rows:
            content = " ".join(str(row.get("content") or "").split())
            if not content:
                continue
            role = str(row.get("role") or "message")
            lines.append(f"{role}: {content[:180]}")
        return "\n".join(lines)

    # Human-friendly frequency strings → timedelta for _compute_next_run
    _FREQ_DELTAS: dict[str, Any] = {}  # populated lazily

    def _compute_next_run(
        self, metadata: dict[str, Any], *, user_id: str | int | None = None
    ) -> datetime | None:
        """Compute the next fire time for a recurring reminder.

        Handles both real cron expressions (``0 8 * * *``) **and**
        human-friendly frequency strings stored in the DB/metadata
        (``daily``, ``weekly``, ``hourly``).
        """
        from datetime import timedelta

        from app.domain.reminders.timezone import (
            normalize_user_local_reminder_time,
            resolve_user_sleep_window,
            user_now,
            user_time_to_operator,
        )
        from app.utils.time_utils import normalize_operator, operator_now

        freq = metadata.get("frequency")
        cron_expr = metadata.get("cadence_cron") or freq
        if not cron_expr or freq == "once":
            return None

        now = operator_now()
        user_id_int: int | None = None
        try:
            if user_id is not None and str(user_id).strip():
                user_id_int = int(user_id)
        except Exception:
            user_id_int = None

        # --- Handle human-friendly frequency names (not valid cron) ----------
        _FREQ_TO_DELTA: dict[str, timedelta] = {
            "daily": timedelta(days=1),
            "weekly": timedelta(weeks=1),
            "hourly": timedelta(hours=1),
            "every_other_day": timedelta(days=2),
        }
        delta = _FREQ_TO_DELTA.get(str(cron_expr).lower())
        if delta is not None:
            # Use the configured hour/minute from metadata when available.
            # Explicit None-check so that hour=0 (midnight) isn't skipped.
            hour = metadata.get("specific_hour")
            if hour is None:
                hour = metadata.get("base_hour")
            minute = metadata.get("specific_minute")
            if minute is None:
                minute = metadata.get("base_minute")

            if hour is not None:
                if user_id_int is not None:
                    reference_local = user_now(user_id_int)
                    candidate_local = self._candidate_local_time(
                        reference_local=reference_local,
                        metadata=metadata,
                        delta=delta,
                    )
                    candidate_local = normalize_user_local_reminder_time(
                        candidate_local,
                        reference_local=reference_local,
                        time_of_day=metadata.get("time_of_day"),
                        sleep_window=(
                            resolve_user_sleep_window(user_id_int)
                            if metadata.get("respect_sleep_window", True)
                            else None
                        ),
                        min_lead_minutes=30,
                    )
                    return user_time_to_operator(candidate_local, user_id_int)

                now_naive = now.replace(tzinfo=None)
                candidate = self._candidate_local_time(
                    reference_local=now_naive,
                    metadata=metadata,
                    delta=delta,
                )
                while candidate <= now_naive:
                    candidate += delta
                return normalize_operator(candidate)
            # No hour metadata — just offset from now.
            return now + delta

        # --- Real cron expression (e.g. "0 8 * * *") -------------------------
        try:
            itr = croniter(cron_expr, now)
            return itr.get_next(datetime)
        except Exception:
            logger.warning(
                "Unparseable cadence %r for reminder; disabling.", cron_expr,
            )
            return None

    def _candidate_local_time(
        self,
        *,
        reference_local: datetime,
        metadata: dict[str, Any],
        delta,
    ) -> datetime:
        hour = metadata.get("specific_hour")
        minute = metadata.get("specific_minute")
        if hour is None:
            hour = metadata.get("base_hour")
        if minute is None:
            minute = metadata.get("base_minute")

        allow_jitter = bool(
            metadata.get("allow_jitter")
            or metadata.get("fuzzy")
            or metadata.get("fuzz_minutes")
        )
        time_of_day = str(metadata.get("time_of_day") or "").strip().lower()
        if allow_jitter and time_of_day in {"morning", "afternoon", "evening", "night"}:
            hour, minute = {
                "morning": (9, 0),
                "afternoon": (15, 0),
                "evening": (21, 0),
                "night": (3, 0),
            }[time_of_day]
        candidate_local = reference_local.replace(
            hour=int(9 if hour is None else hour),
            minute=int(0 if minute is None else minute),
            second=0,
            microsecond=0,
        )
        while candidate_local <= reference_local:
            candidate_local += delta
        if allow_jitter:
            fuzz_minutes = metadata.get("fuzz_minutes")
            try:
                fuzz = abs(int(fuzz_minutes)) if fuzz_minutes is not None else 60
            except Exception:
                fuzz = 60
            candidate_local += timedelta(minutes=random.randint(-fuzz, fuzz))
            if candidate_local <= reference_local:
                candidate_local += delta
        return candidate_local.replace(second=0, microsecond=0)

    def _persist_message(
        self, chat_id: int | str, user_id: str | None, message: str
    ) -> None:
        try:
            uid = (
                self._sessions.get_user_id(chat_id) if user_id is None else int(user_id)
            )
            if uid is None:
                return
            session_id = self._sessions.get_or_create_session(uid)
            self._sessions.save_message(session_id, uid, "assistant", message)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to persist reminder message: %s", exc)

    def _mark_members_sent(
        self,
        *,
        reminder_id: Any,
        metadata: dict[str, Any],
        user_id: str | int | None,
    ) -> None:
        members = (
            metadata.get("merged_reminders")
            if isinstance(metadata.get("merged_reminders"), list)
            else []
        )
        if members:
            for member in members:
                if not isinstance(member, dict):
                    continue
                member_id = member.get("id")
                member_meta = (
                    member.get("metadata")
                    if isinstance(member.get("metadata"), dict)
                    else {}
                )
                if member_id is None:
                    continue
                next_run = self._compute_next_run(
                    member_meta if isinstance(member_meta, dict) else {},
                    user_id=user_id,
                )
                self._reminders.mark_sent_and_schedule_next(
                    str(member_id), next_send_time=next_run
                )
                if next_run:
                    event_bus.publish(
                        events.EVENT_REMINDER_UPDATE_NEXT,
                        {
                            "reminder_id": str(member_id),
                            "next_run_at": next_run.isoformat(),
                        },
                    )
            return
        if reminder_id:
            next_run = self._compute_next_run(metadata, user_id=user_id)
            self._reminders.mark_sent_and_schedule_next(
                str(reminder_id), next_send_time=next_run
            )
            if next_run:
                event_bus.publish(
                    events.EVENT_REMINDER_UPDATE_NEXT,
                    {
                        "reminder_id": str(reminder_id),
                        "next_run_at": next_run.isoformat(),
                    },
                )
