from __future__ import annotations

import json

from app.db import db_ro, db_rw
from app.domain.turns.audit import create_turn_audit
from app.domain.turns.models import TurnPlan
from app.utils.time_utils import operator_now
from app.workers.nightly import (
    merge_profile_fact_candidates,
    promote_durable_memories,
    repair_turn_audits_from_contradictions,
    rescore_referenced_memories,
)


def test_merge_profile_fact_candidates_promotes_repeated_values(test_user):
    user_id, _ = test_user
    with db_rw() as conn:
        conn.executemany(
            """
            INSERT INTO profile_fact_candidates (
                user_id, key, value, confidence, contradiction, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (user_id, "location", "dallas", 0.72, 0, "pending"),
                (user_id, "location", "dallas", 0.78, 0, "pending"),
                (user_id, "location", "austin", 0.83, 1, "pending"),
            ],
        )

    merge_profile_fact_candidates()

    with db_ro() as conn:
        profile_row = conn.execute(
            "SELECT value FROM profile_context WHERE user_id = ? AND key = 'location'",
            (user_id,),
        ).fetchone()
        statuses = conn.execute(
            "SELECT value, contradiction, status FROM profile_fact_candidates WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()

    assert profile_row is not None
    assert profile_row["value"] == "dallas"
    assert [row["status"] for row in statuses] == ["promoted", "promoted", "review_needed"]


def test_nightly_memory_promotion_and_rescore_updates_profile_context(test_session):
    user_id, session_id = test_session
    now = operator_now().isoformat()
    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO messages (user_id, session_id, scope, role, content, timestamp)
            VALUES (?, ?, 'standard', 'user', ?, ?)
            """,
            (user_id, session_id, "I have a job interview next Tuesday.", now),
        )
        message_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO conversation_embeddings (
                user_id,
                message_id,
                scope,
                role,
                content,
                summary,
                topics,
                context_window,
                embedding,
                importance_score,
                emotional_salience,
                user_value_score,
                context_score,
                reference_count,
                last_referenced_at
            ) VALUES (?, ?, 'standard', 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                message_id,
                "I have a job interview next Tuesday.",
                "Job interview next Tuesday",
                json.dumps(["career", "interview"]),
                "user: I have a job interview next Tuesday.",
                json.dumps([0.1, 0.2, 0.3]),
                6.2,
                0.6,
                0.8,
                0.35,
                3,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO profile_context (user_id, key, value)
            VALUES (?, 'memory_notes', ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (user_id, json.dumps([{"summary": "Existing note"}])),
        )

    promote_durable_memories(limit_per_user=5)
    rescore_referenced_memories()

    with db_ro() as conn:
        notes_row = conn.execute(
            "SELECT value FROM profile_context WHERE user_id = ? AND key = 'memory_notes'",
            (user_id,),
        ).fetchone()
        score_row = conn.execute(
            "SELECT importance_score FROM conversation_embeddings WHERE message_id = ?",
            (message_id,),
        ).fetchone()

    assert notes_row is not None
    payload = json.loads(str(notes_row["value"]))
    summaries = [str(item.get("summary") or "") for item in payload["notes"]]
    assert "Job interview next Tuesday" in summaries
    assert "Existing note" in summaries
    assert float(score_row["importance_score"]) > 6.2


def test_repair_turn_audits_from_contradictions_flags_pending_repairs(test_user):
    user_id, _ = test_user
    audit_id = create_turn_audit(
        user_id=user_id,
        session_id=None,
        user_message_id=None,
        assistant_message_id=None,
        correlation_id="corr-nightly-repair",
        user_text="My name is Alex now.",
        assistant_text="Okay, Alex.",
        plan=TurnPlan(
            user_id=user_id,
            session_id=None,
            message_text="My name is Alex now.",
            primary_intent="conversation",
            sentiment_priority="normal",
            contradictions=[
                {
                    "key": "name",
                    "existing_value": "Cole",
                    "new_value": "Alex",
                    "reason": "profile_context_mismatch",
                }
            ],
        ),
        route_trace=[],
        status="reply_ready",
    )
    assert audit_id is not None

    repair_turn_audits_from_contradictions()

    with db_ro() as conn:
        row = conn.execute(
            "SELECT status, route_json, followup_json FROM turn_audit_log WHERE id = ?",
            (audit_id,),
        ).fetchone()

    assert row is not None
    assert row["status"] == "repair_pending"
    followup = json.loads(str(row["followup_json"]))
    assert followup["repair_recommended"] is True
    route = json.loads(str(row["route_json"]))
    assert route[-1]["stage"] == "nightly.contradiction_repair_flagged"

