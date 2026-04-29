Testing Matrix (Draft)
======================

Unit
- Domain services: conversation, reminders, personality/profile, moderation/safety, workfocus, outbox.
- Parsers: reminder intent parser, timezone parsing, text cleaning.
- Ports/adapters: DB repos, vector client, LLM client (fakes for tests).

Contract/Integration
- DB repo contract tests against ephemeral SQLite (WAL) with migrations applied.
- Vector client contract tests (use test backend or in-memory stub).
- LLM adapter contract tests using recorded fixtures/golden responses.
- Event bus handler contracts: publish/subscribe semantics, retries, backpressure.

End-to-End
- Telegram flows: onboarding, basic conversation, reminder creation, NSFW/crisis handling paths.
- Admin flows: login, restart, backup, prune memory, diagnostics export.
- Scheduler flows: reminder fired, outbox send, workfocus tick with idempotency keys.

Non-Functional
- Load: soak at expected QPS with concurrent reminders; measure p95/p99 latency and memory.
- Fault injection: DB down, vector down, LLM timeout; expect degraded but safe behavior and alerts.
- Security: auth bypass attempts on admin/API; webhook signature check; log redaction verification.

Coverage Goals
- ≥80% branch coverage on domain + adapters; critical paths (reminders/onboarding/moderation) with golden tests.
- E2E smoke runs on CI per commit; nightly extended suite with load/fault tests.
