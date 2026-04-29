# Onboarding Flow Module Documentation

**Module**: `app/onboarding/flow.py`

---

## Mission Statement

Sophia (the wellness bot) exists to deliver empathetic, proactive support that fits each user's life. The onboarding flow module orchestrates that first impression: it collects preferences, schedules wellness reminders, and ensures every follow-up respects a user's schedule, personality, and goals while maintaining psychological safety.

---

## Module Overview

- Guides new Telegram users through a structured setup conversation.
- Records profile context (timezone, reminders, personality mode, feature toggles).
- Creates customized reminders/check-in cadence while respecting user consent.
- Stores outcomes in SQLite via `app.db` helpers for use across the platform.
- Integrates LLM summaries to enrich profile context for downstream personalization.

### Referenced Modules / Classes

| Reference                              | Purpose                                                   |
| -------------------------------------- | --------------------------------------------------------- |
| `app.db.db_ro`, `app.db.db_rw`         | Database access helpers (read/write contexts).            |
| `app.feature_flags.enabled`            | Feature toggles that alter question flow.                 |
| `app.utils.ollama.generate`            | LLM generation for onboarding summaries.                  |
| `OnboardingFlow` (class within module) | Stateful conversation manager for the onboarding journey. |

---

## Key Constants

| Name                    | Description                                                            |
| ----------------------- | ---------------------------------------------------------------------- |
| `WELCOME_MESSAGE`       | Intro text establishing Sophia's role.                                 |
| `CHECK_IN_PROMPT`       | Menu prompting for automated check-in frequency.                       |
| `REMINDER_TYPES_PROMPT` | Multi-select guidance for reminder categories.                         |
| `FEATURE_PROMPT`        | Feature toggle selection prompt.                                       |
| `PERSONALITY_PROMPT`    | Personal interaction style menu.                                       |
| `TIMEZONE_PROMPT`       | (Updated) US-centric timezone guidance plus UTC fallback instructions. |

---

## Helper Functions

| Function                           | Responsibility                                                                                            |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `_classify_time_of_day(hour)`      | Buckets an hour into morning/afternoon/evening/night for reminder text.                                   |
| `_format_time(hour, minute)`       | Produces 12-hour formatted timestamps for summaries.                                                      |
| `_parse_timezone_offset(raw)`      | Normalizes abbreviations, offsets, and common US city names into minute offsets and an informative label. |
| `_detect_day_of_week(text)`        | Parses weekdays from free-form text for scheduling logic.                                                 |
| `_create_reminders(...)`           | Generates reminder entries and schedules per onboarding responses.                                        |
| `_store_profile_entries(...)`      | Persists profile_context records (timezone, goals, features, etc.).                                       |
| `_build_onboarding_summary(...)`   | Consolidates responses for admin confirmation and logging.                                                |
| `_build_confirmation_message(...)` | Crafts the final success message summarizing setup choices.                                               |
| `_finalize_onboarding(...)`        | Commits onboarding completion, triggers LLM enrichment, and returns summary payloads.                     |

---

## Class Reference

### `OnboardingFlow`

Manages user state machine for onboarding. Core responsibilities include:

| Method                                                       | Purpose                                                                                                                    |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| `_initial_state()`                                           | Seeds state dict with the starting step and empty responses.                                                               |
| `start(db_user_id)`                                          | Persists initial state and emits the welcome/check-in prompt.                                                              |
| `resume(db_user_id)`                                         | Loads saved state to continue an in-progress onboarding.                                                                   |
| `handle_response(db_user_id, message)`                       | Primary router: consumes user input, advances steps, and triggers sub-helpers (timezone parsing, reminder creation, etc.). |
| `_next_step(state, responses)`                               | Determines which prompt to send next based on collected data and feature flags.                                            |
| `_create_reminders(...)`                                     | Converts reminder selections into DB entries using timezone offsets.                                                       |
| `_build_onboarding_summary(...)`                             | Generates structured JSON for admin dashboard ingestion.                                                                   |
| `_build_confirmation_message(...)`                           | Produces the message shown to the user at completion.                                                                      |
| `_save_state(db_user_id, state)` / `_load_state(db_user_id)` | Serialize/deserialize onboarding progress in SQLite.                                                                       |
| `_reset_to_step(db_user_id, step)`                           | Allows restarting specific sections when clarification is needed.                                                          |

---

## Variables & State Artifacts

| Name                     | Description                                                                                    |
| ------------------------ | ---------------------------------------------------------------------------------------------- |
| `state`                  | Dictionary persisted per user containing `current_step`, responses, and queued reminder types. |
| `responses`              | Nested dictionary capturing the user's answers (timezone, features, preferences).              |
| `pending_reminder_types` | Queue used to drive follow-up questions about reminder timing/details.                         |

---

## Recent Update (2025-10-15)

- Enriched `TIMEZONE_PROMPT` with U.S.-focused examples (Central, Eastern, Mountain, Pacific) and reinforced city-name guidance.
- `_parse_timezone_offset` now understands common city names and spelled-out U.S. time-zone phrases, while still accepting UTC offsets. This keeps reminder scheduling aligned with user expectations without forcing a CST-only default.

---

## Future Enhancements

- Integrate geolocation or external timezone APIs to auto-detect offsets from city names and handle daylight-saving transitions dynamically.
- Extend onboarding analytics to highlight frequent clarification loops (e.g., timezone misunderstandings) for iterative UX improvements.
