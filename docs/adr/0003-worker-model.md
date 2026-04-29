ADR 0003: Worker Model & Scheduling
===================================

Context
-------
Current code uses ad-hoc threads for reminders/outbox/workfocus and mixes asyncio (telegram) with tkinter UI. We need predictable scheduling, retries, and graceful shutdown.

Decision
--------
- Adopt asyncio-first workers inside the process, orchestrated by a scheduler (APScheduler AsyncIO scheduler) backed by a persistent store (SQLite WAL initially).
- Use bounded async queues for event bus async handlers; thread pool only for CPU-heavy or blocking I/O that cannot be made async.
- Jobs carry idempotency keys; retries with exponential backoff; dead-letter table/file for manual replay.
- Provide shim to swap scheduler/queue to Redis/RQ/Celery later without changing domain contracts.

Consequences
------------
- Consistent concurrency model aligned with telegram async handlers.
- Reduced thread proliferation; easier graceful shutdown via lifecycle manager.
- Requires careful integration with tkinter if kept; admin surface preferably moves to web to avoid loop conflicts.

