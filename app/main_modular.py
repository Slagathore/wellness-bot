"""
Entry point for the modular runtime (event-bus + scheduler + telegram adapter).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from app.core.container import container
from app.core.events import event_bus
from app.domain import events
from app.interfaces.telegram.adapter import TelegramAdapter
from app.runtime.bootstrap import bootstrap, shutdown, startup
from app.runtime.bot_service import BotRuntime

logger = logging.getLogger(__name__)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Handle SIGINT/SIGTERM to trigger graceful shutdown."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


async def _run() -> None:
    await startup()

    cfg = container.resolve("config")
    adapter = TelegramAdapter()
    bot = BotRuntime(
        token=cfg.telegram_bot_token,
        register_handlers=adapter.register,
        on_started=lambda: logger.info("Telegram runtime started"),
        on_stopped=lambda err: (
            logger.error("Telegram runtime stopped: %s", err)
            if err
            else logger.info("Telegram runtime stopped")
        ),
    )
    bot.start()

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    async def _handle_restart(event) -> None:
        logger.info("Admin restart requested via event; stopping runtime.")
        stop_event.set()

    event_bus.subscribe(events.EVENT_ADMIN_RESTART, _handle_restart, mode="async")

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.info("Event loop cancelled, initiating shutdown.")
    finally:
        # Shut down scheduler first so APScheduler jobs stop firing
        # before the event loop is torn down.
        try:
            scheduler = container.resolve("scheduler")
            scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        bot.stop()
        await shutdown()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bootstrap()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down.")


if __name__ == "__main__":
    main()
