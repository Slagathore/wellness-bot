"""
Vector client wrapper to standardize operations, retries, and backend swapping.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, List, Sequence, cast

from app.config import settings
from app.vector_backends import get_backend

logger = logging.getLogger(__name__)


class VectorClient:
    """Thin facade over the configured vector backend with basic retries."""

    def __init__(
        self,
        backend: Any | None = None,
        *,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
    ) -> None:
        cfg = settings()
        self._backend = backend or get_backend()
        self._max_retries = (
            max_retries if max_retries is not None else cfg.vector_max_retries
        )
        self._backoff = (
            backoff_seconds
            if backoff_seconds is not None
            else cfg.vector_backoff_seconds
        )

    def upsert(self, items: Iterable[dict[str, Any]]) -> int:
        """Insert or update embeddings; returns count."""
        return cast(int, self._call_with_retry("upsert", items))

    def delete(self, ids: Sequence[str]) -> int:
        """Delete embeddings by id; returns count deleted."""
        return cast(int, self._call_with_retry("delete", ids))

    def search(self, query: Any, k: int = 5) -> List[Any]:
        """Similarity search wrapper."""
        return cast(List[Any], self._call_with_retry("search", query, k=k))

    def _call_with_retry(self, method: str, *args: Any, **kwargs: Any):
        fn = getattr(self._backend, method, None) or getattr(
            self._backend, f"{method}_vector", None
        )
        if not callable(fn):
            raise NotImplementedError(f"Backend does not support {method}")

        attempts = 0
        last_exc: Exception | None = None
        while attempts <= self._max_retries:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                attempts += 1
                if attempts > self._max_retries:
                    break
                delay = self._backoff * attempts
                logger.warning(
                    "Vector %s failed (attempt %s/%s): %s",
                    method,
                    attempts,
                    self._max_retries + 1,
                    exc,
                )
                time.sleep(delay)
        if last_exc:
            raise last_exc
        raise RuntimeError(f"Vector {method} failed without exception")


def default_vector_client() -> VectorClient:
    return VectorClient()
