"""
Event-driven conversation handler that ties together ConversationService and UserSessionStore.
"""

from __future__ import annotations

import logging

from app.core.events import event_bus
from app.domain import events
from app.domain.conversation.service import ConversationService, UserMessage
from app.domain.metrics.decorators import message_metrics
from app.domain.safety.filter import SafetyFilter
from app.domain.turns.audit import append_turn_route, build_route_entry
from app.domain.turns.audit import create_turn_audit
from app.monitoring_tracing import start_span
from app.orchestrator.context_builder import schedule_session_summary
from app.runtime.services.user_sessions import UserSessionStore

logger = logging.getLogger(__name__)


class ConversationEventHandler:
    def __init__(
        self,
        service: ConversationService,
        sessions: UserSessionStore,
        safety: SafetyFilter | None = None,
    ) -> None:
        self._service = service
        self._sessions = sessions
        self._safety = safety or SafetyFilter()

    @message_metrics("telegram")
    async def handle(self, event) -> None:
        with start_span("conversation.handle"):
            payload = event.payload
            tg_user_id = payload.get("user_id")
            if tg_user_id is None:
                logger.warning("Conversation payload missing user_id; dropping event")
                return
            msg = UserMessage(
                user_id=str(tg_user_id),
                text=str(payload.get("text") or ""),
                chat_id=payload.get("chat_id"),
                correlation_id=event.correlation_id,
                route_trace=[
                    build_route_entry(
                        "conversation.handler.received",
                        chat_id=payload.get("chat_id"),
                    )
                ],
            )
            # Safety/rate-limit gate
            try:
                if not self._safety.allow(int(msg.user_id), msg.text):
                    # Optional: reply with a gentle throttle message if chat_id available
                    audit_id: int | None = None
                    try:
                        audit_id = create_turn_audit(
                            user_id=int(payload.get("db_user_id") or int(msg.user_id)),
                            session_id=None,
                            user_message_id=None,
                            assistant_message_id=None,
                            correlation_id=event.correlation_id,
                            user_text=msg.text,
                            assistant_text="Please slow down; I'm processing your recent messages.",
                            plan=None,
                            route_trace=list(msg.route_trace or []) + [
                                build_route_entry("conversation.handler.safety_throttled")
                            ],
                            status="throttled",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Safety throttle audit creation failed: %s", exc)
                    if payload.get("chat_id"):
                        event_bus.publish(
                            events.EVENT_SEND_REPLY,
                            {
                                "user_id": msg.user_id,
                                "chat_id": payload.get("chat_id"),
                                "text": "Please slow down; I'm processing your recent messages.",
                                "audit_id": audit_id,
                            },
                            correlation_id=event.correlation_id,
                        )
                        if audit_id is not None:
                            append_turn_route(
                                audit_id=audit_id,
                                stage="conversation.handler.safety_reply_published",
                                status="reply_dispatched",
                            )
                    return
                if msg.route_trace is not None:
                    msg.route_trace.append(
                        build_route_entry("conversation.handler.safety_passed")
                    )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Safety filter error: %s", exc)

            # Ensure user/session persistence
            try:
                uid = payload.get("db_user_id")
                if uid is None:
                    uid = self._sessions.ensure_user(
                        int(msg.user_id),
                        username=payload.get("username"),
                        name=payload.get("first_name"),
                    )
                msg.db_user_id = int(uid)
                if msg.route_trace is not None:
                    msg.route_trace.append(
                        build_route_entry(
                            "conversation.handler.user_resolved",
                            db_user_id=msg.db_user_id,
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Conversation persistence failed: %s", exc)
            result = await self._service.process_user_message_async(
                msg, record_timing_now=True
            )
            if result.summary_needed and result.session_id:
                schedule_session_summary(result.session_id)
            event_bus.publish(
                events.EVENT_SEND_REPLY,
                {
                    "user_id": msg.user_id,
                    "chat_id": msg.chat_id,
                    "text": result.text,
                    "turn_plan": result.turn_plan.to_dict() if result.turn_plan else None,
                    "user_message_id": result.user_message_id,
                    "assistant_message_id": result.assistant_message_id,
                    "audit_id": result.audit_id,
                    "live_search_mode": result.live_search_mode,
                },
                correlation_id=event.correlation_id,
            )
            if result.audit_id is not None:
                append_turn_route(
                    audit_id=result.audit_id,
                    stage="conversation.handler.send_reply_published",
                    chat_id=msg.chat_id,
                    status="reply_dispatched",
                )
            event_bus.publish(
                events.EVENT_TURN_FOLLOWUP,
                {
                    "user_id": msg.db_user_id or msg.user_id,
                    "session_id": result.session_id,
                    "chat_id": msg.chat_id,
                    "correlation_id": event.correlation_id,
                    "user_text": msg.text,
                    "assistant_text": result.text,
                    "user_message_id": result.user_message_id,
                    "assistant_message_id": result.assistant_message_id,
                    "turn_plan": result.turn_plan.to_dict() if result.turn_plan else None,
                    "audit_id": result.audit_id,
                    "live_search_mode": result.live_search_mode,
                },
                correlation_id=event.correlation_id,
            )
            if result.audit_id is not None:
                append_turn_route(
                    audit_id=result.audit_id,
                    stage="conversation.handler.turn_followup_published",
                )


def register_conversation_handler(handler: ConversationEventHandler) -> None:
    event_bus.subscribe(events.EVENT_CONVERSATION_MESSAGE, handler.handle, mode="async")
