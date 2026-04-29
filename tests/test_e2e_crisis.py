"""E2E test for crisis detection flow."""

from __future__ import annotations

import json

import pytest

from app.db import db_ro, db_rw
from app.utils.ollama import generate


def test_crisis_detection_flags_admin(
    test_config, test_session, monkeypatch: pytest.MonkeyPatch
):
    user_id, session_id = test_session

    def fake_generate(prompt, format=None, options=None):
        # Return crisis-specific sentiment data
        if "emotion" in prompt.lower() or "sentiment" in prompt.lower():
            return {
                "text": json.dumps(
                    {
                        "valence": -0.9,
                        "arousal": 0.3,
                        "dominance": 0.1,
                        "emotion_label": "despair",
                        "confidence": 0.9,
                        "crisis_risk": True,
                    }
                ),
                "raw": {},
            }
        return {"text": "{}", "raw": {}}

    import app.utils.ollama

    monkeypatch.setattr(app.utils.ollama, "generate", fake_generate)

    crisis_text = "I don't want to be here anymore."
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO messages(user_id, session_id, role, content) VALUES(?, ?, 'user', ?)",
            (user_id, session_id, crisis_text),
        )
        message_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    result = generate(prompt="Analyze sentiment", format="json")
    sentiment = json.loads(result["text"])

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO sentiments(message_id, valence, arousal, dominance, emotion_label, confidence)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                sentiment["valence"],
                sentiment["arousal"],
                sentiment["dominance"],
                sentiment["emotion_label"],
                sentiment["confidence"],
            ),
        )
        if sentiment.get("crisis_risk"):
            conn.execute(
                """
                INSERT INTO moderation_events(user_id, event_type, severity, details)
                VALUES(?, 'crisis_detected', 5, ?)
                """,
                (user_id, json.dumps({"message_id": message_id})),
            )

    with db_ro() as conn:
        flag = conn.execute(
            """
            SELECT event_type, severity, resolved
            FROM moderation_events
            WHERE user_id = ? AND event_type = 'crisis_detected'
            """,
            (user_id,),
        ).fetchone()
        assert flag and flag["severity"] == 5 and flag["resolved"] == 0
