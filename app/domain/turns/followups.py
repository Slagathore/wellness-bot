"""Warm-path follow-up work for turn orchestration."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.db import db_rw
from app.orchestrator.context_builder import invalidate_profile_context_cache
from app.utils.web_search import enhance_response_with_search_async

from .audit import append_turn_route, create_turn_audit, update_turn_followup
from .models import TurnPlan, coerce_turn_plan

logger = logging.getLogger(__name__)
_MEDIA_TAG_RE = re.compile(r"\[(?:GENERATE_IMAGE|GENERATE_VIDEO):[^\]]*\]", re.IGNORECASE | re.DOTALL)
_REMINDER_TAG_RE = re.compile(r"\[SET_REMINDER:[^\]]*\]", re.IGNORECASE | re.DOTALL)
_SENTINEL_RE = re.compile(r"(?:\*\*)?END_END_END(?:\*\*)?", re.IGNORECASE)
_UNCERTAINTY_HINTS = (
    "i'm not sure",
    "i am not sure",
    "i think",
    "maybe",
    "might",
    "could be",
    "not fully sure",
    "double-check",
)


def _coerce_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


@dataclass(slots=True)
class TurnFollowupPayload:
    user_id: int
    session_id: int | None
    chat_id: int | None
    correlation_id: str | None
    user_message_id: int | None
    assistant_message_id: int | None
    audit_id: int | None
    live_search_mode: str | None
    user_text: str
    assistant_text: str
    plan: TurnPlan | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TurnFollowupPayload":
        plan = coerce_turn_plan(payload.get("turn_plan"))
        return cls(
            user_id=_coerce_int(payload.get("user_id"), 0) or 0,
            session_id=_coerce_int(payload.get("session_id")),
            chat_id=_coerce_int(payload.get("chat_id")),
            correlation_id=payload.get("correlation_id"),
            user_message_id=_coerce_int(payload.get("user_message_id")),
            assistant_message_id=_coerce_int(payload.get("assistant_message_id")),
            audit_id=_coerce_int(payload.get("audit_id")),
            live_search_mode=str(payload["live_search_mode"]) if payload.get("live_search_mode") is not None else None,
            user_text=str(payload.get("user_text") or ""),
            assistant_text=str(payload.get("assistant_text") or ""),
            plan=plan,
        )


class TurnFollowupService:
    """Executes deferred work (search, profile candidates, audit updates)."""

    def handle_followup(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = TurnFollowupPayload.from_payload(payload)
        result: dict[str, Any] = {
            "audit_id": None,
            "profile_candidates": 0,
            "profile_promoted": 0,
            "search_followup_sent": False,
            "assistant_reply_review": {},
        }
        plan = data.plan
        if plan is None:
            return result

        if data.audit_id is not None:
            append_turn_route(
                audit_id=data.audit_id,
                stage="turn_followup.received",
                status="followup_running",
            )
        try:
            audit_id = data.audit_id
            if audit_id is None:
                audit_id = create_turn_audit(
                    user_id=data.user_id,
                    session_id=data.session_id,
                    user_message_id=data.user_message_id,
                    assistant_message_id=data.assistant_message_id,
                    correlation_id=data.correlation_id,
                    user_text=data.user_text,
                    assistant_text=data.assistant_text,
                    plan=plan,
                    route_trace=[],
                    status="followup_running",
                )
            result["audit_id"] = audit_id
        except Exception as exc:  # noqa: BLE001
            logger.debug("Turn audit write failed: %s", exc)

        if plan.profile_candidates:
            try:
                profile_result = _persist_profile_candidates(
                    user_id=data.user_id,
                    session_id=data.session_id,
                    message_id=data.user_message_id,
                    correlation_id=data.correlation_id,
                    candidates=plan.profile_candidates,
                )
                result.update(profile_result)
                audit_id = _coerce_int(result.get("audit_id"))
                if audit_id is not None:
                    append_turn_route(
                        audit_id=audit_id,
                        stage="turn_followup.profile_candidates_persisted",
                        profile_candidates=profile_result.get("profile_candidates"),
                        profile_promoted=profile_result.get("profile_promoted"),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Profile candidate persistence failed: %s", exc)

        return result

    async def handle_followup_async(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = TurnFollowupPayload.from_payload(payload)
        result = self.handle_followup(payload)
        plan = data.plan
        if plan is None:
            return result

        search_followup: str | None = None
        should_search_followup = (
            data.chat_id is not None
            and (
                plan.needs_live_search_followup
                or (plan.needs_live_search_now and data.live_search_mode != "now")
            )
        )
        if should_search_followup:
            followup = await enhance_response_with_search_async(
                data.user_text,
                model=None,
                use_llm_decision=False,
                personality=None,
            )
            if followup:
                result["search_followup_text"] = followup
                search_followup = followup
                audit_id = _coerce_int(result.get("audit_id"))
                if audit_id is not None:
                    append_turn_route(
                        audit_id=audit_id,
                        stage="turn_followup.web_search_completed",
                    )
        review = _review_assistant_reply(
            assistant_text=data.assistant_text,
            plan=plan,
            search_followup_text=search_followup,
        )
        result["assistant_reply_review"] = review
        audit_id = _coerce_int(result.get("audit_id"))
        if audit_id is not None:
            append_turn_route(
                audit_id=audit_id,
                stage="turn_followup.assistant_reply_reviewed",
                needs_followup=bool(review.get("needs_followup")),
                is_correction=bool(review.get("is_correction")),
            )
        if review.get("needs_followup") and review.get("followup_message_text"):
            result["search_followup_sent"] = True
            result["followup_message_text"] = review["followup_message_text"]
            if audit_id is not None:
                append_turn_route(
                    audit_id=audit_id,
                    stage="turn_followup.assistant_reply_reconciled",
                    correction=bool(review.get("is_correction")),
                )
        if audit_id is not None:
            try:
                update_turn_followup(
                    audit_id=audit_id,
                    followup_json=result,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to update turn audit followup: %s", exc)
        return result


def _persist_profile_candidates(
    *,
    user_id: int,
    session_id: int | None,
    message_id: int | None,
    correlation_id: str | None,
    candidates: list,
) -> dict[str, int]:
    created = 0
    promoted = 0
    with db_rw() as conn:
        for candidate in candidates:
            created += 1
            conn.execute(
                """
                INSERT INTO profile_fact_candidates (
                    user_id,
                    session_id,
                    message_id,
                    correlation_id,
                    key,
                    value,
                    confidence,
                    source,
                    reason,
                    contradiction,
                    existing_value,
                    status,
                    metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_id,
                    message_id,
                    correlation_id,
                    candidate.key,
                    candidate.value,
                    float(candidate.confidence),
                    candidate.source,
                    candidate.reason,
                    1 if candidate.contradiction else 0,
                    candidate.existing_value,
                    "pending",
                    json.dumps(candidate.metadata, ensure_ascii=True),
                ),
            )
            if candidate.confidence >= 0.85 and not candidate.contradiction:
                conn.execute(
                    """
                    INSERT INTO profile_context (user_id, key, value, created_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                    """,
                    (user_id, candidate.key, candidate.value),
                )
                promoted += 1
    if promoted:
        try:
            invalidate_profile_context_cache(user_id)
        except Exception:
            pass
    return {"profile_candidates": created, "profile_promoted": promoted}


def _review_assistant_reply(
    *,
    assistant_text: str,
    plan: TurnPlan,
    search_followup_text: str | None,
) -> dict[str, Any]:
    lowered = (assistant_text or "").lower()
    leaked_tags = bool(_MEDIA_TAG_RE.search(assistant_text) or _REMINDER_TAG_RE.search(assistant_text))
    leaked_sentinel = bool(_SENTINEL_RE.search(assistant_text))
    uncertain = any(token in lowered for token in _UNCERTAINTY_HINTS)
    needs_correction = leaked_tags or leaked_sentinel
    followup_message_text: str | None = None
    is_correction = False

    if search_followup_text:
        if plan.needs_live_search_followup or uncertain:
            prefix = (
                "Quick correction after checking live info:\n\n"
                if uncertain and plan.needs_live_search_now
                else "I checked the live info and here's what I found:\n\n"
            )
            followup_message_text = prefix + search_followup_text
            is_correction = uncertain and plan.needs_live_search_now
    if needs_correction and not followup_message_text:
        cleaned = _SENTINEL_RE.sub("", assistant_text or "")
        cleaned = _MEDIA_TAG_RE.sub("", cleaned)
        cleaned = _REMINDER_TAG_RE.sub("", cleaned).strip()
        if cleaned:
            followup_message_text = f"Quick correction:\n\n{cleaned}"
            is_correction = True

    return {
        "leaked_tags": leaked_tags,
        "leaked_sentinel": leaked_sentinel,
        "uncertain_current_info": uncertain and plan.needs_live_search_now,
        "needs_followup": bool(followup_message_text),
        "is_correction": is_correction,
        "followup_message_text": followup_message_text,
    }
