"""Per-turn planner that decides hot-path and follow-up actions."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.orchestrator.context_builder import user_profile_context
from app.orchestrator.persona_runtime import get_user_personality_name
from app.rag.service import get_retriever
from app.utils.web_search import should_search_web

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


@dataclass(slots=True)
class PlannerInputs:
    user_id: int
    session_id: int | None
    message_text: str
    personality_name: str
    profile_context_text: str | None


class TurnPlanner:
    """Cheap heuristics for turn routing and decision gating."""

    def build_plan(self, *, user_id: int, session_id: int | None, message_text: str) -> TurnPlan:
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

    def _plan(self, inputs: PlannerInputs) -> TurnPlan:
        text = (inputs.message_text or "").strip()
        lowered = text.lower()
        primary_intent = self._classify_intent(lowered)
        sentiment_priority, emotion_label = self._estimate_emotion(lowered)
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
            emotion_label=emotion_label,
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
        if contradictions:
            plan.reasoning.append("profile_contradiction")
        if needs_live_search_now:
            plan.reasoning.append("web_search_needed")
        if needs_live_search_followup:
            plan.reasoning.append("web_search_deferred")
        if needs_rag:
            plan.reasoning.append("rag_needed")
        return plan

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
