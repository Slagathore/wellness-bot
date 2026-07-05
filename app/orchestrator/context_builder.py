"""Utilities for constructing conversational context for the LLM."""

from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Sequence

from app.config import settings
from app.db import db_ro, db_rw
from app.feature_flags import enabled
from app.memory import ConversationMemoryRetriever
from app.utils.ollama import chat, chat_async
from app.utils.text import embed_text, embed_text_async
from app.vector_backends import get_backend

_CACHE_LOCK = threading.Lock()
_MEMORY_CACHE: dict[tuple[int, str, str], tuple[float, list[dict[str, Any]]]] = {}
_PROFILE_CACHE: dict[int, tuple[float, str]] = {}
_QUICK_REF_CACHE: dict[int, tuple[float, str]] = {}

_SUMMARY_INFLIGHT_LOCK = threading.Lock()
_SUMMARY_INFLIGHT: set[int] = set()

_WORD_RE = re.compile(r"[a-zA-Z0-9']+")
_MEMORY_HINT_PATTERNS = (
    "remember",
    "remind me",
    "last time",
    "before",
    "earlier",
    "previously",
    "you said",
    "we talked",
    "as we discussed",
    "follow up",
    "again",
    "still",
)


def recent_messages(user_id: int, session_id: int, max_msgs: int = 30) -> list[dict]:
    """Return the latest conversation turns for the session."""

    with db_ro() as conn:
        cur = conn.execute(
            """
            SELECT id, role, content, timestamp
            FROM messages
            WHERE user_id = ? AND session_id = ? AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, session_id, max_msgs),
        )
        rows = [dict(row) for row in cur.fetchall()]
    rows.reverse()
    return rows


def retrieved_memories(
    user_id: int,
    query_text: str,
    k: int | None = None,
    *,
    scope_filter: str | None = None,
) -> list[dict]:
    """Perform semantic retrieval of long-term memories for the user."""

    cfg = settings()
    k = k or cfg.top_k_retrieval

    if enabled("conversation_memory_v2"):
        retriever = ConversationMemoryRetriever()
        return retriever.search(
            user_id=user_id,
            query=query_text,
            top_k=k,
            scope_filter=scope_filter,
        )

    query_vector = embed_text(query_text)
    backend = get_backend()
    return backend.top_k(
        user_id=user_id,
        query_vector=query_vector,
        k=k,
        role_filter=("user", "assistant"),
        scope_filter=scope_filter,
    )


async def retrieved_memories_controlled_async(
    user_id: int,
    query_text: str,
    *,
    k: int | None = None,
    join_timeout_seconds: float | None = None,
    scope_filter: str | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """
    Latency-aware memory retrieval.

    Order:
    1) lexical lookup first
    2) tiny classifier fallback gates semantic retrieval
    3) semantic retrieval always starts in background to enrich cache
    """

    cfg = settings()
    k = k or cfg.top_k_retrieval
    join_timeout = float(
        join_timeout_seconds
        if join_timeout_seconds is not None
        else getattr(cfg, "memory_semantic_join_timeout_seconds", 0.45)
    )

    lexical_start = time.perf_counter()
    lexical = lexical_memory_candidates(
        user_id=user_id,
        query_text=query_text,
        k=k,
        scope_filter=scope_filter,
    )
    lexical_ms = (time.perf_counter() - lexical_start) * 1000

    normalized_query = _normalize_query(query_text)
    cache_hit = False
    cache_scope = scope_filter or "*"
    cached = _memory_cache_get(user_id, normalized_query, cache_scope)
    if cached is not None:
        cache_hit = True
        semantic_task = None
        semantic_now = cached
    else:
        semantic_task = asyncio.create_task(
            _semantic_retrieval_async(
                user_id=user_id,
                query_text=query_text,
                k=k,
                scope_filter=scope_filter,
            )
        )
        semantic_now = None

    classifier_score = tiny_memory_classifier_score(
        query_text=query_text, lexical_hits=len(lexical)
    )
    use_semantic_now = (not lexical) and classifier_score >= float(
        getattr(cfg, "memory_classifier_threshold", 0.45)
    )

    retrieval_mode = "lexical_only" if lexical else "none"
    semantic_ms = 0.0
    semantic_rows: list[dict] = []

    if semantic_now is not None:
        semantic_rows = semantic_now
        retrieval_mode = "semantic_cache"
    elif semantic_task is not None and use_semantic_now:
        wait_start = time.perf_counter()
        try:
            semantic_rows = await asyncio.wait_for(semantic_task, timeout=join_timeout)
            retrieval_mode = "semantic_join"
            _memory_cache_put(
                user_id=user_id,
                normalized_query=normalized_query,
                scope_key=cache_scope,
                rows=semantic_rows,
                ttl_seconds=float(getattr(cfg, "memory_cache_ttl_seconds", 60.0)),
            )
        except asyncio.TimeoutError:
            retrieval_mode = "semantic_timeout"
            semantic_task.add_done_callback(
                lambda t: _consume_semantic_task(
                    task=t,
                    user_id=user_id,
                    normalized_query=normalized_query,
                    scope_key=cache_scope,
                    ttl_seconds=float(getattr(cfg, "memory_cache_ttl_seconds", 60.0)),
                )
            )
        except Exception:
            retrieval_mode = "semantic_error"
        finally:
            semantic_ms = (time.perf_counter() - wait_start) * 1000
    elif semantic_task is not None:
        retrieval_mode = "background_enrichment"
        semantic_task.add_done_callback(
            lambda t: _consume_semantic_task(
                task=t,
                user_id=user_id,
                normalized_query=normalized_query,
                scope_key=cache_scope,
                ttl_seconds=float(getattr(cfg, "memory_cache_ttl_seconds", 60.0)),
            )
        )

    merged = _merge_memory_results(lexical=lexical, semantic=semantic_rows, k=k)
    if merged and retrieval_mode in {"none", "lexical_only"}:
        retrieval_mode = "lexical_only"

    timing = {
        "lexical_ms": round(lexical_ms, 1),
        "memory_ms": round(semantic_ms + lexical_ms, 1),
        "memory_cache_hit": cache_hit,
        "memory_classifier_score": round(classifier_score, 3),
        "memory_mode": retrieval_mode,
        "memory_count": len(merged),
    }
    return merged, timing


def lexical_memory_candidates(
    user_id: int,
    query_text: str,
    k: int = 5,
    *,
    scope_filter: str | None = None,
) -> list[dict]:
    """Fast lexical lookup against conversation memory table."""

    terms = _extract_terms(query_text)
    if not terms:
        return []

    where_parts: list[str] = []
    params: list[Any] = [user_id, scope_filter, scope_filter]
    for term in terms:
        like = f"%{term}%"
        where_parts.append(
            "(LOWER(content) LIKE ? OR LOWER(COALESCE(summary, '')) LIKE ? OR LOWER(COALESCE(topics, '')) LIKE ?)"
        )
        params.extend([like, like, like])

    sql = f"""
        SELECT
            message_id,
            role,
            content,
            created_at,
            summary,
            topics,
            context_window,
            COALESCE(importance_score, 5.0) AS importance_score,
            COALESCE(emotional_salience, 0.0) AS emotional_salience,
            COALESCE(user_value_score, 0.0) AS user_value_score,
            COALESCE(context_score, 0.0) AS context_score,
            COALESCE(reference_count, 0) AS reference_count,
            last_referenced_at
        FROM conversation_embeddings
        WHERE user_id = ?
          AND (? IS NULL OR scope = ?)
          AND ({' OR '.join(where_parts)})
        ORDER BY COALESCE(last_referenced_at, created_at) DESC
        LIMIT ?
    """
    params.append(max(k * 4, 20))

    try:
        with db_ro() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []

    results: list[dict] = []
    for row in rows:
        content = row["content"] or ""
        summary = row["summary"] or ""
        topics = row["topics"] or ""
        lexical_hits = _lexical_hits(
            terms=terms,
            content=content,
            summary=summary,
            topics=topics,
            context_window=row["context_window"] or "",
        )
        if lexical_hits == 0:
            continue

        recency = _recency_component(row["last_referenced_at"] or row["created_at"])
        usage = _usage_component(row["reference_count"])
        importance = max(0.0, min(1.0, float(row["importance_score"]) / 10.0))
        emotional = max(0.0, min(1.0, float(row["emotional_salience"] or 0.0)))
        user_value = max(0.0, min(1.0, float(row["user_value_score"] or 0.0)))
        context_score = max(0.0, min(1.0, float(row["context_score"] or 0.0)))
        role_weight = _role_weight(row["role"])
        score = (
            0.40 * lexical_hits
            + 0.14 * importance
            + 0.13 * recency
            + 0.10 * usage
            + 0.08 * emotional
            + 0.07 * user_value
            + 0.04 * context_score
            + 0.04 * role_weight
        )
        results.append(
            {
                "message_id": row["message_id"],
                "role": row["role"],
                "content": content,
                "timestamp": row["created_at"],
                "summary": summary,
                "topics": _parse_topics(topics),
                "context_window": row["context_window"] or "",
                "importance_score": round(float(row["importance_score"] or 5.0), 2),
                "emotional_salience": round(emotional, 4),
                "user_value_score": round(user_value, 4),
                "context_score": round(context_score, 4),
                "role_weight": round(role_weight, 4),
                "lexical_score": round(score, 4),
                "rank_score": round(score, 4),
                "retrieval_source": "lexical",
            }
        )

    results.sort(key=lambda item: item.get("rank_score", 0.0), reverse=True)
    top = results[:k]
    _touch_memory_references([row["message_id"] for row in top if row.get("message_id")])
    return top


def tiny_memory_classifier_score(query_text: str, lexical_hits: int = 0) -> float:
    """Lightweight heuristic classifier to decide whether semantic memory is needed."""

    text = (query_text or "").strip().lower()
    if not text:
        return 0.0

    score = 0.0
    if lexical_hits > 0:
        score += 0.25
    if any(pattern in text for pattern in _MEMORY_HINT_PATTERNS):
        score += 0.65
    if "?" in text:
        score += 0.05

    pronoun_refs = ("that", "it", "they", "this", "those")
    if any(f" {token} " in f" {text} " for token in pronoun_refs):
        score += 0.10

    token_count = len(_WORD_RE.findall(text))
    if token_count >= 14:
        score += 0.05

    return min(score, 1.0)


def rolling_summary(session_id: int) -> str | None:
    """Return the stored rolling summary for the session if present."""

    with db_ro() as conn:
        row = conn.execute(
            "SELECT summary FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row and row["summary"]:
        return row["summary"]
    return None


def should_create_summary(session_id: int) -> bool:
    """Determine whether the session needs to be summarised."""

    cfg = settings()
    with db_ro() as conn:
        row = conn.execute(
            """
            SELECT message_count, token_count, ctx_token_budget
            FROM sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
    if not row:
        return False

    if row["message_count"] and row["message_count"] > 50:
        return True

    token_count = row["token_count"]
    budget = row["ctx_token_budget"] or cfg.ctx_token_budget
    if token_count and token_count >= budget * 0.8:
        return True

    return False


def create_session_summary(session_id: int) -> None:
    """Generate and persist a concise summary of the conversation to date."""

    with db_ro() as conn:
        msgs = conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE session_id = ? AND role IN ('user', 'assistant')
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
    if not msgs:
        return

    transcript = "\n\n".join(f"{row['role'].upper()}: {row['content']}" for row in msgs)

    prompt = [
        {
            "role": "system",
            "content": (
                "You are summarizing a conversation for memory retention. "
                "Create a concise 2-3 paragraph summary capturing key topics, "
                "user concerns, and action items."
            ),
        },
        {
            "role": "user",
            "content": f"Summarize this conversation:\n\n{transcript}",
        },
    ]

    response = chat(messages=prompt, options={"temperature": 0.3})
    summary_text = response["text"].strip()

    with db_rw() as conn:
        conn.execute(
            "UPDATE sessions SET summary = ? WHERE id = ?",
            (summary_text, session_id),
        )


async def create_session_summary_async(session_id: int) -> None:
    """Async variant used by post-send background summarization."""

    with db_ro() as conn:
        msgs = conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE session_id = ? AND role IN ('user', 'assistant')
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
    if not msgs:
        return

    transcript = "\n\n".join(f"{row['role'].upper()}: {row['content']}" for row in msgs)
    prompt = [
        {
            "role": "system",
            "content": (
                "You are summarizing a conversation for memory retention. "
                "Create a concise 2-3 paragraph summary capturing key topics, "
                "user concerns, and action items."
            ),
        },
        {
            "role": "user",
            "content": f"Summarize this conversation:\n\n{transcript}",
        },
    ]

    response = await chat_async(messages=prompt, options={"temperature": 0.3})
    summary_text = (response.get("text") or "").strip()
    if not summary_text:
        return

    with db_rw() as conn:
        conn.execute(
            "UPDATE sessions SET summary = ? WHERE id = ?",
            (summary_text, session_id),
        )


def schedule_session_summary(session_id: int) -> bool:
    """Schedule summarization once per session; returns True if queued."""

    with _SUMMARY_INFLIGHT_LOCK:
        if session_id in _SUMMARY_INFLIGHT:
            return False
        _SUMMARY_INFLIGHT.add(session_id)

    async def _runner() -> None:
        try:
            await create_session_summary_async(session_id)
        except Exception:
            try:
                await asyncio.to_thread(create_session_summary, session_id)
            except Exception:
                pass
        finally:
            with _SUMMARY_INFLIGHT_LOCK:
                _SUMMARY_INFLIGHT.discard(session_id)

    try:
        asyncio.get_running_loop().create_task(_runner())
    except RuntimeError:
        try:
            create_session_summary(session_id)
        finally:
            with _SUMMARY_INFLIGHT_LOCK:
                _SUMMARY_INFLIGHT.discard(session_id)
    return True


def user_profile_context(user_id: int) -> str:
    """Load profile context with a short TTL cache to avoid repetitive DB reads."""

    now = time.monotonic()
    cfg = settings()
    ttl = float(getattr(cfg, "profile_context_cache_ttl_seconds", 300.0))

    with _CACHE_LOCK:
        cached = _PROFILE_CACHE.get(user_id)
        if cached and cached[0] > now:
            return cached[1]

    chunks: list[str] = []
    with db_ro() as conn:
        user_row = conn.execute(
            """
            SELECT display_name, telegram_username
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if user_row:
            display = user_row["display_name"] or user_row["telegram_username"] or ""
            if display:
                chunks.append(f"User name: {display}")

        profile_rows = conn.execute(
            """
            SELECT key, value
            FROM profile_context
            WHERE user_id = ?
            ORDER BY key ASC
            """,
            (user_id,),
        ).fetchall()
        for row in profile_rows:
            key = (row["key"] or "").strip()
            value = (row["value"] or "").strip()
            if not key or not value:
                continue
            if len(value) > 180:
                value = value[:180] + "..."
            chunks.append(f"{key}: {value}")

    text = "\n".join(chunks).strip()
    with _CACHE_LOCK:
        _PROFILE_CACHE[user_id] = (now + ttl, text)
        if len(_PROFILE_CACHE) > 500:
            # Evict the 50 soonest-to-expire entries (lazy sweep, avoids background thread)
            stale = sorted(_PROFILE_CACHE, key=lambda k: _PROFILE_CACHE[k][0])[:50]
            for k in stale:
                _PROFILE_CACHE.pop(k, None)
    return text


def user_quick_reference(user_id: int) -> str:
    """Build a compact, human-readable profile summary for legacy prompt parity."""

    now = time.monotonic()
    ttl = float(getattr(settings(), "profile_context_cache_ttl_seconds", 300.0))
    with _CACHE_LOCK:
        cached = _QUICK_REF_CACHE.get(user_id)
        if cached and cached[0] > now:
            return cached[1]

    lines: list[str] = ["", "=== QUICK USER REFERENCE ==="]
    onboarding: dict[str, Any] = {}
    psych_profile: dict[str, Any] = {}
    with db_ro() as conn:
        user_row = conn.execute(
            "SELECT display_name, telegram_username, onboarding_data FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not user_row:
            return ""

        display_name = (
            user_row["display_name"] or user_row["telegram_username"] or "Unknown User"
        )
        lines.append(f"Name: {display_name}")

        onboarding_raw = user_row["onboarding_data"] if user_row["onboarding_data"] else "{}"
        onboarding = _safe_json_dict(onboarding_raw)
        if onboarding:
            lines.append(
                f"Check-in Frequency: {onboarding.get('check_in_frequency', 'not set')}"
            )
            pronouns = onboarding.get("pronouns") or onboarding.get("preferred_pronouns")
            if pronouns:
                lines.append(f"Pronouns: {pronouns}")
            support_pref = onboarding.get("support_preferences") or onboarding.get(
                "support_preference"
            )
            if support_pref:
                lines.append(f"Preferred Support: {support_pref}")
            nsfw_opt = onboarding.get("nsfw_opt_in")
            if nsfw_opt is not None:
                enabled_opt = str(nsfw_opt).lower() in {"true", "1", "yes", "on"}
                lines.append(f"NSFW Access: {'enabled' if enabled_opt else 'disabled'}")
            focus = onboarding.get("focus_areas", [])
            if isinstance(focus, list) and focus:
                lines.append(f"Focus Areas: {', '.join(str(item) for item in focus[:6])}")

        summary_row = conn.execute(
            "SELECT value FROM profile_context WHERE user_id = ? AND key = 'latest_assessment_summary'",
            (user_id,),
        ).fetchone()
        if summary_row and summary_row["value"]:
            lines.append(f"Assessment Snapshot: {str(summary_row['value']).strip()}")

        notes_row = conn.execute(
            "SELECT value FROM profile_context WHERE user_id = ? AND key = 'memory_notes'",
            (user_id,),
        ).fetchone()
        if notes_row and notes_row["value"]:
            parsed_notes = _safe_json_dict(notes_row["value"])
            note_items = parsed_notes.get("notes", [])
            if isinstance(note_items, list) and note_items:
                lines.append("Personal Notes:")
                for note in note_items[:3]:
                    if isinstance(note, dict):
                        summary = str(note.get("summary") or note.get("note") or "").strip()
                    else:
                        summary = str(note).strip()
                    if summary:
                        lines.append(f"  - {summary}")

        try:
            imported_row = conn.execute(
                """
                SELECT file_name, content
                FROM profile_import_documents
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        except Exception:
            imported_row = None
        if imported_row and imported_row["content"]:
            source = imported_row["file_name"] or "imported document"
            excerpt = str(imported_row["content"]).strip().replace("\n", " ")
            if len(excerpt) > 180:
                excerpt = excerpt[:180] + "..."
            lines.append(f"Imported Profile ({source}): {excerpt}")

        try:
            psych_row = conn.execute(
                """
                SELECT profile_data
                FROM psychological_profiles
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        except Exception:
            psych_row = None
        if psych_row and psych_row["profile_data"]:
            psych_profile = _safe_json_dict(psych_row["profile_data"])

    if psych_profile:
        mental = psych_profile.get("mental_health_indicators", {})
        depression_val = _extract_metric_value(mental, "depression_likelihood", 0.0)
        anxiety_val = _extract_metric_value(mental, "anxiety_likelihood", 0.0)
        if depression_val > 0.5:
            lines.append("⚠ Elevated depression indicators")
        if anxiety_val > 0.5:
            lines.append("⚠ Elevated anxiety indicators")

        interests = psych_profile.get("interests_topics", [])
        if isinstance(interests, list) and interests:
            lines.append(f"Interests: {', '.join(str(item) for item in interests[:5])}")

        quirks = psych_profile.get("idiosyncrasies", [])
        if isinstance(quirks, list) and quirks:
            lines.append(f"Note: {', '.join(str(item) for item in quirks[:2])}")

    if not psych_profile and onboarding:
        interests = onboarding.get("interests", [])
        if isinstance(interests, list) and interests:
            lines.append(f"Interests: {', '.join(str(item) for item in interests[:5])}")

    lines.append("===========================")
    quick_ref = "\n".join(lines)
    with _CACHE_LOCK:
        _QUICK_REF_CACHE[user_id] = (now + ttl, quick_ref)
        if len(_QUICK_REF_CACHE) > 500:
            stale = sorted(_QUICK_REF_CACHE, key=lambda k: _QUICK_REF_CACHE[k][0])[:50]
            for k in stale:
                _QUICK_REF_CACHE.pop(k, None)
    return quick_ref


def invalidate_profile_context_cache(user_id: int) -> None:
    with _CACHE_LOCK:
        _PROFILE_CACHE.pop(user_id, None)
        _QUICK_REF_CACHE.pop(user_id, None)


def clear_context_caches() -> None:
    with _CACHE_LOCK:
        _MEMORY_CACHE.clear()
        _PROFILE_CACHE.clear()
        _QUICK_REF_CACHE.clear()


async def _semantic_retrieval_async(
    *,
    user_id: int,
    query_text: str,
    k: int,
    scope_filter: str | None = None,
) -> list[dict]:
    if enabled("conversation_memory_v2"):
        retriever = ConversationMemoryRetriever()
        try:
            return await retriever.search_async(
                user_id=user_id,
                query=query_text,
                top_k=k,
                scope_filter=scope_filter,
            )
        except AttributeError:
            return await asyncio.to_thread(
                retriever.search,
                user_id=user_id,
                query=query_text,
                top_k=k,
                scope_filter=scope_filter,
            )

    embedding = await embed_text_async(query_text)
    backend = get_backend()
    rows = await asyncio.to_thread(
        backend.top_k,
        user_id=user_id,
        query_vector=embedding,
        k=k,
        role_filter=("user", "assistant"),
        scope_filter=scope_filter,
    )
    return list(rows or [])


def _consume_semantic_task(
    *,
    task: asyncio.Task[list[dict]],
    user_id: int,
    normalized_query: str,
    scope_key: str,
    ttl_seconds: float,
) -> None:
    try:
        rows = task.result()
    except Exception:
        return
    _memory_cache_put(
        user_id=user_id,
        normalized_query=normalized_query,
        scope_key=scope_key,
        rows=rows,
        ttl_seconds=ttl_seconds,
    )


def _memory_cache_get(
    user_id: int, normalized_query: str, scope_key: str
) -> list[dict] | None:
    now = time.monotonic()
    key = (user_id, normalized_query, scope_key)
    with _CACHE_LOCK:
        item = _MEMORY_CACHE.get(key)
        if not item:
            return None
        expires_at, rows = item
        if expires_at <= now:
            _MEMORY_CACHE.pop(key, None)
            return None
        return list(rows)


def _memory_cache_put(
    *,
    user_id: int,
    normalized_query: str,
    scope_key: str,
    rows: Sequence[dict],
    ttl_seconds: float,
) -> None:
    key = (user_id, normalized_query, scope_key)
    with _CACHE_LOCK:
        _MEMORY_CACHE[key] = (time.monotonic() + ttl_seconds, [dict(r) for r in rows])
        if len(_MEMORY_CACHE) > 2000:
            stale_keys = sorted(_MEMORY_CACHE, key=lambda k: _MEMORY_CACHE[k][0])[:200]
            for stale in stale_keys:
                _MEMORY_CACHE.pop(stale, None)


def _normalize_query(query_text: str) -> str:
    return " ".join((query_text or "").strip().lower().split())


def _extract_terms(query_text: str, *, max_terms: int = 7) -> list[str]:
    tokens = [tok.lower() for tok in _WORD_RE.findall(query_text or "")]
    stopwords = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "is",
        "it",
        "this",
        "that",
        "i",
        "you",
        "we",
        "me",
        "my",
        "our",
    }
    deduped: list[str] = []
    for token in tokens:
        if len(token) < 3 or token in stopwords:
            continue
        if token not in deduped:
            deduped.append(token)
        if len(deduped) >= max_terms:
            break
    return deduped


def _lexical_hits(
    *,
    terms: Sequence[str],
    content: str,
    summary: str,
    topics: str,
    context_window: str = "",
) -> float:
    haystack = f"{content} {summary} {topics} {context_window}".lower()
    hits = sum(1 for term in terms if term in haystack)
    return min(1.0, hits / max(1, len(terms)))


def _recency_component(iso_value: str | None) -> float:
    if not iso_value:
        return 0.4
    try:
        dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except Exception:
        return 0.4
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = max(
        0.0,
        (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
        / 86400.0,
    )
    return math.exp(-age_days / 14.0)


def _usage_component(ref_count: int | float | None) -> float:
    if not ref_count:
        return 0.0
    value = float(ref_count)
    return min(1.0, math.log1p(value) / math.log1p(25.0))


def _role_weight(role: str | None) -> float:
    lowered = str(role or "").strip().lower()
    if lowered == "user":
        return 1.0
    if lowered == "assistant":
        return 0.72
    return 0.85


def _parse_topics(raw: str | None) -> list[str]:
    if not raw:
        return []
    cleaned = raw.strip()
    if not cleaned:
        return []
    if cleaned.startswith("[") and cleaned.endswith("]"):
        try:
            import json

            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return [str(item) for item in parsed[:8]]
        except Exception:
            return []
    return [part.strip() for part in cleaned.split(",") if part.strip()][:8]


def _merge_memory_results(*, lexical: Sequence[dict], semantic: Sequence[dict], k: int) -> list[dict]:
    by_id: dict[Any, dict] = {}

    for row in semantic:
        key = row.get("message_id")
        if key is None:
            continue
        item = dict(row)
        item.setdefault("retrieval_source", "semantic")
        item.setdefault(
            "rank_score",
            item.get("semantic_score") or item.get("similarity") or 0.0,
        )
        by_id[key] = item

    for row in lexical:
        key = row.get("message_id")
        if key is None:
            continue
        existing = by_id.get(key)
        if not existing:
            by_id[key] = dict(row)
            continue
        merged = dict(existing)
        merged.update(
            {
                "lexical_score": max(
                    float(existing.get("lexical_score") or 0.0),
                    float(row.get("lexical_score") or 0.0),
                ),
                "retrieval_source": "hybrid",
            }
        )
        semantic_rank = float(existing.get("rank_score") or 0.0)
        lexical_rank = float(row.get("rank_score") or 0.0)
        lexical_signal = max(
            float(existing.get("lexical_score") or 0.0),
            float(row.get("lexical_score") or 0.0),
        )
        merged["rank_score"] = max(semantic_rank, lexical_rank) + (0.04 * lexical_signal)
        by_id[key] = merged

    merged_rows = list(by_id.values())
    merged_rows.sort(
        key=lambda item: float(item.get("rank_score") or 0.0), reverse=True
    )
    return _prioritize_user_memories(merged_rows, k)


def _prioritize_user_memories(rows: Sequence[dict], k: int) -> list[dict]:
    if k <= 0:
        return []

    ordered = [dict(row) for row in rows]
    user_rows = [row for row in ordered if str(row.get("role") or "").lower() == "user"]
    if not user_rows:
        return ordered[:k]

    reserved_user_slots = min(len(user_rows), max(1, math.ceil(k * 0.6)))
    selected: list[dict] = []
    seen_message_ids: set[Any] = set()

    for row in user_rows[:reserved_user_slots]:
        message_id = row.get("message_id")
        if message_id in seen_message_ids:
            continue
        selected.append(row)
        seen_message_ids.add(message_id)

    for row in ordered:
        if len(selected) >= k:
            break
        message_id = row.get("message_id")
        if message_id in seen_message_ids:
            continue
        selected.append(row)
        seen_message_ids.add(message_id)

    return selected[:k]


def _safe_json_dict(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _extract_metric_value(data: Any, key: str, default: float = 0.0) -> float:
    if not isinstance(data, dict) or key not in data:
        return default
    value = data[key]
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _touch_memory_references(message_ids: Sequence[Any]) -> None:
    cleaned = [int(mid) for mid in message_ids if str(mid).strip()]
    if not cleaned:
        return
    placeholders = ",".join("?" for _ in cleaned)
    with db_rw() as conn:
        conn.execute(
            f"""
            UPDATE conversation_embeddings
            SET reference_count = COALESCE(reference_count, 0) + 1,
                last_referenced_at = CURRENT_TIMESTAMP
            WHERE message_id IN ({placeholders})
            """,
            cleaned,
        )
