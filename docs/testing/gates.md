Release Gates & Verification
============================

Pre-Merge (per PR)
- Lint/type checks green; unit tests for touched modules.
- Contract tests for adapters if modified (DB/vector/LLM).
- New events/handlers documented and covered by unit tests.

Pre-Canary
- CI: unit + contract + E2E smoke (telegram onboarding/reminder create/fire).
- Metrics/logging redaction verified in staging; health/readiness endpoints passing.
- Scheduler jobs registered and visible; no orphan threads/tasks after shutdown.

Canary
- Shadow mode for new handlers where possible (dual-write/read).
- p95/p99 latency within 10% of baseline; no spike in errors or DLQ growth.
- Admin actions (restart/backup/prune) audited and authorized.

GA
- Soak test 60m at expected QPS with reminders; stable RSS and thread counts.
- Backup/restore drill completed; rollback script validated.
- DSAR export/delete path manually tested; log redaction spot-checked.
