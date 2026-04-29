# Reminder Service Module Documentation

**Module**: `app/domain/reminders/service.py`
**Purpose**: Domain-level orchestration for reminder scheduling and dispatch
**Status**: Active and invoked by modular runtime + admin APIs

---

## Mission Statement

Keep Wellness Bot users on track with supportive nudges by translating normalized reminder
commands into durable storage operations and high-signal delivery events. The module protects the
scheduling rules, minimizes duplicate deliveries, and ensures every reminder event carries the
context (text, timezone, chat, metadata) downstream workers require for empathetic messaging.

---

## Module Overview

- Accepts create/update/disable commands and persists them via a repository abstraction.
- Queries due reminders, decorates them with context, and publishes `EVENT_REMINDER_DUE` messages.
- Provides convenience helpers for legacy custom reminders so onboarding/admin flows can reuse the
  same scheduling surface area.
- Delegates storage concerns to `ReminderRepository` implementations (SQLite today, but pluggable).

---

## Classes

### `Reminder`

Domain data container describing a single reminder instance.

- `id (str)`: Primary key / persistent identifier.
- `user_id (str)`: Owning user.
- `text (str)`: Human-friendly reminder content.
- `due_at (datetime)`: Next scheduled fire time.
- `timezone (str | None)`: Optional user timezone override.
- `recurring (bool)`: Flag for repeating reminders.
- `recurrence (str | None)`: Cron/keyword expression for future runs.
- `metadata (dict | None)`: Extra scheduling/context payload supplied at creation.
- `chat_id (int | None)`: Target Telegram chat for delivery; enables channel-aware fan out.

### `ReminderRepository`

`typing.Protocol` describing the persistence contract. Implementations must provide CRUD behavior
and scheduling helpers so the service can remain persistence-agnostic.

### `ReminderService`

Facade coordinating reminder lifecycle. Consumes repository implementations, publishes events, and
exposes helper methods for onboarding/admin flows.

---

## Methods & Their Roles

| Method                                                     | Responsibility                                                                                                                      |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `process_due(now)`                                         | Fetch due reminders, publish `EVENT_REMINDER_DUE` with chat/text metadata, return count.                                            |
| `mark_sent(reminder_id, sent_at)`                          | Mark a reminder as delivered in storage.                                                                                            |
| `list_for_user(user_id, limit)`                            | Return existing reminders for administrative views.                                                                                 |
| `disable(reminder_id)`                                     | Pause a single reminder.                                                                                                            |
| `disable_all_for_user(user_id)`                            | Bulk-disable when users opt out.                                                                                                    |
| `create(cmd)`                                              | Persist a reminder. Does **not** emit an immediate due event — the periodic scanner handles dispatch when `next_run_at` is reached. |
| `mark_sent_and_schedule_next(reminder_id, next_send_time)` | Update both delivery marker and next cron fire.                                                                                     |
| `create_custom_reminder(...)`                              | Legacy convenience builder that normalizes metadata + command creation.                                                             |

---

## Variables & Data Contracts

- `Reminder.metadata`: Expects serialized payloads from onboarding/custom reminder builders.
- `Reminder.chat_id`: Populated by repositories joining against `users.telegram_user_id` so the
  worker delivering events knows where to send the message.
- `events.EVENT_REMINDER_DUE` payload schema: `{reminder_id, user_id, chat_id, text, due_at,
timezone, metadata}`.

---

## Collaborators and Dependencies

- `app.domain.reminders.commands.CreateReminderCommand`: Strongly-typed creation input.
- `app.domain.reminders.payloads.build_payload`: Used by `create_custom_reminder` to mirror legacy
  metadata formats.
- `app.core.events.event_bus`: Publish/subscribe mechanism for downstream reminder workers.
- `app.domain.events`: Provides `EVENT_REMINDER_DUE` constant.
- Repository implementations (e.g., `app.infra.db.reminders_repo.SqliteReminderRepository`).

---

## Referenced Modules / Classes

- `SqliteReminderRepository` (implements `ReminderRepository` protocol)
- `CreateReminderCommand`
- `ReminderPayload`
- `event_bus`
- `EVENT_REMINDER_DUE`

---

## Extension Ideas

- Capture per-channel delivery priorities so SMS/email adapters can share the same service surface.
- Track reminder-level analytics counters (sent/acknowledged) for health dashboards.

---

## Cloud Drain Prevention (Phase 8 Fix)

### Problem

Stuck-due reminders whose `next_run_at` was in the past were re-scanned every 60 seconds.
Each scan produced cloud LLM calls (up to 4 per reminder with event-bus retries). With 6–14
stuck reminders, this generated ~24+ cloud calls/minute, exhausting the weekly Ollama Cloud
quota in under a day.

### Root Causes

1. **No fallback on LLM failure**: If `_generate_message()` raised, `mark_sent_and_schedule_next`
   never ran, leaving the reminder perpetually stuck-due.
2. **Immediate fire on create**: `service.create()` published `EVENT_REMINDER_DUE` instantly,
   even for future reminders — causing unnecessary LLM calls.
3. **"daily" not a valid cron**: `_compute_next_run()` passed `"daily"` to `croniter`, which
   raised, returning `None` and disabling recurring reminders instead of rescheduling them.
4. **Background workers defaulted to cloud model**: Sentiments, nightly, and dispatcher all
   used `settings().chat_model` (cloud) instead of a dedicated local `worker_model`.

### Fixes Applied

| File                              | Change                                                                                                                                  |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `dispatcher.py:handle_due`        | LLM try/except with fallback text; `mark_sent_and_schedule_next` always runs                                                            |
| `dispatcher.py:_compute_next_run` | Handles `daily`, `weekly`, `hourly`, `every_other_day` frequency strings using `timedelta`; preserves `specific_hour`/`specific_minute` |
| `service.py:create`               | Removed immediate `EVENT_REMINDER_DUE` publish                                                                                          |
| `config.py`                       | Added `worker_model: str \| None = None` setting                                                                                        |
| `sentiments.py`                   | Uses `settings().worker_model` in `generate()` call                                                                                     |
| `nightly.py`                      | Uses `worker_model` for sentiment reprocessing and psych profiles                                                                       |
| `.env`                            | Added `WORKER_MODEL=huihui_ai/gemma3n-abliterated:e2b-fp16`                                                                             |

### Configuration

Add to `.env`:

```
WORKER_MODEL=huihui_ai/gemma3n-abliterated:e2b-fp16
```

When set, all background workers (sentiments, nightly, reminder dispatch) use this local model
instead of `CHAT_MODEL`. When unset (`None`), falls back to `CHAT_MODEL`.
