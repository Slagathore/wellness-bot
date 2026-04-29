"""Create tables for conversation memory v2 and profile import documents."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


CONV_TABLE = """
CREATE TABLE IF NOT EXISTS conversation_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    topics TEXT,
    embedding TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
)
"""

CONV_INDEX_USER = (
    "CREATE INDEX IF NOT EXISTS idx_conv_embeddings_user ON conversation_embeddings(user_id, created_at DESC)"
)
CONV_INDEX_MESSAGE = (
    "CREATE INDEX IF NOT EXISTS idx_conv_embeddings_message ON conversation_embeddings(message_id)"
)

IMPORT_TABLE = """
CREATE TABLE IF NOT EXISTS profile_import_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    file_name TEXT,
    source TEXT,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
)
"""

IMPORT_INDEX_USER = (
    "CREATE INDEX IF NOT EXISTS idx_profile_import_user ON profile_import_documents(user_id, created_at DESC)"
)


def apply(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(CONV_TABLE)
        conn.execute(CONV_INDEX_USER)
        conn.execute(CONV_INDEX_MESSAGE)
        conn.execute(IMPORT_TABLE)
        conn.execute(IMPORT_INDEX_USER)
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Add conversation memory v2 tables.")
    parser.add_argument("--db", type=Path, default=Path("wellness_data/telegram_wellness.db"))
    args = parser.parse_args()

    print(f"Applying conversation memory migration to {args.db}")
    apply(args.db)
    print("Migration complete.")


if __name__ == "__main__":
    main()
