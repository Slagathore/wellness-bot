from __future__ import annotations

import asyncio

import pytest

from app.core.events import EventBus


@pytest.mark.asyncio
async def test_event_bus_async_handler_invoked():
    bus = EventBus()
    hit = asyncio.Event()

    async def handler(event):
        if event.payload["v"] == 1:
            hit.set()

    bus.subscribe("ping", handler, mode="async")
    await bus.start()
    bus.publish("ping", {"v": 1})

    await asyncio.wait_for(hit.wait(), timeout=1.0)
    await bus.stop()


@pytest.mark.asyncio
async def test_event_bus_retries_and_dead_letter():
    errors = []
    bus = EventBus(dead_letter=lambda e, exc: errors.append((e.name, str(exc))))
    attempts = {"count": 0}

    def handler(_event):
        attempts["count"] += 1
        raise RuntimeError("boom")

    bus.subscribe("fail", handler, mode="sync", retries=2, backoff_seconds=0)
    await bus.start()
    bus.publish("fail", {})
    await asyncio.sleep(0.1)  # allow retries
    await bus.stop()

    assert attempts["count"] == 3  # initial + 2 retries
    assert errors and errors[0][0] == "fail"
