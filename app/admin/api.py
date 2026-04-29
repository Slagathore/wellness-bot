"""Lightweight admin API for reviewing moderation events and rate limits."""

from __future__ import annotations

import secrets
from typing import List

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

import json

from app.config import settings
from app.db import db_ro, db_rw

security = HTTPBasic()
app = FastAPI()


def _require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    cfg = settings()
    if not cfg.admin_username:
        raise HTTPException(status_code=503, detail="Admin console disabled")
    if not secrets.compare_digest(credentials.username, cfg.admin_username):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    return credentials.username


@app.get("/moderation/events")
def list_moderation_events(
    resolve_status: str = "pending", admin: str = Depends(_require_admin)
) -> List[dict]:
    """Return moderation events filtered by resolution state."""

    resolved_clause = {
        "pending": "resolved = 0",
        "resolved": "resolved = 1",
        "all": "1 = 1",
    }.get(resolve_status)
    if resolved_clause is None:
        raise HTTPException(status_code=400, detail="Invalid resolve_status value")

    with db_ro() as conn:
        rows = conn.execute(
            f"""
            SELECT id, user_id, timestamp, event_type, severity, details, resolved, resolved_at, resolved_by
            FROM moderation_events
            WHERE {resolved_clause}
            ORDER BY severity DESC, timestamp DESC
            LIMIT 200
            """
        ).fetchall()
    return [dict(row) for row in rows]


class ResolveRequest(BaseModel):
    notes: str | None = None


@app.post("/moderation/events/{event_id}/resolve")
def resolve_event(
    event_id: int, payload: ResolveRequest, admin: str = Depends(_require_admin)
) -> dict:
    """Mark a moderation event as resolved by the authenticated admin."""

    with db_rw() as conn:
        updated = conn.execute(
            """
            UPDATE moderation_events
            SET resolved = 1,
                resolved_at = datetime('now'),
                resolved_by = ?,
                notes = COALESCE(?, notes)
            WHERE id = ?
            """,
            (admin, payload.notes, event_id),
        )
        if updated.rowcount == 0:
            raise HTTPException(status_code=404, detail="Event not found")
    return {"status": "ok"}


@app.get("/rate_limits")
def list_rate_limits(admin: str = Depends(_require_admin)) -> List[dict]:
    """List users currently warned, blocked, or banned."""

    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, status, blocked_until, violations, created_at
            FROM rate_limits
            WHERE status IN ('warned', 'blocked', 'banned')
            ORDER BY created_at DESC
            LIMIT 200
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/memory/conversation/{user_id}")
def list_conversation_memory(
    user_id: int,
    limit: int = 20,
    admin: str = Depends(_require_admin),
) -> List[dict]:
    """Return recent conversation memory embeddings for a user."""

    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT
                ce.message_id,
                ce.role,
                ce.summary,
                ce.topics,
                ce.created_at,
                m.content
            FROM conversation_embeddings AS ce
            LEFT JOIN messages AS m ON m.id = ce.message_id
            WHERE ce.user_id = ?
            ORDER BY ce.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

    response: List[dict] = []
    for row in rows:
        topics_raw = row["topics"]
        try:
            topics = json.loads(topics_raw) if topics_raw else []
        except (TypeError, json.JSONDecodeError):
            topics = []

        response.append(
            {
                "message_id": row["message_id"],
                "role": row["role"],
                "summary": row["summary"],
                "topics": topics,
                "created_at": row["created_at"],
                "content": row["content"],
            }
        )

    return response
