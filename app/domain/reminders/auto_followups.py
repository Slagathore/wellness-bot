"""Live follow-up reminder creation driven by the current user message."""

from __future__ import annotations

import json
import logging
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from app.core.container import container
from app.db import db_ro, db_rw
from app.history_scope import history_scope_for_user, table_has_column
from app.orchestrator.persona_runtime import get_user_personality_name
from app.personality.modes import PERSONALITY_MODES, is_custom_character
from app.runtime.services.preferences import PreferenceService
from app.utils.time_utils import normalize_operator, to_user_time

logger = logging.getLogger(__name__)

_BLOCK_ORDER = ["night", "morning", "afternoon", "evening"]
_BLOCK_WINDOWS = {
    "night": (0, 6),
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (18, 24),
}
_BLOCK_CENTERS = {
    "night": (3, 0),
    "morning": (9, 0),
    "afternoon": (15, 0),
    "evening": (21, 0),
}
_DISTRESS_TERMS = {
    "anxious",
    "anxiety",
    "nervous",
    "panic",
    "panicking",
    "overwhelmed",
    "spiraling",
    "depressed",
    "hopeless",
    "heartbroken",
    "grieving",
    "grief",
    "sad",
    "miserable",
    "ashamed",
    "embarrassed",
    "stressed",
    "terrified",
    "scared",
    "afraid",
    "worried",
    "dreading",
}
_LOW_ENERGY_TERMS = {
    "exhausted",
    "drained",
    "numb",
    "empty",
    "burned out",
    "burnt out",
    "tired",
    "sick",
    "ill",
    "fever",
    "vomiting",
    "migraine",
    "wiped out",
}
_HIGH_ENERGY_TERMS = {
    "thrilled",
    "excited",
    "hyped",
    "buzzing",
    "ecstatic",
    "amped",
    "wired",
    "shaking",
    "freaking out",
    "losing my mind",
}
_FUTURE_TERMS = (
    "tomorrow",
    "tonight",
    "later",
    "this evening",
    "this afternoon",
    "next week",
    "next month",
    "coming up",
    "upcoming",
    "soon",
    "in ",
)
_MAJOR_EVENT_KEYWORDS = {
    "birthday": "check how their birthday went",
    "interview": "check how the interview went",
    "date": "check how the date went",
    "party": "check how the party went",
    "concert": "check how the concert went",
    "wedding": "check how the wedding went",
    "exam": "check how the exam went",
    "test": "check how the test went",
    "surgery": "check how surgery went",
    "appointment": "check how the appointment went",
    "doctor": "check how the appointment went",
    "court": "check how court went",
    "funeral": "check how they are doing after the funeral",
    "graduation": "check how graduation went",
}
_SOFT_EVENT_KEYWORDS = {
    "work": "check how work went",
    "shift": "check how the shift went",
    "trip": "check how the trip went",
    "flight": "check how the trip went",
    "meeting": "check how the meeting went",
}
_EVENT_KEYWORDS = {
    **_MAJOR_EVENT_KEYWORDS,
    **_SOFT_EVENT_KEYWORDS,
}
_SCHEDULED_EVENT_KEYWORDS = frozenset(_MAJOR_EVENT_KEYWORDS)
_LOW_PRIORITY_EVENT_KEYWORDS = frozenset(_SOFT_EVENT_KEYWORDS)
_POSITIVE_EVENT_KEYWORDS = {
    "birthday",
    "anniversary",
    "graduation",
    "party",
    "date",
    "concert",
    "vacation",
    "trip",
    "wedding",
}
_STRONG_DISTRESS_PHRASES = (
    "panic attack",
    "can't stop crying",
    "can’t stop crying",
    "falling apart",
    "not okay",
    "losing it",
    "really nervous",
    "so nervous",
)
_EXPLICIT_FOLLOWUP_RE = re.compile(
    r"\b(check in|check on me|follow up|follow-up|ask me later|remind me|reach out)\b",
    re.IGNORECASE,
)
_IN_HOURS_RE = re.compile(r"\bin\s+(\d+)\s+hours?\b", re.IGNORECASE)
_IN_DAYS_RE = re.compile(r"\bin\s+(\d+)\s+days?\b", re.IGNORECASE)
_IN_WEEKS_RE = re.compile(r"\bin\s+(\d+)\s+weeks?\b", re.IGNORECASE)
_NUMERIC_TIME_RE = re.compile(
    r"\b(?:at|around|by)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)
_TIME_OF_DAY_HINTS = ("morning", "afternoon", "evening", "night")
_MIN_BLOCK_WINDOW_MINUTES = 61
_FOLLOWUP_CLARIFICATION_KEY = "pending_followup_clarification"
_IMPORTANCE_TERMS = (
    "important",
    "really need",
    "need to",
    "have to",
    "can't forget",
    "dont forget",
    "don't forget",
    "must",
    "deadline",
    "big day",
    "matters a lot",
)
_PROFILE_SIGNAL_KEYS = ("memory_notes", "latest_assessment_summary", "wellness_goals")
_STOPWORDS = {
    "about", "after", "again", "also", "and", "because", "been", "before", "being",
    "could", "from", "have", "just", "like", "maybe", "more", "need", "really",
    "some", "that", "their", "there", "they", "this", "thing", "with", "would",
    "your", "you're", "youre", "when", "what", "where", "will", "been", "into",
}


@dataclass(slots=True)
class FollowupDecision:
    should_create: bool
    reason_text: str
    followup_kind: str
    trigger_label: str
    energy_score: int
    valence_score: float
    time_of_day: str | None
    next_run_local: datetime | None
    event_due_local: datetime | None
    ask_clarification: bool = False
    clarification_text: str | None = None
    importance_score: float = 0.0
    arousal_score: float | None = None
    dominance_score: float | None = None
    emotion_label: str | None = None
    sentiment_source: str | None = None


@dataclass(slots=True)
class SentimentSignals:
    valence: float | None = None
    arousal: float | None = None
    dominance: float | None = None
    emotion_label: str | None = None
    confidence: float | None = None
    source: str = "none"


@dataclass(slots=True)
class HistorySignals:
    repeated_hits: int = 0
    profile_hits: int = 0
    importance_score: float = 0.0
    matched_keywords: tuple[str, ...] = ()


def _matched_event_keyword(lowered: str) -> str | None:
    for keyword in sorted(_EVENT_KEYWORDS, key=len, reverse=True):
        if keyword in lowered:
            return keyword
    return None


def _has_future_anchor(lowered: str) -> bool:
    return any(token in lowered for token in _FUTURE_TERMS)


def _default_event_hour(event_keyword: str | None, lowered: str) -> int:
    if any(token in lowered for token in ("evening", "night", "tonight")):
        return 19
    if event_keyword in {"date", "party", "concert", "birthday", "wedding"}:
        return 19
    if event_keyword in {"interview", "exam", "test", "appointment", "doctor", "court"}:
        return 10
    if event_keyword in {"work", "shift", "meeting"}:
        return 9
    return 14


def _sentiment_marker_hits(sentiment: SentimentSignals) -> tuple[str, ...]:
    if sentiment.source != "message":
        return ()

    hits: list[str] = []
    if sentiment.valence is not None and abs(sentiment.valence) >= 0.45:
        hits.append("valence")
    if sentiment.arousal is not None:
        if sentiment.arousal >= 0.75:
            hits.append("arousal")
        elif (
            sentiment.valence is not None
            and sentiment.valence <= -0.35
            and sentiment.arousal <= 0.25
        ):
            hits.append("arousal")
    if (
        sentiment.dominance is not None
        and sentiment.valence is not None
        and sentiment.valence <= -0.25
        and sentiment.dominance <= 0.35
    ):
        hits.append("dominance")
    if sentiment.emotion_label in {"sadness", "fear", "anger", "disgust"}:
        hits.append("emotion_label")
    if sentiment.confidence is not None and sentiment.confidence >= 0.6:
        hits.append("confidence")
    return tuple(hits)


def maybe_create_followup_for_message(
    *,
    user_id: int,
    session_id: int | None,
    message_id: int | None,
    text: str,
    message_timestamp: str | datetime | None = None,
) -> str | None:
    """Best-effort live follow-up creation after a user message is persisted."""

    cleaned = " ".join((text or "").split())
    if len(cleaned) < 12:
        return None
    if not _auto_followups_enabled(user_id):
        _clear_pending_clarification(user_id)
        return None
    if message_id is not None and _has_existing_followup_for_origin_message(
        user_id=user_id,
        origin_message_id=message_id,
    ):
        return None

    reference_local = _resolve_reference_local(user_id, message_timestamp)
    resolved_pending = _maybe_resolve_pending_clarification(
        user_id=user_id,
        session_id=session_id,
        message_id=message_id,
        text=cleaned,
        reference_local=reference_local,
    )
    if resolved_pending is not None:
        return resolved_pending

    sentiment = _sentiment_signals(user_id=user_id, message_id=message_id)
    history = _history_signals(user_id=user_id, text=cleaned)
    decision = _decide_followup(
        cleaned,
        reference_local,
        user_id=user_id,
        sentiment=sentiment,
        history=history,
    )
    if decision.ask_clarification and decision.clarification_text:
        _store_pending_clarification(
            user_id=user_id,
            payload={
                "origin_message_id": message_id,
                "origin_session_id": session_id,
                "origin_excerpt": cleaned[:280],
                "origin_timestamp": reference_local.isoformat(timespec="minutes"),
                "reason_text": decision.reason_text,
                "followup_kind": decision.followup_kind,
                "trigger_label": decision.trigger_label,
                "scope": history_scope_for_user(user_id),
                "energy_score": decision.energy_score,
                "valence_score": round(decision.valence_score, 3),
                "importance_score": round(decision.importance_score, 3),
            },
        )
        _send_followup_message(
            user_id=user_id,
            text=decision.clarification_text,
        )
        logger.info(
            "[REMINDER-TELEMETRY] followup_clarification_requested user_id=%s origin_message_id=%s kind=%s",
            user_id,
            message_id,
            decision.followup_kind,
        )
        return None
    if not decision.should_create or decision.next_run_local is None:
        return None

    metadata = {
        "origin_message_id": message_id,
        "origin_session_id": session_id,
        "origin_excerpt": cleaned[:280],
        "origin_timestamp": reference_local.isoformat(timespec="minutes"),
        "event_due_local": (
            decision.event_due_local.isoformat(timespec="minutes")
            if decision.event_due_local
            else None
        ),
        "followup_kind": decision.followup_kind,
        "trigger_label": decision.trigger_label,
        "scope": history_scope_for_user(user_id),
        "energy_score": decision.energy_score,
        "valence_score": round(decision.valence_score, 3),
        "arousal_score": (
            round(decision.arousal_score, 3)
            if decision.arousal_score is not None
            else None
        ),
        "dominance_score": (
            round(decision.dominance_score, 3)
            if decision.dominance_score is not None
            else None
        ),
        "emotion_label": decision.emotion_label,
        "sentiment_source": decision.sentiment_source,
        "importance_score": round(decision.importance_score, 3),
        "history_keyword_hits": list(history.matched_keywords),
        "history_repeated_hits": history.repeated_hits,
        "profile_signal_hits": history.profile_hits,
        "time_of_day": decision.time_of_day,
        "respect_sleep_window": True,
        "allow_jitter": False,
    }

    next_run_operator = _to_operator_time(user_id, decision.next_run_local)
    reminder_service = container.resolve("reminder_service")
    reminder_id = reminder_service.create_custom_reminder(
        user_id=str(user_id),
        text=decision.reason_text,
        next_run_at=next_run_operator,
        frequency="once",
        time_of_day=decision.time_of_day,
        allow_jitter=False,
        base_hour=decision.next_run_local.hour,
        base_minute=decision.next_run_local.minute,
        specific_hour=decision.next_run_local.hour,
        specific_minute=decision.next_run_local.minute,
        metadata=metadata,
    )
    logger.info(
        "[REMINDER-TELEMETRY] auto_followup reminder_id=%s user_id=%s origin_message_id=%s kind=%s when_local=%s energy=%s valence=%.2f importance=%.2f sentiment_source=%s",
        reminder_id,
        user_id,
        message_id,
        decision.followup_kind,
        decision.next_run_local.isoformat(timespec="minutes"),
        decision.energy_score,
        decision.valence_score,
        decision.importance_score,
        decision.sentiment_source,
    )
    return reminder_id


def _auto_followups_enabled(user_id: int) -> bool:
    if not PreferenceService().get_followup_pref(user_id):
        return False

    personality = get_user_personality_name(user_id)
    if is_custom_character(personality):
        return False
    config = PERSONALITY_MODES.get(personality, {})
    return bool(config.get("enable_reminders", True))


def _resolve_reference_local(
    user_id: int, message_timestamp: str | datetime | None
) -> datetime:
    from app.domain.reminders.timezone import resolve_user_tz_offset, user_now

    if not message_timestamp:
        return user_now(user_id)
    try:
        if isinstance(message_timestamp, datetime):
            operator_dt = normalize_operator(message_timestamp)
        else:
            operator_dt = normalize_operator(datetime.fromisoformat(str(message_timestamp)))
        offset = resolve_user_tz_offset(user_id)
        return to_user_time(operator_dt, offset, reference=operator_dt)
    except Exception:
        return user_now(user_id)


def _to_operator_time(user_id: int, local_dt: datetime) -> datetime:
    from app.domain.reminders.timezone import user_time_to_operator

    return user_time_to_operator(local_dt, user_id)


def _sentiment_signals(user_id: int, message_id: int | None) -> SentimentSignals:
    scope = history_scope_for_user(user_id)
    try:
        with db_ro() as conn:
            if message_id is not None:
                row = conn.execute(
                    """
                    SELECT s.valence, s.arousal, s.dominance, s.emotion_label, s.confidence
                    FROM sentiments AS s
                    JOIN messages AS m ON m.id = s.message_id
                    WHERE m.id = ?
                    """,
                    (message_id,),
                ).fetchone()
                if row:
                    return SentimentSignals(
                        valence=float(row["valence"]) if row["valence"] is not None else None,
                        arousal=float(row["arousal"]) if row["arousal"] is not None else None,
                        dominance=float(row["dominance"]) if row["dominance"] is not None else None,
                        emotion_label=str(row["emotion_label"] or "").strip().lower() or None,
                        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
                        source="message",
                    )

            scope_sql = ""
            params: list[object] = [user_id]
            if table_has_column("messages", "scope"):
                scope_sql = "AND COALESCE(m.scope, 'standard') = ?"
                params.append(scope)
            rows = conn.execute(
                f"""
                SELECT s.valence, s.arousal, s.dominance, s.emotion_label, s.confidence
                FROM sentiments AS s
                JOIN messages AS m ON m.id = s.message_id
                WHERE m.user_id = ?
                  AND m.role = 'user'
                  {scope_sql}
                ORDER BY m.id DESC
                LIMIT 8
                """,
                tuple(params),
            ).fetchall()
    except Exception:
        return SentimentSignals()

    if not rows:
        return SentimentSignals()

    def _avg(column: str) -> float | None:
        values = [float(row[column]) for row in rows if row[column] is not None]
        if not values:
            return None
        return sum(values) / len(values)

    labels = [
        str(row["emotion_label"] or "").strip().lower()
        for row in rows
        if row["emotion_label"]
    ]
    dominant = Counter(labels).most_common(1)[0][0] if labels else None
    confidences = [float(row["confidence"]) for row in rows if row["confidence"] is not None]
    return SentimentSignals(
        valence=_avg("valence"),
        arousal=_avg("arousal"),
        dominance=_avg("dominance"),
        emotion_label=dominant,
        confidence=(sum(confidences) / len(confidences)) if confidences else None,
        source="recent_average",
    )


def _history_signals(user_id: int, text: str) -> HistorySignals:
    keywords = _message_keywords(text)
    if not keywords:
        return HistorySignals()
    scope = history_scope_for_user(user_id)
    try:
        with db_ro() as conn:
            scope_sql = ""
            params: list[object] = [user_id]
            if table_has_column("messages", "scope"):
                scope_sql = "AND COALESCE(scope, 'standard') = ?"
                params.append(scope)
            rows = conn.execute(
                f"""
                SELECT content
                FROM messages
                WHERE user_id = ?
                  AND role = 'user'
                  AND content <> ''
                  {scope_sql}
                ORDER BY id DESC
                LIMIT 80
                """,
                tuple(params),
            ).fetchall()
            profile_rows = conn.execute(
                """
                SELECT key, value
                FROM profile_context
                WHERE user_id = ?
                  AND key IN (?, ?, ?)
                """,
                (user_id, *_PROFILE_SIGNAL_KEYS),
            ).fetchall()
    except Exception:
        return HistorySignals()

    repeated_hits = 0
    for row in rows:
        overlap = keywords & _message_keywords(str(row["content"] or ""))
        if len(overlap) >= 2:
            repeated_hits += 1

    profile_hits = 0
    for row in profile_rows:
        lowered = str(row["value"] or "").lower()
        if any(keyword in lowered for keyword in keywords):
            profile_hits += 1

    lowered_text = text.lower()
    importance = 0.0
    if any(term in lowered_text for term in _IMPORTANCE_TERMS):
        importance += 0.35
    if any(keyword in lowered_text for keyword in _EVENT_KEYWORDS):
        importance += 0.15
    if repeated_hits >= 2:
        importance += min(0.3, 0.1 * repeated_hits)
    if profile_hits:
        importance += min(0.2, 0.1 * profile_hits)
    if len(text) >= 180:
        importance += 0.05

    return HistorySignals(
        repeated_hits=repeated_hits,
        profile_hits=profile_hits,
        importance_score=min(1.0, importance),
        matched_keywords=tuple(sorted(keywords)[:8]),
    )


def _decide_followup(
    text: str,
    reference_local: datetime,
    *,
    user_id: int,
    sentiment: SentimentSignals,
    history: HistorySignals,
) -> FollowupDecision:
    lowered = text.lower()
    event_keyword = _matched_event_keyword(lowered)
    scheduled_event = event_keyword in _SCHEDULED_EVENT_KEYWORDS
    soft_event = event_keyword in _LOW_PRIORITY_EVENT_KEYWORDS
    future_event = bool(event_keyword and _has_future_anchor(lowered))
    positive_hits = sum(
        1 for token in ("excited", "thrilled", "happy", "relieved", "can’t wait", "can't wait") if token in lowered
    )
    negative_hits = sum(
        1 for token in _DISTRESS_TERMS if token in lowered
    ) + sum(1 for token in _LOW_ENERGY_TERMS if token in lowered)
    high_hits = sum(1 for token in _HIGH_ENERGY_TERMS if token in lowered)
    sentiment_hits = _sentiment_marker_hits(sentiment)
    strong_message_sentiment = len(sentiment_hits) >= 3
    meaningful_message_sentiment = len(sentiment_hits) >= 2

    energy = 5 + (2 * high_hits) + min(2, text.count("!")) - min(3, len(_LOW_ENERGY_TERMS.intersection(set(lowered.split()))))
    if any(token in lowered for token in ("barely", "can’t move", "can't move", "dead tired", "wiped out")):
        energy -= 2
    if any(token in lowered for token in ("shaking", "freaking out", "panicking")):
        energy += 2
    if sentiment.arousal is not None:
        sentiment_energy = max(0, min(10, int(round(sentiment.arousal * 10))))
        energy = int(round((energy * 0.6) + (sentiment_energy * 0.4)))
    energy = max(0, min(10, energy))

    valence = 0.0
    if positive_hits:
        valence += min(0.9, 0.25 * positive_hits)
    if negative_hits:
        valence -= min(0.95, 0.22 * negative_hits)
    if any(token in lowered for token in ("sick", "ill", "pain", "fever")):
        valence = min(valence, -0.55)
    if sentiment.valence is not None:
        valence = (valence * 0.55) + (sentiment.valence * 0.45)

    event_due_local = _extract_event_due_local(
        lowered,
        reference_local,
        event_keyword=event_keyword,
    )
    explicit_followup = bool(_EXPLICIT_FOLLOWUP_RE.search(lowered))
    strong_lexical_distress = negative_hits >= 2 or any(
        phrase in lowered for phrase in _STRONG_DISTRESS_PHRASES
    )
    health_distress = any(
        token in lowered
        for token in (
            "sick",
            "ill",
            "pain",
            "hurts",
            "hurting",
            "fever",
            "migraine",
            "wiped out",
        )
    )
    relationship_distress = any(
        token in lowered for token in ("breakup", "grief", "funeral", "heartbroken")
    )
    lexical_distress_now = negative_hits > 0 or any(
        token in lowered for token in ("sick", "ill", "hurts", "crying", "spiral", "spiraling")
    )
    message_distress_now = False
    if sentiment.source == "message":
        if sentiment.emotion_label in {"sadness", "fear", "anger", "disgust"} and (
            sentiment.confidence is None or sentiment.confidence >= 0.55
        ):
            message_distress_now = True
        if (
            sentiment.dominance is not None
            and sentiment.valence is not None
            and sentiment.dominance <= 0.35
            and sentiment.valence <= -0.25
        ):
            message_distress_now = True
    distress_now = lexical_distress_now or message_distress_now
    positive_event = future_event and bool(
        event_keyword and event_keyword in _POSITIVE_EVENT_KEYWORDS
    )
    importance_score = history.importance_score
    should_create_non_event = (
        explicit_followup
        or strong_message_sentiment
        or (
            distress_now
            and (
                relationship_distress
                or health_distress
                or (
                    meaningful_message_sentiment
                    and (importance_score >= 0.35 or history.repeated_hits >= 1)
                )
                or (
                    strong_lexical_distress
                    and (importance_score >= 0.45 or history.repeated_hits >= 2)
                )
                or (health_distress and energy <= 3)
            )
        )
    )
    should_create_event = future_event and (
        explicit_followup
        or strong_message_sentiment
        or (
            scheduled_event
            and (
                distress_now
                or (positive_event and (positive_hits > 0 or importance_score >= 0.35))
                or meaningful_message_sentiment
                or importance_score >= 0.35
            )
        )
        or (
            soft_event
            and (
                strong_lexical_distress
                or strong_message_sentiment
                or importance_score >= 0.75
            )
        )
    )
    should_create = should_create_event or should_create_non_event

    if not should_create:
        return FollowupDecision(
            False,
            "",
            "none",
            "",
            energy,
            valence,
            None,
            None,
            event_due_local,
            importance_score=importance_score,
            arousal_score=sentiment.arousal,
            dominance_score=sentiment.dominance,
            emotion_label=sentiment.emotion_label,
            sentiment_source=sentiment.source,
        )

    followup_kind = "event_followup" if future_event else "emotional_checkin"
    trigger_label = (
        "explicit_followup"
        if explicit_followup
        else "future_event"
        if future_event
        else "emotional_intensity"
    )
    reason_text = _reason_text(
        lowered,
        positive_event=positive_event,
        distress_now=distress_now,
        future_event=future_event,
    )
    if future_event and event_due_local is None:
        return FollowupDecision(
            False,
            reason_text,
            followup_kind,
            trigger_label,
            energy,
            valence,
            None,
            None,
            None,
            importance_score=importance_score,
            arousal_score=sentiment.arousal,
            dominance_score=sentiment.dominance,
            emotion_label=sentiment.emotion_label,
            sentiment_source=sentiment.source,
        )
    next_run_local, time_of_day = _choose_followup_time(
        reference_local=reference_local,
        user_id=user_id,
        event_due_local=event_due_local if future_event else None,
        followup_kind=followup_kind,
        distress_now=distress_now,
        energy_score=energy,
        valence_score=valence,
    )
    return FollowupDecision(
        True,
        reason_text,
        followup_kind,
        trigger_label,
        energy,
        valence,
        time_of_day,
        next_run_local,
        event_due_local if future_event else None,
        importance_score=importance_score,
        arousal_score=sentiment.arousal,
        dominance_score=sentiment.dominance,
        emotion_label=sentiment.emotion_label,
        sentiment_source=sentiment.source,
    )


def _reason_text(
    lowered: str,
    *,
    positive_event: bool,
    distress_now: bool,
    future_event: bool,
) -> str:
    for keyword, reason in _EVENT_KEYWORDS.items():
        if keyword in lowered:
            return reason
    if any(token in lowered for token in ("sick", "ill", "fever", "migraine", "hurt", "hurting")):
        return "check how they're feeling"
    if any(token in lowered for token in ("breakup", "grief", "funeral", "heartbroken")):
        return "check how they're holding up"
    if distress_now:
        return "check how they're doing"
    if positive_event or future_event:
        return "check how it went"
    return "check in after an emotionally intense message"


def _clarification_text(lowered: str) -> str:
    label = "that event"
    for keyword in _EVENT_KEYWORDS:
        if keyword in lowered:
            label = f"the {keyword}"
            break
    return (
        f"I want to check in after {label}, but I don’t know when it’ll be over yet. "
        "Roughly when do you expect it to wrap up? A time or part of day is enough."
    )


def _extract_event_due_local(
    lowered: str,
    reference_local: datetime,
    *,
    event_keyword: str | None = None,
) -> datetime | None:
    if match := _IN_HOURS_RE.search(lowered):
        return reference_local + timedelta(hours=int(match.group(1)))
    if match := _IN_DAYS_RE.search(lowered):
        return reference_local + timedelta(days=int(match.group(1)))
    if match := _IN_WEEKS_RE.search(lowered):
        return reference_local + timedelta(weeks=int(match.group(1)))

    base_day = reference_local.date()
    default_hour = _default_event_hour(event_keyword, lowered)
    if "tomorrow" in lowered:
        base_day = base_day + timedelta(days=1)
        if not any(token in lowered for token in _TIME_OF_DAY_HINTS) and not _NUMERIC_TIME_RE.search(lowered):
            return datetime.combine(base_day, time(hour=default_hour, minute=0))
    elif "next week" in lowered:
        target_day = (reference_local + timedelta(days=7)).date()
        if not any(token in lowered for token in _TIME_OF_DAY_HINTS) and not _NUMERIC_TIME_RE.search(lowered):
            return datetime.combine(target_day, time(hour=default_hour, minute=0))
        base_day = target_day
    elif "tonight" in lowered:
        base_day = reference_local.date()
        if not any(token in lowered for token in _TIME_OF_DAY_HINTS) and not _NUMERIC_TIME_RE.search(lowered):
            return datetime.combine(base_day, time(hour=max(default_hour, 19), minute=0))
    elif not any(token in lowered for token in _TIME_OF_DAY_HINTS) and not _NUMERIC_TIME_RE.search(lowered):
        return None

    hour: int | None = None
    minute = 0
    if match := _NUMERIC_TIME_RE.search(lowered):
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    elif "morning" in lowered:
        hour = 9
    elif "afternoon" in lowered:
        hour = 14
    elif "evening" in lowered:
        hour = 19
    elif "night" in lowered or "tonight" in lowered:
        hour = 21

    if hour is None:
        return None
    return datetime.combine(base_day, time(hour=hour, minute=minute))


def _choose_followup_time(
    *,
    reference_local: datetime,
    user_id: int,
    event_due_local: datetime | None,
    followup_kind: str,
    distress_now: bool,
    energy_score: int,
    valence_score: float,
) -> tuple[datetime, str]:
    sleep_window = _resolve_sleep_window(user_id)
    current_block_idx = _BLOCK_ORDER.index(_block_for_hour(reference_local.hour))
    if event_due_local is not None:
        earliest = event_due_local + timedelta(hours=1)
        latest = None
    elif followup_kind == "emotional_checkin":
        if distress_now and (valence_score <= -0.55 or energy_score <= 3):
            earliest = reference_local + timedelta(days=1)
            latest = reference_local + timedelta(days=2)
        elif distress_now:
            earliest = reference_local + timedelta(hours=18)
            latest = reference_local + timedelta(hours=36)
        else:
            earliest = reference_local + timedelta(hours=12)
            latest = reference_local + timedelta(hours=24)
    else:
        earliest = reference_local + timedelta(hours=8)
        latest = reference_local + timedelta(hours=24)

    for relaxed in (False, True):
        for step in range(2, 10):
            absolute_idx = current_block_idx + step
            block = _BLOCK_ORDER[absolute_idx % len(_BLOCK_ORDER)]
            day_shift = absolute_idx // len(_BLOCK_ORDER)
            window = _available_window_for_block(
                base_date=(reference_local.date() + timedelta(days=day_shift)),
                block=block,
                sleep_window=sleep_window,
            )
            if not window:
                continue
            start, end = window
            start = max(start, earliest)
            if latest and not relaxed:
                end = min(end, latest)
            if end <= start:
                continue
            if (end - start) < timedelta(minutes=_MIN_BLOCK_WINDOW_MINUTES):
                continue
            return _random_time_between(start, end), block

    fallback = earliest.replace(second=0, microsecond=0)
    if sleep_window:
        fallback = _normalize_out_of_sleep(fallback, sleep_window)
    return fallback, _block_for_hour(fallback.hour)


def _available_window_for_block(
    *,
    base_date,
    block: str,
    sleep_window: tuple[tuple[int, int], tuple[int, int]] | None,
) -> tuple[datetime, datetime] | None:
    start_hour, end_hour = _BLOCK_WINDOWS[block]
    start = datetime.combine(base_date, time(hour=start_hour))
    if end_hour == 24:
        end = datetime.combine(base_date + timedelta(days=1), time.min)
    else:
        end = datetime.combine(base_date, time(hour=end_hour))

    if not sleep_window:
        return start, end

    adjusted_start = _normalize_out_of_sleep(start, sleep_window, time_of_day=block)
    if adjusted_start >= end:
        return None

    sleep_start = _next_sleep_start_after(adjusted_start, sleep_window[0])
    adjusted_end = min(end, sleep_start)
    if adjusted_end <= adjusted_start:
        return None
    return adjusted_start, adjusted_end


def _random_time_between(start: datetime, end: datetime) -> datetime:
    if end <= start:
        return start.replace(second=0, microsecond=0)
    delta_seconds = int((end - start).total_seconds())
    jitter = random.randint(0, max(0, delta_seconds))
    return (start + timedelta(seconds=jitter)).replace(second=0, microsecond=0)


def _block_for_hour(hour: int) -> str:
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 24:
        return "evening"
    return "night"


def _resolve_sleep_window(
    user_id: int,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    from app.domain.reminders.timezone import resolve_user_sleep_window

    return resolve_user_sleep_window(user_id)


def _normalize_out_of_sleep(
    candidate: datetime,
    sleep_window: tuple[tuple[int, int], tuple[int, int]],
    *,
    time_of_day: str | None = None,
) -> datetime:
    bedtime, wake_time = sleep_window
    adjusted = candidate.replace(second=0, microsecond=0)
    for _ in range(4):
        if not _is_within_sleep_window(adjusted, bedtime, wake_time):
            break
        adjusted = _next_awake_time(adjusted, bedtime, wake_time, time_of_day=time_of_day)
    return adjusted


def _is_within_sleep_window(
    candidate_local: datetime,
    bedtime: tuple[int, int],
    wake_time: tuple[int, int],
) -> bool:
    minutes = candidate_local.hour * 60 + candidate_local.minute
    bedtime_minutes = bedtime[0] * 60 + bedtime[1]
    wake_minutes = wake_time[0] * 60 + wake_time[1]
    if bedtime_minutes == wake_minutes:
        return False
    if bedtime_minutes < wake_minutes:
        return bedtime_minutes <= minutes < wake_minutes
    return minutes >= bedtime_minutes or minutes < wake_minutes


def _next_awake_time(
    candidate_local: datetime,
    bedtime: tuple[int, int],
    wake_time: tuple[int, int],
    *,
    time_of_day: str | None = None,
) -> datetime:
    wake_dt = candidate_local.replace(
        hour=wake_time[0],
        minute=wake_time[1],
        second=0,
        microsecond=0,
    )
    bedtime_minutes = bedtime[0] * 60 + bedtime[1]
    wake_minutes = wake_time[0] * 60 + wake_time[1]
    candidate_minutes = candidate_local.hour * 60 + candidate_local.minute
    if bedtime_minutes > wake_minutes:
        if candidate_minutes >= bedtime_minutes:
            wake_dt += timedelta(days=1)
    elif candidate_minutes >= wake_minutes:
        wake_dt += timedelta(days=1)

    adjusted = wake_dt + timedelta(minutes=60)
    if str(time_of_day or "").strip().lower() == "afternoon" and adjusted.hour < 13:
        adjusted = adjusted.replace(hour=13, minute=0)
    return adjusted


def _next_sleep_start_after(
    dt_value: datetime, bedtime: tuple[int, int]
) -> datetime:
    sleep_start = dt_value.replace(
        hour=bedtime[0],
        minute=bedtime[1],
        second=0,
        microsecond=0,
    )
    if sleep_start <= dt_value:
        sleep_start += timedelta(days=1)
    return sleep_start


def _load_pending_clarification(user_id: int) -> dict[str, Any] | None:
    try:
        with db_ro() as conn:
            row = conn.execute(
                """
                SELECT value
                FROM profile_context
                WHERE user_id = ?
                  AND key = ?
                """,
                (user_id, _FOLLOWUP_CLARIFICATION_KEY),
            ).fetchone()
    except Exception:
        return None
    if not row or not row["value"]:
        return None
    try:
        payload = json.loads(row["value"])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _store_pending_clarification(user_id: int, payload: dict[str, Any]) -> None:
    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO profile_context (user_id, key, value, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, key)
            DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, _FOLLOWUP_CLARIFICATION_KEY, json.dumps(payload)),
        )


def _clear_pending_clarification(user_id: int) -> None:
    with db_rw() as conn:
        conn.execute(
            "DELETE FROM profile_context WHERE user_id = ? AND key = ?",
            (user_id, _FOLLOWUP_CLARIFICATION_KEY),
        )


def _send_followup_message(user_id: int, text: str) -> None:
    if not text:
        return
    try:
        with db_ro() as conn:
            row = conn.execute(
                "SELECT telegram_user_id FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
    except Exception:
        return
    if not row or row["telegram_user_id"] is None:
        return
    from app.core.events import event_bus
    from app.domain import events

    event_bus.publish(
        events.EVENT_SEND_REPLY,
        {
            "user_id": str(user_id),
            "chat_id": int(row["telegram_user_id"]),
            "text": text,
        },
    )


def _maybe_resolve_pending_clarification(
    *,
    user_id: int,
    session_id: int | None,
    message_id: int | None,
    text: str,
    reference_local: datetime,
) -> str | None:
    pending = _load_pending_clarification(user_id)
    if not pending:
        return None

    lowered = text.lower()
    if any(token in lowered for token in ("never mind", "nevermind", "skip it", "cancel that", "don't set it")):
        _clear_pending_clarification(user_id)
        return None

    event_due_local = _extract_event_due_local(lowered, reference_local)
    if event_due_local is None:
        return None

    next_run_local, time_of_day = _choose_followup_time(
        reference_local=reference_local,
        user_id=user_id,
        event_due_local=event_due_local,
        followup_kind=str(pending.get("followup_kind") or "event_followup"),
        distress_now=False,
        energy_score=int(pending.get("energy_score") or 5),
        valence_score=float(pending.get("valence_score") or 0.0),
    )
    metadata = {
        "origin_message_id": pending.get("origin_message_id"),
        "origin_session_id": pending.get("origin_session_id") or session_id,
        "origin_excerpt": pending.get("origin_excerpt"),
        "origin_timestamp": pending.get("origin_timestamp"),
        "event_due_local": event_due_local.isoformat(timespec="minutes"),
        "followup_kind": pending.get("followup_kind") or "event_followup",
        "trigger_label": pending.get("trigger_label") or "future_event",
        "scope": pending.get("scope") or history_scope_for_user(user_id),
        "energy_score": pending.get("energy_score"),
        "valence_score": pending.get("valence_score"),
        "importance_score": pending.get("importance_score"),
        "time_of_day": time_of_day,
        "respect_sleep_window": True,
        "allow_jitter": False,
        "clarified_by_message_id": message_id,
        "clarified_event_timing": text[:120],
    }
    reminder_service = container.resolve("reminder_service")
    reminder_id = reminder_service.create_custom_reminder(
        user_id=str(user_id),
        text=str(pending.get("reason_text") or "check how it went"),
        next_run_at=_to_operator_time(user_id, next_run_local),
        frequency="once",
        time_of_day=time_of_day,
        allow_jitter=False,
        base_hour=next_run_local.hour,
        base_minute=next_run_local.minute,
        specific_hour=next_run_local.hour,
        specific_minute=next_run_local.minute,
        metadata=metadata,
    )
    _clear_pending_clarification(user_id)
    _send_followup_message(
        user_id,
        "Got it. I’ll check in after that.",
    )
    logger.info(
        "[REMINDER-TELEMETRY] followup_clarification_resolved user_id=%s reminder_id=%s origin_message_id=%s clarified_by=%s",
        user_id,
        reminder_id,
        pending.get("origin_message_id"),
        message_id,
    )
    return reminder_id


def _has_existing_followup_for_origin_message(
    *, user_id: int, origin_message_id: int
) -> bool:
    try:
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT payload
                FROM reminders
                WHERE user_id = ?
                  AND enabled = 1
                ORDER BY id DESC
                LIMIT 100
                """,
                (user_id,),
            ).fetchall()
    except Exception:
        return False
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        if payload.get("origin_message_id") == origin_message_id:
            return True
        origin_ids = payload.get("origin_message_ids")
        if isinstance(origin_ids, list) and origin_message_id in origin_ids:
            return True
    pending = _load_pending_clarification(user_id)
    return bool(pending and pending.get("origin_message_id") == origin_message_id)


def _message_keywords(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9']+", (text or "").lower())
        if len(token) >= 4 and token not in _STOPWORDS
    }
