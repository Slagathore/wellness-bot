"""Abstract base interface for vector backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence


class VectorBackend(ABC):
    """Interface that all vector backends must implement."""

    @abstractmethod
    def ensure_ready(self, dim: int) -> None:
        """Prepare any backing tables or resources for the given vector dimension."""

    @abstractmethod
    def upsert(self, message_id: int, vector: Sequence[float], payload: dict) -> str:
        """Insert or update the vector for a message and return the backend-specific key."""

    @abstractmethod
    def delete(self, message_id: int) -> None:
        """Remove any stored vector associated with the message."""

    @abstractmethod
    def top_k(
        self,
        user_id: int,
        query_vector: Sequence[float],
        k: int,
        role_filter: tuple[str, ...] = ("user", "assistant"),
        ts_cutoff: str | None = None,
        scope_filter: str | None = None,
    ) -> list[dict]:
        """Return the closest matching message payloads for the query vector."""
