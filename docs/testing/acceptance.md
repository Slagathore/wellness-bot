Acceptance Criteria (Draft)
===========================

User-Facing (Telegram/Surfaces)
- Message handling: response is timely, relevant, polite; no duplicate replies per user message.
- Reminder scheduling: user-created reminders fire within 60s of target, once per occurrence, with correct text/timezone.
- Onboarding: guided flow completes; personality captured; safety consent prompts shown where required.
- Safety/Moderation: NSFW/crisis filters applied; blocked content results in safe response and audit log.
- Workfocus/Outbox: queued messages respect rate limits; cancellations honored.

Operator/Admin
- Admin login/auth required; RBAC limits destructive actions.
- Audit log captures who/what/when for admin actions (delete, prune, backup, restart).
- Diagnostics bundle exportable (logs + health) without crashing runtime.
- Start/stop/restart are graceful: no lost reminders/messages; queues drain or checkpoint.

Reliability/Performance
- p95 latency meets SLO target; p99 acceptable under load test baseline.
- No memory leak across 60-minute soak at expected QPS; threads/tasks are bounded.
- Health/readiness endpoints reflect dependency state (DB, vector, LLM).

Compliance/Privacy
- PII storage paths documented; retention rules applied; deletion/export flows function on request.
- Logs redact PII and secrets; no plaintext secrets in env or logs.
