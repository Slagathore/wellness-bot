"""Worker that reliably delivers messages to Telegram."""

from __future__ import annotations

import time

import requests

from app.config import settings
from app.db import db_rw


def enqueue_outbox(conn, user_id: int, chat_id: int, text: str) -> None:
    """Insert an outgoing message into the outbox table."""

    conn.execute(
        """
        INSERT INTO telegram_outbox(user_id, chat_id, message_text)
        VALUES(?, ?, ?)
        """,
        (user_id, chat_id, text),
    )


def send_loop() -> None:
    """Continuously send unsent messages from the outbox."""

    cfg = settings()
    bot_token = cfg.telegram_bot_token
    endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    print("[outbox_sender] Starting send loop...")
    while True:
        try:
            with db_rw() as conn:
                row = conn.execute(
                    """
                    SELECT id, user_id, chat_id, message_text
                    FROM telegram_outbox
                    WHERE sent = 0
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (),
                ).fetchone()

                if not row:
                    time.sleep(1)
                    continue

                response = requests.post(
                    endpoint,
                    json={"chat_id": row["chat_id"], "text": row["message_text"]},
                    timeout=30,
                )

                if response.ok:
                    message_id = response.json().get("result", {}).get("message_id")
                    conn.execute(
                        """
                        UPDATE telegram_outbox
                        SET sent = 1,
                            sent_at = datetime('now'),
                            telegram_message_id = ?
                        WHERE id = ?
                        """,
                        (message_id, row["id"]),
                    )
                else:
                    print(
                        f"[outbox_sender] Failed to send message {row['id']}: {response.status_code}"
                    )
                    time.sleep(5)
        except KeyboardInterrupt:
            print("[outbox_sender] Shutting down...")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"[outbox_sender] Error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    send_loop()
