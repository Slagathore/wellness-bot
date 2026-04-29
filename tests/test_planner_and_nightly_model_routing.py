from __future__ import annotations

import json
from types import SimpleNamespace

from app.db import db_rw
from app.domain.turns.llm_analyzer import LLMTurnAnalyzer
from app.utils.time_utils import operator_now
from app.workers.nightly import _analyze_user_psychological_profile, reprocess_sentiments
from app.workers.sentiments import process_batch


def test_llm_turn_analyzer_prefers_planner_model(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.domain.turns.llm_analyzer.settings",
        lambda: SimpleNamespace(
            planner_model="mistral-large-3:675b-cloud",
            turn_planner_model=None,
            worker_model="local-worker",
            turn_planner_timeout_seconds=8.0,
        ),
    )

    def _fake_generate(**kwargs):
        captured["model"] = kwargs.get("model")
        return {"text": json.dumps({"sentiment_priority": "normal"})}

    analyzer = LLMTurnAnalyzer(generate_fn=_fake_generate)
    result = analyzer.analyze(
        user_id=1,
        session_id=None,
        message_text="hello there",
        personality_name="friendly",
        heuristic_plan={},
        profile_context_text=None,
    )

    assert result is not None
    assert captured["model"] == "mistral-large-3:675b-cloud"


def test_sentiment_worker_uses_planner_model(test_user, monkeypatch):
    user_id, _ = test_user
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.workers.sentiments.settings",
        lambda: SimpleNamespace(
            planner_model="mistral-large-3:675b-cloud",
            turn_planner_model=None,
            worker_model="local-worker",
        ),
    )
    monkeypatch.setattr(
        "app.workers.sentiments._pending_messages",
        lambda batch_size: iter(
            [
                {
                    "id": 1001,
                    "user_id": user_id,
                    "session_id": None,
                    "timestamp": operator_now().isoformat(),
                    "content": "I feel awful about tomorrow.",
                }
            ]
        ),
    )
    monkeypatch.setattr("app.workers.sentiments._store_sentiment", lambda *args, **kwargs: None)

    import app.domain.reminders.auto_followups as auto_followups

    monkeypatch.setattr(auto_followups, "maybe_create_followup_for_message", lambda **_: None)

    def _fake_generate(**kwargs):
        captured["model"] = kwargs.get("model")
        return {
            "text": json.dumps(
                {
                    "valence": -0.7,
                    "arousal": 0.8,
                    "dominance": 0.2,
                    "emotion_label": "sadness",
                    "confidence": 0.91,
                    "crisis_risk": False,
                }
            )
        }

    monkeypatch.setattr("app.workers.sentiments.generate", _fake_generate)

    processed = process_batch(batch_size=1)

    assert processed == 1
    assert captured["model"] == "mistral-large-3:675b-cloud"


def test_nightly_reprocess_sentiments_uses_nightly_model(test_user, monkeypatch):
    user_id, _ = test_user
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO sessions(user_id, status, ctx_token_budget) VALUES(?, 'active', 1024)",
            (user_id,),
        )
        session_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO messages(user_id, session_id, role, content, timestamp, scope)
            VALUES (?, ?, 'user', ?, ?, 'standard')
            """,
            (user_id, session_id, "I still feel awful about tomorrow.", operator_now().isoformat()),
        )
        message_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO sentiments(message_id, valence, arousal, dominance, emotion_label, confidence, processed_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (message_id, -0.2, 0.4, 0.5, "sadness", 0.2),
        )

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "app.workers.nightly.settings",
        lambda: SimpleNamespace(
            nightly_model="gemini-3-flash-preview:cloud",
            planner_model="mistral-large-3:675b-cloud",
            turn_planner_model=None,
            worker_model="local-worker",
            psych_model="psych-model",
        ),
    )

    def _fake_generate(**kwargs):
        captured["model"] = kwargs.get("model")
        return {
            "text": json.dumps(
                {
                    "valence": -0.6,
                    "arousal": 0.7,
                    "dominance": 0.3,
                    "emotion_label": "sadness",
                    "confidence": 0.9,
                    "crisis_risk": False,
                }
            )
        }

    monkeypatch.setattr("app.workers.nightly.generate", _fake_generate)

    reprocess_sentiments(limit=1)

    assert captured["model"] == "gemini-3-flash-preview:cloud"


def test_nightly_profile_analysis_uses_nightly_model(test_user, monkeypatch):
    user_id, telegram_user_id = test_user
    with db_rw() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS psychological_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                profile_data TEXT NOT NULL,
                created_at DATETIME DEFAULT (datetime('now')),
                messages_analyzed INTEGER,
                confidence_score REAL,
                big_five TEXT,
                mental_health_indicators TEXT,
                cognitive_metrics TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions(user_id, status, ctx_token_budget) VALUES(?, 'active', 1024)",
            (user_id,),
        )
        session_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        for index in range(25):
            conn.execute(
                """
                INSERT INTO messages(user_id, session_id, role, content, timestamp, scope)
                VALUES (?, ?, 'user', ?, ?, 'standard')
                """,
                (
                    user_id,
                    session_id,
                    f"Message {index} about a difficult situation.",
                    operator_now().isoformat(),
                ),
            )

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "app.workers.nightly.settings",
        lambda: SimpleNamespace(
            nightly_model="gemini-3-flash-preview:cloud",
            psych_model="psych-model",
            worker_model="local-worker",
        ),
    )

    def _fake_generate_profile(conversation_sample, message_count, model):
        captured["model"] = model
        return SimpleNamespace(
            profile={
                "big_five": {},
                "mental_health_indicators": {},
                "cognitive_metrics": {},
            },
            message_count=message_count,
        )

    monkeypatch.setattr(
        "app.workers.nightly.generate_comprehensive_profile",
        _fake_generate_profile,
    )

    assert _analyze_user_psychological_profile(user_id, telegram_user_id, 25) is True
    assert captured["model"] == "gemini-3-flash-preview:cloud"
