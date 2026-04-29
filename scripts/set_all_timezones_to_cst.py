"""Mission Statement: ensure every stored user timezone matches the known CST deployment baseline so reminders fire at the expected local times."""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import db_ro, db_rw

CST_OFFSET_MINUTES = -360
CST_OFFSET_HOURS = -6
CST_LABEL = "Central Time (CST/CDT)"


@dataclass
class UpdateResult:
    """Container for the outcome of a timezone update."""

    user_id: int
    changed: bool


def _fetch_user_ids() -> List[int]:
    """Return all user IDs currently in the database."""

    with db_ro() as conn:
        rows = conn.execute("SELECT id FROM users").fetchall()
    return [int(row["id"]) for row in rows]


def _determine_current_offset(user_id: int) -> int | None:
    """Read the existing timezone_offset_minutes value for a user."""

    query = (
        "SELECT value FROM profile_context "
        "WHERE user_id = ? AND key = 'timezone_offset_minutes'"
    )
    with db_ro() as conn:
        row = conn.execute(query, (user_id,)).fetchone()
    if not row:
        return None
    try:
        return int(float(row["value"]))
    except (TypeError, ValueError):
        return None


def _upsert_profile_value(user_id: int, key: str, value: str) -> None:
    """Write a profile_context value, overriding existing records if present."""

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO profile_context (user_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, key)
            DO UPDATE SET value = excluded.value, updated_at = datetime('now')
            """,
            (user_id, key, value),
        )


def _apply_timezone(user_id: int, dry_run: bool) -> UpdateResult:
    """Ensure the user has CST-aligned values."""

    current_offset = _determine_current_offset(user_id)
    if current_offset == CST_OFFSET_MINUTES:
        return UpdateResult(user_id=user_id, changed=False)

    if dry_run:
        return UpdateResult(user_id=user_id, changed=True)

    _upsert_profile_value(user_id, "timezone_offset_minutes", str(CST_OFFSET_MINUTES))
    _upsert_profile_value(user_id, "timezone_offset", str(CST_OFFSET_HOURS))
    _upsert_profile_value(user_id, "timezone", CST_LABEL)
    #todo persist audit trail of timezone normalization actions for compliance reporting
    return UpdateResult(user_id=user_id, changed=True)


def _format_summary(results: Iterable[UpdateResult]) -> str:
    """Generate a human-readable summary of update operations."""

    total = 0
    changed = 0
    changed_ids: List[int] = []
    for result in results:
        total += 1
        if result.changed:
            changed += 1
            changed_ids.append(result.user_id)
    if total == 0:
        return "No users found."
    lines = [
        f"Processed {total} users.",
        f"Applied CST for {changed} users (already aligned: {total - changed}).",
    ]
    if changed_ids:
        sample = ", ".join(str(uid) for uid in changed_ids[:10])
        if len(changed_ids) > 10:
            sample += ", ..."
        lines.append(f"Updated user IDs: {sample}")
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    """Script entry point for bulk timezone normalization."""

    parser = argparse.ArgumentParser(description="Normalize all user timezones to CST.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which users would change without modifying the database.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    user_ids = _fetch_user_ids()
    results = [_apply_timezone(user_id, args.dry_run) for user_id in user_ids]
    print(_format_summary(results))
    if args.dry_run:
        print("\nDry run only. No database rows were modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
