"""Shared context object provided to runtime handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class RuntimeDeps:
    """Container for runtime dependencies passed into handler modules."""

    app: Any  # UnifiedWellnessBot or future service object
    message_queue: Any | None = None
