"""
Wire up container registrations, event handlers, and scheduler jobs for the modular runtime.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.utils.time_utils import operator_now

from app.core.container import container
from app.core.events import event_bus
from app.core.scheduler import Scheduler
from app.domain import events
from app.domain.conversation.service import ConversationService
from app.domain.conversation.handler import (
    ConversationEventHandler,
    register_conversation_handler,
)
from app.domain.reminders.service import ReminderService
from app.domain.reminders.dispatcher import ReminderDispatcher
from app.domain.workfocus.service import WorkFocusService
from app.domain.onboarding.service import OnboardingService
from app.domain.onboarding.gate import OnboardingGate, register_onboarding_gate
from app.domain.safety.filter import SafetyFilter
from app.domain.safety.service import SafetyService
from app.domain.safety.handler import SafetyEventHandler, register_safety_handler
from app.infra.db.moderation_repo import ModerationRepository
from app.infra.db.reminders_repo import SqliteReminderRepository
from app.infra.db.conversation_repo import SqliteConversationRepository
from app.infra.db.checkins_repo import SqliteCheckinsRepository
from app.infra.llm.client import default_llm_client
from app.infra.vector.client import default_vector_client
from app.runtime.services.user_sessions import UserSessionStore
from app.runtime.catchup import OfflineCatchupManager
from app.personality.manager import PersonalityManager
from app.runtime.services.preferences import PreferenceService
from app.domain.turns.llm_analyzer import LLMTurnAnalyzer
from app.domain.turns.planner import TurnPlanner
from app.domain.turns.followups import TurnFollowupService
from app.domain.turns.audit import append_turn_route

logger = logging.getLogger(__name__)


def register_defaults() -> None:
    """Register baseline providers for LLM/vector/repos/services."""

    if "llm_client" not in container:
        container.register("llm_client", default_llm_client, singleton=True)
    if "vector_client" not in container:
        container.register("vector_client", default_vector_client, singleton=True)
    if "reminder_repo" not in container:
        container.register("reminder_repo", SqliteReminderRepository, singleton=True)
    if "conversation_repo" not in container:
        container.register(
            "conversation_repo", SqliteConversationRepository, singleton=True
        )
    if "checkins_repo" not in container:
        container.register("checkins_repo", SqliteCheckinsRepository, singleton=True)
    if "moderation_repo" not in container:
        container.register("moderation_repo", ModerationRepository, singleton=True)
    if "user_session_store" not in container:
        cfg = container.resolve("config")
        container.register(
            "user_session_store",
            lambda: UserSessionStore(
                data_root=cfg.data_root, ctx_token_budget=cfg.ctx_token_budget
            ),
            singleton=True,
        )
    if "personality_manager" not in container:
        cfg = container.resolve("config")
        container.register(
            "personality_manager",
            lambda: PersonalityManager(
                config_path=Path(cfg.data_root) / "config.json",
                db_path=cfg.database_path,
            ),
            singleton=True,
        )
    if "preference_service" not in container:
        container.register("preference_service", PreferenceService, singleton=True)
    if "onboarding_service" not in container:
        container.register("onboarding_service", OnboardingService, singleton=True)
    if "reminder_service" not in container:
        container.register(
            "reminder_service",
            lambda: ReminderService(container.resolve("reminder_repo")),
            singleton=True,
        )
    if "turn_planner" not in container:
        container.register(
            "turn_planner",
            lambda: TurnPlanner(analyzer=container.resolve("llm_turn_analyzer")),
            singleton=True,
        )
    if "llm_turn_analyzer" not in container:
        container.register("llm_turn_analyzer", LLMTurnAnalyzer, singleton=True)
    if "turn_followup_service" not in container:
        container.register("turn_followup_service", TurnFollowupService, singleton=True)
    if "conversation_service" not in container:
        container.register(
            "conversation_service",
            lambda: ConversationService(
                container.resolve("conversation_repo"),
                container.resolve("llm_client"),
                response_filter=_strip_admin_commands,
                turn_planner=container.resolve("turn_planner"),
            ),
            singleton=True,
        )
    if "catchup_manager" not in container:
        container.register(
            "catchup_manager",
            lambda: _build_catchup_manager(
                container.resolve("llm_client"), container.resolve("user_session_store")
            ),
            singleton=True,
        )
    if "conversation_handler" not in container:
        container.register(
            "conversation_handler",
            lambda: ConversationEventHandler(
                container.resolve("conversation_service"),
                container.resolve("user_session_store"),
                container.resolve("safety_filter"),
            ),
            singleton=True,
        )
    if "onboarding_gate" not in container:
        container.register(
            "onboarding_gate",
            lambda: OnboardingGate(
                container.resolve("onboarding_service"),
                container.resolve("user_session_store"),
            ),
            singleton=True,
        )
    if "reminder_dispatcher" not in container:
        container.register(
            "reminder_dispatcher",
            lambda: ReminderDispatcher(
                container.resolve("reminder_service"),
                container.resolve("llm_client"),
                container.resolve("user_session_store"),
            ),
            singleton=True,
        )
    if "workfocus_service" not in container:
        container.register(
            "workfocus_service",
            lambda: WorkFocusService(
                container.resolve("checkins_repo"), container.resolve("llm_client")
            ),
            singleton=True,
        )
    if "safety_filter" not in container:
        container.register("safety_filter", SafetyFilter, singleton=True)
    if "safety_service" not in container:
        container.register(
            "safety_service",
            lambda: SafetyService(container.resolve("moderation_repo")),
            singleton=True,
        )
    if "safety_handler" not in container:
        container.register(
            "safety_handler",
            lambda: SafetyEventHandler(container.resolve("safety_service")),
            singleton=True,
        )


def register_event_handlers() -> None:
    """Bridge bus events to domain services."""
    conv_handler = container.resolve("conversation_handler")
    _reminder_service = container.resolve("reminder_service")
    dispatcher = container.resolve("reminder_dispatcher")
    _workfocus_service = container.resolve("workfocus_service")
    onboarding_gate = container.resolve("onboarding_gate")
    safety_handler = container.resolve("safety_handler")
    turn_followup_service = container.resolve("turn_followup_service")

    async def _handle_reminder_due(event) -> None:
        dispatcher.handle_due(event.payload)

    async def _handle_checkin_due(event) -> None:
        payload = event.payload
        chat_id = payload.get("chat_id")
        text = payload.get("text") or "Work focus check-in"
        if chat_id:
            event_bus.publish(
                events.EVENT_SEND_REPLY,
                {"user_id": payload.get("user_id"), "chat_id": chat_id, "text": text},
                correlation_id=event.correlation_id,
            )

    register_onboarding_gate(onboarding_gate)
    register_safety_handler(safety_handler)
    register_conversation_handler(conv_handler)
    event_bus.subscribe(events.EVENT_REMINDER_DUE, _handle_reminder_due, mode="async")
    event_bus.subscribe(events.EVENT_CHECKIN_DUE, _handle_checkin_due, mode="async")

    async def _handle_turn_followup(event) -> None:
        result = await turn_followup_service.handle_followup_async(event.payload)
        if result.get("search_followup_sent") and event.payload.get("chat_id"):
            audit_id = event.payload.get("audit_id")
            event_bus.publish(
                events.EVENT_SEND_REPLY,
                {
                    "user_id": event.payload.get("user_id"),
                    "chat_id": event.payload.get("chat_id"),
                    "text": result.get("followup_message_text", result.get("search_followup_text", "")),
                    "audit_id": audit_id,
                },
                correlation_id=event.correlation_id,
            )
            if isinstance(audit_id, int):
                append_turn_route(
                    audit_id=audit_id,
                    stage="runtime.turn_followup.search_reply_published",
                )

    event_bus.subscribe(events.EVENT_TURN_FOLLOWUP, _handle_turn_followup, mode="async")


def _strip_admin_commands(text: str) -> str:
    """
    Remove lines starting with '!' to avoid leaking control commands in replies.

    Mirrors legacy admin console safeguard; safe to use for user replies as well.
    """
    kept = []
    for line in text.splitlines():
        if line.strip().startswith("!"):
            continue
        kept.append(line)
    filtered = "\n".join(kept).strip()
    return filtered or text


def register_jobs(scheduler: Scheduler) -> None:
    """Register periodic jobs (reminder due scan)."""
    reminder_service = container.resolve("reminder_service")

    def _scan_due() -> None:
        from app.monitoring_tracing import start_span

        with start_span("reminders.scan"):
            count = reminder_service.process_due(now=operator_now())
            if count:
                logger.info("Emitted %s due reminders", count)

    scheduler.add_interval_job(
        _scan_due, seconds=60, id="reminders.scan_due", max_instances=1
    )


def _build_catchup_manager(llm_client, session_store) -> OfflineCatchupManager:
    """Factory wrapper to keep the container registration small."""
    return OfflineCatchupManager(llm=llm_client, sessions=session_store)
