"""
Lifecycle manager to orchestrate startup/shutdown of resources in order.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Awaitable, Callable, Deque, Iterable

Hook = Callable[[], Awaitable[None] | None]


class LifecycleManager:
    """Manages ordered startup/shutdown hooks with async support."""

    def __init__(self) -> None:
        self._startup: Deque[Hook] = deque()
        self._shutdown: Deque[Hook] = deque()

    def add_startup(self, hook: Hook) -> None:
        self._startup.append(hook)

    def add_shutdown(self, hook: Hook) -> None:
        # shutdown runs in reverse registration order
        self._shutdown.appendleft(hook)

    async def _run_hooks(self, hooks: Iterable[Hook]) -> None:
        for hook in hooks:
            result = hook()
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result

    async def startup(self) -> None:
        await self._run_hooks(self._startup)

    async def shutdown(self) -> None:
        await self._run_hooks(self._shutdown)


lifecycle = LifecycleManager()
