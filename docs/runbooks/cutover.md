Cutover Runbook (Modular Runtime)
=================================

Pre-flight
- Confirm backups and rollback scripts ready.
- Metrics server running (Prometheus scrape) and /readyz passes (DB/vector/LLM checks).
- Admin auth verified; audit logging enabled.
- Feature flags/routers ready to shadow or canary traffic.

Shadow
- Run modular runtime alongside legacy; mirror inbound Telegram events (no user-facing replies).
- Monitor latency/error/safety-block/reminder delivery in logs/metrics.
- Verify moderation_events inserts and safety handler stability.
- Confirm tracing spans (conversation.handle, reminders.scan, reminder.dispatch, safety.handle) appear in logs/trace backend.

Canary
- Route small cohort to modular runtime.
- Validate onboarding, conversation, reminders (create/list/cancel), check-ins, admin actions.
- Watch p95/p99 latency, error rate, safety blocks, reminder failures; ensure metrics scrape healthy.

Full Cutover
- Shift all traffic to modular runtime; disable legacy reminder/check-in loops; archive `unified_bot.py`.
- Keep rollback plan and backups handy; monitor dashboards for 24–48h.
- Confirm CODEOWNERS updated and legacy entrypoints removed from ops scripts.

Rollback
- Switch router/feature flag back to legacy (if still available) or pause traffic.
- Restore from backups if data corruption; re-enable legacy reminder loop only if necessary.

Post-Cutover
- Update CODEOWNERS/runbooks; document outcomes; schedule cleanup of legacy assets.
