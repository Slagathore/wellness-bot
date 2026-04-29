"""Profile document ingestion and retrieval service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from collections import defaultdict
from contextlib import suppress
from typing import Any, Sequence

from app.utils.time_utils import operator_now


class ProfileDocumentService:
    """Manages imported profile documents for retrieval."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._registry: dict[int, set[str]] = defaultdict(set)
        self._lock = threading.Lock()

    # Ingestion ----------------------------------------------------------------

    async def ingest_documents(
        self,
        *,
        user_id: int,
        combined_text: str,
        combined_path: str | None,
        source_documents: Sequence[str] | None = None,
        total_characters: int | None = None,
    ) -> dict | None:
        """Vectorize imported profile documents for retrieval."""

        if not combined_text or not combined_text.strip():
            return None

        vector_store = getattr(self.bot, "vector_store", None)
        if vector_store is None:
            self.bot.logger.debug(
                "Vector store unavailable; skipping profile document ingestion."
            )
            return None

        import_time = operator_now()
        digest = hashlib.sha1(combined_text.encode("utf-8")).hexdigest()[:12]
        doc_id = f"profile_import:{user_id}:{digest}"
        title = f"Imported Profile ({import_time.strftime('%Y-%m-%d %H:%M')})"
        chunk_size = 650

        metadata: dict[str, Any] = {
            "doc_type": "profile_import",
            "owner_user_id": user_id,
            "imported_at": import_time.isoformat(),
            "stored_path": combined_path,
            "total_characters": total_characters,
            "source_documents": list(source_documents or []),
        }

        document = {
            "doc_id": doc_id,
            "title": title,
            "content": combined_text,
            "category": f"user_profile:{user_id}",
            "source": "Profile Import",
            "url": None,
            "metadata": metadata,
        }

        def _ingest() -> int:
            return vector_store.add_documents([document], chunk_size=chunk_size)

        added = await asyncio.to_thread(_ingest)
        chunk_count = self._estimate_chunk_count(combined_text, chunk_size)
        metadata["chunk_count"] = chunk_count
        self._register_document(user_id, doc_id)

        return {
            "doc_id": doc_id,
            "ingested_chunks": chunk_count,
            "status": "added" if added else "skipped",
            "metadata": metadata,
        }

    # Registry -----------------------------------------------------------------

    def _register_document(self, user_id: int, doc_id: str) -> None:
        with self._lock:
            self._registry[user_id].add(doc_id)

    def prime_registry(self) -> None:
        """Load existing profile import documents from the vector store."""

        vector_store = getattr(self.bot, "vector_store", None)
        if vector_store is None or not getattr(vector_store, "db_path", None):
            return

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(vector_store.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT doc_id, metadata FROM wellness_documents WHERE metadata IS NOT NULL"
            ).fetchall()
            for row in rows:
                metadata_raw = row["metadata"]
                if not metadata_raw:
                    continue
                try:
                    metadata = json.loads(metadata_raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if metadata.get("doc_type") != "profile_import":
                    continue
                owner = metadata.get("owner_user_id")
                if owner is None:
                    continue
                self._register_document(int(owner), row["doc_id"])
        except Exception as exc:  # noqa: BLE001
            self.bot.logger.error(
                "Failed to prime profile document registry: %s", exc, exc_info=True
            )
        finally:
            if conn is not None:
                with suppress(Exception):
                    conn.close()

    # Retrieval ----------------------------------------------------------------

    def should_query_profile_docs(self, user_id: int, message: str) -> bool:
        """Heuristic to decide if the user is referencing imported documents."""

        message_lower = (message or "").lower()
        if not message_lower:
            return False

        with self._lock:
            has_docs = bool(self._registry.get(user_id))

        if not has_docs:
            return False

        doc_keywords = [
            "imported doc",
            "imported profile",
            "profile pdf",
            "those instructions",
            "uploaded file",
            "uploaded document",
            "the pdf",
            "that document",
            "chatgpt export",
            "custom instructions",
        ]

        if not any(keyword in message_lower for keyword in doc_keywords):
            return False

        interrogative = (
            any(
                message_lower.startswith(prefix)
                for prefix in ("what", "how", "where", "why", "who", "when")
            )
            or "?" in message_lower
            or "explain" in message_lower
        )

        return interrogative

    async def retrieve_profile_documents(
        self, user_id: int, query: str
    ) -> dict[str, Any] | None:
        """Retrieve user-imported documents relevant to the current query."""

        query = (query or "").strip()
        vector_store = getattr(self.bot, "vector_store", None)
        if not query or vector_store is None:
            return None

        def _search() -> list[dict[str, Any]]:
            return vector_store.search(
                query,
                top_k=3,
                metadata_filter={
                    "owner_user_id": user_id,
                    "doc_type": "profile_import",
                },
            )

        results = await asyncio.to_thread(_search)
        if not results:
            return None

        context_parts = ["**Imported Profile References:**\n"]
        sources: list[str] = []

        for idx, result in enumerate(results, 1):
            snippet = (result.get("chunk_text") or "").strip()
            if len(snippet) > 800:
                snippet = snippet[:800].rstrip() + "…"

            doc_meta = result.get("metadata") or {}
            imported_at = doc_meta.get("imported_at")
            source_files = doc_meta.get("source_documents") or []
            label = result.get("title") or f"Profile Import #{idx}"
            context_parts.append(
                f"{idx}. *{label}* — imported at {imported_at or 'unknown'}:\n{snippet}\n"
            )
            sources.extend(source_files)

        return {
            "context": "\n".join(context_parts),
            "sources": sources,
        }

    # Utilities ----------------------------------------------------------------

    @staticmethod
    def _estimate_chunk_count(text: str, chunk_size: int) -> int:
        """Approximate chunk count using the vector store's paragraph-based strategy."""

        if not text or not text.strip():
            return 0

        paragraphs = [para.strip() for para in text.split("\n\n") if para.strip()]
        if not paragraphs:
            return 0

        chunks: list[str] = []
        current = ""
        for para in paragraphs:
            if current and len(current) + len(para) > chunk_size:
                chunks.append(current)
                current = para
            else:
                current = f"{current}\n\n{para}" if current else para

        if current:
            chunks.append(current)

        return len(chunks)
