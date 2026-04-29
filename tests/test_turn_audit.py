from __future__ import annotations

import json

from app.db import db_ro
from app.domain.turns.audit import (
    append_turn_route,
    create_turn_audit,
    update_turn_followup,
)
from app.domain.turns.models import TurnPlan


def test_turn_audit_route_trace_updates(test_user):
    user_id, _ = test_user
    audit_id = create_turn_audit(
        user_id=user_id,
        session_id=None,
        user_message_id=11,
        assistant_message_id=12,
        correlation_id="corr-123",
        user_text="hello",
        assistant_text="hi there",
        plan=TurnPlan(
            user_id=user_id,
            session_id=None,
            message_text="hello",
            primary_intent="conversation",
            sentiment_priority="normal",
        ),
        route_trace=[],
        status="reply_ready",
    )
    assert audit_id is not None

    assert append_turn_route(
        audit_id=audit_id,
        stage="conversation.handler.send_reply_published",
        status="reply_dispatched",
        chat_id=123,
    )

    update_turn_followup(
        audit_id=audit_id,
        followup_json={"search_followup_sent": False},
    )

    with db_ro() as conn:
        row = conn.execute(
            "SELECT status, route_json, followup_json FROM turn_audit_log WHERE id = ?",
            (audit_id,),
        ).fetchone()

    assert row is not None
    assert row["status"] == "followed_up"
    route_trace = json.loads(str(row["route_json"]))
    assert route_trace[-1]["stage"] == "conversation.handler.send_reply_published"
    assert json.loads(str(row["followup_json"]))["search_followup_sent"] is False
