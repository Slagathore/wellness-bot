"""
Offline catch-up manager.

Tracks downtime windows and, on the next startup, batches all missed user
messages per user/chat, then asks the LLM to craft a single combined response
that acknowledges what the user said, addresses overdue reminders/check-ins,
and responds meaningfully to the content of the messages.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Protocol, Tuple

from app.config import settings
from app.core.events import event_bus
from app.domain import events
from app.db import db_ro

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OfflineCatchupManager:
    """Coordinates offline-window detection and catch-up messaging."""

    def __init__(self, llm: "_ChatClient", sessions: "_UserSessionResolver") -> None:
        self._llm = llm
        self._sessions = sessions
        cfg = settings()
        self._state_path = Path(cfg.data_root) / "state" / "last_online.json"
        self._state_path.parent.mkdir(parents=True, exist_ok=True)

        now = _utc_now()
        last_online = self._read_last_online() or now
        self.offline_start = last_online
        self.offline_end = now
        self.started_at = now
        self.active = (self.offline_end - self.offline_start) > timedelta(seconds=1)
        self._missed_messages: dict[Tuple[int, int], List[Dict[str, Any]]] = (
            defaultdict(list)
        )
        self._user_info: dict[Tuple[int, int], Dict[str, str | None]] = {}
        self._sent: set[Tuple[int, int]] = set()
        self._last_offline_note_at: datetime | None = None
        self._min_idle_before_flush_seconds = 8.0
        self._max_wait_before_flush_seconds = 90.0

        # Persist immediately so next boot has a bounded window even if we exit early.
        self._write_last_online(self.offline_end)
        if self.active:
            logger.info(
                "Offline window detected: %s -> %s",
                self.offline_start.isoformat(),
                self.offline_end.isoformat(),
            )

    def heartbeat(self) -> None:
        """Update the last-online timestamp.  Call periodically while running."""
        self._write_last_online(_utc_now())

    def note_incoming_message(
        self,
        tg_user_id: int,
        chat_id: int,
        text: str,
        msg_ts: datetime,
        *,
        username: str | None = None,
        name: str | None = None,
    ) -> bool:
        """Record a message that arrived during the offline window."""
        if not self.active:
            return False
        ts = msg_ts if msg_ts.tzinfo else msg_ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        if not (self.offline_start <= ts < self.offline_end):
            return False
        key = (tg_user_id, chat_id)
        self._missed_messages[key].append({"text": text, "timestamp": ts.isoformat()})
        if key not in self._user_info:
            self._user_info[key] = {"username": username, "name": name}
        self._last_offline_note_at = _utc_now()
        return True

    def send_catchup_if_needed(
        self,
        tg_user_id: int,
        chat_id: int,
        username: str | None = None,
        name: str | None = None,
    ) -> bool:
        """Generate and send a single catch-up message per chat.

        All missed messages from the same user/chat are batched together and
        sent to the LLM so it can craft a meaningful, combined response.
        """
        if not self.active:
            return False
        if not chat_id:
            return False
        key = (tg_user_id, chat_id)
        if key in self._sent:
            return False

        # Mark sent FIRST to prevent duplicate processing from subsequent
        # messages arriving while we generate the response.
        self._sent.add(key)

        db_user_id = self._sessions.ensure_user(
            tg_user_id, username=username, name=name
        )
        context = self._build_context(db_user_id, tg_user_id, chat_id)
        if (
            not context["missed_messages"]
            and not context["overdue_reminders"]
            and not context["overdue_checkins"]
        ):
            return False

        # Try LLM-generated response; fall back to template if LLM fails
        text = self._generate_llm_response(context)
        if not text:
            text = self._build_fallback_text(context)
        if not text:
            return False

        event_bus.publish(
            events.EVENT_SEND_REPLY,
            {"user_id": str(tg_user_id), "chat_id": chat_id, "text": text},
        )
        return True

    def flush_all_catchups(self) -> int:
        """Send catch-up messages for ALL accumulated user/chat pairs.

        Called once at startup after a delay, so all queued offline messages
        have been noted.  Returns the number of catch-ups actually sent.
        """
        if not self.active:
            return 0
        if not self.ready_to_flush():
            return 0
        sent_count = 0
        for key in list(self._missed_messages.keys()):
            if key in self._sent:
                continue
            tg_user_id, chat_id = key
            info = self._user_info.get(key, {})
            ok = self.send_catchup_if_needed(
                tg_user_id,
                chat_id,
                username=info.get("username"),
                name=info.get("name"),
            )
            if ok:
                sent_count += 1
        # Deactivate offline processing after flush — all subsequent messages
        # should go through the normal pipeline.
        self.active = False
        logger.info("Offline catchup flush complete: %d messages sent", sent_count)
        return sent_count

    def ready_to_flush(
        self,
        *,
        min_idle_seconds: float | None = None,
        max_wait_seconds: float | None = None,
    ) -> bool:
        if not self.active:
            return False

        now = _utc_now()
        idle_threshold = (
            self._min_idle_before_flush_seconds
            if min_idle_seconds is None
            else float(min_idle_seconds)
        )
        max_wait = (
            self._max_wait_before_flush_seconds
            if max_wait_seconds is None
            else float(max_wait_seconds)
        )

        if (now - self.started_at).total_seconds() >= max_wait:
            return True

        if not self._missed_messages:
            return False
        if self._last_offline_note_at is None:
            return False
        return (now - self._last_offline_note_at).total_seconds() >= idle_threshold

    # Internal helpers -----------------------------------------------------
    def _read_last_online(self) -> datetime | None:
        if not self._state_path.exists():
            return None
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            val = data.get("last_online_at")
            if val:
                return datetime.fromisoformat(val)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to read last_online state: %s", exc)
        return None

    def _write_last_online(self, when: datetime) -> None:
        try:
            self._state_path.write_text(
                json.dumps({"last_online_at": when.isoformat()}), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to write last_online state: %s", exc)

    def _build_context(
        self, db_user_id: int, tg_user_id: int, chat_id: int
    ) -> Dict[str, Any]:
        key = (tg_user_id, chat_id)
        missed = self._missed_messages.get(key, [])
        reminders = self._overdue_reminders(db_user_id)
        checkins = self._overdue_checkins(db_user_id)
        return {
            "offline_window": {
                "start": self.offline_start.isoformat(),
                "end": self.offline_end.isoformat(),
            },
            "missed_messages": missed,
            "overdue_reminders": reminders,
            "overdue_checkins": checkins,
        }

    def _overdue_reminders(self, db_user_id: int) -> List[Dict[str, Any]]:
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT id, kind, payload, next_run_at
                FROM reminders
                WHERE user_id = ? AND enabled = 1
                  AND SUBSTR(next_run_at, 1, 19) >= SUBSTR(?, 1, 19)
                  AND SUBSTR(next_run_at, 1, 19) < SUBSTR(?, 1, 19)
                ORDER BY next_run_at ASC
                """,
                (
                    db_user_id,
                    self.offline_start.isoformat(),
                    self.offline_end.isoformat(),
                ),
            ).fetchall()
        overdue: List[Dict[str, Any]] = []
        for row in rows:
            text = ""
            if row["payload"]:
                try:
                    payload = json.loads(row["payload"])
                    text = (
                        payload.get("text")
                        or payload.get("reminder_text")
                        or payload.get("label")
                        or payload.get("title")
                        or ""
                    )
                except Exception:
                    text = ""
            if not text:
                text = row["kind"] or "Reminder"
            overdue.append(
                {"id": row["id"], "text": text, "scheduled_at": row["next_run_at"]}
            )
        return overdue

    def _overdue_checkins(self, db_user_id: int) -> List[Dict[str, Any]]:
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT personalized_prompt, next_checkin_at, frequency
                FROM checkin_configs
                WHERE user_id = ?
                  AND is_active = 1
                  AND next_checkin_at >= ?
                  AND next_checkin_at < ?
                ORDER BY next_checkin_at ASC
                """,
                (
                    db_user_id,
                    self.offline_start.isoformat(),
                    self.offline_end.isoformat(),
                ),
            ).fetchall()
        return [
            {
                "prompt": row["personalized_prompt"] or "",
                "scheduled_at": row["next_checkin_at"],
                "frequency": row["frequency"] or "",
            }
            for row in rows
        ]

    def _generate_llm_response(self, context: Dict[str, Any]) -> str:
        """Use the LLM to generate a meaningful response to caught-up messages."""
        try:
            prompt = self._build_prompt(context)
            response = self._llm.chat(prompt)
            text = self._extract_text(response).strip()
            if text:
                logger.info("Generated LLM catch-up response (%d chars)", len(text))
                return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM catch-up generation failed, using fallback: %s", exc)
        return ""

    def _build_fallback_text(self, context: Dict[str, Any]) -> str:
        offline_minutes = int(
            max(
                0,
                (
                    datetime.fromisoformat(context["offline_window"]["end"])
                    - datetime.fromisoformat(context["offline_window"]["start"])
                ).total_seconds()
                // 60,
            )
        )
        missed_count = len(context["missed_messages"])
        reminder_count = len(context["overdue_reminders"])
        checkin_count = len(context["overdue_checkins"])

        parts: list[str] = []
        parts.append(
            f"Sorry for the delay while I was offline for about {offline_minutes} minute(s)."
        )
        if missed_count:
            # Include a summary of what the user actually said
            msg_previews = []
            for msg in context["missed_messages"][:5]:
                txt = msg.get("text", "").strip()
                if txt:
                    preview = txt[:80] + ("..." if len(txt) > 80 else "")
                    msg_previews.append(f'  - "{preview}"')
            if msg_previews:
                parts.append(
                    f"I saw {missed_count} message(s) from you while I was away:"
                )
                parts.append("\n".join(msg_previews))
            else:
                parts.append(
                    f"I saw {missed_count} message(s) from you while I was away."
                )
        if reminder_count:
            top = ", ".join(
                r.get("text", "Reminder")
                for r in context["overdue_reminders"][:2]
                if isinstance(r, dict)
            )
            if top:
                parts.append(
                    f"You have {reminder_count} overdue reminder(s) ({top})."
                )
            else:
                parts.append(f"You have {reminder_count} overdue reminder(s).")
        if checkin_count:
            parts.append(f"You have {checkin_count} overdue check-in(s).")
        parts.append(
            "I'm back now and ready to help! Let me know what you'd like to talk about."
        )
        return "\n".join(parts).strip()

    def _build_prompt(self, context: Dict[str, Any]) -> list[dict[str, str]]:
        """Construct a structured prompt to let the LLM author the catch-up note."""
        offline_minutes = int(
            max(
                0,
                (
                    datetime.fromisoformat(context["offline_window"]["end"])
                    - datetime.fromisoformat(context["offline_window"]["start"])
                ).total_seconds()
                // 60,
            )
        )

        # Format missed messages with their content for the LLM
        missed_lines = []
        for msg in context["missed_messages"]:
            txt = msg.get("text", "").strip()
            if txt:
                missed_lines.append(f'  User said: "{txt}"')
        missed_section = "\n".join(missed_lines) if missed_lines else "  (none)"

        # Format reminders
        reminder_lines = []
        for r in context["overdue_reminders"]:
            reminder_lines.append(f'  - {r.get("text", "Reminder")}')
        reminder_section = (
            "\n".join(reminder_lines) if reminder_lines else "  (none)"
        )

        # Format check-ins
        checkin_lines = []
        for c in context["overdue_checkins"]:
            checkin_lines.append(f'  - {c.get("prompt", "Check-in")}')
        checkin_section = (
            "\n".join(checkin_lines) if checkin_lines else "  (none)"
        )

        system = (
            "You are a caring wellness companion catching up after being offline. "
            "Write ONE combined message that:\n"
            "1. Briefly acknowledges you were away (don't over-apologize)\n"
            "2. RESPONDS to each user message meaningfully — address what they said, "
            "answer questions, react to their feelings/news\n"
            "3. Mentions any overdue reminders naturally (don't just list them)\n"
            "4. Ends warmly and invites continued conversation\n\n"
            "Keep it natural and conversational. Don't use bullet points or numbered lists. "
            "Don't say 'I was offline' — say something warmer like 'Hey! Sorry I missed your messages.' "
            "The most important thing is to actually respond to what the user SAID, not just "
            "acknowledge that messages exist."
        )
        user = (
            f"I was offline for about {offline_minutes} minutes. "
            f"Here's what I missed:\n\n"
            f"Messages from the user:\n{missed_section}\n\n"
            f"Overdue reminders:\n{reminder_section}\n\n"
            f"Overdue check-ins:\n{checkin_section}\n\n"
            "Write a single warm, combined response that addresses everything above."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _extract_text(response: Any) -> str:
        if isinstance(response, dict):
            return (
                response.get("text")
                or response.get("message", {}).get("content")
                or response.get("content")
                or response.get("response")
                or ""
            )
        if isinstance(response, str):
            return response
        return str(response)


class _ChatClient(Protocol):
    def chat(self, prompt: Any) -> Any:  # pragma: no cover - typing contract only
        ...


class _UserSessionResolver(Protocol):
    def ensure_user(
        self, tg_user_id: int, username: str | None = None, name: str | None = None
    ) -> int:  # pragma: no cover - typing contract only
        ...
