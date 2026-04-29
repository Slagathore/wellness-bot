"""
Import adventure threads and message history from the Perchance export.

Maps threads → adventures, messages → adventure_messages.
Grants access to Cole and C.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EXPORT_FILE = PROJECT_ROOT / "perchance-characters-export-2026-02-06.json"

# Only import threads with at least this many messages (skip stubs)
MIN_MESSAGES = 2

# Users to own the imported adventures
TARGET_USERS = ["C", "Cole"]


def _ts(ms: int | None) -> str:
    """Convert millisecond epoch to ISO-8601 UTC string."""
    if not ms:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def main() -> None:
    with open(EXPORT_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    tables: dict[str, list[dict]] = {}
    for entry in raw.get("data", {}).get("data", []):
        tables[entry["tableName"]] = entry.get("rows", [])

    perchance_chars = tables.get("characters", [])
    threads = tables.get("threads", [])
    messages = tables.get("messages", [])

    # ------------------------------------------------------------------
    # Connect and build name→db_id map for characters
    # ------------------------------------------------------------------
    cfg = settings()
    conn = sqlite3.connect(cfg.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Map perchance character name → our DB id
    db_chars = conn.execute(
        "SELECT id, name, display_name FROM custom_characters"
    ).fetchall()
    name_to_db_id: dict[str, int] = {}
    for row in db_chars:
        name_to_db_id[row["name"]] = row["id"]
        if row["display_name"]:
            name_to_db_id[row["display_name"]] = row["id"]

    # Map perchance char id → our DB id
    perchance_id_to_db_id: dict[int, int] = {}
    for pc in perchance_chars:
        pid = pc.get("id")
        pname = pc.get("name", "")
        if pid is not None and pname in name_to_db_id:
            perchance_id_to_db_id[pid] = name_to_db_id[pname]

    logger.info(
        "Mapped %d/%d Perchance characters to DB", len(perchance_id_to_db_id), len(perchance_chars)
    )

    # ------------------------------------------------------------------
    # Resolve target users
    # ------------------------------------------------------------------
    target_user_ids: list[int] = []
    for name in TARGET_USERS:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_username = ? OR display_name = ? LIMIT 1",
            (name, name),
        ).fetchone()
        if row:
            target_user_ids.append(row["id"])
            logger.info("  User '%s' → id=%d", name, row["id"])
        else:
            logger.warning("  User '%s' not found — skipping", name)

    if not target_user_ids:
        logger.error("No target users found. Aborting.")
        sys.exit(1)

    # Primary owner = first resolved user (C)
    owner_id = target_user_ids[0]

    # ------------------------------------------------------------------
    # Group messages by thread
    # ------------------------------------------------------------------
    msg_by_thread: dict[int, list[dict]] = {}
    for m in messages:
        tid = m.get("threadId")
        if tid is not None:
            msg_by_thread.setdefault(tid, []).append(m)

    # Sort each thread's messages by order then creationTime
    for tid in msg_by_thread:
        msg_by_thread[tid].sort(key=lambda m: (m.get("order", 0), m.get("creationTime", 0)))

    # ------------------------------------------------------------------
    # Check for already-imported adventures
    # ------------------------------------------------------------------
    existing_titles = {
        row[0]
        for row in conn.execute("SELECT title FROM adventures WHERE user_id = ?", (owner_id,)).fetchall()
    }

    # ------------------------------------------------------------------
    # Import each thread
    # ------------------------------------------------------------------
    adventures_imported = 0
    messages_imported = 0

    for thread in threads:
        tid = thread.get("id")
        tid_int = tid if isinstance(tid, int) else None
        msgs = msg_by_thread.get(tid_int, []) if tid_int is not None else []

        # Filter out threads with too few real messages
        real_msgs = [m for m in msgs if m.get("message", "").strip()]
        if len(real_msgs) < MIN_MESSAGES:
            logger.info("  Skipping thread %s (%d messages) — too few", tid, len(real_msgs))
            continue

        # Build title
        raw_name = thread.get("name", "").strip()
        primary_perchance_char_id = thread.get("characterId")
        primary_perchance_char_id_int = (
            primary_perchance_char_id if isinstance(primary_perchance_char_id, int) else None
        )
        primary_char_name = next(
            (pc.get("name", "") for pc in perchance_chars if pc.get("id") == primary_perchance_char_id_int),
            "Unknown",
        )
        title = raw_name if raw_name and raw_name != "Unnamed Thread" else f"Adventure with {primary_char_name}"

        if title in existing_titles:
            logger.info("  Skipping already-imported adventure: %s", title)
            continue

        created_at = _ts(thread.get("creationTime"))
        updated_at = _ts(thread.get("lastMessageTime"))

        # Gather lore book description if any
        lore_book_id = thread.get("loreBookId")
        description = f"Imported from Perchance. Character: {primary_char_name}."
        if lore_book_id:
            description += f" Lore book: {lore_book_id}."

        # Insert adventure
        cur = conn.execute(
            """
            INSERT INTO adventures (user_id, title, description, status, created_at, updated_at)
            VALUES (?, ?, ?, 'completed', ?, ?)
            """,
            (owner_id, title, description, created_at, updated_at),
        )
        adv_id = cur.lastrowid
        assert adv_id is not None

        existing_titles.add(title)

        # Link primary character if we know its DB id
        primary_db_char_id = (
            perchance_id_to_db_id.get(primary_perchance_char_id_int)
            if primary_perchance_char_id_int is not None
            else None
        )
        if primary_db_char_id:
            conn.execute(
                "INSERT OR IGNORE INTO adventure_characters (adventure_id, character_id, role) VALUES (?, ?, 'npc')",
                (adv_id, primary_db_char_id),
            )

        # Track other characters that appear in this thread
        seen_char_ids: set[int] = set()
        if primary_db_char_id:
            seen_char_ids.add(primary_db_char_id)

        # Import messages
        for m in real_msgs:
            char_id = m.get("characterId")
            msg_text = m.get("message", "").strip()
            if not msg_text:
                continue

            # Map to role
            if char_id == -1:
                role = "user"
                db_char_id = None
            elif char_id == -2:
                role = "narrator"
                db_char_id = None
            else:
                role = "character"
                db_char_id = (
                    perchance_id_to_db_id.get(char_id)
                    if isinstance(char_id, int)
                    else None
                )
                if db_char_id and db_char_id not in seen_char_ids:
                    seen_char_ids.add(db_char_id)
                    conn.execute(
                        "INSERT OR IGNORE INTO adventure_characters (adventure_id, character_id, role) VALUES (?, ?, 'npc')",
                        (adv_id, db_char_id),
                    )

            ts = _ts(m.get("creationTime"))
            conn.execute(
                """
                INSERT INTO adventure_messages
                    (adventure_id, character_id, role, content, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (adv_id, db_char_id, role, msg_text, ts),
            )
            messages_imported += 1

        adventures_imported += 1
        logger.info(
            "  Imported adventure '%s' (id=%d) with %d messages, %d chars",
            title, adv_id, len(real_msgs), len(seen_char_ids),
        )

    conn.commit()

    # ------------------------------------------------------------------
    # Grant access entries (user_character_access) for any new chars
    # seen across adventures
    # ------------------------------------------------------------------
    # Already handled by import_perchance_characters.py; no action needed.

    logger.info(
        "Done. %d adventures, %d messages imported.",
        adventures_imported, messages_imported,
    )
    conn.close()


if __name__ == "__main__":
    main()
