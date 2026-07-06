"""Tests for Telegram WebApp initData verification."""

from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from app.interfaces.webapp.auth import (InitDataError, parse_webapp_user,
                                        verify_init_data)

BOT_TOKEN = "123456:test-bot-token"


def _sign(fields: dict, token: str = BOT_TOKEN) -> str:
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def _fields(auth_date: int = 10_000, uid: int = 42) -> dict:
    return {
        "auth_date": str(auth_date),
        "query_id": "AAA",
        "user": json.dumps({"id": uid, "username": "alex", "first_name": "Alex"}),
    }


def test_valid_init_data_passes():
    init = _sign(_fields(auth_date=10_000, uid=42))
    parsed = verify_init_data(init, BOT_TOKEN, max_age_seconds=3600, now=10_100)
    user = parse_webapp_user(parsed)
    assert user.telegram_user_id == 42
    assert user.username == "alex"


def test_tampered_user_fails():
    fields = _fields(uid=42)
    init = _sign(fields)
    # Swap the user for a different id without re-signing.
    tampered = init.replace("%22id%22%3A+42", "%22id%22%3A+999").replace(
        "%22id%22:+42", "%22id%22:+999"
    )
    # Ensure we actually changed something; if urlencoding differs, mutate raw.
    tampered = tampered if tampered != init else init.replace("42", "999", 1)
    with pytest.raises(InitDataError):
        verify_init_data(tampered, BOT_TOKEN, now=10_100)


def test_wrong_token_fails():
    init = _sign(_fields(), token="different:token")
    with pytest.raises(InitDataError):
        verify_init_data(init, BOT_TOKEN, now=10_100)


def test_missing_hash_fails():
    fields = _fields()
    with pytest.raises(InitDataError):
        verify_init_data(urlencode(fields), BOT_TOKEN, now=10_100)


def test_stale_init_data_fails():
    init = _sign(_fields(auth_date=10_000))
    with pytest.raises(InitDataError):
        verify_init_data(init, BOT_TOKEN, max_age_seconds=60, now=10_000 + 999)
