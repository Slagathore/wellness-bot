Cutover Checklist (Shadow/Canary → Full)
========================================

Pre-Cutover
- Metrics server up; health/ready endpoints passing; admin auth works.
- Onboarding gate active; conversation/reminder/check-in flows driven by event bus.
- Safety filter + safety handler logging to `moderation_events`; audits enabled for admin actions.
- Legacy reminder/check-in/wellness/disruption logic in `unified_bot.py` disabled.

Shadow
- Run modular runtime in parallel; mirror inbound Telegram updates to modular path (no user-facing responses) and compare logs/latency.
- Verify moderation_events insertions and safety blocks; ensure no crashes.

Canary
- Route small cohort to modular runtime; monitor metrics (latency, errors, safety blocks, reminders/check-ins delivered).
- Verify admin actions (restart, reminder disable/create) work; metrics scrape succeeds.
- Exercise safety/crisis scenarios; confirm moderation_events entries and audits.

Full Cutover
- Route all traffic to modular runtime; stop legacy reminder/check-in loops; archive `unified_bot.py`.
- Update runbooks/CODEOWNERS; ensure backup/rollback plan.
- Remove legacy worker scripts (bus_consumer, telegram_webhook) from ops paths.

Post-Cutover
- Soak 24–48h; watch p95/p99, error rates, safety blocks, moderation events.
- Confirm onboarding, reminders, check-ins, conversation persistence, and admin ops behave.
