Unified Wellness Bot Refactor (Monolith → Modular Runtime)
===========================================================

Problem
-------
`unified_bot.py` is a ~500KB monolith combining UI, Telegram runtime, background workers, DB/vector access, LLM calls, and ops controls. This creates tight coupling, hard testing, fragile threading, poor observability, and security risk (secrets/PII spread).

Goals
-----
- Reliability: graceful startup/shutdown, idempotent handlers, backpressure, retries, and health checks.
- Modularity: clear bounded contexts (surfaces, domain services, infra adapters) with dependency rules.
- Observability: structured logs, correlation IDs, metrics, tracing around DB/vector/LLM.
- Security/Privacy: centralized secrets, RBAC/audit for admin, PII handling/redaction, retention policy.
- Performance/Cost: meet latency SLOs, steady memory, controlled LLM/vector costs, batching/caching where safe.
- Maintainability: small testable units, code ownership, ADRs for critical choices, runbooks.

Non-Goals (for this refactor)
-----------------------------
- Net-new product features.
- Changing user-facing behavior beyond reliability/latency fixes.
- Long-term MLOps platform; we keep minimal model registry/wrappers.

Architecture (Target)
---------------------
- Core: config/secret loader + validation, DI container + lifecycle hooks, event bus (sync/async) with backpressure and DLQ, logging/metrics/tracing setup.
- Domain services: conversation, reminders, personality/profile, moderation/safety, workfocus, outbox, onboarding. Depend only on domain ports + core.
- Infra adapters: DB repos, vector client, LLM client, file store, clock/timers. Hidden behind interfaces; retries/timeouts/circuit breakers here.
- Interfaces (surfaces): Telegram adapter, Admin surface (web preferred), HTTP ops endpoints. Thin: translate surface events ↔ domain commands/events.
- Workers/Schedulers: reminder/outbox/workfocus as scheduled jobs or queue consumers; persistent job store, dedup keys.
- Observability: metrics (latency, queue depth, failures), tracing spans (DB/vector/LLM), structured logs with correlation IDs.

Layering Rules
--------------
- Interfaces → (event bus + domain services). No surface calling infra directly.
- Domain → ports (interfaces) → adapters in `infra/`.
- Core is dependency of all; nothing depends on Interfaces.
- Cross-domain communication via events, not direct imports.

SLO Drafts (to refine with SRE/PM)
----------------------------------
- Message handling: p95 ≤ 2.5s, p99 ≤ 5s under expected QPS.
- Reminder delivery: ≥ 98% on-time (<60s skew) over 24h, 0% duplicate deliveries.
- Crash-free sessions: ≥ 99.5% per day.
- Admin actions success: ≥ 99%, audit logged within 5s.

Migration Strategy
------------------
1) Skeleton: create packages (`core/`, `domain/`, `infra/`, `interfaces/`, `workers/`, `admin/`), DI, logging, event bus, lifecycle.
2) Adapters: extract DB/vector/LLM/file access into `infra/` with ports, retries, metrics.
3) Domain: extract conversation/reminders/personality/moderation/workfocus/outbox into services; unit + contract tests.
4) Surfaces: move Telegram/admin logic into adapters that publish events/commands; remove business logic from handlers.
5) Scheduling: move reminder/outbox/workfocus to workers with persistent store; add idempotency + dedup keys.
6) Observability/Security: structured logs, metrics, tracing; secret handling; RBAC/audit; redaction.
7) Cutover: shadow + dual-write, canary, rollback scripts, archive `unified_bot.py`.

Risks & Mitigations
-------------------
- Threading/async mix: choose model (ADR 0003), add lifecycle manager and shutdown hooks.
- Data integrity during migration: dual-write and idempotent handlers; backups; migration scripts with checkpoints.
- Latency regressions: per-span tracing, load tests, cache/batch LLM/vector.
- Operator impact: admin UX changes require training; keep minimal desktop launcher until web admin is adopted.
- Moderation gaps: migrate crisis/disruption detection out of legacy `unified_bot.py` into modular safety handlers; ensure `moderation_events` persisted via repo.

Open Decisions
--------------
- Worker model: asyncio-first with internal queues vs external queue (e.g., Redis/RQ/Celery). See ADR 0003.
- Admin surface: web (preferred) vs tkinter maintenance mode. See ADR 0004.
- Persistent job store choice (SQLite WAL vs Redis) for reminders/outbox. Document in ADR 0005 if non-default.
