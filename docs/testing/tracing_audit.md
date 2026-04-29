Tracing & Audit Verification
============================

Goals
- Ensure spans recorded around conversation handling, reminder scans/dispatch, and safety checks.
- Confirm audit logs for admin actions and moderation events.

Checks
- Conversation: span `conversation.handle` present in logs/trace backend; message metrics emit with direction labels.
- Reminders: spans `reminders.scan` and `reminder.dispatch` present; outbound metrics increment; next-run updates logged.
- Safety: span `safety.handle` present; safety blocks increment `SAFETY_BLOCKS`; moderation_events populated for crisis keywords.
- Admin: restart/disable/create reminder actions log with correlation IDs; /readyz returns dependency status; metrics scrape works.

How to validate
- Run modular runtime with tracing-enabled logger; trigger a conversation, reminder due, and safety keyword; inspect logs for span entries and audit logs.
- Check DB `moderation_events` for crisis entries; check Prometheus metrics for counters/histograms.
- Hit admin endpoints and confirm 200s plus audit log lines.
