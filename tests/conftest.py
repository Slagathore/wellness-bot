from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path
from typing import Iterator, Tuple

import pytest

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from app.config import Settings  # noqa: E402
from app.db import close_pool, db_rw  # noqa: E402


@pytest.fixture(scope="function")
def test_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    data_root = tmp_path / "data"
    db_path = data_root / "test.sqlite"
    data_root.mkdir(parents=True, exist_ok=True)

    cfg = Settings(
        telegram_bot_token="test-token",
        redis_url="redis://localhost:6379/15",
        ollama_host="http://localhost:11434",
        embed_model="nomic-embed-text",
        data_root=str(data_root),
        database_path=str(db_path),
        admin_username="admin",
        vector_backend="sqlite-vec",
        ctx_token_budget=1024,
        top_k_retrieval=3,
        feature_flags={
            "user_feedback": True,
            "token_budget_dynamic": True,
            "nsfw_preferences": True,
            "adaptive_psych_tests": False,
        },
    )

    import app.config
    import app.vector_backends as vb

    monkeypatch.setattr(app.config, "settings", lambda: cfg)
    monkeypatch.setattr(vb, "_BACKEND", None)

    close_pool()
    init_test_database(db_path)
    yield cfg
    close_pool()


def init_test_database(db_path: Path) -> None:
    schema_path = Path("schema") / "init_db.sql"
    if not schema_path.exists():
        raise FileNotFoundError("Schema file missing")

    conn = sqlite3.connect(db_path)
    with open(schema_path, "r", encoding="utf-8") as handle:
        conn.executescript(handle.read())
    conn.close()


@pytest.fixture
def test_user(test_config: Settings) -> Tuple[int, int]:
    # Generate unique telegram_user_id for each test to avoid UNIQUE constraint violations
    telegram_user_id = random.randint(100000000, 999999999)
    username = f"test_user_{telegram_user_id}"

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO users(telegram_user_id, telegram_username, display_name)
            VALUES(?, ?, ?)
            """,
            (telegram_user_id, username, "Test User"),
        )
        user_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    from app.utils.fs import ensure_user_dirs

    ensure_user_dirs(user_id, username)
    return user_id, telegram_user_id


@pytest.fixture
def test_session(test_user: Tuple[int, int], test_config: Settings) -> Tuple[int, int]:
    user_id, _ = test_user
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO sessions(user_id, status, ctx_token_budget) VALUES(?, 'active', ?)",
            (user_id, test_config.ctx_token_budget),  # Use test config's budget (1024)
        )
        session_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return user_id, session_id


@pytest.fixture
def mock_ollama(monkeypatch: pytest.MonkeyPatch):
    import app.utils.ollama
    import app.utils.text

    def fake_chat(messages, options=None):
        # Check all messages for reminder/context keywords, not just the last one
        all_content = " ".join(msg.get("content", "") for msg in messages).lower()

        if "hydration" in all_content or "water" in all_content:
            response = "Have you had some water today?"
        elif "medication" in all_content:
            response = "Did you remember to take your medication?"
        else:
            last_msg = messages[-1]["content"]
            response = f"Echo: {last_msg[:80]}"
        return {"text": response, "raw": {}}

    def fake_generate(prompt, format=None, options=None):
        if "emotion" in prompt.lower() or "sentiment" in prompt.lower():
            return {
                "text": json.dumps(
                    {
                        "valence": 0.5,
                        "arousal": 0.3,
                        "dominance": 0.6,
                        "emotion_label": "neutral",
                        "confidence": 0.8,
                        "crisis_risk": False,
                    }
                ),
                "raw": {},
            }
        return {"text": "{}", "raw": {}}

    def fake_embed(text):
        return [0.1] * 384

    monkeypatch.setattr(app.utils.ollama, "chat", fake_chat)
    monkeypatch.setattr(app.utils.ollama, "generate", fake_generate)
    monkeypatch.setattr(app.utils.text, "embed_text", fake_embed)
    return fake_chat
