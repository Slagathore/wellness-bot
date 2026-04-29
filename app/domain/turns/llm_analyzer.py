"""LLM-backed enrichment for per-turn planning and sentiment analysis."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from app.config import settings
from app.utils.llm_json import parse_llm_json
from app.utils.ollama import generate

logger = logging.getLogger(__name__)

_VALID_INTENTS = {
    "conversation",
    "question",
    "reminder_request",
    "media_request",
    "smalltalk",
}
_VALID_PRIORITIES = {"normal", "elevated", "high"}


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return None


def _coerce_float(
    value: object,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _coerce_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_reasoning(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text[:120])
    return items


@dataclass(slots=True)
class LLMTurnAnalysis:
    primary_intent: str | None = None
    sentiment_priority: str | None = None
    emotion_label: str | None = None
    sentiment_valence: float | None = None
    sentiment_arousal: float | None = None
    sentiment_dominance: float | None = None
    sentiment_confidence: float | None = None
    needs_rag: bool | None = None
    needs_live_search_now: bool | None = None
    needs_live_search_followup: bool | None = None
    search_query: str | None = None
    search_reason: str | None = None
    allow_media_action: bool | None = None
    allow_reminder_action: bool | None = None
    clarification_required: bool | None = None
    clarification_text: str | None = None
    crisis_risk: bool = False
    scheduled_event: bool = False
    timing_question_ok: bool = False
    reasoning: list[str] = field(default_factory=list)
    model_name: str | None = None

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        model_name: str | None,
    ) -> "LLMTurnAnalysis":
        primary_intent = _coerce_text(payload.get("primary_intent"))
        if primary_intent not in _VALID_INTENTS:
            primary_intent = None

        sentiment_priority = _coerce_text(payload.get("sentiment_priority"))
        if sentiment_priority not in _VALID_PRIORITIES:
            sentiment_priority = None

        clarification_required = _coerce_bool(payload.get("clarification_required"))
        clarification_text = _coerce_text(payload.get("clarification_text"))
        if clarification_required is False:
            clarification_text = None
        if clarification_required is None and clarification_text:
            clarification_required = True

        return cls(
            primary_intent=primary_intent,
            sentiment_priority=sentiment_priority,
            emotion_label=_coerce_text(payload.get("emotion_label")),
            sentiment_valence=_coerce_float(
                payload.get("sentiment_valence"), min_value=-1.0, max_value=1.0
            ),
            sentiment_arousal=_coerce_float(
                payload.get("sentiment_arousal"), min_value=0.0, max_value=1.0
            ),
            sentiment_dominance=_coerce_float(
                payload.get("sentiment_dominance"), min_value=0.0, max_value=1.0
            ),
            sentiment_confidence=_coerce_float(
                payload.get("sentiment_confidence"), min_value=0.0, max_value=1.0
            ),
            needs_rag=_coerce_bool(payload.get("needs_rag")),
            needs_live_search_now=_coerce_bool(payload.get("needs_live_search_now")),
            needs_live_search_followup=_coerce_bool(
                payload.get("needs_live_search_followup")
            ),
            search_query=_coerce_text(payload.get("search_query")),
            search_reason=_coerce_text(payload.get("search_reason")),
            allow_media_action=_coerce_bool(payload.get("allow_media_action")),
            allow_reminder_action=_coerce_bool(payload.get("allow_reminder_action")),
            clarification_required=clarification_required,
            clarification_text=clarification_text,
            crisis_risk=bool(_coerce_bool(payload.get("crisis_risk"))),
            scheduled_event=bool(_coerce_bool(payload.get("scheduled_event"))),
            timing_question_ok=bool(_coerce_bool(payload.get("timing_question_ok"))),
            reasoning=_coerce_reasoning(payload.get("reasoning")),
            model_name=model_name,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LLMTurnAnalyzer:
    """Ask a worker model for richer emotional + routing signal."""

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout_seconds: float | None = None,
        generate_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        cfg = settings()
        self._model_override = model
        self._timeout_seconds = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(getattr(cfg, "turn_planner_timeout_seconds", 8.0) or 8.0)
        )
        self._generate = generate_fn or generate

    def resolve_model(self) -> str | None:
        if self._model_override:
            return self._model_override
        cfg = settings()
        configured = (
            getattr(cfg, "planner_model", None)
            or getattr(cfg, "turn_planner_model", None)
            or cfg.worker_model
        )
        if configured:
            return str(configured)
        return None

    def analyze(
        self,
        *,
        user_id: int,
        session_id: int | None,
        message_text: str,
        personality_name: str,
        heuristic_plan: dict[str, Any] | None = None,
        profile_context_text: str | None = None,
    ) -> LLMTurnAnalysis | None:
        model_name = self.resolve_model()
        if not model_name or not message_text.strip():
            return None

        prompt = self._build_prompt(
            user_id=user_id,
            session_id=session_id,
            message_text=message_text,
            personality_name=personality_name,
            heuristic_plan=heuristic_plan or {},
            profile_context_text=profile_context_text,
        )
        try:
            response = self._generate(
                prompt=prompt,
                model=model_name,
                format="json",
                options={
                    "temperature": 0.1,
                    "request_timeout": self._timeout_seconds,
                },
            )
            payload = parse_llm_json(response.get("text") or "{}")
            if not isinstance(payload, dict):
                return None
            return LLMTurnAnalysis.from_payload(payload, model_name=model_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("LLM turn analysis failed for user %s: %s", user_id, exc)
            return None

    def _build_prompt(
        self,
        *,
        user_id: int,
        session_id: int | None,
        message_text: str,
        personality_name: str,
        heuristic_plan: dict[str, Any],
        profile_context_text: str | None,
    ) -> str:
        heuristic_json = json.dumps(heuristic_plan, ensure_ascii=True)
        profile_excerpt = (profile_context_text or "").strip()
        if len(profile_excerpt) > 800:
            profile_excerpt = profile_excerpt[:800]

        return (
            "Analyze this user turn for emotional state and routing hints. "
            "Return JSON only.\n\n"
            "Rules:\n"
            "- Be conservative about enabling reminder/media/search actions.\n"
            "- scheduled_event=true only for concrete upcoming events such as interviews, appointments, exams, funerals, burials, surgeries, court dates, weddings, or similar.\n"
            "- Do not treat routine recurring obligations like work, class, chores, or errands as scheduled events by default.\n"
            "- Funeral or burial language can imply a scheduled grief event without implying crisis risk.\n"
            "- timing_question_ok=true only when it would feel natural for the assistant to ask about timing within the conversational reply.\n"
            "- crisis_risk=true only for clear self-harm or severe crisis signal.\n"
            "- needs_live_search_now is for time-sensitive facts; needs_live_search_followup is for recommendation-style follow-up.\n"
            "- Do not wrap the JSON in markdown fences.\n"
            "- If uncertain, leave optional text fields null and keep booleans false.\n\n"
            "Return this JSON shape:\n"
            "{\n"
            '  "primary_intent": "conversation|question|reminder_request|media_request|smalltalk",\n'
            '  "sentiment_priority": "normal|elevated|high",\n'
            '  "emotion_label": "<label or null>",\n'
            '  "sentiment_valence": <float -1.0..1.0>,\n'
            '  "sentiment_arousal": <float 0.0..1.0>,\n'
            '  "sentiment_dominance": <float 0.0..1.0>,\n'
            '  "sentiment_confidence": <float 0.0..1.0>,\n'
            '  "needs_rag": <boolean>,\n'
            '  "needs_live_search_now": <boolean>,\n'
            '  "needs_live_search_followup": <boolean>,\n'
            '  "search_query": "<string or null>",\n'
            '  "search_reason": "<string or null>",\n'
            '  "allow_media_action": <boolean>,\n'
            '  "allow_reminder_action": <boolean>,\n'
            '  "clarification_required": <boolean>,\n'
            '  "clarification_text": "<string or null>",\n'
            '  "crisis_risk": <boolean>,\n'
            '  "scheduled_event": <boolean>,\n'
            '  "timing_question_ok": <boolean>,\n'
            '  "reasoning": ["short note", "short note"]\n'
            "}\n\n"
            f"User ID: {user_id}\n"
            f"Session ID: {session_id}\n"
            f"Personality: {personality_name}\n"
            f"User message: {message_text!r}\n"
            f"Heuristic draft plan: {heuristic_json}\n"
            f"Profile context excerpt: {profile_excerpt!r}\n"
        )
