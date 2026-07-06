"""End-to-end tests for the Mini App API (initData auth -> service -> DB)."""

from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app.db import db_rw

TOKEN = "test-token"  # matches conftest test_config.telegram_bot_token


def _auth_header(telegram_user_id: int) -> dict:
    fields = {
        "auth_date": "1000000",
        "user": json.dumps({"id": telegram_user_id, "username": "alex", "first_name": "Alex"}),
    }
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": "tma " + urlencode(fields)}


@pytest.fixture
def client(test_config, monkeypatch):
    from app.interfaces.webapp import server

    # server.py binds `from app.config import settings` at import, so patch the
    # server module's reference too (or auth verifies against the real bot
    # token, not the test token, when run after other tests).
    monkeypatch.setattr(server, "settings", lambda: test_config)
    # initData freshness would reject auth_date=1e6; disable age check for tests.
    test_config.webapp_initdata_max_age_seconds = 0
    return TestClient(server.app)


def _make_adventure(user_id: int, title="Test Quest", lore="A dark wood.") -> int:
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO adventures(user_id, title, lore, status) VALUES (?, ?, ?, 'active')",
            (user_id, title, lore),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def test_requires_valid_initdata(client):
    assert client.get("/api/adventures").status_code == 401
    assert client.get(
        "/api/adventures", headers={"Authorization": "tma bogus=1&hash=deadbeef"}
    ).status_code == 401


def test_list_and_open_adventure(client, test_user):
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)
    headers = _auth_header(telegram_id)

    listing = client.get("/api/adventures", headers=headers).json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == adv_id

    detail = client.get(f"/api/adventures/{adv_id}", headers=headers).json()
    assert detail["title"] == "Test Quest"

    msgs = client.get(f"/api/adventures/{adv_id}/messages", headers=headers).json()
    assert msgs["messages"] == []


def test_turn_generates_and_persists(client, test_user, monkeypatch):
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)

    import app.interfaces.webapp.service as svc

    async def fake_chat_async(messages, model=None, options=None):
        return {"text": "You step into the clearing. **END_END_END**", "raw": {}}

    monkeypatch.setattr(svc, "chat_async", fake_chat_async)

    resp = client.post(
        f"/api/adventures/{adv_id}/turn",
        headers=_auth_header(telegram_id),
        json={"text": "I look around"},
    )
    assert resp.status_code == 200
    reply = resp.json()["reply"]
    assert "clearing" in reply
    assert "END_END_END" not in reply  # sentinel stripped

    msgs = client.get(
        f"/api/adventures/{adv_id}/messages", headers=_auth_header(telegram_id)
    ).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "narrator"]
    assert msgs[0]["content"] == "I look around"


def test_cannot_access_another_users_adventure(client, test_user):
    _db_user_id, telegram_id = test_user
    # Adventure owned by a different user id.
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO users(telegram_user_id, telegram_username, display_name) "
            "VALUES (999999, 'other', 'Other')"
        )
        other_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    other_adv = _make_adventure(other_id)

    resp = client.get(f"/api/adventures/{other_adv}", headers=_auth_header(telegram_id))
    assert resp.status_code == 404
