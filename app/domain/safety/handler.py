"""Event handlers for the safety subsystem.

- :class:`SafetyEventHandler` inspects conversation messages, and on a detected
  crisis publishes a crisis-resource reply to the user.
- :class:`CrisisAlertHandler` subscribes to ``EVENT_CRISIS_DETECTED`` and emits a
  real-time, structured WARNING. This is the escalation hook that was previously
  missing entirely (the event had zero subscribers) — swap the log for a pager /
  operator DM / webhook when one is available.
"""

from __future__ import annotations

import logging

from app.core.events import event_bus
from app.domain import events
from app.domain.safety.resources import CRISIS_RESOURCE_MESSAGE
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
                crisis = self._service.inspect_message(
                    user_id=user_id, chat_id=chat_id, text=text
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Safety handler error: %s", exc)
                return

            if crisis and chat_id:
                event_bus.publish(
                    events.EVENT_SEND_REPLY,
                    {
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "text": CRISIS_RESOURCE_MESSAGE,
                    },
                    correlation_id=event.correlation_id,
                )


class CrisisAlertHandler:
    """Real-time escalation hook for detected crises."""

    async def handle(self, event) -> None:
        payload = event.payload or {}
        details = payload.get("details") or {}
        logger.warning(
            "CRISIS DETECTED user_id=%s chat_id=%s severity=%s scope=%s source=%s",
            payload.get("user_id"),
            payload.get("chat_id"),
            payload.get("severity"),
            details.get("scope"),
            details.get("source"),
        )


def register_safety_handler(handler: SafetyEventHandler) -> None:
    event_bus.subscribe(events.EVENT_CONVERSATION_MESSAGE, handler.handle, mode="async")


def register_crisis_alert_handler(handler: CrisisAlertHandler) -> None:
    event_bus.subscribe(events.EVENT_CRISIS_DETECTED, handler.handle, mode="async")
