"""Worker that performs sentiment analysis on user messages."""

from __future__ import annotations

import json
import time
from typing import Iterable

from app.config import settings
from app.db import db_ro, db_rw
from app.history_scope import (HISTORY_SCOPE_STANDARD,
                               automated_moderation_allowed_for_scope,
                               inferred_history_scope_for_message,
                               table_has_column)
from app.utils.ollama import generate

SENTIMENT_PROMPT_TEMPLATE = """Analyze the emotional content of this message and respond with valid JSON only.\n\nMessage: \"{message}\"\n\nOutput format:\n{{\n    \"valence\": <float between -1.0 and 1.0>,\n    \"arousal\": <float between 0.0 and 1.0>,\n    \"dominance\": <float between 0.0 and 1.0>,\n    \"emotion_label\": \"<joy|sadness|anger|fear|disgust|surprise|neutral>\",\n    \"confidence\": <float between 0.0 and 1.0>,\n    \"crisis_risk\": <boolean>\n}}"""


def _pending_messages(batch_size: int) -> Iterable[dict]:
    joins = ""
    scope_predicate = "1 = 1"
    if table_has_column("messages", "scope"):
        scope_predicate = "COALESCE(m.scope, 'standard') = 'standard'"
    elif table_has_column("sessions", "scope"):
        joins = "LEFT JOIN sessions AS sess ON sess.id = m.session_id"
        scope_predicate = "COALESCE(sess.scope, 'standard') = 'standard'"
    elif table_has_column("users", "personality"):
        joins = "LEFT JOIN users AS u ON u.id = m.user_id"
        scope_predicate = (
            "LOWER(COALESCE(u.personality, 'standard')) NOT IN ('downbad', 'roleplay')"
        )

    with db_ro() as conn:
        rows = conn.execute(
            f"""
            SELECT m.id, m.user_id, m.session_id, m.timestamp, m.content
            FROM messages AS m
            LEFT JOIN sentiments AS s ON s.message_id = m.id
            {joins}
            WHERE s.id IS NULL
              AND m.role = 'user'
              AND {scope_predicate}
              AND m.content <> ''
            ORDER BY m.id ASC
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
    for row in rows:
        yield dict(row)


def _store_sentiment(message_id: int, sentiment: dict) -> None:
    scope = inferred_history_scope_for_message(message_id=message_id) or HISTORY_SCOPE_STANDARD

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO sentiments(
                message_id, valence, arousal, dominance, emotion_label, confidence
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                sentiment.get("valence"),
                sentiment.get("arousal"),
                sentiment.get("dominance"),
                sentiment.get("emotion_label"),
                sentiment.get("confidence"),
            ),
        )
        if sentiment.get("crisis_risk") and automated_moderation_allowed_for_scope(scope):
            conn.execute(
                """
                INSERT INTO moderation_events(user_id, event_type, severity, details)
                SELECT user_id, 'crisis_detected', 5, ?
                FROM messages
                WHERE id = ?
                """,
                (
                    json.dumps(
                        {
                            "message_id": message_id,
                            "source": "sentiment_worker",
                            "scope": scope,
                        }
                    ),
                    message_id,
                ),
            )


def process_batch(batch_size: int = 10) -> int:
    """Analyze up to atch_size pending messages."""

    processed = 0
    for msg in _pending_messages(batch_size):
        prompt = SENTIMENT_PROMPT_TEMPLATE.format(message=msg["content"])
        try:
            _worker_model = settings().worker_model
            response = generate(
                prompt=prompt, model=_worker_model, format="json", options={"temperature": 0.3}
            )
            sentiment = json.loads(response["text"])
            _store_sentiment(msg["id"], sentiment)
            try:
                from app.domain.reminders.auto_followups import (
                    maybe_create_followup_for_message,
                )

                maybe_create_followup_for_message(
                    user_id=int(msg["user_id"]),
                    session_id=int(msg["session_id"]) if msg.get("session_id") is not None else None,
                    message_id=int(msg["id"]),
                    text=str(msg["content"] or ""),
                    message_timestamp=msg.get("timestamp"),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[sentiments] Follow-up reevaluation skipped for message {msg['id']}: {exc}")
            processed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[sentiments] Failed to analyze message {msg['id']}: {exc}")
    return processed


def sentiment_loop(batch_size: int = 10, sleep_seconds: int = 5) -> None:
    """Continuously process pending sentiment analyses."""

    print("[sentiments] Worker started")
    while True:
        try:
            count = process_batch(batch_size=batch_size)
            if count == 0:
                time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            print("[sentiments] Shutting down...")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"[sentiments] Error: {exc}")
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    sentiment_loop()
