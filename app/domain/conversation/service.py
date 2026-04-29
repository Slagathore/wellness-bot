"""
Conversation domain service.

Responsible for orchestrating message handling, calling LLM, and emitting replies.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

from app.core.events import event_bus
from app.domain import events
from app.domain.conversation.pipeline import generate_response, generate_response_async
from app.domain.turns.models import TurnPlan
from app.domain.turns.planner import TurnPlanner
from app.domain.turns.audit import (
    append_turn_route,
    build_route_entry,
    create_turn_audit,
)
from app.infra.llm.client import LLMClient
from app.monitoring_latency import record_message_timing

logger = logging.getLogger(__name__)

_GENERIC_FALLBACK = "I'm having a little trouble right now — give me a moment and try again."

# Strip media generation tags before persisting — the adapter handles execution;
# the tag should not appear in conversation history the character can react to.
_MEDIA_TAG_RE = re.compile(
    r"\[(?:GENERATE_IMAGE|GENERATE_VIDEO):[^\]]*\]",
    re.IGNORECASE | re.DOTALL,
)


def _fallback_error_text(exc: Exception) -> str:
    """Return a user-facing error message with a hint about what went wrong."""
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return "My response took too long and timed out. Try sending a shorter message, or try again in a moment."
    if "connection" in msg or "refused" in msg or "unreachable" in msg:
        return "I can't reach my language model right now — the server may be restarting. Please try again in a minute."
    if "out of memory" in msg or "oom" in msg or "cuda" in msg:
        return "The AI model ran out of memory. The admin may need to free up resources. Please try again shortly."
    if "model" in msg and ("not found" in msg or "does not exist" in msg):
        return "The AI model I need isn't available right now. The admin may need to pull or load it."
    return _GENERIC_FALLBACK


@dataclass(slots=True)
class UserMessage:
    """Envelope describing the inbound Telegram user message."""

    user_id: str
    text: str
    chat_id: int | None = None
    correlation_id: str | None = None
    db_user_id: int | None = None
    route_trace: list[dict[str, Any]] | None = None


@dataclass(slots=True)
class ProcessedReply:
    text: str
    status: str
    error: str | None
    db_user_id: int | None
    session_id: int | None
    rag_ms: float | None
    llm_ms: float | None
    lexical_ms: float | None
    memory_ms: float | None
    memory_mode: str | None
    total_ms: float
    persist_ms: float | None
    summary_needed: bool
    live_search_mode: str | None
    turn_plan: TurnPlan | None
    user_message_id: int | None
    assistant_message_id: int | None
    audit_id: int | None
    route_trace: list[dict[str, Any]]


class ConversationRepository(Protocol):
    """Abstraction for persisting conversation history."""

    def append(
        self, message: UserMessage, reply: str | None = None
    ) -> dict[str, object] | None:  # pragma: no cover - interface
        ...

    def get_session_id(self, db_user_id: int) -> int:  # pragma: no cover - interface
        ...


class ConversationService:
    """Conversation orchestrator."""

    def __init__(
        self,
        repo: ConversationRepository,
        llm: LLMClient,
        *,
        response_filter: Callable[[str], str] | None = None,
        response_generator: Callable[[UserMessage], object] | None = None,
        turn_planner: TurnPlanner | None = None,
    ) -> None:
        self._repo = repo
        self._llm = llm
        self._filter = response_filter
        self._response_generator = response_generator
        self._turn_planner = turn_planner

    def handle_user_message(self, msg: UserMessage) -> None:
        """Legacy sync process path; emits a send-reply event."""

        result = self._generate_and_record_sync(msg)
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
            correlation_id=msg.correlation_id,
        )
        if result.audit_id is not None:
            append_turn_route(
                audit_id=result.audit_id,
                stage="conversation.service.send_reply_published",
                chat_id=msg.chat_id,
                mode="sync",
                status="reply_dispatched",
            )
        event_bus.publish(
            events.EVENT_TURN_FOLLOWUP,
            {
                "user_id": result.db_user_id or msg.user_id,
                "session_id": result.session_id,
                "chat_id": msg.chat_id,
                "correlation_id": msg.correlation_id,
                "user_text": msg.text,
                "assistant_text": result.text,
                "user_message_id": result.user_message_id,
                "assistant_message_id": result.assistant_message_id,
                "turn_plan": result.turn_plan.to_dict() if result.turn_plan else None,
                "audit_id": result.audit_id,
                "live_search_mode": result.live_search_mode,
            },
            correlation_id=msg.correlation_id,
        )
        if result.audit_id is not None:
            append_turn_route(
                audit_id=result.audit_id,
                stage="conversation.service.turn_followup_published",
                mode="sync",
            )

    async def handle_user_message_async(self, msg: UserMessage) -> None:
        """Async process path; emits a send-reply event."""

        result = await self._generate_and_record_async(msg)
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
            correlation_id=msg.correlation_id,
        )
        if result.audit_id is not None:
            append_turn_route(
                audit_id=result.audit_id,
                stage="conversation.service.send_reply_published",
                chat_id=msg.chat_id,
                mode="async",
                status="reply_dispatched",
            )
        event_bus.publish(
            events.EVENT_TURN_FOLLOWUP,
            {
                "user_id": result.db_user_id or msg.user_id,
                "session_id": result.session_id,
                "chat_id": msg.chat_id,
                "correlation_id": msg.correlation_id,
                "user_text": msg.text,
                "assistant_text": result.text,
                "user_message_id": result.user_message_id,
                "assistant_message_id": result.assistant_message_id,
                "turn_plan": result.turn_plan.to_dict() if result.turn_plan else None,
                "audit_id": result.audit_id,
                "live_search_mode": result.live_search_mode,
            },
            correlation_id=msg.correlation_id,
        )
        if result.audit_id is not None:
            append_turn_route(
                audit_id=result.audit_id,
                stage="conversation.service.turn_followup_published",
                mode="async",
            )

    async def process_user_message_async(
        self,
        msg: UserMessage,
        *,
        record_timing_now: bool = False,
    ) -> ProcessedReply:
        """Async process path that returns reply metadata without sending."""

        result = await self._generate_and_record_async(msg)
        if record_timing_now:
            self._record_timing(msg, result)
        return result

    def _generate_and_record_sync(self, msg: UserMessage) -> ProcessedReply:
        started = time.perf_counter()
        generation_error: str | None = None
        session_id: int | None = None
        db_user_id: int | None = None
        turn_plan: TurnPlan | None = None
        route_trace: list[dict[str, Any]] = list(msg.route_trace or [])
        raw: object
        route_trace.append(build_route_entry("conversation.service.started", mode="sync"))

        try:
            if self._response_generator:
                raw = self._response_generator(msg)
            else:
                db_user_id = msg.db_user_id or int(msg.user_id)
                session_id = self._repo.get_session_id(db_user_id)
                route_trace.append(
                    build_route_entry(
                        "conversation.service.session_resolved",
                        db_user_id=db_user_id,
                        session_id=session_id,
                    )
                )
                if self._turn_planner:
                    try:
                        turn_plan = self._turn_planner.build_plan(
                            user_id=db_user_id,
                            session_id=session_id,
                            message_text=msg.text,
                        )
                        route_trace.append(
                            build_route_entry(
                                "conversation.turn_planner.completed",
                                primary_intent=turn_plan.primary_intent,
                                sentiment_priority=turn_plan.sentiment_priority,
                                needs_rag=turn_plan.needs_rag,
                                needs_live_search_now=turn_plan.needs_live_search_now,
                            )
                        )
                        if turn_plan.clarification_required and turn_plan.clarification_text:
                            route_trace.append(
                                build_route_entry(
                                    "conversation.turn_planner.clarification_short_circuit"
                                )
                            )
                            raw = {"text": turn_plan.clarification_text}
                        else:
                            raw = generate_response(
                                user_id=db_user_id,
                                session_id=session_id,
                                user_text=msg.text,
                                turn_plan=turn_plan,
                            )
                    except Exception:
                        raw = generate_response(
                            user_id=db_user_id,
                            session_id=session_id,
                            user_text=msg.text,
                        )
                else:
                    raw = generate_response(
                        user_id=db_user_id,
                        session_id=session_id,
                        user_text=msg.text,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("Conversation generation failed (sync) for user %s: %s", msg.user_id, exc, exc_info=True)
            generation_error = str(exc)
            raw = {"text": _fallback_error_text(exc)}
            route_trace.append(
                build_route_entry(
                    "conversation.service.generation_failed",
                    error=str(exc),
                )
            )

        result = self._normalize_result(
            msg=msg,
            raw=raw,
            generation_error=generation_error,
            started=started,
            db_user_id=db_user_id,
            session_id=session_id,
            turn_plan=turn_plan,
            route_trace=route_trace,
        )
        self._record_timing(msg, result)
        return result

    async def _generate_and_record_async(self, msg: UserMessage) -> ProcessedReply:
        started = time.perf_counter()
        generation_error: str | None = None
        session_id: int | None = None
        db_user_id: int | None = None
        turn_plan: TurnPlan | None = None
        route_trace: list[dict[str, Any]] = list(msg.route_trace or [])
        raw: object
        route_trace.append(build_route_entry("conversation.service.started", mode="async"))

        try:
            if self._response_generator:
                raw = self._response_generator(msg)
            else:
                db_user_id = msg.db_user_id or int(msg.user_id)
                session_id = self._repo.get_session_id(db_user_id)
                route_trace.append(
                    build_route_entry(
                        "conversation.service.session_resolved",
                        db_user_id=db_user_id,
                        session_id=session_id,
                    )
                )
                if self._turn_planner:
                    try:
                        turn_plan = self._turn_planner.build_plan(
                            user_id=db_user_id,
                            session_id=session_id,
                            message_text=msg.text,
                        )
                        route_trace.append(
                            build_route_entry(
                                "conversation.turn_planner.completed",
                                primary_intent=turn_plan.primary_intent,
                                sentiment_priority=turn_plan.sentiment_priority,
                                needs_rag=turn_plan.needs_rag,
                                needs_live_search_now=turn_plan.needs_live_search_now,
                            )
                        )
                        if turn_plan.clarification_required and turn_plan.clarification_text:
                            route_trace.append(
                                build_route_entry(
                                    "conversation.turn_planner.clarification_short_circuit"
                                )
                            )
                            raw = {"text": turn_plan.clarification_text}
                        else:
                            raw = await generate_response_async(
                                user_id=db_user_id,
                                session_id=session_id,
                                user_text=msg.text,
                                turn_plan=turn_plan,
                            )
                    except Exception:
                        raw = await generate_response_async(
                            user_id=db_user_id,
                            session_id=session_id,
                            user_text=msg.text,
                        )
                else:
                    raw = await generate_response_async(
                        user_id=db_user_id,
                        session_id=session_id,
                        user_text=msg.text,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("Conversation generation failed (async) for user %s: %s", msg.user_id, exc, exc_info=True)
            generation_error = str(exc)
            raw = {"text": _fallback_error_text(exc)}
            route_trace.append(
                build_route_entry(
                    "conversation.service.generation_failed",
                    error=str(exc),
                )
            )

        return self._normalize_result(
            msg=msg,
            raw=raw,
            generation_error=generation_error,
            started=started,
            db_user_id=db_user_id,
            session_id=session_id,
            turn_plan=turn_plan,
            route_trace=route_trace,
        )

    def _normalize_result(
        self,
        *,
        msg: UserMessage,
        raw: object,
        generation_error: str | None,
        started: float,
        db_user_id: int | None,
        session_id: int | None,
        turn_plan: TurnPlan | None,
        route_trace: list[dict[str, Any]],
    ) -> ProcessedReply:
        rag_ms: float | None = None
        llm_ms: float | None = None
        lexical_ms: float | None = None
        memory_ms: float | None = None
        memory_mode: str | None = None
        total_ms: float | None = None
        summary_needed = False
        live_search_mode: str | None = None

        if isinstance(raw, dict):
            timing = raw.get("_timing") if isinstance(raw.get("_timing"), dict) else {}
            if isinstance(timing, dict):
                rag_ms = _as_float(timing.get("rag_ms"))
                llm_ms = _as_float(timing.get("llm_ms"))
                lexical_ms = _as_float(timing.get("lexical_ms"))
                memory_ms = _as_float(timing.get("memory_ms"))
                memory_mode = (
                    str(timing.get("memory_mode")) if timing.get("memory_mode") else None
                )
                total_ms = _as_float(timing.get("total_ms"))
                summary_needed = bool(timing.get("summary_needed"))
                live_search_mode = (
                    str(timing.get("live_search_mode"))
                    if timing.get("live_search_mode")
                    else None
                )
            if raw.get("_session_id") is not None:
                try:
                    session_id = int(raw["_session_id"])
                except Exception:
                    pass
            summary_needed = summary_needed or bool(raw.get("_summary_needed"))
            if raw.get("_turn_plan") and turn_plan is None:
                try:
                    from app.domain.turns.models import TurnPlan as _TurnPlan

                    turn_plan = _TurnPlan.from_dict(raw["_turn_plan"])
                except Exception:
                    pass
            routing = raw.get("_routing_trace")
            if isinstance(routing, list):
                route_trace.extend(dict(item) for item in routing if isinstance(item, dict))

        reply = self._normalize_response(raw)
        if self._filter:
            reply = self._filter(reply)

        persist_ms: float | None = None
        persist_result: dict[str, object] | None = None
        reminder_stage = "conversation.auto_followup.skipped"
        persist_started = time.perf_counter()
        try:
            persist_result = self._repo.append(msg, _MEDIA_TAG_RE.sub("", reply).strip())
            persist_ms = round((time.perf_counter() - persist_started) * 1000, 1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Conversation persistence failed: %s", exc)
            route_trace.append(
                build_route_entry("conversation.persistence.failed", error=str(exc))
            )
        else:
            user_message_id = _as_int((persist_result or {}).get("user_message_id"))
            persisted_session_id = _as_int((persist_result or {}).get("session_id"))
            if persisted_session_id is not None:
                session_id = persisted_session_id
            route_trace.append(
                build_route_entry(
                    "conversation.persistence.completed",
                    session_id=session_id,
                    user_message_id=user_message_id,
                    assistant_message_id=_as_int((persist_result or {}).get("assistant_message_id")),
                    persist_ms=persist_ms,
                )
            )
            effective_db_user_id = (
                db_user_id if db_user_id is not None else msg.db_user_id
            )
            timestamp_value = (persist_result or {}).get("timestamp")
            normalized_timestamp: str | datetime | None = None
            if timestamp_value is None or isinstance(timestamp_value, (str, datetime)):
                normalized_timestamp = timestamp_value
            else:
                isoformat = getattr(timestamp_value, "isoformat", None)
                if not callable(isoformat):
                    isoformat = None
                if isoformat is not None:
                    try:
                        normalized_timestamp = str(isoformat())
                    except Exception:
                        normalized_timestamp = None
            try:
                from app.domain.reminders.auto_followups import (
                    maybe_create_followup_for_message,
                )

                if (
                    user_message_id is not None
                    and session_id is not None
                    and effective_db_user_id is not None
                    and (turn_plan is None or turn_plan.allow_reminder_action)
                ):
                    maybe_create_followup_for_message(
                        user_id=effective_db_user_id,
                        session_id=session_id,
                        message_id=user_message_id,
                        text=msg.text,
                        message_timestamp=normalized_timestamp,
                    )
                    reminder_stage = "conversation.auto_followup.checked"
            except Exception as exc:  # noqa: BLE001
                logger.debug("Live auto-followup creation skipped: %s", exc)
                reminder_stage = "conversation.auto_followup.failed"
                route_trace.append(
                    build_route_entry(reminder_stage, error=str(exc))
                )
            else:
                route_trace.append(build_route_entry(reminder_stage))

        if total_ms is None:
            total_ms = round((time.perf_counter() - started) * 1000, 1)

        status = "ok" if generation_error is None else "fallback_error"
        audit_id: int | None = None
        effective_user_id = db_user_id or msg.db_user_id
        if effective_user_id is not None:
            try:
                audit_id = create_turn_audit(
                    user_id=int(effective_user_id),
                    session_id=session_id,
                    user_message_id=_as_int((persist_result or {}).get("user_message_id")),
                    assistant_message_id=_as_int((persist_result or {}).get("assistant_message_id")),
                    correlation_id=msg.correlation_id,
                    user_text=msg.text,
                    assistant_text=reply,
                    plan=turn_plan,
                    route_trace=route_trace,
                    status="reply_ready" if generation_error is None else "fallback_error",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Turn audit creation failed: %s", exc)
        return ProcessedReply(
            text=reply,
            status=status,
            error=generation_error,
            db_user_id=db_user_id or msg.db_user_id,
            session_id=session_id,
            rag_ms=rag_ms,
            llm_ms=llm_ms,
            lexical_ms=lexical_ms,
            memory_ms=memory_ms,
            memory_mode=memory_mode,
            total_ms=float(total_ms),
            persist_ms=persist_ms,
            summary_needed=summary_needed,
            live_search_mode=live_search_mode,
            turn_plan=turn_plan,
            user_message_id=_as_int((persist_result or {}).get("user_message_id")),
            assistant_message_id=_as_int((persist_result or {}).get("assistant_message_id")),
            audit_id=audit_id,
            route_trace=route_trace,
        )

    def _record_timing(self, msg: UserMessage, result: ProcessedReply) -> None:
        try:
            record_message_timing(
                user_id=result.db_user_id,
                session_id=result.session_id,
                correlation_id=msg.correlation_id,
                rag_ms=result.rag_ms,
                llm_ms=result.llm_ms,
                total_ms=result.total_ms,
                status=result.status,
                error=result.error,
                lexical_ms=result.lexical_ms,
                memory_ms=result.memory_ms,
                memory_mode=result.memory_mode,
                persist_ms=result.persist_ms,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to persist message timing: %s", exc)

    @staticmethod
    def _normalize_response(response: object) -> str:
        """Handle multiple possible response formats from LLM adapters."""

        response_text: str | None = None
        if isinstance(response, dict):
            response_text = (
                response.get("text")
                or response.get("message", {}).get("content")
                or response.get("content")
                or response.get("response")
            )
        elif isinstance(response, str):
            response_text = response
        else:
            response_text = str(response)

        response_text = (response_text or "").strip()
        if not response_text:
            response_text = "[No response received from LLM]"
        return response_text


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
