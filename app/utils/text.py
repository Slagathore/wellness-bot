"""Helpers for working with text embeddings."""

from __future__ import annotations

from typing import Sequence

from app.utils.ollama import get_embeddings, get_embeddings_async


def embed_text(text: str, timeout: float = 10.0) -> list[float]:
    """Generate an embedding vector for the provided text via Ollama."""

    return get_embeddings([text], timeout=timeout)[0]


def embed_batch(texts: Sequence[str], timeout: float = 10.0) -> list[list[float]]:
    """Generate embeddings for multiple texts sequentially."""

    if not texts:
        return []
    return get_embeddings(list(texts), timeout=timeout)


async def embed_text_async(text: str, timeout: float = 10.0) -> list[float]:
    """Async convenience wrapper for generating a single embedding."""

    embeddings = await get_embeddings_async([text], timeout=timeout)
    return embeddings[0]


async def embed_batch_async(
    texts: Sequence[str], timeout: float = 10.0
) -> list[list[float]]:
    """Async convenience wrapper for generating embeddings."""

    if not texts:
        return []
    return await get_embeddings_async(list(texts), timeout=timeout)
