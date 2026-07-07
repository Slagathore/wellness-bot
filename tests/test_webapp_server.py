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


@pytest.fixture
def mock_narrator(monkeypatch):
    import app.interfaces.webapp.service as svc

    async def fake_chat_async(messages, model=None, options=None):
        return {"text": "The story advances.", "raw": {}}

    monkeypatch.setattr(svc, "chat_async", fake_chat_async)
    # Avoid scheduling background lore refresh in tests.
    monkeypatch.setattr(svc.WebappService, "_schedule_lore_refresh", lambda self, aid: None)


def _messages(client, adv_id, telegram_id):
    return client.get(
        f"/api/adventures/{adv_id}/messages", headers=_auth_header(telegram_id)
    ).json()["messages"]


def test_say_mode_formats_dialogue(client, test_user, mock_narrator):
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)
    client.post(
        f"/api/adventures/{adv_id}/turn",
        headers=_auth_header(telegram_id),
        json={"text": "hello there", "mode": "say"},
    )
    msgs = _messages(client, adv_id, telegram_id)
    assert msgs[0]["content"] == 'You say, "hello there"'
    assert msgs[0]["role"] == "user"


def test_story_mode_inserts_narration(client, test_user, mock_narrator):
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)
    client.post(
        f"/api/adventures/{adv_id}/turn",
        headers=_auth_header(telegram_id),
        json={"text": "Rain begins to fall.", "mode": "story"},
    )
    msgs = _messages(client, adv_id, telegram_id)
    # Player-authored narration is stored as narrator, then the AI narrates.
    assert msgs[0]["role"] == "narrator" and msgs[0]["content"] == "Rain begins to fall."
    assert msgs[1]["role"] == "narrator"


def test_continue_adds_narration_without_user_message(client, test_user, mock_narrator):
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)
    client.post(f"/api/adventures/{adv_id}/continue", headers=_auth_header(telegram_id))
    msgs = _messages(client, adv_id, telegram_id)
    assert [m["role"] for m in msgs] == ["narrator"]


def test_retry_replaces_last_narrator(client, test_user, mock_narrator):
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)
    client.post(
        f"/api/adventures/{adv_id}/turn",
        headers=_auth_header(telegram_id),
        json={"text": "I look around", "mode": "do"},
    )
    before = _messages(client, adv_id, telegram_id)
    assert len(before) == 2
    client.post(f"/api/adventures/{adv_id}/retry", headers=_auth_header(telegram_id))
    after = _messages(client, adv_id, telegram_id)
    assert len(after) == 2  # still one exchange (narrator regenerated, not appended)
    assert [m["role"] for m in after] == ["user", "narrator"]


def test_erase_removes_last_exchange(client, test_user, mock_narrator):
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)
    client.post(
        f"/api/adventures/{adv_id}/turn",
        headers=_auth_header(telegram_id),
        json={"text": "I look around", "mode": "do"},
    )
    resp = client.post(f"/api/adventures/{adv_id}/erase", headers=_auth_header(telegram_id))
    assert resp.json()["removed"] == 2
    assert _messages(client, adv_id, telegram_id) == []


def test_create_adventure(client, test_user, mock_narrator):
    _db_user_id, telegram_id = test_user
    headers = _auth_header(telegram_id)
    resp = client.post(
        "/api/adventures",
        headers=headers,
        json={"title": "The Sunken City", "premise": "A diver finds a door underwater.", "player_role": "a treasure hunter"},
    )
    assert resp.status_code == 200
    body = resp.json()
    adv_id = body["id"]
    assert body["title"] == "The Sunken City"
    assert body["opening"]

    # It shows in the list and has an opening narrator message.
    listing = client.get("/api/adventures", headers=headers).json()
    assert any(a["id"] == adv_id for a in listing["items"])
    msgs = _messages(client, adv_id, telegram_id)
    assert len(msgs) == 1 and msgs[0]["role"] == "narrator"


def test_create_adventure_requires_title_or_premise(client, test_user, mock_narrator):
    _db_user_id, telegram_id = test_user
    resp = client.post(
        "/api/adventures", headers=_auth_header(telegram_id), json={"title": "", "premise": ""}
    )
    assert resp.status_code == 400


CHAR_BLOCK = (
    "[CHARACTER]\n"
    "Name: Seraphina\n"
    "Emoji: 🗡️\n"
    "Greeting: Well met, traveller.\n"
    "Temperature: 1.1\n"
    "System Prompt: You are Seraphina, a sardonic sellsword with a soft heart.\n"
    "[/CHARACTER]"
)


@pytest.fixture
def mock_char(monkeypatch):
    import app.interfaces.webapp.service as svc

    async def fake_chat_async(messages, model=None, options=None):
        return {"text": CHAR_BLOCK, "raw": {}}

    monkeypatch.setattr(svc, "chat_async", fake_chat_async)
    monkeypatch.setattr(svc.WebappService, "_schedule_lore_refresh", lambda self, aid: None)


def test_create_character_and_list(client, test_user, mock_char):
    _db_user_id, telegram_id = test_user
    headers = _auth_header(telegram_id)
    resp = client.post("/api/characters", headers=headers, json={"name": "Seraphina", "description": "a sellsword"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Seraphina" and body["emoji"] == "🗡️"

    chars = client.get("/api/characters", headers=headers).json()["characters"]
    assert any(c["id"] == body["id"] for c in chars)


def test_attach_detach_character(client, test_user, mock_char):
    db_user_id, telegram_id = test_user
    headers = _auth_header(telegram_id)
    adv_id = _make_adventure(db_user_id)
    cid = client.post("/api/characters", headers=headers, json={"name": "Seraphina", "description": "x"}).json()["id"]

    client.post(f"/api/adventures/{adv_id}/characters", headers=headers, json={"character_id": cid, "role": "companion"})
    attached = client.get(f"/api/adventures/{adv_id}/characters", headers=headers).json()["characters"]
    assert [c["id"] for c in attached] == [cid] and attached[0]["role"] == "companion"

    client.delete(f"/api/adventures/{adv_id}/characters/{cid}", headers=headers)
    assert client.get(f"/api/adventures/{adv_id}/characters", headers=headers).json()["characters"] == []


def test_cannot_attach_other_users_character(client, test_user):
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)
    # A character owned by someone else, not global.
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO users(telegram_user_id, telegram_username, display_name) "
            "VALUES (424242, 'other', 'Other')"
        )
        other_uid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO custom_characters(name, display_name, system_prompt, creator_user_id, is_global) "
            "VALUES ('x','X','p', ?, 0)",
            (other_uid,),
        )
        other_cid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    resp = client.post(
        f"/api/adventures/{adv_id}/characters",
        headers=_auth_header(telegram_id),
        json={"character_id": other_cid},
    )
    assert resp.status_code == 400


def test_create_adventure_with_characters(client, test_user, mock_char):
    db_user_id, telegram_id = test_user
    headers = _auth_header(telegram_id)
    cid = client.post("/api/characters", headers=headers, json={"name": "Seraphina", "description": "x"}).json()["id"]
    adv = client.post("/api/adventures", headers=headers, json={"title": "Quest", "premise": "p", "character_ids": [cid]}).json()
    attached = client.get(f"/api/adventures/{adv['id']}/characters", headers=headers).json()["characters"]
    assert [c["id"] for c in attached] == [cid]


def test_memory_get_and_update(client, test_user, mock_char):
    db_user_id, telegram_id = test_user
    headers = _auth_header(telegram_id)
    adv_id = _make_adventure(db_user_id, lore="Initial canon.")
    mem = client.get(f"/api/adventures/{adv_id}/memory", headers=headers).json()
    assert mem["lore"] == "Initial canon."

    updated = client.put(
        f"/api/adventures/{adv_id}/memory",
        headers=headers,
        json={"lore": "The king is dead.", "player_role": "the heir", "objective": "claim the throne"},
    ).json()
    assert updated["lore"] == "The king is dead."
    assert updated["player_role"] == "the heir"
    assert updated["objective"] == "claim the throne"


@pytest.fixture
def mock_images(monkeypatch):
    import app.interfaces.webapp.server as srv
    import app.interfaces.webapp.service as svc

    async def fake_generate_image(subject, *, style=None, nsfw=False, seed=None, engine=None, loras=None):
        return b"\x89PNG-" + style.encode() if style else b"\x89PNG"

    async def fake_chat_async(messages, model=None, options=None):
        return {"text": "a dark corridor, torchlight, cold stone, ominous mood", "raw": {}}

    async def fake_health():
        return {"ok": True}

    async def fake_list_engines():
        return [{"key": "wai", "label": "Anime", "ecosystem": "illustrious",
                 "present": True, "supports_lora": True, "nsfw_capable": True}]

    async def fake_list_loras(engine):
        return [{"file": f"{engine}/x.safetensors", "label": "x", "ecosystem": "illustrious"}]

    monkeypatch.setattr(svc, "generate_image", fake_generate_image)
    monkeypatch.setattr(svc, "chat_async", fake_chat_async)
    monkeypatch.setattr(svc.WebappService, "_schedule_lore_refresh", lambda self, aid: None)
    monkeypatch.setattr(srv, "is_enabled", lambda: True)
    monkeypatch.setattr(srv, "image_health", fake_health)
    monkeypatch.setattr(srv, "list_engines", fake_list_engines)
    monkeypatch.setattr(srv, "list_loras", fake_list_loras)


def test_image_health(client, test_user, mock_images):
    _db_user_id, telegram_id = test_user
    resp = client.get("/api/image/health", headers=_auth_header(telegram_id))
    assert resp.json()["available"] is True


def test_scene_image_returns_png(client, test_user, mock_images):
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO adventure_messages(adventure_id, role, content) VALUES (?, 'narrator', ?)",
            (adv_id, "You enter a dark corridor lit by guttering torches."),
        )
    resp = client.post(f"/api/adventures/{adv_id}/image", headers=_auth_header(telegram_id), json={})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(b"\x89PNG")


def test_scene_image_nothing_to_illustrate(client, test_user, mock_images):
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)  # no messages yet
    resp = client.post(f"/api/adventures/{adv_id}/image", headers=_auth_header(telegram_id), json={})
    assert resp.status_code == 400


def test_scene_image_unavailable_returns_503(client, test_user, mock_images, monkeypatch):
    import app.interfaces.webapp.service as svc
    from app.services.dm_image import ImageUnavailable

    async def boom(subject, *, style=None, nsfw=False, seed=None, engine=None, loras=None):
        raise ImageUnavailable("server down")

    monkeypatch.setattr(svc, "generate_image", boom)
    db_user_id, telegram_id = test_user
    adv_id = _make_adventure(db_user_id)
    with db_rw() as conn:
        conn.execute(
            "INSERT INTO adventure_messages(adventure_id, role, content) VALUES (?, 'narrator', 'scene')",
            (adv_id,),
        )
    resp = client.post(f"/api/adventures/{adv_id}/image", headers=_auth_header(telegram_id), json={})
    assert resp.status_code == 503


def test_character_portrait_png(client, test_user, mock_images, mock_char):
    db_user_id, telegram_id = test_user
    headers = _auth_header(telegram_id)
    cid = client.post("/api/characters", headers=headers, json={"name": "Seraphina", "description": "x"}).json()["id"]
    resp = client.post(f"/api/characters/{cid}/image", headers=headers, json={"engine": "anime", "shot": "portrait"})
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"


def test_image_health_reports_engines_and_nsfw(client, test_user, mock_images):
    _db_user_id, telegram_id = test_user
    body = client.get("/api/image/health", headers=_auth_header(telegram_id)).json()
    assert body["available"] is True
    assert "nsfw_allowed" in body
    assert body["engines"][0]["key"] == "wai"


def test_list_engines_endpoint(client, test_user, mock_images):
    _db_user_id, telegram_id = test_user
    body = client.get("/api/image/engines", headers=_auth_header(telegram_id)).json()
    assert body["engines"][0]["supports_lora"] is True


def test_list_loras_endpoint(client, test_user, mock_images):
    _db_user_id, telegram_id = test_user
    body = client.get("/api/image/loras?engine=wai", headers=_auth_header(telegram_id)).json()
    assert body["loras"][0]["file"] == "wai/x.safetensors"


def test_image_endpoints_require_auth(client):
    assert client.get("/api/image/engines").status_code == 401
    assert client.get("/api/image/loras?engine=wai").status_code == 401


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
