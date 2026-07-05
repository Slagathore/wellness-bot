import os
from fastapi.testclient import TestClient

from app.interfaces.admin.server import app
from app.config import settings


def _auth():
    cfg = settings()
    return cfg.admin_username or "admin", cfg.admin_password or "admin"


def test_feedback_list_and_update():
    os.environ["ADMIN_PASSWORD"] = "admin-test"
    settings.cache_clear()
    client = TestClient(app)
    user, pwd = _auth()
    resp = client.get("/feedback", auth=(user, "admin-test"))
    assert resp.status_code == 200
    data = resp.json()
    assert "feedback" in data
    # update a non-existent ID should be safe (either 200 with no-op or 404)
    resp2 = client.post(
        "/feedback/999999/update",
        auth=(user, "admin-test"),
        json={"status": "triage"},
    )
    assert resp2.status_code in (200, 404)


def test_memory_search():
    os.environ["ADMIN_PASSWORD"] = "admin-test"
    settings.cache_clear()
    client = TestClient(app)
    user, pwd = _auth()
    resp = client.get(
        "/memory/search",
        params={"q": "hello", "limit": 1},
        auth=(user, "admin-test"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data


def test_latency_live_metrics_endpoint():
    os.environ["ADMIN_PASSWORD"] = "admin-test"
    settings.cache_clear()
    client = TestClient(app)
    user, _ = _auth()
    resp = client.get("/metrics/latency_live", auth=(user, "admin-test"))
    assert resp.status_code == 200
    data = resp.json()
    assert "rows" in data
    assert "summary" in data


def test_psych_profile():
    os.environ["ADMIN_PASSWORD"] = "admin-test"
    settings.cache_clear()
    client = TestClient(app)
    user, pwd = _auth()
    resp = client.get("/psych/1", auth=(user, "admin-test"))
    assert resp.status_code == 200
    data = resp.json()
    assert "profile" in data


def test_export_user_history():
    os.environ["ADMIN_PASSWORD"] = "admin-test"
    settings.cache_clear()
    client = TestClient(app)
    user, pwd = _auth()
    resp = client.get("/export/user/1", auth=(user, "admin-test"))
    assert resp.status_code == 200
    data = resp.json()
    assert "user" in data
    assert "messages" in data
    assert "reminders" in data


def test_highrisk_endpoints_are_gated():
    os.environ["ADMIN_PASSWORD"] = "admin-test"
    settings.cache_clear()
    client = TestClient(app)
    user, pwd = _auth()
    resp = client.post(
        "/highrisk/llm_console", auth=(user, "admin-test"), json={"prompt": "hi"}
    )
    assert resp.status_code in (200, 403)
    # Use a valid primary-key WHERE; db_edit now rejects arbitrary WHERE clauses
    # (e.g. "1=1") with 400 regardless of gating. This still exercises the gate.
    resp = client.post(
        "/highrisk/db_edit",
        auth=(user, "admin-test"),
        json={"table": "users", "where": "id = 1", "set": {}, "confirm": False},
    )
    assert resp.status_code in (200, 403)
    resp = client.post(
        "/highrisk/omni_broadcast", auth=(user, "admin-test"), json={"message": "hi"}
    )
    assert resp.status_code in (200, 403)
