"""Crisis-detection path tests.

These drive the real ``SafetyFilter`` and ``SafetyService`` instead of
hand-inserting moderation rows (as the older ``test_e2e_crisis`` did), so they
actually guard the behaviour that was broken:

- a crisis message must NOT be blocked/throttled by the rate-limit gate;
- crisis detection must fire in every history scope, including ``downbad``;
- detection must log a moderation event and publish ``EVENT_CRISIS_DETECTED``.
"""

from __future__ import annotations

import json
from contextlib import suppress

import pytest

from app.core.events import event_bus
from app.db import db_ro, db_rw
from app.domain import events
from app.domain.safety.filter import SafetyFilter, matches_crisis
from app.domain.safety.service import SafetyService
from app.infra.db.moderation_repo import ModerationRepository

CRISIS_TEXT = "honestly sometimes I just want to kill myself"
CALM_TEXT = "the weather is really nice today"


def _ensure_personality_column(user_id: int, value: str) -> None:
    with db_rw() as conn:
        with suppress(Exception):
            conn.execute("ALTER TABLE users ADD COLUMN personality TEXT")
        conn.execute("UPDATE users SET personality = ? WHERE id = ?", (value, user_id))


def test_matches_crisis_keywords() -> None:
    assert matches_crisis(CRISIS_TEXT)
    assert matches_crisis("I want to die")
    assert not matches_crisis(CALM_TEXT)
    assert not matches_crisis("")


def test_filter_does_not_block_crisis(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.domain.safety.filter as filter_mod

    monkeypatch.setattr(filter_mod, "check_and_enforce_rate_limit", lambda uid: False)
    flt = SafetyFilter()

    decision = flt.evaluate(1, CRISIS_TEXT)
    assert decision.crisis is True
    assert decision.rate_limited is False
    assert decision.allowed is True  # a crisis must never block the message
    assert flt.allow(1, CRISIS_TEXT) is True


def test_filter_blocks_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.domain.safety.filter as filter_mod

    monkeypatch.setattr(filter_mod, "check_and_enforce_rate_limit", lambda uid: True)
    flt = SafetyFilter()

    decision = flt.evaluate(1, CALM_TEXT)
    assert decision.rate_limited is True
    assert decision.allowed is False
    assert flt.allow(1, CALM_TEXT) is False


def test_service_flags_and_publishes_crisis(test_user) -> None:
    user_id, _ = test_user
    captured: list[dict] = []

    def _capture(event) -> None:
        captured.append(event.payload)

    event_bus.subscribe(events.EVENT_CRISIS_DETECTED, _capture, mode="sync")
    try:
        service = SafetyService(ModerationRepository())
        flagged = service.inspect_message(user_id=user_id, chat_id=999, text=CRISIS_TEXT)
    finally:
        event_bus.unsubscribe(events.EVENT_CRISIS_DETECTED, _capture)

    assert flagged is True
    assert captured and captured[0]["severity"] == 5

    with db_ro() as conn:
        row = conn.execute(
            "SELECT event_type, severity, resolved FROM moderation_events WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    assert row is not None
    assert row["event_type"] == "crisis_detected"
    assert row["resolved"] == 0


def test_service_flags_crisis_in_downbad_scope(test_user) -> None:
    """The core regression: crisis detection must not be suppressed in NSFW/roleplay scope."""
    user_id, _ = test_user
    _ensure_personality_column(user_id, "downbad")

    service = SafetyService(ModerationRepository())
    flagged = service.inspect_message(user_id=user_id, chat_id=1, text=CRISIS_TEXT)

    assert flagged is True
    with db_ro() as conn:
        row = conn.execute(
            "SELECT details FROM moderation_events WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    assert row is not None
    assert json.loads(row["details"])["scope"] == "downbad"


def test_service_ignores_calm_message(test_user) -> None:
    user_id, _ = test_user
    service = SafetyService(ModerationRepository())
    flagged = service.inspect_message(user_id=user_id, chat_id=1, text=CALM_TEXT)

    assert flagged is False
    with db_ro() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM moderation_events WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    assert count["n"] == 0
