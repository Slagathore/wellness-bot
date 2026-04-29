"""End-to-end orchestration for generating assistant responses."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

from app.config import settings
from app.db import db_rw
from app.features.token_budget.dynamic import resolve_context_window
from app.history_scope import history_scope_for_personality
from app.orchestrator.context_builder import (recent_messages,
                                              retrieved_memories,
                                              rolling_summary,
                                              should_create_summary,
                                              user_profile_context)
from app.orchestrator.persona_runtime import (build_user_persona_runtime,
                                              filter_prompt_history_for_personality,
                                              filter_prompt_memories_for_personality,
                                              get_user_personality_name,
                                              resolve_user_model)
from app.personality.modes import is_custom_character
from app.orchestrator.prompt_builder import (RESPONSE_COMPLETION_SENTINEL,
                                             build_prompt_with_system_prompt)
from app.rag.service import format_citations, get_retriever
from app.utils.fs import pending_server_events_for_session
from app.utils.ollama import chat
from app.utils.text_cleaner import clean_for_llm

LOGGER = logging.getLogger(__name__)

_SENTINEL_RE = re.compile(
    r"\s*" + re.escape(RESPONSE_COMPLETION_SENTINEL) + r"\s*$"
)


def generate_response(
    user_id: int, session_id: int, user_text: str, model: str | None = None
) -> dict:
    """Produce the assistant reply for a given user message."""

    cfg = settings()
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

    recent = recent_messages(user_id, session_id, max_msgs=30)
    recent_for_prompt = [
        {**msg, "content": clean_for_llm(msg.get("content", ""))} for msg in recent
    ]
    recent_for_prompt, _ = filter_prompt_history_for_personality(
        personality_name=personality_mode,
        messages=recent_for_prompt,
    )
    memory_scope = history_scope_for_personality(personality_mode)
    memories = [] if is_custom_character(personality_mode) else retrieved_memories(
        user_id,
        user_text,
        k=cfg.top_k_retrieval,
        scope_filter=memory_scope,
    )
    memories, _ = filter_prompt_memories_for_personality(
        personality_name=personality_mode,
        memories=memories,
    )
    summary = rolling_summary(session_id)
    _no_profile = personality_mode in ("downbad", "roleplay") or is_custom_character(personality_mode)
    profile_context = None if _no_profile else user_profile_context(user_id)
    events = pending_server_events_for_session(user_id, session_id)
    persona_runtime = build_user_persona_runtime(
        user_id=user_id,
        profile_context=profile_context,
    )

    rag_context = ""
    rag_sources: list[str] = []
    try:
        retriever = get_retriever()
        if retriever.should_retrieve(user_text):
            start_time = time.perf_counter()

            def _retrieve():
                return retriever.retrieve(user_text, top_k=min(3, cfg.top_k_retrieval))

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_retrieve)
                try:
                    retrieval = future.result(timeout=2.5)
                except FuturesTimeout:
                    LOGGER.info(
                        "[RAG] Retrieval skipped for user %s due to timeout", user_id
                    )
                    retrieval = {}
            rag_context = retrieval.get("context", "")
            rag_sources = retrieval.get("sources", [])
            duration_ms = (time.perf_counter() - start_time) * 1000
            LOGGER.info(
                "[RAG] Retrieved %s resources for user %s in %.0f ms",
                len(retrieval.get("resources", [])),
                user_id,
                duration_ms,
            )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[RAG] Retrieval failed for user %s: %s", user_id, exc)

    prompt = build_prompt_with_system_prompt(
        system_prompt=persona_runtime.system_prompt,
        session_summary=summary,
        retrieved_memories=memories,
        server_events=events,
        recent_messages=recent_for_prompt,
        user_message=clean_for_llm(user_text),
        rag_context=rag_context,
    )

    response = chat(
        messages=prompt,
        model=resolved_model,
        options={
            "temperature": _safe_float(persona_runtime.personality_config.get("temperature"), 0.7),
            "top_p": _safe_float(persona_runtime.personality_config.get("top_p"), 0.9),
            "top_k": 40,
            "num_ctx": num_ctx,
            "repeat_penalty": _safe_float(
                persona_runtime.personality_config.get("repeat_penalty"), 1.1
            ),
            "request_timeout": float(getattr(cfg, "llm_timeout_seconds", 30.0) or 30.0),
        },
    )

    # Strip sentinel marker from response text
    if isinstance(response, dict) and response.get("text"):
        response["text"] = _SENTINEL_RE.sub("", response["text"])

    if rag_sources and response.get("text"):
        citation_block = format_citations(rag_sources)
        if citation_block:
            response["text"] = response["text"].rstrip() + citation_block

    if isinstance(response, dict):
        response["_summary_needed"] = bool(summary_needed)
        response["_session_id"] = session_id
        response["_personality_mode"] = persona_runtime.personality_name
    return response


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
