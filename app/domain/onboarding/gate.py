"""
Onboarding gate that intercepts user messages and routes to onboarding flow until completed.
"""

from __future__ import annotations

import logging

from app.core.events import event_bus
from app.domain import events
from app.domain.metrics.decorators import message_metrics
from app.domain.onboarding.service import OnboardingService
from app.domain.turns.audit import append_turn_route, build_route_entry, create_turn_audit
from app.runtime.services.user_sessions import UserSessionStore
from app.db import db_ro

logger = logging.getLogger(__name__)


class OnboardingGate:
    def __init__(
        self, onboarding: OnboardingService, sessions: UserSessionStore
    ) -> None:
        self._onboarding = onboarding
        self._sessions = sessions

    @message_metrics("telegram")
    async def handle(self, event) -> None:
        payload = event.payload
        tg_id = payload.get("user_id")
        if tg_id is None:
            return

        username = payload.get("username")
        first_name = payload.get("first_name")
        chat_id = payload.get("chat_id")
        text = payload.get("text") or ""

        try:
            db_user_id = self._sessions.ensure_user(
                int(tg_id), username=username, name=first_name
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to ensure user for onboarding: %s", exc)
            return

        # Check onboarding status
        try:
            with db_ro() as conn:
                row = conn.execute(
                    "SELECT onboarding_completed FROM users WHERE id = ?",
                    (db_user_id,),
                ).fetchone()
                completed = bool(row["onboarding_completed"]) if row else False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read onboarding status: %s", exc)
            completed = True  # fail open to avoid blocking

        if not completed:
            reply = self._onboarding.handle_message(int(tg_id), db_user_id, text)
            audit_id: int | None = None
            try:
                audit_id = create_turn_audit(
                    user_id=db_user_id,
                    session_id=None,
                    user_message_id=None,
                    assistant_message_id=None,
                    correlation_id=event.correlation_id,
                    user_text=str(text),
                    assistant_text=str(reply or ""),
                    plan=None,
                    route_trace=[
                        build_route_entry("onboarding.gate.received", chat_id=chat_id),
                        build_route_entry("onboarding.gate.user_resolved", db_user_id=db_user_id),
                        build_route_entry("onboarding.gate.onboarding_required"),
                    ],
                    status="onboarding_reply" if reply else "onboarding_consumed",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Onboarding audit creation failed: %s", exc)
            if reply and chat_id:
                event_bus.publish(
                    events.EVENT_SEND_REPLY,
                    {"user_id": tg_id, "chat_id": chat_id, "text": reply, "audit_id": audit_id},
                    correlation_id=event.correlation_id,
                )
                if audit_id is not None:
                    append_turn_route(
                        audit_id=audit_id,
                        stage="onboarding.gate.send_reply_published",
                        status="reply_dispatched",
                    )
            return

        # Already onboarded: forward to conversation handler directly
        event_bus.publish(
            events.EVENT_CONVERSATION_MESSAGE,
            {**payload, "db_user_id": db_user_id},
            correlation_id=event.correlation_id,
        )


def register_onboarding_gate(gate: OnboardingGate) -> None:
    event_bus.subscribe(events.EVENT_USER_MESSAGE, gate.handle, mode="async")
