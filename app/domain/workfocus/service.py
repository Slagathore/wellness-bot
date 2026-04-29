"""
WorkFocus/check-in domain service skeleton.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Protocol

from app.core.events import event_bus
from app.domain import events
from app.infra.llm.client import LLMClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkFocusCheckin:
    user_id: str
    telegram_user_id: int | None
    prompt: str
    frequency: str


class CheckinRepository(Protocol):
    def due_checkins(
        self, now_str: str
    ) -> Iterable[WorkFocusCheckin]:  # pragma: no cover - interface
        ...


class WorkFocusService:
    def __init__(self, repo: CheckinRepository, llm: LLMClient):
        self._repo = repo
        self._llm = llm

    def emit_due_checkins(self, now_str: str) -> int:
        count = 0
        for checkin in self._repo.due_checkins(now_str):
            msg = self._generate_message(checkin.prompt)
            event_bus.publish(
                events.EVENT_CHECKIN_DUE,
                {
                    "user_id": checkin.user_id,
                    "chat_id": checkin.telegram_user_id,
                    "text": msg,
                    "frequency": checkin.frequency,
                },
            )
            count += 1
        return count

    def _generate_message(self, prompt: str) -> str:
        system_prompt = (
            "Generate a SHORT, motivating check-in message for a user in Work Focus mode who hasn't messaged in 10+ minutes. "
            "Be brief (1-2 sentences), encouraging but firm, ask what they're working on now, use accountability language, support ADHD users, include a relevant emoji."
        )
        resp = self._llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        )
        if isinstance(resp, dict):
            return (
                resp.get("text")
                or resp.get("content")
                or resp.get("response")
                or "💼 Hey! What are you focusing on right now?"
            )
        if isinstance(resp, str):
            return resp
        return "💼 Hey! What are you focusing on right now?"
