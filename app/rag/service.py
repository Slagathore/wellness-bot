"""Shared helpers for accessing RAG components."""

from __future__ import annotations

import logging
from threading import RLock
from typing import Iterable

from .ingestion import ResourceIngester
from .retrieval import WellnessRetriever
from .vector_store import WellnessVectorStore

_LOGGER = logging.getLogger(__name__)
_STORE: WellnessVectorStore | None = None
_RETRIEVER: WellnessRetriever | None = None
_LOCK = RLock()


def get_vector_store() -> WellnessVectorStore:
    """Return a process-wide wellness vector store instance."""

    global _STORE
    with _LOCK:
        if _STORE is None:
            _STORE = WellnessVectorStore()
            stats = _STORE.get_stats()
            if stats.get("total_documents", 0) == 0:
                _LOGGER.info("[RAG] No documents found; ingesting seed data")
                ResourceIngester(_STORE).ingest_seed_data()
        return _STORE


def get_retriever() -> WellnessRetriever:
    """Return a process-wide wellness retriever instance."""

    global _RETRIEVER
    with _LOCK:
        if _RETRIEVER is None:
            store = get_vector_store()
            _RETRIEVER = WellnessRetriever(store)
        return _RETRIEVER


def format_citations(sources: Iterable[str]) -> str:
    """Format RAG citation strings for user display."""

    cleaned = []
    for source in sources or []:
        if not source:
            continue
        # Clean up source citations (remove common artifacts)
        line = str(source).strip()
        if not line:
            continue
        cleaned.append(f"- {line}")

    if not cleaned:
        return ""

    return "\n\nSources:\n" + "\n".join(cleaned)
