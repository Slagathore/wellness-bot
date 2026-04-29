"""Filesystem utilities: user directories, transcript shards, and media storage."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Tuple

from app.config import settings
from app.db import db_ro, db_rw
from app.utils.time_utils import operator_now


def ensure_directory(path: str | Path) -> None:
    """Ensure a directory exists, creating it if necessary."""
    Path(path).mkdir(parents=True, exist_ok=True)


def user_dir(user_id: int) -> str:
    """Return the absolute path to the user's root directory."""

    root = settings().data_root
    return os.path.join(root, "users", str(user_id))


def ensure_user_dirs(user_id: int, username: str | None = None) -> None:
    """Create the standard directory structure for a user if missing."""

    base = Path(user_dir(user_id))
    subdirectories = [
        "media/images",
        "media/audio",
        "media/video",
        "media/documents",
        "transcripts",
        "derived/analytics",
        "derived/summaries",
        "derived/cleaned",
        "uploads/sleep",
        "uploads/activity",
        "exports",
    ]
    for sub in subdirectories:
        (base / sub).mkdir(parents=True, exist_ok=True)

    if username:
        alias_root = Path(settings().data_root) / "users" / "_by_username"
        alias_root.mkdir(parents=True, exist_ok=True)
        alias_path = alias_root / username
        try:
            if alias_path.exists() or alias_path.is_symlink():
                alias_path.unlink()
        except OSError:
            pass
        try:
            alias_path.symlink_to(Path("..") / str(user_id))
        except OSError:
            # Symlinks may not be supported on all filesystems; ignore if creation fails
            pass


def get_active_shard_path(user_id: int, session_id: int) -> Tuple[str, int | None]:
    """Return (absolute_path, shard_id) for the currently active shard or next shard."""

    base = user_dir(user_id)
    with db_ro() as conn:
        row = conn.execute(
            """
            SELECT id, path
            FROM transcript_shards
            WHERE user_id = ? AND session_id = ? AND closed_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, session_id),
        ).fetchone()
        if row:
            return os.path.join(base, row["path"]), row["id"]

        count_row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM transcript_shards
            WHERE user_id = ? AND session_id = ?
            """,
            (user_id, session_id),
        ).fetchone()

    next_index = (count_row["c"] if count_row else 0) + 1
    relative = f"transcripts/session_{session_id}_shard_{next_index:03d}.jsonl"
    return os.path.join(base, relative), None


def should_rotate_shard(shard_path: str, shard_id: int | None) -> bool:
    """Return True if the shard should be rotated based on size or message count."""

    if shard_id is None:
        return False

    cfg = settings()
    if (
        os.path.exists(shard_path)
        and os.path.getsize(shard_path) >= cfg.max_shard_bytes
    ):
        return True

    with db_ro() as conn:
        row = conn.execute(
            "SELECT message_count FROM transcript_shards WHERE id = ?",
            (shard_id,),
        ).fetchone()
    if row and row["message_count"] >= cfg.max_shard_messages:
        return True

    return False


def close_shard(shard_id: int, last_msg_id: int) -> None:
    """Mark the shard as closed and update metadata counts."""

    with db_rw() as conn:
        shard_row = conn.execute(
            "SELECT user_id, path, start_msg_id FROM transcript_shards WHERE id = ?",
            (shard_id,),
        ).fetchone()
        if not shard_row:
            return

        message_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM messages
            WHERE id >= ? AND id <= ?
            """,
            (shard_row["start_msg_id"], last_msg_id),
        ).fetchone()["c"]

        abs_path = os.path.join(user_dir(shard_row["user_id"]), shard_row["path"])
        file_size = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0

        conn.execute(
            """
            UPDATE transcript_shards
            SET closed_at = datetime('now'),
                end_msg_id = ?,
                message_count = ?,
                bytes = ?
            WHERE id = ?
            """,
            (last_msg_id, message_count, file_size, shard_id),
        )


def create_new_shard(user_id: int, session_id: int, first_msg_id: int) -> int:
    """Create a new shard record and return its identifier."""

    with db_rw() as conn:
        count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM transcript_shards
            WHERE user_id = ? AND session_id = ?
            """,
            (user_id, session_id),
        ).fetchone()["c"]

        relative = f"transcripts/session_{session_id}_shard_{count + 1:03d}.jsonl"
        conn.execute(
            """
            INSERT INTO transcript_shards(user_id, session_id, path, start_msg_id)
            VALUES(?, ?, ?, ?)
            """,
            (user_id, session_id, relative, first_msg_id),
        )
        shard_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    return shard_id


def append_to_shard(
    user_id: int, session_id: int, msg_id: int, role: str, content: str
) -> None:
    """Append a message to the active shard, rotating if thresholds are exceeded."""

    shard_path, shard_id = get_active_shard_path(user_id, session_id)

    if shard_id is not None and should_rotate_shard(shard_path, shard_id):
        close_shard(shard_id, msg_id - 1)
        shard_id = create_new_shard(user_id, session_id, msg_id)
        shard_path, _ = get_active_shard_path(user_id, session_id)

    if shard_id is None:
        shard_id = create_new_shard(user_id, session_id, msg_id)

    record = {
        "id": msg_id,
        "ts": operator_now().isoformat(),
        "role": role,
        "content": content,
    }

    os.makedirs(os.path.dirname(shard_path), exist_ok=True)
    with open(shard_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    with db_rw() as conn:
        conn.execute(
            """
            UPDATE transcript_shards
            SET message_count = message_count + 1
            WHERE id = ?
            """,
            (shard_id,),
        )
        conn.execute(
            """
            UPDATE messages
            SET shard_path = (SELECT path FROM transcript_shards WHERE id = ?)
            WHERE id = ?
            """,
            (shard_id, msg_id),
        )


def pending_server_events_for_session(user_id: int, session_id: int) -> list[str]:
    """Return server_event content that has not yet received an assistant response."""

    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT content
            FROM messages
            WHERE user_id = ?
              AND session_id = ?
              AND role = 'server_event'
              AND id > COALESCE(
                    (SELECT MAX(id)
                     FROM messages
                     WHERE user_id = ?
                       AND session_id = ?
                       AND role = 'assistant'),
                    0)
            ORDER BY id ASC
            """,
            (user_id, session_id, user_id, session_id),
        ).fetchall()
    return [row["content"] for row in rows]


def save_media(
    user_id: int, media_type: str, telegram_file_id: str, file_data: bytes
) -> str:
    """Persist media for a user and return the relative path under their directory."""

    mapping = {
        "image": "media/images",
        "audio": "media/audio",
        "video": "media/video",
        "document": "media/documents",
    }
    subdir = mapping.get(media_type, "media/documents")
    filename = telegram_file_id

    base = Path(user_dir(user_id))
    target = base / subdir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(file_data)

    return os.path.join(subdir, filename)
