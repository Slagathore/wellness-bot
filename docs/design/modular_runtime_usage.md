Running the Modular Runtime
===========================

Prereqs
- `.env` populated (telegram_bot_token, vector backend, etc.)—loaded via `app/config.py`.
- Python deps installed (`requirements.txt`), SQLite DB reachable at configured path.

Commands
- Start modular runtime (telegram + event bus + scheduler):
  - `python -m app.main_modular`
- Start admin/ops server (health/readiness/metrics stubs):
  - `python -m app.interfaces.admin.server`

What Runs
- Bootstrap config + DI + event bus + scheduler.
- Telegram adapter publishes user messages to the event bus and consumes send-reply events.
- Reminder scan job every 60s emits due reminders → send-reply events and marks sent.
- Heartbeat job every 60s emits `system.heartbeat` events.

Stopping
- Ctrl+C triggers graceful shutdown (stops telegram runtime, scheduler, event bus).
