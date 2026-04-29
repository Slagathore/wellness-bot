"""Create tables for adaptive psych assessment sessions."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS profile_assessment_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    focus_area TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'completed', 'cancelled')),
    question_data TEXT NOT NULL,
    current_index INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
)
"""

CREATE_RESPONSES = """
CREATE TABLE IF NOT EXISTS profile_assessment_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    question_index INTEGER NOT NULL,
    question TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES profile_assessment_sessions(id) ON DELETE CASCADE
)
"""

INDEX_SESSIONS = (
    "CREATE INDEX IF NOT EXISTS idx_assessment_sessions_user ON profile_assessment_sessions(user_id, status)"
)

INDEX_RESPONSES = (
    "CREATE INDEX IF NOT EXISTS idx_assessment_responses_session ON profile_assessment_responses(session_id)"
)


def apply(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(CREATE_SESSIONS)
        conn.execute(CREATE_RESPONSES)
        conn.execute(INDEX_SESSIONS)
        conn.execute(INDEX_RESPONSES)
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Add profile assessment tables.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("wellness_data/telegram_wellness.db"),
        help="Path to SQLite database file.",
    )
    args = parser.parse_args()
    print(f"Applying profile assessment migration to {args.db}")
    apply(args.db)
    print("Migration complete.")


if __name__ == "__main__":
    main()
