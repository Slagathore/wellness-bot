"""Runtime interface protocols used for typing."""

from __future__ import annotations

from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from telegram.ext import Application


class UnifiedWellnessBot(Protocol):
    """Structural typing alias for the shared bot/controller interface."""

    telegram_app: "Application | None"

    def __getattr__(self, name: str) -> Any: ...
