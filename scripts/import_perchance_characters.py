"""
Import characters from a Perchance AI character export (Dexie IndexedDB format).

Usage:
    python -m scripts.import_perchance_characters

Reads: perchance-characters-export-2026-02-06.json
Writes: custom_characters and user_character_access rows in the SQLite database.
Assigns all imported characters to users "C" and "Cole".
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EXPORT_FILE = PROJECT_ROOT / "perchance-characters-export-2026-02-06.json"

# Users to grant access to (by telegram_username or display_name)
TARGET_USERS = ["C", "Cole"]


def _load_export(path: Path) -> dict:
    logger.info("Loading export from %s ...", path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_tables(raw: dict) -> dict[str, list[dict]]:
    """Pull the row-lists out of the Dexie export format."""
    tables: dict[str, list[dict]] = {}
    for entry in raw.get("data", {}).get("data", []):
        tables[entry["tableName"]] = entry.get("rows", [])
    return tables


def _clean_role_instruction(text: str, char_name: str) -> str:
    """Replace Perchance placeholders and clean up the system prompt."""
    text = text.replace("{{char}}", char_name)
    text = text.replace("{{user}}", "the user")
    # Strip consecutive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_initial_message(char: dict) -> str:
    """Pull the first AI message from initialMessages, if any."""
    msgs = char.get("initialMessages")
    if isinstance(msgs, list) and msgs:
        first = msgs[0]
        if isinstance(first, dict):
            content = first.get("content", "")
            if isinstance(content, str):
                return content.strip()
    return ""


def _resolve_user_ids(conn: sqlite3.Connection, names: list[str]) -> list[int]:
    """Find database user IDs by telegram_username or display_name."""
    user_ids: list[int] = []
    for name in names:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_username = ? OR display_name = ? LIMIT 1",
            (name, name),
        ).fetchone()
        if row:
            user_ids.append(row[0])
            logger.info("  Resolved user '%s' -> id=%d", name, row[0])
        else:
            logger.warning("  User '%s' not found in database — skipping", name)
    return user_ids


def main() -> None:
    if not EXPORT_FILE.exists():
        logger.error("Export file not found: %s", EXPORT_FILE)
        sys.exit(1)

    raw = _load_export(EXPORT_FILE)
    tables = _extract_tables(raw)

    characters = tables.get("characters", [])
    lore_entries = tables.get("lore", [])
    threads = tables.get("threads", [])

    logger.info("Found %d characters, %d lore entries, %d threads", len(characters), len(lore_entries), len(threads))

    # Build a map: character perchance ID -> set of lore bookIds used in threads
    # The thread table has loreBookId, and lore entries have bookId
    # But the lore bookId values in the actual lore table are only 5, 6, and None
    # We'll just store ALL lore as a shared pool and let individual characters
    # get the lore entries that are relevant to them via thread linkage.
    # For simplicity: bookId=5 lore goes to "The Menagerie" and related chars,
    # bookId=6 goes to Headmistress Liora Voss group, bookId=None is standalone.

    # Group lore by bookId
    lore_by_book: dict[int | None, list[dict]] = {}
    for entry in lore_entries:
        bid = entry.get("bookId")
        lore_by_book.setdefault(bid, []).append({"text": entry.get("text", "")})

    # Map character perchance_id -> which lore bookIds their threads reference
    # But since lore bookIds (5,6,None) don't map 1:1 to thread loreBookIds (0-28),
    # we can't use threads for this. Instead, assign lore by character grouping:
    # - bookId=5: "The Menagerie" (id=25) — mansion scenario lore
    # - bookId=6: similar adventure lore
    # - bookId=None: standalone character descriptions (Alpine, monster girls, etc.)

    # Characters that are part of "The Menagerie" scenario
    menagerie_char_names = {
        "The Menagerie", "Starfire", "Raven", "Blackfire", "Terra",
        "Fluttershy", "Darkness", "Megumin",
    }

    # Characters related to the academy/bookId=6 scenario
    academy_char_names = {"Headmistress Liora Voss"}

    cfg = settings()
    db_path = cfg.database_path
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # Ensure tables exist
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS custom_characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            emoji TEXT DEFAULT '🎭',
            system_prompt TEXT NOT NULL,
            temperature REAL DEFAULT 0.85,
            top_p REAL DEFAULT 0.9,
            repeat_penalty REAL DEFAULT 1.1,
            initial_message TEXT,
            avatar_url TEXT,
            lore TEXT,
            creator_user_id INTEGER,
            is_global INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (creator_user_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS user_character_access (
            user_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            granted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, character_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (character_id) REFERENCES custom_characters(id) ON DELETE CASCADE
        );
    """)

    # Check for already-imported characters to avoid duplicates
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM custom_characters"
        ).fetchall()
    }

    imported_ids: list[int] = []
    skipped = 0

    for char in characters:
        name = char.get("name", "").strip()
        if not name:
            continue

        if name in existing:
            logger.info("  Skipping already-imported character: %s", name)
            skipped += 1
            continue

        role_instruction = char.get("roleInstruction", "")
        if not role_instruction.strip():
            logger.warning("  Skipping character with no roleInstruction: %s", name)
            continue

        system_prompt = _clean_role_instruction(role_instruction, name)
        temperature = char.get("temperature", 0.85)
        avatar_url = ""
        avatar = char.get("avatar")
        if isinstance(avatar, dict):
            avatar_url = avatar.get("url", "")
        initial_message = _extract_initial_message(char)

        # Assign lore
        char_lore: list[dict] = []
        if name in menagerie_char_names:
            char_lore = lore_by_book.get(5, [])
        elif name in academy_char_names:
            char_lore = lore_by_book.get(6, [])

        # Also check for standalone lore referencing this character name
        if not char_lore:
            standalone = lore_by_book.get(None, [])
            matching = [
                e for e in standalone
                if name.lower() in e.get("text", "").lower()
                or name.split()[0].lower() in e.get("text", "").lower()
            ]
            if matching:
                char_lore = matching

        lore_json = json.dumps(char_lore) if char_lore else None

        cur = conn.execute(
            """
            INSERT INTO custom_characters
                (name, display_name, emoji, system_prompt, temperature, top_p,
                 repeat_penalty, initial_message, avatar_url, lore, is_global)
            VALUES (?, ?, '🎭', ?, ?, 0.9, 1.1, ?, ?, ?, 0)
            """,
            (
                name,
                name,
                system_prompt,
                temperature,
                initial_message,
                avatar_url,
                lore_json,
            ),
        )
        char_id = cur.lastrowid
        assert char_id is not None
        imported_ids.append(char_id)
        logger.info("  Imported: %s (id=%d, lore=%d entries)", name, char_id, len(char_lore))

    conn.commit()
    logger.info("Imported %d characters (%d skipped as duplicates)", len(imported_ids), skipped)

    # Grant access to target users
    if imported_ids:
        logger.info("Resolving target users: %s", TARGET_USERS)
        user_ids = _resolve_user_ids(conn, TARGET_USERS)

        grants = 0
        for uid in user_ids:
            for cid in imported_ids:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO user_character_access (user_id, character_id) VALUES (?, ?)",
                        (uid, cid),
                    )
                    grants += 1
                except Exception:
                    pass
        conn.commit()
        logger.info("Granted %d access entries", grants)

    conn.close()
    logger.info("Done.")


if __name__ == "__main__":
    main()
