"""Worker that generates embeddings for messages lacking vectors."""

from __future__ import annotations

import time
from typing import Iterable

from app.db import db_ro, db_rw
from app.utils.text import embed_text
from app.vector_backends import get_backend


def _pending_messages(batch_size: int) -> Iterable[dict]:
    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, content
            FROM messages
            WHERE processed = 0
              AND role IN ('user', 'assistant')
              AND content <> ''
            ORDER BY id ASC
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
    for row in rows:
        yield dict(row)


def _mark_processed(message_id: int) -> None:
    with db_rw() as conn:
        conn.execute(
            "UPDATE messages SET processed = 1 WHERE id = ?",
            (message_id,),
        )


def process_batch(batch_size: int = 20) -> int:
    """Generate embeddings for up to atch_size pending messages."""

    backend = get_backend()
    processed = 0
    for msg in _pending_messages(batch_size):
        try:
            vector = embed_text(msg["content"])
            backend.upsert(msg["id"], vector, {"user_id": msg["user_id"]})
            _mark_processed(msg["id"])
            processed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[embeddings] Failed to embed message {msg['id']}: {exc}")
    return processed


def embedding_loop(batch_size: int = 20, sleep_seconds: int = 5) -> None:
    """Continuously process messages needing embeddings."""

    print("[embeddings] Worker started")
    while True:
        try:
            count = process_batch(batch_size=batch_size)
            if count == 0:
                time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            print("[embeddings] Shutting down...")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"[embeddings] Error: {exc}")
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    embedding_loop()
