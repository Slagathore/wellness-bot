"""Event handler to run safety checks on conversation messages."""

from __future__ import annotations

import logging

from app.core.events import event_bus
from app.domain import events
from app.domain.safety.service import SafetyService
from app.monitoring_tracing import start_span

logger = logging.getLogger(__name__)


class SafetyEventHandler:
    def __init__(self, service: SafetyService) -> None:
        self._service = service

    async def handle(self, event) -> None:
        with start_span("safety.handle"):
            payload = event.payload
            user_id = payload.get("user_id")
            text = payload.get("text") or ""
            chat_id = payload.get("chat_id")
            if user_id is None:
                return
            try:
                self._service.inspect_message(
                    user_id=user_id, chat_id=chat_id, text=text
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Safety handler error: %s", exc)


def register_safety_handler(handler: SafetyEventHandler) -> None:
    event_bus.subscribe(events.EVENT_CONVERSATION_MESSAGE, handler.handle, mode="async")
