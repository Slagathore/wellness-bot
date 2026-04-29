"""
Lightweight dependency injection container and resource registry.

This is intentionally minimal to keep construction/teardown explicit and testable.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Tuple


Provider = Callable[[], Any]


class Container:
    """Simple DI container with singleton and factory support."""

    def __init__(self) -> None:
        self._providers: Dict[str, Tuple[Provider, bool]] = {}
        self._singletons: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def register(
        self, name: str, provider: Provider, *, singleton: bool = True
    ) -> None:
        """Register a provider function; singleton caches result."""
        self._providers[name] = (provider, singleton)

    def resolve(self, name: str) -> Any:
        """Resolve an instance; build and cache singleton if needed."""
        if name in self._singletons:
            return self._singletons[name]
        if name not in self._providers:
            raise KeyError(f"Provider not found: {name}")
        provider, singleton = self._providers[name]
        instance = provider()
        if singleton:
            with self._lock:
                if name not in self._singletons:
                    self._singletons[name] = instance
                else:
                    instance = self._singletons[name]
        return instance

    def clear_singletons(self) -> None:
        """Reset cached singletons (useful for tests)."""
        with self._lock:
            self._singletons.clear()

    def __contains__(self, name: str) -> bool:  # pragma: no cover - trivial
        return name in self._providers


container = Container()
