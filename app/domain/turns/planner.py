"""Per-turn planner that decides hot-path and follow-up actions."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from app.feature_flags import enabled as feature_enabled
from app.orchestrator.context_builder import user_profile_context
from app.orchestrator.persona_runtime import get_user_personality_name
from app.rag.service import get_retriever
from app.utils.web_search import should_search_web

from .llm_analyzer import LLMTurnAnalysis, LLMTurnAnalyzer
from .models import (
    TurnMemoryCandidate,
    TurnPlan,
    TurnProfileCandidate,
)

logger = logging.getLogger(__name__)

_REMINDER_HINTS = ("remind me", "set a reminder", "reminder")
_MEDIA_HINTS = ("generate an image", "make an image", "create an image", "draw", "generate a video", "make a video")
_PROFILE_PATTERNS = (
    (re.compile(r"\bi am ([^.!?]{2,80})", re.IGNORECASE), "self_description"),
    (re.compile(r"\bi'm ([^.!?]{2,80})", re.IGNORECASE), "self_description"),
    (re.compile(r"\bmy name is ([^.!?]{2,60})", re.IGNORECASE), "name"),
    (re.compile(r"\bi live in ([^.!?]{2,80})", re.IGNORECASE), "location"),
    (re.compile(r"\bmy birthday is ([^.!?]{2,40})", re.IGNORECASE), "birthday"),
    (re.compile(r"\bi (?:like|love) ([^.!?]{2,80})", re.IGNORECASE), "likes"),
    (re.compile(r"\bi (?:hate|dislike) ([^.!?]{2,80})", re.IGNORECASE), "dislikes"),
    (re.compile(r"\bmy goal is ([^.!?]{2,120})", re.IGNORECASE), "goal"),
)
_IMPORTANT_HINTS = (
    "important",
    "really need",
    "can't forget",
    "dont forget",
    "don't forget",
    "must",
    "deadline",
    "big day",
)
_REMINDER_EVENT_HINTS = (
    "interview",
    "appointment",
    "doctor",
    "therapy",
    "exam",
    "test",
    "surgery",
    "court",
    "funeral",
    "graduation",
    "date",
    "birthday",
    "party",
    "concert",
    "wedding",
)
_REMINDER_ROUTINE_HINTS = (
    "work",
    "shift",
    "errand",
    "errands",
    "chores",
    "homework",
    "gym",
    "class",
    "meeting",
    "trip",
    "flight",
)
_REMINDER_FUTURE_HINTS = (
    "tomorrow",
    "tonight",
    "later",
    "coming up",
    "upcoming",
    "soon",
    "next week",
    "this evening",
    "this afternoon",
    "this weekend",
    "in ",
)
_REMINDER_DISTRESS_HINTS = (
    "anxious",
    "anxiety",
    "nervous",
    "panic",
    "panicking",
    "overwhelmed",
    "stressed",
    "worried",
    "scared",
    "afraid",
    "terrified",
    "heartbroken",
    "grief",
    "breakup",
    "sick",
    "ill",
    "hurting",
    "dreading",
)
_IMMEDIATE_SEARCH_HINTS = (
    "weather",
    "forecast",
    "temperature",
    "news",
    "latest",
    "today",
    "current",
    "score",
    "stock price",
    "exchange rate",
    "time in",
)
_DEFERRED_SEARCH_HINTS = (
    "best ",
    "recommend",
    "restaurants",
    "where to eat",
    "good places",
    "top 10",
    "near me",
    "nearby",
)
_EMOTION_TERMS = {
    "anxious": "anxiety",
    "anxiety": "anxiety",
    "panic": "panic",
    "panicking": "panic",
    "overwhelmed": "overwhelmed",
    "depressed": "sadness",
    "hopeless": "sadness",
    "heartbroken": "sadness",
    "grief": "sadness",
    "sad": "sadness",
    "miserable": "sadness",
    "ashamed": "shame",
    "embarrassed": "shame",
    "stressed": "stress",
    "terrified": "fear",
    "scared": "fear",
    "afraid": "fear",
    "excited": "excitement",
    "thrilled": "excitement",
}
_SENTIMENT_PRIORITY_ORDER = {"normal": 0, "elevated": 1, "high": 2}


@dataclass(slots=True)
class PlannerInputs:
    user_id: int
    session_id: int | None
    message_text: str
    personality_name: str
    profile_context_text: str | None


class TurnPlanner:
    """Cheap heuristics for turn routing and decision gating."""

    def __init__(
        self,
        analyzer: LLMTurnAnalyzer | None = None,
        *,
        shadow_enabled: bool | None = None,
        llm_primary_enabled: bool | None = None,
    ) -> None:
        self._analyzer = analyzer
        self._shadow_enabled = shadow_enabled
        self._llm_primary_enabled = llm_primary_enabled

    def build_plan(
        self, *, user_id: int, session_id: int | None, message_text: str
    ) -> TurnPlan:
        personality = get_user_personality_name(user_id)
        profile_ctx = None
        try:
            profile_ctx = user_profile_context(user_id)
        except Exception:
            profile_ctx = None
        inputs = PlannerInputs(
            user_id=user_id,
            session_id=session_id,
            message_text=message_text,
            personality_name=personality,
            profile_context_text=profile_ctx,
        )
        return self._plan(inputs)

    async def build_plan_async(
        self, *, user_id: int, session_id: int | None, message_text: str
    ) -> TurnPlan:
        return await asyncio.to_thread(
            self.build_plan,
            user_id=user_id,
            session_id=session_id,
            message_text=message_text,
        )

    def _plan(self, inputs: PlannerInputs) -> TurnPlan:
        total_started = time.perf_counter()
        heuristic_started = time.perf_counter()
        text = (inputs.message_text or "").strip()
        lowered = text.lower()
        primary_intent = self._classify_intent(lowered)
        sentiment_priority, emotion_label = self._estimate_emotion(lowered)
        scheduled_event = self._detect_scheduled_event(lowered)
        timing_question_ok = self._timing_question_ok(
            lowered,
            primary_intent=primary_intent,
            scheduled_event=scheduled_event,
        )
        allow_media_action = self._media_allowed(lowered, inputs.personality_name)
        allow_reminder_action = self._reminder_allowed(
            lowered,
            inputs.personality_name,
            sentiment_priority=sentiment_priority,
        )
        profile_candidates = self._extract_profile_candidates(lowered, inputs.profile_context_text)
        contradictions = self._detect_contradictions(profile_candidates, inputs.profile_context_text)

        needs_rag = False
        try:
            retriever = get_retriever()
            needs_rag = bool(retriever.should_retrieve(text))
        except Exception:
            needs_rag = False

        search_decision = should_search_web(text)
        needs_live_search_now = False
        needs_live_search_followup = False
        search_query = None
        search_reason = None
        if bool(search_decision.get("needs_search")):
            search_query = str(search_decision.get("query") or text)
            search_reason = str(search_decision.get("reason") or "Live search needed")
            search_mode = self._search_mode(lowered, reason=search_reason)
            needs_live_search_now = search_mode == "now"
            needs_live_search_followup = search_mode == "followup"
        if not needs_live_search_now and not needs_live_search_followup and "weather" in lowered:
            if any(token in lowered for token in ("today", "tomorrow", "forecast", "temperature", "in ")):
                needs_live_search_now = True
                search_query = text
                search_reason = "Weather query"

        clarification_required, clarification_text = self._clarification_gate(
            lowered,
            allow_reminder_action=allow_reminder_action,
            allow_media_action=allow_media_action,
            needs_live_search_now=needs_live_search_now,
        )

        memory_candidates = self._memory_candidates(lowered, primary_intent, sentiment_priority)

        followup_jobs = []
        if needs_live_search_followup:
            followup_jobs.append("web_search")
        if profile_candidates:
            followup_jobs.append("profile_candidates")
        followup_jobs.append("audit")

        plan = TurnPlan(
            user_id=inputs.user_id,
            session_id=inputs.session_id,
            message_text=text,
            primary_intent=primary_intent,
            sentiment_priority=sentiment_priority,
            planner_latency_ms=None,
            emotion_label=emotion_label,
            scheduled_event=scheduled_event,
            timing_question_ok=timing_question_ok,
            needs_rag=needs_rag,
            needs_live_search_now=needs_live_search_now,
            needs_live_search_followup=needs_live_search_followup,
            search_query=search_query,
            search_reason=search_reason,
            allow_media_action=allow_media_action,
            media_action="generate_media" if allow_media_action else None,
            allow_reminder_action=allow_reminder_action,
            reminder_action="create_reminder" if allow_reminder_action else None,
            clarification_required=clarification_required,
            clarification_text=clarification_text,
            profile_candidates=profile_candidates,
            memory_candidates=memory_candidates,
            contradictions=contradictions,
            followup_jobs=followup_jobs,
            reasoning=[],
        )
        heuristic_latency_ms = round((time.perf_counter() - heuristic_started) * 1000.0, 2)
        heuristic_summary = self._plan_summary(plan)
        analysis, llm_latency_ms = self._llm_analysis(inputs, plan)
        if analysis is not None:
            llm_summary = self._analysis_summary(analysis)
            plan.shadow_comparison = self._build_shadow_comparison(
                heuristic_summary=heuristic_summary,
                llm_summary=llm_summary,
                heuristic_latency_ms=heuristic_latency_ms,
                llm_latency_ms=llm_latency_ms,
                llm_model=analysis.model_name,
            )
        if analysis is not None:
            if self._llm_primary_active():
                self._apply_primary_analysis(plan, analysis, inputs.personality_name)
            else:
                self._apply_shadow_analysis(plan, analysis)
        if contradictions:
            plan.reasoning.append("profile_contradiction")
        if needs_live_search_now:
            plan.reasoning.append("web_search_needed")
        if needs_live_search_followup:
            plan.reasoning.append("web_search_deferred")
        if needs_rag:
            plan.reasoning.append("rag_needed")
        plan.planner_latency_ms = round((time.perf_counter() - total_started) * 1000.0, 2)
        return plan

    def _shadow_active(self) -> bool:
        if self._shadow_enabled is not None:
            return self._shadow_enabled
        return feature_enabled("llm_turn_planner_shadow") or feature_enabled(
            "turn_planner_shadow"
        )

    def _llm_primary_active(self) -> bool:
        if self._llm_primary_enabled is not None:
            return self._llm_primary_enabled
        return feature_enabled("llm_turn_planner_v1") or feature_enabled(
            "turn_planner_llm_primary"
        )

    def _llm_analysis(
        self,
        inputs: PlannerInputs,
        heuristic_plan: TurnPlan,
    ) -> tuple[LLMTurnAnalysis | None, float | None]:
        if self._analyzer is None:
            return None, None
        if not self._shadow_active() and not self._llm_primary_active():
            return None, None
        started = time.perf_counter()
        try:
            analysis = self._analyzer.analyze(
                user_id=inputs.user_id,
                session_id=inputs.session_id,
                message_text=inputs.message_text,
                personality_name=inputs.personality_name,
                heuristic_plan=heuristic_plan.to_dict(),
                profile_context_text=inputs.profile_context_text,
            )
            latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            return analysis, latency_ms
        except Exception as exc:  # noqa: BLE001
            logger.debug("Turn planner LLM analysis failed: %s", exc)
            return None, None

    def _apply_shadow_analysis(
        self,
        plan: TurnPlan,
        analysis: LLMTurnAnalysis,
    ) -> None:
        plan.planner_source = "heuristic+llm_shadow"
        self._apply_sentiment_fields(plan, analysis)
        self._merge_reasoning(plan, analysis.reasoning, "llm_shadow_enrichment")

    def _apply_primary_analysis(
        self,
        plan: TurnPlan,
        analysis: LLMTurnAnalysis,
        personality_name: str,
    ) -> None:
        plan.planner_source = "llm_primary"
        self._apply_sentiment_fields(plan, analysis)
        if analysis.primary_intent:
            plan.primary_intent = analysis.primary_intent
        if analysis.sentiment_priority:
            plan.sentiment_priority = analysis.sentiment_priority
        if analysis.emotion_label is not None:
            plan.emotion_label = analysis.emotion_label
        plan.crisis_risk = analysis.crisis_risk
        plan.scheduled_event = analysis.scheduled_event
        plan.timing_question_ok = analysis.timing_question_ok
        if analysis.needs_rag is not None:
            plan.needs_rag = analysis.needs_rag
        if analysis.needs_live_search_now is not None:
            plan.needs_live_search_now = analysis.needs_live_search_now
        if analysis.needs_live_search_followup is not None:
            plan.needs_live_search_followup = analysis.needs_live_search_followup
        if analysis.search_query:
            plan.search_query = analysis.search_query
        if analysis.search_reason:
            plan.search_reason = analysis.search_reason
        if analysis.allow_media_action is not None and personality_name.lower() not in {"downbad"}:
            plan.allow_media_action = analysis.allow_media_action
            plan.media_action = "generate_media" if plan.allow_media_action else None
        if (
            analysis.allow_reminder_action is not None
            and personality_name.lower() not in {"downbad", "roleplay"}
        ):
            plan.allow_reminder_action = analysis.allow_reminder_action
            plan.reminder_action = "create_reminder" if plan.allow_reminder_action else None
        if analysis.clarification_required is not None:
            plan.clarification_required = analysis.clarification_required
            plan.clarification_text = analysis.clarification_text
        self._sync_followup_jobs(plan)
        self._merge_reasoning(plan, analysis.reasoning, "llm_primary_override")

    def _apply_sentiment_fields(
        self,
        plan: TurnPlan,
        analysis: LLMTurnAnalysis,
    ) -> None:
        if analysis.sentiment_priority and (
            self._priority_rank(analysis.sentiment_priority)
            >= self._priority_rank(plan.sentiment_priority)
        ):
            plan.sentiment_priority = analysis.sentiment_priority
        if analysis.emotion_label:
            plan.emotion_label = analysis.emotion_label
        plan.sentiment_valence = analysis.sentiment_valence
        plan.sentiment_arousal = analysis.sentiment_arousal
        plan.sentiment_dominance = analysis.sentiment_dominance
        plan.sentiment_confidence = analysis.sentiment_confidence
        plan.crisis_risk = plan.crisis_risk or analysis.crisis_risk
        plan.scheduled_event = plan.scheduled_event or analysis.scheduled_event
        plan.timing_question_ok = plan.timing_question_ok or analysis.timing_question_ok
        if analysis.crisis_risk:
            plan.sentiment_priority = "high"

    def _merge_reasoning(
        self,
        plan: TurnPlan,
        extra: list[str],
        fallback_marker: str,
    ) -> None:
        merged = list(plan.reasoning)
        if fallback_marker not in merged:
            merged.append(fallback_marker)
        for item in extra:
            if item not in merged:
                merged.append(item)
        plan.reasoning = merged

    @staticmethod
    def _priority_rank(priority: str) -> int:
        return _SENTIMENT_PRIORITY_ORDER.get(priority, 0)

    def _plan_summary(self, plan: TurnPlan) -> dict[str, Any]:
        return {
            "primary_intent": plan.primary_intent,
            "sentiment_priority": plan.sentiment_priority,
            "emotion_label": plan.emotion_label,
            "needs_rag": plan.needs_rag,
            "needs_live_search_now": plan.needs_live_search_now,
            "needs_live_search_followup": plan.needs_live_search_followup,
            "search_query": plan.search_query,
            "search_reason": plan.search_reason,
            "allow_media_action": plan.allow_media_action,
            "allow_reminder_action": plan.allow_reminder_action,
            "clarification_required": plan.clarification_required,
            "clarification_text": plan.clarification_text,
            "crisis_risk": plan.crisis_risk,
            "scheduled_event": plan.scheduled_event,
            "timing_question_ok": plan.timing_question_ok,
        }

    def _analysis_summary(self, analysis: LLMTurnAnalysis) -> dict[str, Any]:
        return {
            "primary_intent": analysis.primary_intent,
            "sentiment_priority": analysis.sentiment_priority,
            "emotion_label": analysis.emotion_label,
            "needs_rag": analysis.needs_rag,
            "needs_live_search_now": analysis.needs_live_search_now,
            "needs_live_search_followup": analysis.needs_live_search_followup,
            "search_query": analysis.search_query,
            "search_reason": analysis.search_reason,
            "allow_media_action": analysis.allow_media_action,
            "allow_reminder_action": analysis.allow_reminder_action,
            "clarification_required": analysis.clarification_required,
            "clarification_text": analysis.clarification_text,
            "crisis_risk": analysis.crisis_risk,
            "scheduled_event": analysis.scheduled_event,
            "timing_question_ok": analysis.timing_question_ok,
        }

    def _build_shadow_comparison(
        self,
        *,
        heuristic_summary: dict[str, Any],
        llm_summary: dict[str, Any],
        heuristic_latency_ms: float,
        llm_latency_ms: float | None,
        llm_model: str | None,
    ) -> dict[str, Any]:
        mismatch_fields = self._mismatch_fields(heuristic_summary, llm_summary)
        return {
            "heuristic_summary": heuristic_summary,
            "llm_summary": llm_summary,
            "mismatch_fields": mismatch_fields,
            "heuristic_latency_ms": heuristic_latency_ms,
            "llm_latency_ms": llm_latency_ms,
            "llm_model": llm_model,
        }

    def _mismatch_fields(
        self,
        heuristic_summary: dict[str, Any],
        llm_summary: dict[str, Any],
    ) -> list[str]:
        mismatches: list[str] = []
        for key, heuristic_value in heuristic_summary.items():
            llm_value = llm_summary.get(key)
            if llm_value is None:
                continue
            if llm_value != heuristic_value:
                mismatches.append(key)
        return mismatches

    def _sync_followup_jobs(self, plan: TurnPlan) -> None:
        jobs = [job for job in plan.followup_jobs if job != "web_search"]
        if plan.needs_live_search_followup:
            jobs.append("web_search")
        if "audit" not in jobs:
            jobs.append("audit")
        plan.followup_jobs = jobs

    def _search_mode(self, lowered: str, *, reason: str) -> str:
        if any(token in lowered for token in _DEFERRED_SEARCH_HINTS):
            return "followup"
        if any(token in lowered for token in _IMMEDIATE_SEARCH_HINTS):
            return "now"
        reason_lower = reason.lower()
        if "recommendation" in reason_lower or "venues" in reason_lower:
            return "followup"
        return "now"

    def _classify_intent(self, lowered: str) -> str:
        if any(token in lowered for token in _MEDIA_HINTS):
            return "media_request"
        if any(token in lowered for token in _REMINDER_HINTS):
            return "reminder_request"
        if "?" in lowered:
            return "question"
        if len(lowered) < 8:
            return "smalltalk"
        return "conversation"

    def _estimate_emotion(self, lowered: str) -> tuple[str, str | None]:
        for term, label in _EMOTION_TERMS.items():
            if term in lowered:
                if label in {"panic", "fear", "sadness"}:
                    return "high", label
                if label in {"stress", "overwhelmed"}:
                    return "elevated", label
                return "normal", label
        if "!" in lowered and len(lowered) > 20:
            return "elevated", None
        return "normal", None

    def _detect_scheduled_event(self, lowered: str) -> bool:
        has_event = any(token in lowered for token in _REMINDER_EVENT_HINTS)
        has_future_anchor = any(token in lowered for token in _REMINDER_FUTURE_HINTS)
        return has_event and has_future_anchor

    def _timing_question_ok(
        self,
        lowered: str,
        *,
        primary_intent: str,
        scheduled_event: bool,
    ) -> bool:
        if primary_intent == "reminder_request":
            return False
        if not scheduled_event:
            return False
        if any(token in lowered for token in (" at ", " at\n", " at\t")):
            return False
        if re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", lowered):
            return False
        return True

    def _media_allowed(self, lowered: str, personality: str) -> bool:
        if personality.lower() in {"downbad"}:
            return False
        return any(token in lowered for token in _MEDIA_HINTS)

    def _reminder_allowed(
        self,
        lowered: str,
        personality: str,
        *,
        sentiment_priority: str,
    ) -> bool:
        if personality.lower() in {"downbad", "roleplay"}:
            return False
        if any(token in lowered for token in _REMINDER_HINTS):
            return True
        has_event = any(token in lowered for token in _REMINDER_EVENT_HINTS)
        has_future_anchor = any(token in lowered for token in _REMINDER_FUTURE_HINTS)
        has_routine_activity = any(token in lowered for token in _REMINDER_ROUTINE_HINTS)
        has_distress = (
            sentiment_priority == "high"
            or any(token in lowered for token in _REMINDER_DISTRESS_HINTS)
        )
        has_importance = any(token in lowered for token in _IMPORTANT_HINTS)

        if has_event and (has_future_anchor or has_distress or has_importance):
            return True
        if has_distress and not has_routine_activity:
            return True
        if has_importance and has_distress:
            return True
        return False

    def _clarification_gate(
        self,
        lowered: str,
        *,
        allow_reminder_action: bool,
        allow_media_action: bool,
        needs_live_search_now: bool,
    ) -> tuple[bool, str | None]:
        if allow_reminder_action and "remind" in lowered:
            if not any(token in lowered for token in ("at ", "tomorrow", "tonight", "in ")):
                return True, "When should I remind you?"
        if allow_media_action and "image" in lowered and "of" not in lowered and "about" not in lowered:
            return True, "What would you like me to generate an image of?"
        if needs_live_search_now and any(term in lowered for term in ("near me", "nearby")):
            if "in " not in lowered and "near " not in lowered:
                return True, "What city or area should I search in?"
        return False, None

    def _extract_profile_candidates(
        self, lowered: str, profile_context_text: str | None
    ) -> list[TurnProfileCandidate]:
        candidates: list[TurnProfileCandidate] = []
        for pattern, key in _PROFILE_PATTERNS:
            match = pattern.search(lowered)
            if not match:
                continue
            value = match.group(1).strip().strip(".")
            if not value or len(value) < 2:
                continue
            confidence = 0.55
            if key in {"name", "birthday"}:
                confidence = 0.8
            if key in {"goal"}:
                confidence = 0.7
            candidates.append(
                TurnProfileCandidate(
                    key=key,
                    value=value,
                    confidence=confidence,
                    reason=f"pattern:{key}",
                )
            )
        if not candidates:
            return []
        existing_map = _profile_context_map(profile_context_text)
        for candidate in candidates:
            existing = existing_map.get(candidate.key)
            if existing and existing.strip().lower() != candidate.value.lower():
                candidate.contradiction = True
                candidate.existing_value = existing
        return candidates

    def _detect_contradictions(
        self, candidates: list[TurnProfileCandidate], profile_context_text: str | None
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        existing_map = _profile_context_map(profile_context_text)
        contradictions: list[dict[str, Any]] = []
        for candidate in candidates:
            existing = existing_map.get(candidate.key)
            if existing and existing.strip().lower() != candidate.value.lower():
                contradictions.append(
                    {
                        "key": candidate.key,
                        "existing_value": existing,
                        "new_value": candidate.value,
                        "reason": "profile_context_mismatch",
                    }
                )
        return contradictions

    def _memory_candidates(
        self, lowered: str, intent: str, sentiment_priority: str
    ) -> list[TurnMemoryCandidate]:
        if not lowered:
            return []
        base_importance = 3.0 if intent in {"conversation", "question"} else 4.5
        if sentiment_priority == "high":
            base_importance += 1.5
        if any(token in lowered for token in _IMPORTANT_HINTS):
            base_importance += 1.0
        emotional_salience = 0.6 if sentiment_priority == "high" else (0.3 if sentiment_priority == "elevated" else 0.1)
        user_value = 0.5 if any(token in lowered for token in ("i ", "my ", "me ")) else 0.2
        context_score = 0.3 if "?" in lowered else 0.15
        candidate = TurnMemoryCandidate(
            kind="conversation",
            excerpt=lowered[:240],
            importance_score=min(10.0, base_importance),
            emotional_salience=emotional_salience,
            user_value_score=user_value,
            context_score=context_score,
            store_long_term=base_importance >= 5.0,
            relevance_tags=[],
            reason="heuristic",
        )
        return [candidate]


def _profile_context_map(profile_context_text: str | None) -> dict[str, str]:
    if not profile_context_text:
        return {}
    mapping: dict[str, str] = {}
    for raw_line in profile_context_text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        mapping[key.strip().lower()] = value.strip()
    return mapping


def serialize_plan(plan: TurnPlan | None) -> str:
    if plan is None:
        return "{}"
    try:
        return json.dumps(plan.to_dict(), ensure_ascii=True)
    except Exception:
        return "{}"
