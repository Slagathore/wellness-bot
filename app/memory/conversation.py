"""Conversation memory indexing and retrieval helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.db import db_ro, db_rw
from app.feature_flags import enabled
from app.utils.ollama import generate
from app.utils.text import embed_text, embed_text_async
from app.vector_backends import get_backend

LOGGER = logging.getLogger(__name__)
_EMBED_PREVIEW_LIMIT = 900
_THREAD_CONTEXT_WINDOW = 2


def _safe_json_loads(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _safe_json_dumps(value: Any) -> Optional[str]:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


@dataclass
class ConversationMemoryRecord:
    message_id: int
    user_id: int
    scope: str
    role: str
    content: str
    summary: Optional[str]
    topics: List[str]
    context_window: Optional[str]
    embedding: List[float]
    importance_score: float
    emotional_salience: float
    user_value_score: float
    context_score: float


class ConversationMemoryIndexer:
    """Create and persist semantic memory artefacts for conversation messages."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or LOGGER

    def index_message(
        self, *, message_id: int, user_id: int, scope: str, role: str, content: str
    ) -> None:
        """Store semantic memory for a message if the feature flag is enabled."""

        if not enabled("conversation_memory_v2"):
            return

        text = (content or "").strip()
        if not text:
            return

        try:
            if self._already_indexed(message_id):
                return

            analysis = self._analyze_text(role=role, text=text)
            context_window = self._build_thread_context(message_id)
            embedding_text = _build_embedding_text(
                text=text,
                summary=analysis.get("summary"),
                topics=analysis.get("topics", []),
                context_window=context_window,
            )
            embedding = embed_text(embedding_text)
            emotional_salience = self._estimate_emotional_salience(
                message_id=message_id,
                role=role,
                text=text,
            )
            user_value_score = self._estimate_user_value(
                role=role,
                text=text,
                topics=analysis.get("topics", []),
            )
            context_score = self._estimate_context_score(
                role=role,
                text=text,
                context_window=context_window,
            )
            record = ConversationMemoryRecord(
                message_id=message_id,
                user_id=user_id,
                scope=scope,
                role=role,
                content=text,
                summary=analysis.get("summary"),
                topics=analysis.get("topics", []),
                context_window=context_window,
                embedding=embedding,
                importance_score=self._estimate_importance(
                    role=role,
                    text=text,
                    topics=analysis.get("topics", []),
                    emotional_salience=emotional_salience,
                    user_value_score=user_value_score,
                    context_score=context_score,
                ),
                emotional_salience=emotional_salience,
                user_value_score=user_value_score,
                context_score=context_score,
            )
            self._persist(record)
        except Exception as exc:  # noqa: BLE001
            self._logger.error(
                "Conversation memory indexing failed for message %s: %s",
                message_id,
                exc,
                exc_info=True,
            )

    def _already_indexed(self, message_id: int) -> bool:
        with db_ro() as conn:
            row = conn.execute(
                "SELECT 1 FROM conversation_embeddings WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return bool(row)

    def _analyze_text(self, *, role: str, text: str) -> Dict[str, Any]:
        prompt = (
            "You are extracting structured memory snippets from a single chat message.\n"
            "Return JSON with keys:\n"
            "{\n"
            '  "summary": <string up to 160 chars>,\n'
            '  "topics": ["keyword1", "keyword2", ...]  # up to 5 concise nouns/phrases\n'
            "}\n"
            "Keep the summary first-person friendly when possible."
        )
        try:
            result = generate(
                prompt=f"{prompt}\n\nRole: {role}\nMessage:\n{text}",
                format="json",
                options={"temperature": 0.2},
            )
            raw = (result or {}).get("text") or "{}"
            parsed = json.loads(raw)
            summary = parsed.get("summary")
            if isinstance(summary, str):
                summary = summary.strip()[:320]
            topics = parsed.get("topics")
            if not isinstance(topics, list):
                topics = []
            cleaned_topics = []
            for topic in topics:
                if isinstance(topic, str):
                    topic = topic.strip()
                    if topic:
                        cleaned_topics.append(topic[:60])
                if len(cleaned_topics) >= 5:
                    break
            return {"summary": summary or text[:160], "topics": cleaned_topics}
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("Conversation memory analysis failed: %s", exc)
            return {"summary": text[:160], "topics": []}

    def _build_thread_context(self, message_id: int) -> str | None:
        with db_ro() as conn:
            current = conn.execute(
                "SELECT session_id, role, content FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if not current or not current["session_id"]:
                return None
            rows = conn.execute(
                """
                SELECT role, content
                FROM messages
                WHERE session_id = ?
                  AND id < ?
                  AND role IN ('user', 'assistant')
                ORDER BY id DESC
                LIMIT ?
                """,
                (current["session_id"], message_id, _THREAD_CONTEXT_WINDOW),
            ).fetchall()
        if not rows:
            return None
        lines: list[str] = []
        for row in reversed(rows):
            role = str(row["role"] or "").strip().lower() or "message"
            excerpt = _trim_for_memory(str(row["content"] or ""), limit=180)
            if excerpt:
                lines.append(f"{role}: {excerpt}")
        if not lines:
            return None
        return "\n".join(lines)

    def _estimate_importance(
        self,
        *,
        role: str,
        text: str,
        topics: Sequence[str] | None = None,
        emotional_salience: float = 0.0,
        user_value_score: float = 0.0,
        context_score: float = 0.0,
    ) -> float:
        lowered = (text or "").lower()
        score = 4.0

        if role == "user":
            score += 1.0
        if len(lowered) > 220:
            score += 1.0
        if "?" in lowered:
            score += 0.5
        if any(
            hint in lowered
            for hint in (
                "remember",
                "important",
                "appointment",
                "medication",
                "goal",
                "plan",
                "anxious",
                "depressed",
                "panic",
                "sleep",
                "work",
            )
        ):
            score += 1.5
        if topics:
            score += min(1.0, 0.2 * len(list(topics)))
        score += 1.6 * emotional_salience
        score += 2.0 * user_value_score
        score += 1.4 * context_score

        return max(0.0, min(10.0, score))

    def _estimate_emotional_salience(
        self, *, message_id: int, role: str, text: str
    ) -> float:
        lowered = (text or "").lower()
        score = 0.0
        with db_ro() as conn:
            row = conn.execute(
                """
                SELECT s.valence, s.arousal, s.dominance, s.emotion_label
                FROM sentiments AS s
                WHERE s.message_id = ?
                """,
                (message_id,),
            ).fetchone()
        if row:
            try:
                arousal = float(row["arousal"] or 0.0)
                valence = abs(float(row["valence"] or 0.0))
                score = max(score, min(1.0, 0.55 * arousal + 0.35 * valence))
            except Exception:
                pass
            emotion = str(row["emotion_label"] or "").lower()
            if emotion in {"fear", "sadness", "anger", "surprise"}:
                score = min(1.0, score + 0.1)

        if any(token in lowered for token in ("panic", "terrified", "love", "hate", "furious", "heartbroken", "excited")):
            score += 0.2
        if text.count("!") >= 2 or text.count("?") >= 2:
            score += 0.1
        if role == "user" and len(lowered) > 180:
            score += 0.05
        return max(0.0, min(1.0, score))

    def _estimate_user_value(
        self, *, role: str, text: str, topics: Sequence[str] | None = None
    ) -> float:
        lowered = (text or "").lower()
        score = 0.15 if role == "assistant" else 0.35
        if any(token in lowered for token in ("i am", "i'm", "my ", "me ", "mine", "myself")):
            score += 0.18
        if any(
            hint in lowered
            for hint in (
                "remember",
                "important",
                "prefer",
                "favorite",
                "dont forget",
                "don't forget",
                "i like",
                "i don't like",
                "my goal",
                "my plan",
                "appointment",
                "birthday",
                "medication",
                "diagnosis",
                "allergy",
                "interview",
                "surgery",
            )
        ):
            score += 0.35
        if topics:
            score += min(0.12, 0.03 * len(list(topics)))
        return max(0.0, min(1.0, score))

    def _estimate_context_score(
        self, *, role: str, text: str, context_window: str | None
    ) -> float:
        lowered = (text or "").lower()
        score = 0.1 if role == "assistant" else 0.2
        if "?" in lowered:
            score += 0.18
        if any(
            hint in lowered
            for hint in (
                "tomorrow",
                "tonight",
                "next week",
                "later",
                "after",
                "before",
                "when you",
                "if i",
                "we were",
                "that thing",
                "as i said",
                "like before",
            )
        ):
            score += 0.28
        if context_window:
            score += 0.22
        return max(0.0, min(1.0, score))

    def _persist(self, record: ConversationMemoryRecord) -> None:
        backend = get_backend()
        backend.upsert(
            record.message_id,
            record.embedding,
            {"user_id": record.user_id, "role": record.role},
        )

        topics_json = _safe_json_dumps(record.topics)
        embedding_json = _safe_json_dumps(record.embedding)

        with db_rw() as conn:
            conn.execute(
                "DELETE FROM conversation_embeddings WHERE message_id = ?",
                (record.message_id,),
            )
            conn.execute(
                """
                INSERT INTO conversation_embeddings (
                    user_id,
                    message_id,
                    scope,
                    role,
                    content,
                    summary,
                    topics,
                    context_window,
                    embedding,
                    importance_score,
                    emotional_salience,
                    user_value_score,
                    context_score,
                    reference_count,
                    last_referenced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                (
                    record.user_id,
                    record.message_id,
                    record.scope,
                    record.role,
                    record.content,
                    record.summary,
                    topics_json,
                    record.context_window,
                    embedding_json,
                    record.importance_score,
                    record.emotional_salience,
                    record.user_value_score,
                    record.context_score,
                ),
            )


class ConversationMemoryRetriever:
    """Semantic retrieval helper enriched with lexical and significance reranking."""

    def search(
        self,
        *,
        user_id: int,
        query: str,
        top_k: int = 5,
        scope_filter: str | None = None,
    ) -> List[dict]:
        if not enabled("conversation_memory_v2"):
            return []

        query = (query or "").strip()
        if not query:
            return []

        embedding = embed_text(query)
        return self._search_from_embedding(
            user_id=user_id,
            query=query,
            query_embedding=embedding,
            top_k=top_k,
            scope_filter=scope_filter,
        )

    async def search_async(
        self,
        *,
        user_id: int,
        query: str,
        top_k: int = 5,
        scope_filter: str | None = None,
    ) -> List[dict]:
        if not enabled("conversation_memory_v2"):
            return []

        query = (query or "").strip()
        if not query:
            return []

        embedding = await embed_text_async(query)
        return await asyncio.to_thread(
            self._search_from_embedding,
            user_id=user_id,
            query=query,
            query_embedding=embedding,
            top_k=top_k,
            scope_filter=scope_filter,
        )

    def _search_from_embedding(
        self,
        *,
        user_id: int,
        query: str,
        query_embedding: Sequence[float],
        top_k: int,
        scope_filter: str | None,
    ) -> List[dict]:
        backend = get_backend()
        rows = backend.top_k(
            user_id=user_id,
            query_vector=query_embedding,
            k=max(top_k * 3, 12),
            role_filter=("user", "assistant"),
            scope_filter=scope_filter,
        )
        if not rows:
            return []

        message_ids = [row["message_id"] for row in rows if row.get("message_id")]
        if not message_ids:
            return []

        placeholders = ",".join("?" for _ in message_ids)
        with db_ro() as conn:
            metadata_rows = conn.execute(
                f"""
                SELECT
                    message_id,
                    summary,
                    topics,
                    context_window,
                    scope,
                    role,
                    content,
                    created_at,
                    COALESCE(importance_score, 5.0) AS importance_score,
                    COALESCE(emotional_salience, 0.0) AS emotional_salience,
                    COALESCE(user_value_score, 0.0) AS user_value_score,
                    COALESCE(context_score, 0.0) AS context_score,
                    COALESCE(reference_count, 0) AS reference_count,
                    last_referenced_at
                FROM conversation_embeddings
                WHERE message_id IN ({placeholders})
                """,
                message_ids,
            ).fetchall()

        metadata_map = {row["message_id"]: dict(row) for row in metadata_rows}
        terms = _extract_terms(query)

        results: List[dict] = []
        for row in rows:
            message_id = row.get("message_id")
            if not message_id:
                continue

            meta = metadata_map.get(message_id) or {}
            content = meta.get("content") or row.get("content") or ""
            summary = meta.get("summary") or ""
            topics_raw = meta.get("topics") or ""
            context_window = meta.get("context_window") or ""
            scope = meta.get("scope") or row.get("scope")
            role = meta.get("role") or row.get("role")
            created_at = meta.get("created_at") or row.get("timestamp")

            distance = row.get("distance")
            semantic_similarity = 0.0
            if isinstance(distance, (int, float)) and float(distance) >= 0:
                semantic_similarity = 1.0 / (1.0 + float(distance))

            lexical = _lexical_score(
                terms=terms,
                content=content,
                summary=summary,
                topics=topics_raw,
                context_window=context_window,
            )
            importance = max(
                0.0,
                min(1.0, float(meta.get("importance_score") or 5.0) / 10.0),
            )
            recency = _recency_score(meta.get("last_referenced_at") or created_at)
            usage = _usage_score(meta.get("reference_count") or 0)
            emotional = max(0.0, min(1.0, float(meta.get("emotional_salience") or 0.0)))
            user_value = max(0.0, min(1.0, float(meta.get("user_value_score") or 0.0)))
            context_score = max(0.0, min(1.0, float(meta.get("context_score") or 0.0)))
            role_weight = _role_weight(role)

            final_score = (
                0.34 * semantic_similarity
                + 0.18 * lexical
                + 0.12 * importance
                + 0.09 * recency
                + 0.09 * usage
                + 0.08 * emotional
                + 0.07 * user_value
                + 0.05 * context_score
                + 0.08 * role_weight
            )

            entry = {
                "message_id": message_id,
                "content": content,
                "timestamp": row.get("timestamp") or created_at,
                "scope": scope,
                "role": role,
                "distance": distance,
                "similarity": round(semantic_similarity, 4),
                "semantic_score": round(semantic_similarity, 4),
                "lexical_score": round(lexical, 4),
                "importance_score": round(float(meta.get("importance_score") or 5.0), 2),
                "emotional_salience": round(emotional, 4),
                "user_value_score": round(user_value, 4),
                "context_score": round(context_score, 4),
                "role_weight": round(role_weight, 4),
                "reference_count": int(meta.get("reference_count") or 0),
                "summary": summary,
                "topics": _safe_json_loads(topics_raw) or [],
                "context_window": context_window,
                "created_at": created_at,
                "rank_score": round(final_score, 4),
                "retrieval_source": "semantic",
            }
            results.append(entry)

        results.sort(key=lambda item: item.get("rank_score", 0.0), reverse=True)
        top = _prioritize_user_results(results, top_k)
        self._touch_references([row["message_id"] for row in top if row.get("message_id")])
        return top

    def _touch_references(self, message_ids: Sequence[int]) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        with db_rw() as conn:
            conn.execute(
                f"""
                UPDATE conversation_embeddings
                SET reference_count = COALESCE(reference_count, 0) + 1,
                    last_referenced_at = CURRENT_TIMESTAMP
                WHERE message_id IN ({placeholders})
                """,
                list(message_ids),
            )


def _extract_terms(text: str, max_terms: int = 6) -> list[str]:
    terms = re.findall(r"[a-zA-Z0-9']+", (text or "").lower())
    stop = {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "you",
        "your",
        "from",
        "have",
        "are",
        "was",
        "were",
    }
    out: list[str] = []
    for term in terms:
        if len(term) < 3 or term in stop:
            continue
        if term not in out:
            out.append(term)
        if len(out) >= max_terms:
            break
    return out


def _lexical_score(
    *,
    terms: Sequence[str],
    content: str,
    summary: str,
    topics: str,
    context_window: str = "",
) -> float:
    if not terms:
        return 0.0
    haystack = f"{content} {summary} {topics} {context_window}".lower()
    hits = sum(1 for term in terms if term in haystack)
    return min(1.0, hits / max(1, len(terms)))


def _recency_score(iso_value: str | None) -> float:
    if not iso_value:
        return 0.4
    try:
        dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except Exception:
        return 0.4
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = (
        datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    ).total_seconds() / 86400.0
    age_days = max(0.0, age_days)
    return math.exp(-age_days / 14.0)


def _usage_score(value: int | float) -> float:
    count = max(0.0, float(value))
    if count == 0:
        return 0.0
    return min(1.0, math.log1p(count) / math.log1p(25.0))


def _role_weight(role: str | None) -> float:
    lowered = str(role or "").strip().lower()
    if lowered == "user":
        return 1.0
    if lowered == "assistant":
        return 0.72
    return 0.85


def _prioritize_user_results(rows: Sequence[dict], top_k: int) -> list[dict]:
    if top_k <= 0:
        return []

    ordered = [dict(row) for row in rows]
    user_rows = [row for row in ordered if str(row.get("role") or "").lower() == "user"]
    if not user_rows:
        return ordered[:top_k]

    reserved_user_slots = min(len(user_rows), max(1, math.ceil(top_k * 0.6)))
    selected: list[dict] = []
    seen_message_ids: set[Any] = set()

    for row in user_rows[:reserved_user_slots]:
        message_id = row.get("message_id")
        if message_id in seen_message_ids:
            continue
        selected.append(row)
        seen_message_ids.add(message_id)

    for row in ordered:
        if len(selected) >= top_k:
            break
        message_id = row.get("message_id")
        if message_id in seen_message_ids:
            continue
        selected.append(row)
        seen_message_ids.add(message_id)

    return selected[:top_k]


def _trim_for_memory(text: str, *, limit: int) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    half = max(80, limit // 2)
    head = value[:half].rstrip()
    tail = value[-half:].lstrip()
    return f"{head} ... {tail}"


def _build_embedding_text(
    *,
    text: str,
    summary: str | None,
    topics: Sequence[str],
    context_window: str | None,
) -> str:
    body = _trim_for_memory(text, limit=_EMBED_PREVIEW_LIMIT)
    parts = [body]
    if summary:
        parts.append(f"Summary: {summary}")
    if topics:
        parts.append("Topics: " + ", ".join(str(topic) for topic in topics[:6]))
    if context_window:
        parts.append("Recent context:\n" + context_window)
    return "\n".join(part for part in parts if part).strip()
