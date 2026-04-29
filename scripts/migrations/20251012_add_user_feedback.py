"""Create the user_feedback table required for the feedback feature."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    feedback_type TEXT NOT NULL CHECK(feedback_type IN ('bug', 'suggestion')),
    content TEXT NOT NULL,
    status TEXT DEFAULT 'new' CHECK(status IN ('new', 'reviewing', 'resolved', 'wont_fix')),
    admin_notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
)
"""

CREATE_STATUS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_feedback_status ON user_feedback(status)"
)
CREATE_TYPE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_feedback_type ON user_feedback(feedback_type)"
)


def guess_db_path() -> Path:
    """Return the first existing database path or default location."""
    candidates = [
        Path("wellness_data/telegram_wellness.db"),
        Path("wellness_data/wellness.db"),
        Path("telegram_wellness.db"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def apply_migration(db_path: Path) -> None:
    """Execute the DDL statements for the user_feedback table."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(CREATE_STATUS_INDEX)
        conn.execute(CREATE_TYPE_INDEX)
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add user_feedback table for bug reporting feature."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to the SQLite database file.",
    )
    args = parser.parse_args()

    db_path = args.db or guess_db_path()
    print(f"Running feedback table migration against {db_path}")
    apply_migration(db_path)
    print("Migration complete.")


if __name__ == "__main__":
    main()
