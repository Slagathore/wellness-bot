"""
Modular conversation pipeline wrapping orchestrator functions with async hot-path support.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from app.config import settings
from app.db import db_ro, db_rw
from app.feature_flags import enabled as feature_enabled
from app.features.token_budget.dynamic import resolve_context_window
from app.history_scope import history_scope_for_personality
from app.orchestrator.context_builder import (
    recent_messages, retrieved_memories_controlled_async, rolling_summary,
    should_create_summary, user_profile_context)
from app.orchestrator.persona_runtime import (build_user_persona_runtime,
                                              filter_prompt_history_for_personality,
                                              filter_prompt_memories_for_personality,
                                              get_user_personality_name,
                                              resolve_user_model)
from app.orchestrator.prompt_builder import (RESPONSE_COMPLETION_SENTINEL,
                                             build_prompt_with_system_prompt)
from app.personality.modes import is_custom_character
from app.rag.service import format_citations, get_retriever
from app.utils.fs import pending_server_events_for_session
from app.utils.ollama import chat, chat_async
from app.utils.text_cleaner import clean_for_llm
from app.domain.turns.models import TurnPlan
from app.utils.web_search import search_context, search_context_async

logger = logging.getLogger(__name__)

# Rough token estimation: ~3 characters per token for English text.
# Most modern tokenizers average 3-3.5 chars/token; using 3 to avoid
# underestimating prompt size which causes the model to run out of room.
_CHARS_PER_TOKEN = 3

# Pre-compiled pattern to detect and strip the end-of-response sentinel.
_SENTINEL_TEXT = "END_END_END"
_SENTINEL_END_RE = re.compile(r"\s*(?:\*\*)?" + re.escape(_SENTINEL_TEXT) + r"(?:\*\*)?\s*$")
_SENTINEL_ANY_RE = re.compile(r"(?:\*\*)?" + re.escape(_SENTINEL_TEXT) + r"(?:\*\*)?")


def _strip_sentinel(text: str) -> tuple[str, bool]:
    """Remove the sentinel marker from the end of the LLM output.

    Returns (cleaned_text, sentinel_was_found).
    """
    if _SENTINEL_END_RE.search(text):
        cleaned = _SENTINEL_END_RE.sub("", text)
        return cleaned, True
    return text, False


def _remove_leaked_sentinels(text: str) -> str:
    """Remove stray sentinel tokens that leaked into visible text."""
    cleaned = _SENTINEL_ANY_RE.sub("", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _response_finish_meta(response: dict[str, Any] | None) -> tuple[str, str]:
    """Extract provider stop metadata from a normalized response dict."""
    if not isinstance(response, dict):
        return "", ""
    raw = response.get("raw", {})
    done_reason = str(raw.get("done_reason", "") or "")
    finish_reason = ""
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        finish_reason = str((choices[0] or {}).get("finish_reason", "") or "")
    return done_reason, finish_reason


def _log_completion_telemetry(
    *,
    user_id: int,
    model: str | None,
    text: str,
    sentinel_found: bool,
    truncated: bool,
    empty_response_retries: int,
    continuation_attempts: int,
    response: dict[str, Any] | None,
    path: str,
) -> None:
    """Emit a compact completion log line for smoke-testing chat flows."""
    done_reason, finish_reason = _response_finish_meta(response)
    logger.info(
        "[LLM-TELEMETRY] path=%s user=%s model=%s text_len=%d sentinel_found=%s "
        "truncated=%s empty=%s empty_retries=%d continuations=%d done_reason=%r finish_reason=%r",
        path,
        user_id,
        model,
        len(text),
        sentinel_found,
        truncated,
        not bool(text.strip()),
        empty_response_retries,
        continuation_attempts,
        done_reason,
        finish_reason,
    )


def _estimate_prompt_tokens(prompt: list[dict[str, str]]) -> int:
    """Fast token estimate without a real tokenizer (good enough for budgeting)."""
    return sum(len(m.get("content", "")) for m in prompt) // _CHARS_PER_TOKEN


def _trim_prompt_to_budget(
    prompt: list[dict[str, str]], num_ctx: int, num_predict: int
) -> list[dict[str, str]]:
    """Drop older conversation turns from the middle of the prompt if it
    exceeds the context window minus the response budget.

    Preserves: system messages at the start, the final user message at the end.
    Trims: conversation turns (user/assistant pairs) from oldest first.
    """
    budget = num_ctx - num_predict - 256  # 256 token safety margin
    if budget <= 0:
        budget = num_ctx // 2

    est = _estimate_prompt_tokens(prompt)
    if est <= budget:
        return prompt

    # Split prompt into: [system messages] + [conversation turns] + [final user msg]
    system_msgs: list[dict[str, str]] = []
    conv_msgs: list[dict[str, str]] = []
    final_user: dict[str, str] | None = None

    for i, msg in enumerate(prompt):
        if msg["role"] == "system":
            system_msgs.append(msg)
        elif i == len(prompt) - 1 and msg["role"] == "user":
            final_user = msg
        else:
            conv_msgs.append(msg)

    # Token costs for the parts we can't trim
    fixed_tokens = _estimate_prompt_tokens(system_msgs)
    if final_user:
        fixed_tokens += len(final_user.get("content", "")) // _CHARS_PER_TOKEN

    remaining_budget = budget - fixed_tokens
    if remaining_budget <= 0:
        # Even system + user message exceeds budget; drop all conv history
        result = system_msgs[:]
        if final_user:
            result.append(final_user)
        logger.warning(
            "[LLM] Context budget exhausted by system prompt alone "
            "(system=%d, budget=%d). Dropped all conversation history.",
            fixed_tokens,
            budget,
        )
        return result

    # Keep as many recent conversation messages as fit
    kept: list[dict[str, str]] = []
    tokens_used = 0
    for msg in reversed(conv_msgs):
        msg_tokens = len(msg.get("content", "")) // _CHARS_PER_TOKEN
        if tokens_used + msg_tokens > remaining_budget:
            break
        kept.append(msg)
        tokens_used += msg_tokens
    kept.reverse()

    dropped = len(conv_msgs) - len(kept)
    if dropped:
        logger.info(
            "[LLM] Trimmed %d old conversation messages to fit context window "
            "(est. %d -> %d tokens, budget=%d).",
            dropped,
            est,
            fixed_tokens + tokens_used + (len(final_user.get("content", "")) // _CHARS_PER_TOKEN if final_user else 0),
            budget,
        )

    result = system_msgs + kept
    if final_user:
        result.append(final_user)
    return result


# Regex to match [SET_REMINDER: reason="..." when="..."] tags in LLM output
_SET_REMINDER_RE = re.compile(
    r'\[SET_REMINDER:\s*reason=["\'](?P<reason>[^"\']+)["\']'
    r'\s+when=["\'](?P<when>[^"\']+)["\']\s*\]',
    re.IGNORECASE,
)


def _process_reminder_tags(text: str, user_id: int, *, allow_reminders: bool = True) -> str:
    """Extract [SET_REMINDER: ...] tags from LLM output, create reminders, and strip them."""
    matches = list(_SET_REMINDER_RE.finditer(text))
    if not matches:
        return text
    if not allow_reminders:
        cleaned = _SET_REMINDER_RE.sub("", text).strip()
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned

    for match in matches[:1]:  # max one reminder per response
        reason = match.group("reason").strip()
        when = match.group("when").strip()
        try:
            from app.core.container import container
            from app.domain.reminders.timezone import (user_now,
                                                       user_time_to_operator)

            reminder_service = container.resolve("reminder_service")
            # Parse relative to the USER's local time, then convert to operator
            # time for storage so the scanner fires at the right moment.
            user_local = _parse_when(when, user_now(user_id))
            next_run = user_time_to_operator(user_local, user_id)
            reminder_service.create_custom_reminder(
                user_id=str(user_id),
                text=reason,
                next_run_at=next_run,
                frequency="once",
                time_of_day=None,
            )
            logger.info(
                "Auto-created reminder for user %s: '%s' at %s (operator time)",
                user_id, reason, next_run.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to auto-create reminder from LLM tag for user %s: %s", user_id, exc)

    # Strip all SET_REMINDER tags from visible text
    cleaned = _SET_REMINDER_RE.sub("", text).strip()
    # Clean up any leftover blank lines
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned


def _parse_when(when_text: str, reference_time: Any) -> Any:
    """Parse natural language time relative to a reference (naive user-local time).

    Returns a naive datetime in the same timezone as reference_time.
    The caller is responsible for converting to operator time before storage.
    """
    from datetime import timedelta
    when_lower = when_text.lower().strip()

    # ------------------------------------------------------------------ #
    # Helper: extract a wall-clock time from text like "at 3pm",          #
    # "at 5:30", "at 15:00", "9am", "3:30 pm", etc.                      #
    # Returns (hour_24, minute) or None.                                   #
    # ------------------------------------------------------------------ #
    def _extract_clock_time(text: str):
        clock_re = re.search(
            r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
            text,
        )
        if not clock_re:
            # Also accept bare "9am" / "3pm" without preceding "at"
            clock_re = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
        if not clock_re:
            return None
        hour = int(clock_re.group(1))
        minute = int(clock_re.group(2) or 0)
        meridiem = clock_re.group(3)
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        # 24-hour bare values ("at 15:00") need no adjustment
        return hour, minute

    # ------------------------------------------------------------------ #
    # Named-period → default hour mapping (used as fallback when "at X"   #
    # is not present alongside a day anchor).                              #
    # ------------------------------------------------------------------ #
    _PERIOD_HOURS = {
        "morning": 9,
        "afternoon": 14,
        "evening": 20,
        "night": 20,
        "tonight": 20,
        "midnight": 0,
        "noon": 12,
        "lunchtime": 12,
    }

    def _period_hour(text: str) -> int | None:
        for kw, hr in _PERIOD_HOURS.items():
            if kw in text:
                return hr
        return None

    def _apply_clock_or_period(base_date, text: str):
        """Return base_date with hour/minute resolved from clock or named period."""
        clock = _extract_clock_time(text)
        if clock:
            return base_date.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        period = _period_hour(text)
        if period is not None:
            return base_date.replace(hour=period, minute=0, second=0, microsecond=0)
        return base_date.replace(hour=10, minute=0, second=0, microsecond=0)

    # ------------------------------------------------------------------ #
    # Resolve the target date/time                                         #
    # ------------------------------------------------------------------ #

    if "tomorrow" in when_lower:
        next_day = reference_time + timedelta(days=1)
        return _apply_clock_or_period(next_day, when_lower)

    # Relative duration: "in 3 hours", "in 2 days", "in 1 week"
    hour_match = re.search(r'(\d+)\s*hour', when_lower)
    if hour_match:
        return reference_time + timedelta(hours=int(hour_match.group(1)))

    day_match = re.search(r'(\d+)\s*day', when_lower)
    if day_match:
        return reference_time + timedelta(days=int(day_match.group(1)))

    week_match = re.search(r'(\d+)\s*week', when_lower)
    if week_match:
        return reference_time + timedelta(weeks=int(week_match.group(1)))

    if "next week" in when_lower:
        base = reference_time + timedelta(weeks=1)
        return _apply_clock_or_period(base, when_lower)

    if "couple" in when_lower or "few" in when_lower:
        if "hour" in when_lower:
            return reference_time + timedelta(hours=2)
        return reference_time + timedelta(days=2)

    # Named period without a day anchor ("tonight", "this evening", etc.)
    period_hr = _period_hour(when_lower)
    if period_hr is not None:
        clock = _extract_clock_time(when_lower)
        candidate = reference_time.replace(
            hour=clock[0] if clock else period_hr,
            minute=clock[1] if clock else 0,
            second=0,
            microsecond=0,
        )
        # If the period has already passed today, move to tomorrow.
        if candidate <= reference_time:
            candidate += timedelta(days=1)
        return candidate

    # Explicit wall-clock time with no other anchor ("at 3pm", "at 14:30")
    clock = _extract_clock_time(when_lower)
    if clock:
        candidate = reference_time.replace(
            hour=clock[0], minute=clock[1], second=0, microsecond=0
        )
        # If the time is already past today, schedule for tomorrow.
        if candidate <= reference_time:
            candidate += timedelta(days=1)
        return candidate

    # Default: 3 hours from now (organic follow-up reminders with vague "when")
    return reference_time + timedelta(hours=3)


def _route_entry(stage: str, *, elapsed_ms: float, **details: Any) -> dict[str, Any]:
    return {
        "stage": stage,
        "elapsed_ms": round(float(elapsed_ms), 1),
        "details": {k: v for k, v in details.items() if v is not None},
    }


def _turn_plan_prompt_block(turn_plan: TurnPlan | None) -> str:
    """Embed planner guidance into the primary system prompt.

    Gemini-class providers can effectively discard the persona when we append a
    second system message after the main persona prompt. Keeping turn-plan hints
    inside the first system message preserves prompt hierarchy.
    """

    if turn_plan is None:
        return ""

    planner_notes = [
        f"Turn plan intent: {turn_plan.primary_intent}",
        f"Sentiment priority: {turn_plan.sentiment_priority}",
    ]
    if turn_plan.needs_live_search_now:
        planner_notes.append("User asked for live info; answer with caution.")
    if turn_plan.allow_media_action:
        planner_notes.append("Media generation allowed this turn.")
    else:
        planner_notes.append("Do not emit media tags unless explicitly allowed.")
    if not turn_plan.allow_reminder_action:
        planner_notes.append("Do not emit reminder tags.")

    return "\n\nTURN_PLAN:\n" + "\n".join(planner_notes)


async def generate_response_async(
    user_id: int,
    session_id: int,
    user_text: str,
    model: str | None = None,
    turn_plan: TurnPlan | None = None,
) -> dict[str, Any]:
    """Produce assistant reply using async retrieval + async chat."""

    total_start = time.perf_counter()
    rag_ms = 0.0
    llm_ms = 0.0
    cfg = settings()
    routing_trace: list[dict[str, Any]] = [
        _route_entry("pipeline.started", elapsed_ms=0.0, session_id=session_id)
    ]

    personality_mode = get_user_personality_name(user_id)
    resolved_model = resolve_user_model(user_id, requested_model=model)
    num_ctx = resolve_context_window(model=resolved_model, personality=personality_mode)
    with db_rw() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET ctx_token_budget = ?
            WHERE id = ? AND (ctx_token_budget IS NULL OR ctx_token_budget != ?)
            """,
            (num_ctx, session_id, num_ctx),
        )

    summary_needed = should_create_summary(session_id)
    history_scope = history_scope_for_personality(personality_mode)
    no_profile = personality_mode in ("downbad", "roleplay") or is_custom_character(personality_mode)
    disable_memories = is_custom_character(personality_mode)

    recent = recent_messages(user_id, session_id, max_msgs=30)
    recent_for_prompt = [
        {**msg, "content": clean_for_llm(msg.get("content", ""))} for msg in recent
    ]
    recent_for_prompt, removed_history = filter_prompt_history_for_personality(
        personality_name=personality_mode,
        messages=recent_for_prompt,
    )

    memory_timing = {
        "lexical_ms": None,
        "memory_ms": None,
        "memory_mode": "disabled_for_character_mode" if disable_memories else None,
        "memory_count": 0,
        "memory_classifier_score": None,
    }
    if disable_memories:
        memories: list[dict[str, Any]] = []
    else:
        memories, memory_timing = await retrieved_memories_controlled_async(
            user_id=user_id,
            query_text=user_text,
            k=cfg.top_k_retrieval,
            scope_filter=history_scope,
        )
    memories, removed_memories = filter_prompt_memories_for_personality(
        personality_name=personality_mode,
        memories=memories,
    )
    routing_trace.append(
        _route_entry(
            "pipeline.context_loaded",
            elapsed_ms=(time.perf_counter() - total_start) * 1000,
            memory_count=memory_timing.get("memory_count"),
            memory_mode=memory_timing.get("memory_mode"),
            summary_needed=bool(summary_needed),
            filtered_refusal_history=removed_history,
            filtered_refusal_memories=removed_memories,
        )
    )
    summary = rolling_summary(session_id)
    profile_context = None if no_profile else user_profile_context(user_id)
    events = list(pending_server_events_for_session(user_id, session_id))
    live_search_context = ""
    if turn_plan:
        if turn_plan.needs_live_search_now and turn_plan.search_query:
            try:
                live_search_context = await search_context_async(
                    turn_plan.search_query,
                    max_results=3,
                    timeout=2,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Hot-path live search failed for user %s: %s", user_id, exc)
    persona_runtime = build_user_persona_runtime(
        user_id=user_id,
        profile_context=profile_context,
    )
    system_prompt = persona_runtime.system_prompt + _turn_plan_prompt_block(turn_plan)

    rag_context = ""
    rag_sources: list[str] = []
    try:
        retriever = get_retriever()
        if turn_plan is None:
            should_retrieve = retriever.should_retrieve(user_text)
        else:
            should_retrieve = bool(turn_plan.needs_rag)
        if should_retrieve:
            rag_start = time.perf_counter()
            try:
                retrieval = await asyncio.wait_for(
                    asyncio.to_thread(
                        retriever.retrieve,
                        user_text,
                        min(3, cfg.top_k_retrieval),
                    ),
                    timeout=2.5,
                )
            except asyncio.TimeoutError:
                logger.info("[RAG] Retrieval skipped for user %s due to timeout", user_id)
                retrieval = {}
            rag_context = retrieval.get("context", "") if retrieval else ""
            rag_sources = retrieval.get("sources", []) if retrieval else []
            rag_ms = (time.perf_counter() - rag_start) * 1000
            logger.info(
                "[RAG] Retrieved %s resources for user %s in %.0f ms",
                len(retrieval.get("resources", [])) if retrieval else 0,
                user_id,
                rag_ms,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RAG] Retrieval failed for user %s: %s", user_id, exc)
    routing_trace.append(
        _route_entry(
            "pipeline.rag_completed",
            elapsed_ms=(time.perf_counter() - total_start) * 1000,
            rag_used=bool(rag_context),
            rag_sources_count=len(rag_sources),
        )
    )

    prompt = build_prompt_with_system_prompt(
        system_prompt=system_prompt,
        session_summary=summary,
        retrieved_memories=memories,
        server_events=events,
        recent_messages=recent_for_prompt,
        user_message=clean_for_llm(user_text),
        rag_context="\n\n".join(
            part for part in (rag_context, live_search_context) if part
        ),
    )
    routing_trace.append(
        _route_entry(
            "pipeline.prompt_built",
            elapsed_ms=(time.perf_counter() - total_start) * 1000,
            prompt_messages=len(prompt),
        )
    )

    llm_opts = resolve_llm_options(user_id, persona_runtime.personality_config, personality_mode)
    llm_opts["num_ctx"] = num_ctx
    num_predict = int(llm_opts.get("num_predict", 4096))

    # Trim conversation history so the model always has room to respond.
    prompt = _trim_prompt_to_budget(prompt, num_ctx, num_predict)

    # Don't set explicit request_timeout in options — doing so disables retries
    # in the Ollama client.  The client already defaults to 120s with 3 attempts.

    llm_start = time.perf_counter()
    response = await chat_async(
        messages=prompt,
        model=resolved_model,
        options=llm_opts,
    )
    llm_ms = (time.perf_counter() - llm_start) * 1000
    empty_response_retries = 0
    continuation_attempts = 0

    # Some cloud providers occasionally report "stop" with an empty string.
    # Retry with an explicit nudge before surfacing a blank reply to the user.
    for empty_attempt in range(2):
        response_text = response.get("text", "") if isinstance(response, dict) else ""
        done_reason, finish_reason = _response_finish_meta(response if isinstance(response, dict) else None)
        if response_text.strip():
            break
        if done_reason not in {"stop", "length"} and finish_reason not in {"stop", "length"}:
            break
        empty_response_retries += 1
        logger.warning(
            "[LLM] Empty response with terminal stop for user %s model=%s "
            "(attempt %d, done_reason=%r, finish_reason=%r). Retrying with recovery prompt.",
            user_id,
            resolved_model,
            empty_attempt + 1,
            done_reason,
            finish_reason,
        )
        retry_prompt = list(prompt)
        retry_prompt.append(
            {
                "role": "user",
                "content": (
                    "Your previous reply was empty. Answer the user's last message now. "
                    "If you were interrupted, continue from where you left off. "
                    "Do not return an empty response."
                ),
            }
        )
        retry_start = time.perf_counter()
        response = await chat_async(
            messages=retry_prompt,
            model=resolved_model,
            options=llm_opts,
        )
        llm_ms += (time.perf_counter() - retry_start) * 1000

    # --- Sentinel-based completion detection ---
    # Check for the #### marker BEFORE API-level truncation signals so
    # we catch truncation even when the provider doesn't report it.
    sentinel_found = False
    if isinstance(response, dict) and response.get("text"):
        response["text"], sentinel_found = _strip_sentinel(response["text"])

    # Detect truncation from multiple signals:
    #  1. Sentinel missing = model didn't finish naturally
    #  2. Ollama local models: raw.done_reason == "length"
    #  3. Cloud models (OpenAI-compatible): choices[0].finish_reason == "length"
    truncated = False
    if isinstance(response, dict) and response.get("text") and not sentinel_found:
        # Non-empty response without the sentinel → almost certainly truncated
        truncated = True
        logger.warning(
            "[LLM] Sentinel marker missing — response likely truncated "
            "for user %s model=%s (text length=%d)",
            user_id, resolved_model, len(response.get("text", "")),
        )
    if isinstance(response, dict):
        raw = response.get("raw", {})
        # Ollama native signal
        done_reason = raw.get("done_reason", "")
        if done_reason == "length":
            truncated = True
        # Cloud / OpenAI-compatible signal
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            finish_reason = (choices[0] or {}).get("finish_reason", "")
            if finish_reason == "length":
                truncated = True
        if truncated:
            logger.warning(
                "[LLM] Response truncated for user %s "
                "model=%s num_predict=%s num_ctx=%s done_reason=%r finish_reason=%r. "
                "Consider increasing limits.",
                user_id,
                resolved_model,
                llm_opts.get("num_predict"),
                llm_opts.get("num_ctx"),
                done_reason,
                (choices[0] or {}).get("finish_reason", "") if isinstance(raw.get("choices"), list) and raw.get("choices") else "",
            )

    # --- Continuation on truncation (gated by feature flag) ---
    # When enabled, if the response was truncated, send a follow-up
    # "please continue" to get the rest of the response and stitch
    # the pieces together. Limited to 2 continuations max.
    if truncated and feature_enabled("llm_continuation_on_truncation") and isinstance(response, dict):
        partial_text = response.get("text", "")
        base_prompt = list(prompt)  # original prompt — do NOT mutate; rebuild each iteration
        for _cont_attempt in range(2):  # max 2 continuations
            continuation_attempts += 1
            # Rebuild from the base prompt each time so we never send duplicate assistant turns.
            continuation_prompt = base_prompt + [
                {"role": "assistant", "content": partial_text},
                {"role": "user", "content": "Please continue exactly where you left off."},
            ]
            try:
                cont_start = time.perf_counter()
                cont_response = await chat_async(
                    messages=continuation_prompt,
                    model=resolved_model,
                    options=llm_opts,
                )
                llm_ms += (time.perf_counter() - cont_start) * 1000
                cont_text = cont_response.get("text", "") if isinstance(cont_response, dict) else ""
                if not cont_text.strip():
                    break
                partial_text += cont_text

                # Check sentinel in continuation chunk
                partial_text, cont_sentinel = _strip_sentinel(partial_text)
                if cont_sentinel:
                    break  # Model finished naturally

                # Check if this continuation was also truncated
                cont_raw = cont_response.get("raw", {}) if isinstance(cont_response, dict) else {}
                cont_truncated = cont_raw.get("done_reason", "") == "length"
                cont_choices = cont_raw.get("choices")
                if isinstance(cont_choices, list) and cont_choices:
                    if (cont_choices[0] or {}).get("finish_reason", "") == "length":
                        cont_truncated = True
                if not cont_truncated:
                    break  # Got a complete response, stop continuing
                logger.info(
                    "[LLM] Continuation %d also truncated for user %s, retrying...",
                    _cont_attempt + 1, user_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[LLM] Continuation attempt %d failed for user %s: %s",
                    _cont_attempt + 1, user_id, exc,
                )
                break
        response["text"] = partial_text
        logger.info(
            "[LLM] Continuation stitched response for user %s (%d chars)",
            user_id, len(partial_text),
        )

    # Strip SET_REMINDER tags from response and create actual reminders
    if isinstance(response, dict) and response.get("text"):
        response["text"] = _process_reminder_tags(
            response["text"],
            user_id,
            allow_reminders=(turn_plan.allow_reminder_action if turn_plan else True),
        )
        response["text"] = _remove_leaked_sentinels(response["text"])

    if rag_sources and isinstance(response, dict) and response.get("text"):
        citation_block = format_citations(rag_sources)
        if citation_block:
            response["text"] = response["text"].rstrip() + citation_block

    if isinstance(response, dict):
        routing_trace.append(
            _route_entry(
                "pipeline.response_ready",
                elapsed_ms=(time.perf_counter() - total_start) * 1000,
                truncated=truncated,
                empty_response_retries=empty_response_retries,
                continuation_attempts=continuation_attempts,
                response_length=len(str(response.get("text") or "")),
            )
        )
        response["_timing"] = {
            "rag_ms": round(rag_ms, 1),
            "llm_ms": round(llm_ms, 1),
            "lexical_ms": memory_timing.get("lexical_ms"),
            "memory_ms": memory_timing.get("memory_ms"),
            "memory_mode": memory_timing.get("memory_mode"),
            "memory_count": memory_timing.get("memory_count"),
            "memory_classifier_score": memory_timing.get("memory_classifier_score"),
            "total_ms": round((time.perf_counter() - total_start) * 1000, 1),
            "rag_sources_count": len(rag_sources),
            "summary_needed": bool(summary_needed),
            "personality_mode": persona_runtime.personality_name,
            "live_search_mode": "now" if bool(live_search_context) else (
                "followup" if turn_plan and turn_plan.needs_live_search_followup else "skip"
            ),
        }
        response["_summary_needed"] = bool(summary_needed)
        response["_session_id"] = session_id
        if turn_plan:
            response["_turn_plan"] = turn_plan.to_dict()
        response["_routing_trace"] = routing_trace

    _log_completion_telemetry(
        path="conversation_async",
        user_id=user_id,
        model=resolved_model,
        text=response.get("text", "") if isinstance(response, dict) else "",
        sentinel_found=sentinel_found,
        truncated=truncated,
        empty_response_retries=empty_response_retries,
        continuation_attempts=continuation_attempts,
        response=response if isinstance(response, dict) else None,
    )

    return response


def generate_response(
    user_id: int,
    session_id: int,
    user_text: str,
    model: str | None = None,
    turn_plan: TurnPlan | None = None,
) -> dict[str, Any]:
    """Synchronous wrapper preserved for legacy callers."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            generate_response_async(
                user_id=user_id,
                session_id=session_id,
                user_text=user_text,
                model=model,
                turn_plan=turn_plan,
            )
        )

    # If already inside an event loop, fall back to sync chat path to avoid nested loop errors.
    return _generate_response_sync(
        user_id=user_id,
        session_id=session_id,
        user_text=user_text,
        model=model,
        turn_plan=turn_plan,
    )


def _generate_response_sync(
    user_id: int,
    session_id: int,
    user_text: str,
    model: str | None = None,
    turn_plan: TurnPlan | None = None,
) -> dict[str, Any]:
    """Compatibility path for sync callers invoked from within an active event loop."""

    total_start = time.perf_counter()
    rag_ms = 0.0
    llm_ms = 0.0
    cfg = settings()
    routing_trace: list[dict[str, Any]] = [
        _route_entry("pipeline.started", elapsed_ms=0.0, session_id=session_id)
    ]

    personality_mode = get_user_personality_name(user_id)
    resolved_model = resolve_user_model(user_id, requested_model=model)
    num_ctx = resolve_context_window(model=resolved_model, personality=personality_mode)
    with db_rw() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET ctx_token_budget = ?
            WHERE id = ? AND (ctx_token_budget IS NULL OR ctx_token_budget != ?)
            """,
            (num_ctx, session_id, num_ctx),
        )

    recent = recent_messages(user_id, session_id, max_msgs=30)
    recent_for_prompt = [
        {**msg, "content": clean_for_llm(msg.get("content", ""))} for msg in recent
    ]
    recent_for_prompt, removed_history = filter_prompt_history_for_personality(
        personality_name=personality_mode,
        messages=recent_for_prompt,
    )
    summary = rolling_summary(session_id)
    no_profile = personality_mode in ("downbad", "roleplay") or is_custom_character(personality_mode)
    profile_context = None if no_profile else user_profile_context(user_id)
    events = list(pending_server_events_for_session(user_id, session_id))
    live_search_context = ""
    if turn_plan:
        if turn_plan.needs_live_search_now and turn_plan.search_query:
            try:
                live_search_context = search_context(
                    turn_plan.search_query,
                    max_results=3,
                    timeout=2,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Hot-path sync live search failed for user %s: %s", user_id, exc)
                live_search_context = ""
    persona_runtime = build_user_persona_runtime(
        user_id=user_id,
        profile_context=profile_context,
    )
    system_prompt = persona_runtime.system_prompt + _turn_plan_prompt_block(turn_plan)
    routing_trace.append(
        _route_entry(
            "pipeline.context_loaded",
            elapsed_ms=(time.perf_counter() - total_start) * 1000,
            summary_needed=bool(should_create_summary(session_id)),
            filtered_refusal_history=removed_history,
            filtered_refusal_memories=0,
        )
    )

    rag_context = ""
    rag_sources: list[str] = []
    try:
        retriever = get_retriever()
        if turn_plan is None:
            should_retrieve = retriever.should_retrieve(user_text)
        else:
            should_retrieve = bool(turn_plan.needs_rag)
        if should_retrieve:
            rag_start = time.perf_counter()
            retrieval = retriever.retrieve(user_text, top_k=min(3, cfg.top_k_retrieval))
            rag_context = retrieval.get("context", "") if retrieval else ""
            rag_sources = retrieval.get("sources", []) if retrieval else []
            rag_ms = (time.perf_counter() - rag_start) * 1000
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RAG] Retrieval failed for user %s: %s", user_id, exc)
    routing_trace.append(
        _route_entry(
            "pipeline.rag_completed",
            elapsed_ms=(time.perf_counter() - total_start) * 1000,
            rag_used=bool(rag_context),
            rag_sources_count=len(rag_sources),
        )
    )

    prompt = build_prompt_with_system_prompt(
        system_prompt=system_prompt,
        session_summary=summary,
        retrieved_memories=[],
        server_events=events,
        recent_messages=recent_for_prompt,
        user_message=clean_for_llm(user_text),
        rag_context="\n\n".join(
            part for part in (rag_context, live_search_context) if part
        ),
    )
    routing_trace.append(
        _route_entry(
            "pipeline.prompt_built",
            elapsed_ms=(time.perf_counter() - total_start) * 1000,
            prompt_messages=len(prompt),
        )
    )

    llm_opts = resolve_llm_options(user_id, persona_runtime.personality_config, personality_mode)
    llm_opts["num_ctx"] = num_ctx
    num_predict = int(llm_opts.get("num_predict", 4096))
    prompt = _trim_prompt_to_budget(prompt, num_ctx, num_predict)

    llm_start = time.perf_counter()
    response = chat(
        messages=prompt,
        model=resolved_model,
        options=llm_opts,
    )
    llm_ms = (time.perf_counter() - llm_start) * 1000
    empty_response_retries = 0
    continuation_attempts = 0

    for empty_attempt in range(2):
        response_text = response.get("text", "") if isinstance(response, dict) else ""
        done_reason, finish_reason = _response_finish_meta(response if isinstance(response, dict) else None)
        if response_text.strip():
            break
        if done_reason not in {"stop", "length"} and finish_reason not in {"stop", "length"}:
            break
        empty_response_retries += 1
        logger.warning(
            "[LLM] Empty response with terminal stop for user %s model=%s "
            "(attempt %d, done_reason=%r, finish_reason=%r). Retrying with recovery prompt.",
            user_id,
            resolved_model,
            empty_attempt + 1,
            done_reason,
            finish_reason,
        )
        retry_prompt = list(prompt)
        retry_prompt.append(
            {
                "role": "user",
                "content": (
                    "Your previous reply was empty. Answer the user's last message now. "
                    "If you were interrupted, continue from where you left off. "
                    "Do not return an empty response."
                ),
            }
        )
        retry_start = time.perf_counter()
        response = chat(
            messages=retry_prompt,
            model=resolved_model,
            options=llm_opts,
        )
        llm_ms += (time.perf_counter() - retry_start) * 1000

    # --- Sentinel-based completion detection (sync mirror) ---
    sentinel_found = False
    if isinstance(response, dict) and response.get("text"):
        response["text"], sentinel_found = _strip_sentinel(response["text"])

    # Detect truncation from multiple signals (mirrors async path)
    truncated = False
    if isinstance(response, dict) and response.get("text") and not sentinel_found:
        truncated = True
        logger.warning(
            "[LLM] Sentinel marker missing — response likely truncated "
            "for user %s model=%s (text length=%d)",
            user_id, resolved_model, len(response.get("text", "")),
        )
    if isinstance(response, dict):
        raw = response.get("raw", {})
        done_reason = raw.get("done_reason", "")
        if done_reason == "length":
            truncated = True
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            finish_reason = (choices[0] or {}).get("finish_reason", "")
            if finish_reason == "length":
                truncated = True
        if truncated:
            logger.warning(
                "[LLM] Response truncated for user %s "
                "model=%s num_predict=%s num_ctx=%s done_reason=%r finish_reason=%r. "
                "Consider increasing limits.",
                user_id,
                resolved_model,
                llm_opts.get("num_predict"),
                llm_opts.get("num_ctx"),
                done_reason,
                (choices[0] or {}).get("finish_reason", "") if isinstance(raw.get("choices"), list) and raw.get("choices") else "",
            )

    # --- Continuation on truncation (gated by feature flag) ---
    # Sync mirror of the async continuation logic above.
    if truncated and feature_enabled("llm_continuation_on_truncation") and isinstance(response, dict):
        partial_text = response.get("text", "")
        base_prompt = list(prompt)  # original prompt — do NOT mutate; rebuild each iteration
        for _cont_attempt in range(2):
            continuation_attempts += 1
            # Rebuild from the base prompt each time so we never send duplicate assistant turns.
            continuation_prompt = base_prompt + [
                {"role": "assistant", "content": partial_text},
                {"role": "user", "content": "Please continue exactly where you left off."},
            ]
            try:
                cont_response = chat(
                    messages=continuation_prompt,
                    model=resolved_model,
                    options=llm_opts,
                )
                cont_text = cont_response.get("text", "") if isinstance(cont_response, dict) else ""
                if not cont_text.strip():
                    break
                partial_text += cont_text

                # Check sentinel in continuation chunk
                partial_text, cont_sentinel = _strip_sentinel(partial_text)
                if cont_sentinel:
                    break  # Model finished naturally

                cont_raw = cont_response.get("raw", {}) if isinstance(cont_response, dict) else {}
                cont_truncated = cont_raw.get("done_reason", "") == "length"
                cont_choices = cont_raw.get("choices")
                if isinstance(cont_choices, list) and cont_choices:
                    if (cont_choices[0] or {}).get("finish_reason", "") == "length":
                        cont_truncated = True
                if not cont_truncated:
                    break
                logger.info(
                    "[LLM] Continuation %d also truncated for user %s, retrying...",
                    _cont_attempt + 1, user_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[LLM] Continuation attempt %d failed for user %s: %s",
                    _cont_attempt + 1, user_id, exc,
                )
                break
        response["text"] = partial_text
        logger.info(
            "[LLM] Continuation stitched response for user %s (%d chars)",
            user_id, len(partial_text),
        )

    # Strip SET_REMINDER tags from response and create actual reminders
    if isinstance(response, dict) and response.get("text"):
        response["text"] = _process_reminder_tags(
            response["text"],
            user_id,
            allow_reminders=(turn_plan.allow_reminder_action if turn_plan else True),
        )
        response["text"] = _remove_leaked_sentinels(response["text"])

    if rag_sources and isinstance(response, dict) and response.get("text"):
        citation_block = format_citations(rag_sources)
        if citation_block:
            response["text"] = response["text"].rstrip() + citation_block

    if isinstance(response, dict):
        routing_trace.append(
            _route_entry(
                "pipeline.response_ready",
                elapsed_ms=(time.perf_counter() - total_start) * 1000,
                truncated=truncated,
                empty_response_retries=empty_response_retries,
                continuation_attempts=continuation_attempts,
                response_length=len(str(response.get("text") or "")),
            )
        )
        response["_timing"] = {
            "rag_ms": round(rag_ms, 1),
            "llm_ms": round(llm_ms, 1),
            "total_ms": round((time.perf_counter() - total_start) * 1000, 1),
            "rag_sources_count": len(rag_sources),
            "personality_mode": persona_runtime.personality_name,
            "live_search_mode": "now" if bool(live_search_context) else (
                "followup" if turn_plan and turn_plan.needs_live_search_followup else "skip"
            ),
        }
        response["_summary_needed"] = bool(should_create_summary(session_id))
        response["_session_id"] = session_id
        if turn_plan:
            response["_turn_plan"] = turn_plan.to_dict()
        response["_routing_trace"] = routing_trace

    _log_completion_telemetry(
        path="conversation_sync",
        user_id=user_id,
        model=resolved_model,
        text=response.get("text", "") if isinstance(response, dict) else "",
        sentinel_found=sentinel_found,
        truncated=truncated,
        empty_response_retries=empty_response_retries,
        continuation_attempts=continuation_attempts,
        response=response if isinstance(response, dict) else None,
    )

    return response


def _llm_options_from_personality(personality_config: dict[str, Any]) -> dict[str, float]:
    return {
        "temperature": _safe_float(personality_config.get("temperature"), 0.7),
        "repeat_penalty": _safe_float(personality_config.get("repeat_penalty"), 1.1),
        "top_p": _safe_float(personality_config.get("top_p"), 0.9),
    }


# Allowed user-tunable LLM parameters with (min, max) ranges
LLM_PARAM_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (0.1, 2.0),
    "top_p": (0.1, 1.0),
    "top_k": (1, 100),
    "repeat_penalty": (0.5, 2.0),
    "num_ctx": (2048, 131072),
    "num_predict": (128, 8192),
}


def resolve_llm_options(
    user_id: int, personality_config: dict[str, Any], personality_name: str
) -> dict[str, Any]:
    """Build LLM options with layered overrides.

    Priority (highest wins):
      1. User personal overrides  (/settings set …)
      2. Admin defaults for personality group (downbad vs standard)
      3. Personality mode built-in defaults (modes.py)
      4. Hardcoded fallbacks
    """
    # Layer 0: personality defaults
    opts: dict[str, Any] = {
        **_llm_options_from_personality(personality_config),
        "top_k": 40,
        "num_predict": 4096,
    }

    # Layer 1: admin defaults
    admin = _load_admin_llm_defaults(personality_name)
    for k, v in admin.items():
        if k in LLM_PARAM_RANGES and v is not None:
            opts[k] = _safe_float(v, opts.get(k, 0))

    # Layer 2: user overrides
    user = _load_user_llm_settings(user_id)
    for k, v in user.items():
        if k in LLM_PARAM_RANGES and v is not None:
            lo, hi = LLM_PARAM_RANGES[k]
            clamped = max(lo, min(hi, _safe_float(v, opts.get(k, 0))))
            opts[k] = clamped

    return opts


def _load_admin_llm_defaults(personality_name: str) -> dict[str, Any]:
    """Load admin-set LLM defaults from llm_defaults.json file."""
    group = "downbad" if personality_name == "downbad" else "standard"
    try:
        from pathlib import Path
        defaults_path = Path(
            getattr(settings(), "data_root", ".") or "."
        ) / "llm_defaults.json"
        if defaults_path.exists():
            data = json.loads(defaults_path.read_text(encoding="utf-8"))
            return data.get(group, {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed loading admin LLM defaults (%s): %s", group, exc)
    return {}


def _load_user_llm_settings(user_id: int) -> dict[str, Any]:
    """Load per-user LLM overrides from profile_context."""
    try:
        with db_ro() as conn:
            row = conn.execute(
                "SELECT value FROM profile_context WHERE user_id = ? AND key = 'llm_settings'",
                (user_id,),
            ).fetchone()
        if row:
            return json.loads(row[0] if isinstance(row, tuple) else row["value"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed loading user LLM settings for %s: %s", user_id, exc)
    return {}


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
