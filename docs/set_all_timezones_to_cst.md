# Bulk Timezone Normalization Script

**Script**: `scripts/set_all_timezones_to_cst.py`

---

## Mission Statement

Sophia’s production deployment currently serves users exclusively within the U.S. Central Time zone. This script guarantees that historical user profiles match that known reality so every reminder, check-in, and analytic view uses the correct local offset.

---

## Module Overview

- Reads all `users.id` values from SQLite via the shared `app.db` helpers.
- Ensures `profile_context` contains CST-aligned values for:
  - `timezone_offset_minutes` (`-360`)
  - Legacy `timezone_offset` (`-6` hours)
  - Human-readable `timezone` label (`Central Time (CST/CDT)`)
- Supports a `--dry-run` mode to preview modifications without committing changes.
- Emits a concise summary (total processed, changed count, sample user IDs) for auditing.

### Referenced Components

| Reference               | Description                                             |
| ----------------------- | ------------------------------------------------------- |
| `app.db.db_ro`          | Shared read-only connection helper.                     |
| `app.db.db_rw`          | Shared write connection helper with locking.            |
| `profile_context` table | Stores per-user metadata; script upserts timezone keys. |

---

## Functions & Data Structures

| Name                                         | Type         | Purpose                                            |
| -------------------------------------------- | ------------ | -------------------------------------------------- |
| `PROJECT_ROOT`                               | `Path`       | Repository root added to `sys.path` for imports.   |
| `CST_OFFSET_MINUTES`                         | `int`        | Canonical minute offset (-360).                    |
| `CST_OFFSET_HOURS`                           | `int`        | Legacy hour offset (-6) for compatibility.         |
| `CST_LABEL`                                  | `str`        | Human-facing label `Central Time (CST/CDT)`.       |
| `UpdateResult`                               | `@dataclass` | Records whether a given user needed changes.       |
| `_fetch_user_ids()`                          | function     | Returns every user ID present in `users`.          |
| `_determine_current_offset(user_id)`         | function     | Reads existing `timezone_offset_minutes` (if any). |
| `_upsert_profile_value(user_id, key, value)` | function     | Performs the `INSERT .. ON CONFLICT` logic.        |
| `_apply_timezone(user_id, dry_run)`          | function     | Core normalization routine; respects dry-run.      |
| `_format_summary(results)`                   | function     | Builds console summary of operations.              |
| `main(argv)`                                 | function     | Argument parsing (`--dry-run`) + orchestration.    |

---

## Usage

```bash
pwsh.exe -Command "python scripts/set_all_timezones_to_cst.py"
```

Preview without applying changes:

```bash
pwsh.exe -Command "python scripts/set_all_timezones_to_cst.py --dry-run"
```

---

## Future Enhancements

- Persist an audit log of changed rows (`#todo` present in code) for compliance tracking.
- Accept a city/state override map in case certain accounts migrate to another timezone later.
- Extend to accept CSV input for selective updates rather than blanket normalization.
