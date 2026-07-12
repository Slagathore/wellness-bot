"""Telegram WebApp initData verification.

A Mini App receives an ``initData`` query string signed by Telegram. We verify
the HMAC-SHA256 signature against the bot token (per Telegram's spec) so we can
trust the embedded user id without a separate login. See
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl


class InitDataError(Exception):
    """Raised when initData is missing, malformed, or fails verification."""


@dataclass(frozen=True, slots=True)
class WebAppUser:
    telegram_user_id: int
    username: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]


def _secret_key(bot_token: str) -> bytes:
    # secret_key = HMAC_SHA256(key="WebAppData", message=bot_token)
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def verify_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86400,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Verify a Telegram WebApp initData string and return its parsed fields.

    Raises InitDataError on any failure. ``now`` is injectable for testing.
    """
    if not init_data or not bot_token:
        raise InitDataError("missing initData or bot token")

    # Preserve empty values and order-independence: parse into a dict.
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("initData missing hash")

    data_check_string = "\n".join(
        f"{key}={pairs[key]}" for key in sorted(pairs.keys())
    )
    computed = hmac.new(
        _secret_key(bot_token), data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        raise InitDataError("initData signature mismatch")

    # Freshness: reject stale initData to limit replay.
    auth_date_raw = pairs.get("auth_date")
    if auth_date_raw:
        try:
            auth_date = int(auth_date_raw)
        except ValueError as exc:
            raise InitDataError("initData auth_date not an integer") from exc
        current = now if now is not None else time.time()
        if max_age_seconds > 0 and current - auth_date > max_age_seconds:
            raise InitDataError("initData is too old")

    return pairs


# ---------------------------------------------------------------------------
# Signed session tokens — for browser access outside Telegram. The same HMAC
# scheme backs both the login cookie and the per-user "magic link" the bot hands
# out, so a token minted by the Telegram side verifies on the webapp side.
# ---------------------------------------------------------------------------

def session_secret(bot_token: str) -> bytes:
    """Derive a stable, secret signing key from the bot token."""
    return hashlib.sha256(("mira-webapp-session|" + (bot_token or "")).encode("utf-8")).digest()


def sign_session(uid: int, secret: bytes, *, ttl_seconds: int, now: Optional[float] = None) -> str:
    """Return a signed ``uid.exp.sig`` token that expires after ttl_seconds."""
    exp = int((now if now is not None else time.time())) + int(ttl_seconds)
    payload = f"{int(uid)}.{exp}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session(token: str, secret: bytes, *, now: Optional[float] = None) -> Optional[int]:
    """Return the uid from a valid, unexpired session token, else None."""
    if not token:
        return None
    try:
        uid_str, exp_str, sig = token.rsplit(".", 2)
        expected = hmac.new(secret, f"{uid_str}.{exp_str}".encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        current = now if now is not None else time.time()
        if int(exp_str) < current:
            return None
        return int(uid_str)
    except (ValueError, AttributeError):
        return None


def parse_webapp_user(verified_fields: Dict[str, Any]) -> WebAppUser:
    """Extract the Telegram user from verified initData fields."""
    user_raw = verified_fields.get("user")
    if not user_raw:
        raise InitDataError("initData has no user")
    try:
        user = json.loads(user_raw)
    except (TypeError, ValueError) as exc:
        raise InitDataError("initData user is not valid JSON") from exc
    uid = user.get("id")
    if not isinstance(uid, int):
        raise InitDataError("initData user id missing")
    return WebAppUser(
        telegram_user_id=uid,
        username=user.get("username"),
        first_name=user.get("first_name"),
        last_name=user.get("last_name"),
    )
