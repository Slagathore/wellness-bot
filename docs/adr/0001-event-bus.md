ADR 0001: Internal Event Bus
============================

Context
-------
We need to decouple surfaces (Telegram/admin/HTTP) from domain services and workers, while keeping low latency and predictable backpressure. The codebase mixes direct calls and threads without observability or retries.

Decision
--------
- Introduce an in-process event bus with typed events and handlers.
- Support sync handlers (for light, in-thread work) and async handlers via an internal queue + worker pool.
- Provide middleware hooks for logging, metrics, tracing, and auth/PII redaction.
- Delivery semantics: at-least-once per handler with idempotency keys on events; dead-letter queue for exceeded retries.
- Backpressure: bounded queue with drop/park policy configured per event type; metrics on queue depth and handler latency.

Consequences
------------
- Surfaces publish events; domain services subscribe—no direct cross-surface calls.
- Failure isolation improves; retries and DLQ available.
- Slight overhead vs direct calls; mitigated by metrics visibility.
- External queue (Redis/Celery/etc.) can be swapped in later behind the same interface.

