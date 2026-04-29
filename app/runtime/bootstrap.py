"""
Bootstrap helpers to wire config, logging, container, and event bus.
"""

from __future__ import annotations

import asyncio
import logging

from app.core import config as core_config
from app.core import logging as core_logging
from app.core.container import container
from app.core.events import event_bus
from app.core.lifecycle import lifecycle
from app.core.scheduler import create_scheduler
from app.runtime import wiring
from app.workers.bootstrap import register_baseline_jobs, register_checkin_job
from app.monitoring import start_metrics_server
from app.infra.db.schema_bootstrap import ensure_schema_current


def bootstrap(logging_level: str = "INFO") -> None:
    """
    Initialize global services for the new modular runtime.

    Idempotent; safe to call once in entrypoints/tests.
    """

    core_logging.configure_logging(level=logging_level)
    ensure_schema_current()
    cfg = core_config.load_config()
    try:
        event_bus.set_workers(getattr(cfg, "event_bus_workers", 32) or 32)
    except Exception:
        pass
    logging.getLogger(__name__).info(
        "Loaded config", extra={"extra_fields": {"config": core_config.redact(cfg)}}
    )

    container.register("config", lambda: cfg, singleton=True)
    container.register("event_bus", lambda: event_bus, singleton=True)
    container.register("lifecycle", lambda: lifecycle, singleton=True)

    wiring.register_defaults()
    scheduler = create_scheduler()
    register_baseline_jobs(scheduler)
    if cfg.enable_checkin_job:
        register_checkin_job(scheduler)
    wiring.register_jobs(scheduler)

    container.register("scheduler", lambda: scheduler, singleton=True)

    wiring.register_event_handlers()

    lifecycle.add_startup(event_bus.start)
    lifecycle.add_startup(scheduler.start)
    lifecycle.add_startup(lambda: start_metrics_server())
    lifecycle.add_shutdown(scheduler.shutdown)
    lifecycle.add_shutdown(event_bus.stop)


async def startup() -> None:
    """Run lifecycle startup hooks."""
    await lifecycle.startup()


async def shutdown() -> None:
    """Run lifecycle shutdown hooks."""
    await lifecycle.shutdown()


def run_lifecycle(main_coro) -> None:
    """
    Helper to run a main coroutine with lifecycle.

    Example:
        run_lifecycle(async_main())
    """

    async def _wrapper():
        await startup()
        try:
            await main_coro
        finally:
            await shutdown()

    asyncio.run(_wrapper())
