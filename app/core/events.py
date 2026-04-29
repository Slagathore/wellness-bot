"""
In-process event bus with sync and async handlers, retries, and dead-letter callback.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Literal, Tuple


logger = logging.getLogger(__name__)

HandlerFn = Callable[["Event"], Awaitable[None] | None]
HandlerMode = Literal["sync", "async"]
DeadLetterFn = Callable[["Event", Exception], None]


@dataclass(slots=True)
class Event:
    name: str
    payload: Dict[str, Any]
    correlation_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    attempts: int = 0


@dataclass(slots=True)
class Handler:
    fn: HandlerFn
    mode: HandlerMode
    retries: int = 3
    backoff_seconds: float = 0.5


class EventBus:
    """Simple event bus supporting sync + async handlers."""

    def __init__(
        self,
        *,
        max_queue: int = 1024,
        workers: int = 32,
        loop: asyncio.AbstractEventLoop | None = None,
        dead_letter: DeadLetterFn | None = None,
    ) -> None:
        self._handlers: Dict[str, List[Handler]] = {}
        self._queue: asyncio.Queue[Tuple[Event, Handler]] = asyncio.Queue(max_queue)
        self._worker_tasks: List[asyncio.Task[None]] = []
        self._loop = loop
        self._workers = workers
        self._dead_letter = dead_letter
        self._running = False

    def set_workers(self, workers: int) -> None:
        """Update worker count before start."""
        if self._running:
            return
        self._workers = max(1, int(workers))

    def subscribe(
        self,
        event_name: str,
        handler: HandlerFn,
        *,
        mode: HandlerMode = "async",
        retries: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        """Register a handler for a given event name."""
        bucket = self._handlers.setdefault(event_name, [])
        # Guard against double-registration: if the same callable is already
        # subscribed for this event, skip to avoid duplicate delivery.
        if any(h.fn is handler for h in bucket):
            return
        bucket.append(
            Handler(
                fn=handler, mode=mode, retries=retries, backoff_seconds=backoff_seconds
            )
        )

    def unsubscribe(self, event_name: str, handler: HandlerFn) -> bool:
        """Remove a previously registered handler.

        Returns True if the handler was found and removed, False otherwise.
        """
        bucket = self._handlers.get(event_name)
        if not bucket:
            return False
        new_bucket = [h for h in bucket if h.fn is not handler]
        if len(new_bucket) == len(bucket):
            return False
        self._handlers[event_name] = new_bucket
        return True

    def publish(
        self,
        event_name: str,
        payload: Dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> None:
        """
        Publish an event. Sync handlers run inline; async handlers are queued.
        """
        event = Event(name=event_name, payload=payload, correlation_id=correlation_id)
        for handler in self._handlers.get(event_name, []):
            if handler.mode == "sync":
                self._run_sync_handler(event, handler)
            else:
                self._enqueue(event, handler)

    def _run_sync_handler(self, event: Event, handler: Handler) -> None:
        try:
            result = handler.fn(event)
            if asyncio.iscoroutine(result):
                asyncio.get_event_loop().create_task(result)
        except Exception as exc:  # noqa: BLE001
            self._handle_failure(event, handler, exc)

    def _enqueue(self, event: Event, handler: Handler) -> None:
        if not self._loop:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
        try:
            self._queue.put_nowait((event, handler))
        except asyncio.QueueFull:
            logger.warning("Event queue full; dropping event %s", event.name)

    async def _worker(self) -> None:
        while True:
            event, handler = await self._queue.get()
            try:
                if event.name == "__shutdown__":
                    return
                await self._run_async_handler(event, handler)
            except Exception as exc:  # noqa: BLE001
                self._handle_failure(event, handler, exc)
            finally:
                self._queue.task_done()

    async def _run_async_handler(self, event: Event, handler: Handler) -> None:
        result = handler.fn(event)
        if asyncio.iscoroutine(result):
            await result

    def _handle_failure(self, event: Event, handler: Handler, exc: Exception) -> None:
        event.attempts += 1
        if event.attempts <= handler.retries:
            delay = handler.backoff_seconds * event.attempts
            logger.warning(
                "Retrying event %s after error (%s), attempt %s",
                event.name,
                exc,
                event.attempts,
            )
            if self._loop and self._loop.is_running():
                self._loop.call_later(delay, self._enqueue, event, handler)
            else:
                time.sleep(delay)
                self._enqueue(event, handler)
            return
        logger.error(
            "Event %s moved to dead-letter after %s attempts: %s",
            event.name,
            handler.retries,
            exc,
        )
        if self._dead_letter:
            try:
                self._dead_letter(event, exc)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Dead-letter handler failed for event %s", event.name)

    async def start(self) -> None:
        """Start background workers for async handlers."""
        if self._running:
            return
        if not self._loop:
            self._loop = asyncio.get_running_loop()
        self._running = True
        self._worker_tasks = [
            self._loop.create_task(self._worker()) for _ in range(self._workers)
        ]

    async def stop(self) -> None:
        """Stop background workers gracefully."""
        if not self._running:
            return
        self._running = False
        for _ in self._worker_tasks:
            await self._queue.put(
                (
                    Event(name="__shutdown__", payload={}),
                    Handler(fn=lambda e: None, mode="async"),
                )
            )
        await self._queue.join()
        for task in self._worker_tasks:
            task.cancel()
        self._worker_tasks = []


event_bus = EventBus()
