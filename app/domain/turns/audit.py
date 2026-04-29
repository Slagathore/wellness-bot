"""Audit helpers for full turn routing traces."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence

from app.db import db_rw
from app.infra.db.schema_bootstrap import ensure_schema_current

from .models import TurnPlan


def build_route_entry(stage: str, **details: Any) -> dict[str, Any]:
    return {
        "stage": str(stage),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": {k: v for k, v in details.items() if v is not None},
    }


def create_turn_audit(
    *,
    user_id: int,
    session_id: int | None,
    user_message_id: int | None,
    assistant_message_id: int | None,
    correlation_id: str | None,
    user_text: str,
    assistant_text: str,
    plan: TurnPlan | None,
    route_trace: Sequence[dict[str, Any]] | None = None,
    status: str = "created",
) -> int | None:
    plan_json = json.dumps(plan.to_dict(), ensure_ascii=True) if plan else "{}"
    route_json = json.dumps(list(route_trace or []), ensure_ascii=True)
    ensure_schema_current()
    with db_rw() as conn:
        cursor = conn.execute(
            """
            INSERT INTO turn_audit_log (
                user_id,
                session_id,
                user_message_id,
                assistant_message_id,
                correlation_id,
                user_text,
                assistant_text,
                plan_json,
                route_json,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                session_id,
                user_message_id,
                assistant_message_id,
                correlation_id,
                user_text[:800],
                assistant_text[:800],
                plan_json,
                route_json,
                status,
            ),
        )
        return int(cursor.lastrowid) if cursor.lastrowid is not None else None


def append_turn_route(
    *,
    stage: str,
    audit_id: int | None = None,
    correlation_id: str | None = None,
    status: str | None = None,
    **details: Any,
) -> bool:
    ensure_schema_current()
    with db_rw() as conn:
        target_id = audit_id
        if target_id is None and correlation_id:
            row = conn.execute(
                """
                SELECT id
                FROM turn_audit_log
                WHERE correlation_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (correlation_id,),
            ).fetchone()
            if row:
                target_id = int(row["id"])
        if target_id is None:
            return False

        row = conn.execute(
            "SELECT route_json FROM turn_audit_log WHERE id = ?",
            (target_id,),
        ).fetchone()
        route_trace: list[dict[str, Any]] = []
        if row and row["route_json"]:
            try:
                parsed = json.loads(str(row["route_json"]))
                if isinstance(parsed, list):
                    route_trace = [dict(item) for item in parsed if isinstance(item, dict)]
            except Exception:
                route_trace = []
        route_trace.append(build_route_entry(stage, **details))
        conn.execute(
            """
            UPDATE turn_audit_log
            SET route_json = ?, updated_at = CURRENT_TIMESTAMP, status = COALESCE(?, status)
            WHERE id = ?
            """,
            (json.dumps(route_trace, ensure_ascii=True), status, target_id),
        )
        return True


def update_turn_followup(
    *,
    audit_id: int,
    followup_json: dict[str, Any],
    status: str = "followed_up",
) -> None:
    ensure_schema_current()
    with db_rw() as conn:
        conn.execute(
            """
            UPDATE turn_audit_log
            SET followup_json = ?, updated_at = CURRENT_TIMESTAMP, status = ?
            WHERE id = ?
            """,
            (json.dumps(followup_json, ensure_ascii=True), status, audit_id),
        )
