"""Bootstrap utilities for initializing the wellness bot environment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import settings
from app.db import db_ro, db_rw
from app.utils.fs import ensure_user_dirs
from app.vector_backends import get_backend

SCHEMA_PATH = Path("schema/init_db.sql")


def init_database() -> None:
    cfg = settings()
    db_path = Path(cfg.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file missing: {SCHEMA_PATH}")

    with db_rw() as conn:
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(schema_sql)
    print(f"[bootstrap] Database initialized at {db_path}")


def init_vector_backend() -> None:
    backend = get_backend()
    backend.ensure_ready(settings().embed_dimensions)
    print("[bootstrap] Vector backend ready")


def create_admin_user(telegram_user_id: int, username: str | None = None, display_name: str | None = None) -> None:
    ensure_user_dirs(telegram_user_id, username)
    with db_rw() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET telegram_username = ?, display_name = ?, last_active_at = datetime('now') WHERE id = ?",
                (username, display_name or username or f"Admin {telegram_user_id}", existing["id"]),
            )
            print(f"[bootstrap] Updated admin user {telegram_user_id}")
            return

        conn.execute(
            """
            INSERT INTO users(telegram_user_id, telegram_username, display_name, onboarding_completed, feature_flags)
            VALUES(?, ?, ?, 1, ?)
            """,
            (
                telegram_user_id,
                telegram_user_id,
                username,
                display_name or username or f"Admin {telegram_user_id}",
                json.dumps({
                    "mood_journaling": True,
                    "sleep_tracking": True,
                    "hydration_tracking": True,
                    "wellness_goals": True,
                    "social_reminders": True,
                }),
            ),
        )
    print(f"[bootstrap] Created admin user {telegram_user_id}")


def create_data_dirs() -> None:
    cfg = settings()
    Path(cfg.data_root, "users").mkdir(parents=True, exist_ok=True)
    Path(cfg.data_root, "backups").mkdir(parents=True, exist_ok=True)
    print(f"[bootstrap] Ensured data directories under {cfg.data_root}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap the wellness bot environment")
    parser.add_argument("--init-db", action="store_true", help="Initialize the SQLite database schema")
    parser.add_argument("--init-vector", action="store_true", help="Initialize the configured vector backend")
    parser.add_argument("--create-admin", type=int, metavar="TELEGRAM_USER_ID", help="Create/update an admin user by Telegram ID")
    parser.add_argument("--username", type=str, help="Optional username for the admin user")
    parser.add_argument("--display-name", type=str, help="Optional display name for the admin user")
    parser.add_argument("--ensure-dirs", action="store_true", help="Create data directories under DATA_ROOT")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.ensure_dirs:
        create_data_dirs()
    if args.init_db:
        init_database()
    if args.init_vector:
        init_vector_backend()
    if args.create_admin is not None:
        create_admin_user(args.create_admin, username=args.username, display_name=args.display_name)

    if not any([
        args.ensure_dirs,
        args.init_db,
        args.init_vector,
        args.create_admin is not None,
    ]):
        print("No actions specified. Use --help for options.")


if __name__ == "__main__":
    main()
