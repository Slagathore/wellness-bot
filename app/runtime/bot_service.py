"""Telegram runtime orchestration extracted from unified_bot."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from contextlib import suppress
from typing import Callable, Protocol

from telegram.ext import (
    Application,
)

logger = logging.getLogger(__name__)


class HandlerRegistrar(Protocol):
    """Callable that registers handlers on a telegram Application."""

    def __call__(
        self, app: Application
    ) -> None:  # pragma: no cover - protocol definition
        ...


class BotRuntime:
    """
    Wrapper around python-telegram-bot Application lifecycle.

    This class will gradually absorb the logic currently in UnifiedWellnessBot.start_bot /
    stop_bot so the UI can delegate control instead of owning the entire stack.
    """

    def __init__(
        self,
        *,
        token: str,
        register_handlers: HandlerRegistrar | None = None,
        before_start: Callable[[Application], None] | None = None,
        on_started: Callable[[], None] | None = None,
        on_stopped: Callable[[Exception | None], None] | None = None,
    ) -> None:
        self._token = token
        self._register_handlers = register_handlers
        self._before_start = before_start
        self._on_started = on_started
        self._on_stopped = on_stopped
        self._application: Application | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = threading.Event()
        self._stop_requested = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def application(self) -> Application | None:
        return self._application

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def event_loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def start(self) -> None:
        """Start the telegram bot on a background thread."""

        if self._thread and self._thread.is_alive():
            logger.debug("BotRuntime.start() ignored; already running.")
            return

        self._stop_requested.clear()

        def _run() -> None:
            error: Exception | None = None
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._application = (
                    Application.builder()
                    .token(self._token)
                    .concurrent_updates(8)
                    .build()
                )
                if self._register_handlers:
                    self._register_handlers(self._application)
                if self._before_start:
                    self._before_start(self._application)
                self._running.set()
                if self._on_started:
                    self._on_started()
                self._application.run_polling(drop_pending_updates=False)
            except Exception as exc:  # noqa: BLE001
                error = exc
                logger.exception("BotRuntime thread crashed: %s", exc)
            finally:
                self._running.clear()
                loop = self._loop
                application = self._application
                with suppress(Exception):
                    if application and loop:
                        loop.run_until_complete(application.shutdown())
                        loop.run_until_complete(application.stop())
                if loop and not loop.is_closed():
                    loop.stop()
                    loop.close()
                if self._on_stopped:
                    self._on_stopped(error)

        self._thread = threading.Thread(
            target=_run, name="telegram-runtime", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 20.0) -> None:
        """Stop the telegram bot and wait for background thread to exit."""

        if not self._thread or not self._thread.is_alive():
            return

        self._stop_requested.set()
        stop_future: Future[None] | None = None
        if self._application and self._loop:
            try:
                stop_future = asyncio.run_coroutine_threadsafe(
                    self._application.stop(), self._loop
                )
                if stop_future is not None:
                    stop_future.result(timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("BotRuntime stop encountered error: %s", exc)
                if stop_future:
                    with suppress(Exception):
                        stop_future.cancel()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._running.clear()
        self._thread = None
        self._application = None
        self._loop = None
