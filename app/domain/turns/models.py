"""Data models for per-turn orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _safe_list(raw: object) -> list[object]:
    if isinstance(raw, list):
        return raw
    return []


@dataclass(slots=True)
class TurnProfileCandidate:
    key: str
    value: str
    confidence: float
    source: str = "message_pattern"
    reason: str | None = None
    contradiction: bool = False
    existing_value: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TurnProfileCandidate":
        return cls(
            key=str(raw.get("key") or "").strip(),
            value=str(raw.get("value") or "").strip(),
            confidence=float(raw.get("confidence") or 0.0),
            source=str(raw.get("source") or "message_pattern"),
            reason=str(raw["reason"]) if raw.get("reason") is not None else None,
            contradiction=bool(raw.get("contradiction")),
            existing_value=(
                str(raw["existing_value"])
                if raw.get("existing_value") is not None
                else None
            ),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(slots=True)
class TurnMemoryCandidate:
    kind: str
    excerpt: str
    importance_score: float
    emotional_salience: float
    user_value_score: float
    context_score: float
    store_long_term: bool = False
    relevance_tags: list[str] = field(default_factory=list)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TurnMemoryCandidate":
        return cls(
            kind=str(raw.get("kind") or "conversation"),
            excerpt=str(raw.get("excerpt") or "").strip(),
            importance_score=float(raw.get("importance_score") or 0.0),
            emotional_salience=float(raw.get("emotional_salience") or 0.0),
            user_value_score=float(raw.get("user_value_score") or 0.0),
            context_score=float(raw.get("context_score") or 0.0),
            store_long_term=bool(raw.get("store_long_term")),
            relevance_tags=[
                str(item).strip()
                for item in _safe_list(raw.get("relevance_tags"))
                if str(item).strip()
            ],
            reason=str(raw["reason"]) if raw.get("reason") is not None else None,
        )


@dataclass(slots=True)
class TurnPlan:
    user_id: int
    session_id: int | None
    message_text: str
    primary_intent: str
    sentiment_priority: str
    planner_source: str = "heuristic"
    planner_latency_ms: float | None = None
    emotion_label: str | None = None
    sentiment_valence: float | None = None
    sentiment_arousal: float | None = None
    sentiment_dominance: float | None = None
    sentiment_confidence: float | None = None
    needs_rag: bool = False
    needs_live_search_now: bool = False
    needs_live_search_followup: bool = False
    search_query: str | None = None
    search_reason: str | None = None
    allow_media_action: bool = False
    media_action: str | None = None
    allow_reminder_action: bool = False
    reminder_action: str | None = None
    clarification_required: bool = False
    clarification_text: str | None = None
    crisis_risk: bool = False
    scheduled_event: bool = False
    timing_question_ok: bool = False
    profile_candidates: list[TurnProfileCandidate] = field(default_factory=list)
    memory_candidates: list[TurnMemoryCandidate] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    followup_jobs: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    shadow_comparison: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "message_text": self.message_text,
            "primary_intent": self.primary_intent,
            "sentiment_priority": self.sentiment_priority,
            "planner_source": self.planner_source,
            "planner_latency_ms": self.planner_latency_ms,
            "emotion_label": self.emotion_label,
            "sentiment_valence": self.sentiment_valence,
            "sentiment_arousal": self.sentiment_arousal,
            "sentiment_dominance": self.sentiment_dominance,
            "sentiment_confidence": self.sentiment_confidence,
            "needs_rag": self.needs_rag,
            "needs_live_search_now": self.needs_live_search_now,
            "needs_live_search_followup": self.needs_live_search_followup,
            "search_query": self.search_query,
            "search_reason": self.search_reason,
            "allow_media_action": self.allow_media_action,
            "media_action": self.media_action,
            "allow_reminder_action": self.allow_reminder_action,
            "reminder_action": self.reminder_action,
            "clarification_required": self.clarification_required,
            "clarification_text": self.clarification_text,
            "crisis_risk": self.crisis_risk,
            "scheduled_event": self.scheduled_event,
            "timing_question_ok": self.timing_question_ok,
            "profile_candidates": [item.to_dict() for item in self.profile_candidates],
            "memory_candidates": [item.to_dict() for item in self.memory_candidates],
            "contradictions": [dict(item) for item in self.contradictions],
            "followup_jobs": list(self.followup_jobs),
            "reasoning": list(self.reasoning),
            "shadow_comparison": (
                dict(self.shadow_comparison) if isinstance(self.shadow_comparison, dict) else None
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TurnPlan":
        return cls(
            user_id=int(raw.get("user_id") or 0),
            session_id=int(raw["session_id"]) if raw.get("session_id") is not None else None,
            message_text=str(raw.get("message_text") or ""),
            primary_intent=str(raw.get("primary_intent") or "conversation"),
            sentiment_priority=str(raw.get("sentiment_priority") or "normal"),
            planner_source=str(raw.get("planner_source") or "heuristic"),
            planner_latency_ms=(
                float(raw["planner_latency_ms"])
                if raw.get("planner_latency_ms") is not None
                else None
            ),
            emotion_label=str(raw["emotion_label"]) if raw.get("emotion_label") else None,
            sentiment_valence=(
                float(raw["sentiment_valence"])
                if raw.get("sentiment_valence") is not None
                else None
            ),
            sentiment_arousal=(
                float(raw["sentiment_arousal"])
                if raw.get("sentiment_arousal") is not None
                else None
            ),
            sentiment_dominance=(
                float(raw["sentiment_dominance"])
                if raw.get("sentiment_dominance") is not None
                else None
            ),
            sentiment_confidence=(
                float(raw["sentiment_confidence"])
                if raw.get("sentiment_confidence") is not None
                else None
            ),
            needs_rag=bool(raw.get("needs_rag")),
            needs_live_search_now=bool(raw.get("needs_live_search_now")),
            needs_live_search_followup=bool(raw.get("needs_live_search_followup")),
            search_query=str(raw["search_query"]) if raw.get("search_query") else None,
            search_reason=str(raw["search_reason"]) if raw.get("search_reason") else None,
            allow_media_action=bool(raw.get("allow_media_action")),
            media_action=str(raw["media_action"]) if raw.get("media_action") else None,
            allow_reminder_action=bool(raw.get("allow_reminder_action")),
            reminder_action=(
                str(raw["reminder_action"]) if raw.get("reminder_action") else None
            ),
            clarification_required=bool(raw.get("clarification_required")),
            clarification_text=(
                str(raw["clarification_text"])
                if raw.get("clarification_text")
                else None
            ),
            crisis_risk=bool(raw.get("crisis_risk")),
            scheduled_event=bool(raw.get("scheduled_event")),
            timing_question_ok=bool(raw.get("timing_question_ok")),
            profile_candidates=[
                TurnProfileCandidate.from_dict(dict(item))
                for item in _safe_list(raw.get("profile_candidates"))
                if isinstance(item, dict)
            ],
            memory_candidates=[
                TurnMemoryCandidate.from_dict(dict(item))
                for item in _safe_list(raw.get("memory_candidates"))
                if isinstance(item, dict)
            ],
            contradictions=[
                dict(item)
                for item in _safe_list(raw.get("contradictions"))
                if isinstance(item, dict)
            ],
            followup_jobs=[
                str(item).strip()
                for item in _safe_list(raw.get("followup_jobs"))
                if str(item).strip()
            ],
            reasoning=[
                str(item).strip()
                for item in _safe_list(raw.get("reasoning"))
                if str(item).strip()
            ],
            shadow_comparison=(
                dict(raw["shadow_comparison"])
                if isinstance(raw.get("shadow_comparison"), dict)
                else None
            ),
        )


def coerce_turn_plan(raw: TurnPlan | dict[str, Any] | None) -> TurnPlan | None:
    if raw is None:
        return None
    if isinstance(raw, TurnPlan):
        return raw
    if isinstance(raw, dict):
        try:
            return TurnPlan.from_dict(raw)
        except Exception:
            return None
    return None
