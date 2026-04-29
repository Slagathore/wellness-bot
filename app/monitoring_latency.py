"""Persistence helpers for live message latency telemetry."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict

from app.infra.db.session import db_ro, db_rw

logger = logging.getLogger(__name__)

_MAX_ROWS = 4000


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_timings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER,
            session_id INTEGER,
            correlation_id TEXT,
            rag_ms REAL,
            llm_ms REAL,
            lexical_ms REAL,
            memory_ms REAL,
            memory_mode TEXT,
            queue_ms REAL,
            persist_ms REAL,
            send_ms REAL,
            e2e_ms REAL,
            total_ms REAL NOT NULL,
            status TEXT NOT NULL,
            error TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_timings_id ON message_timings(id DESC)"
    )
    _ensure_columns(conn)


def _ensure_columns(conn) -> None:
    wanted: dict[str, str] = {
        "lexical_ms": "REAL",
        "memory_ms": "REAL",
        "memory_mode": "TEXT",
        "queue_ms": "REAL",
        "persist_ms": "REAL",
        "send_ms": "REAL",
        "e2e_ms": "REAL",
    }
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(message_timings)").fetchall()
    }
    for col, ddl in wanted.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE message_timings ADD COLUMN {col} {ddl}")


def record_message_timing(
    *,
    user_id: int | None,
    session_id: int | None,
    correlation_id: str | None,
    rag_ms: float | None,
    llm_ms: float | None,
    total_ms: float,
    status: str = "ok",
    error: str | None = None,
    lexical_ms: float | None = None,
    memory_ms: float | None = None,
    memory_mode: str | None = None,
    queue_ms: float | None = None,
    persist_ms: float | None = None,
    send_ms: float | None = None,
    e2e_ms: float | None = None,
) -> None:
    """Persist one message timing sample."""

    try:
        with db_rw() as conn:
            _ensure_table(conn)
            conn.execute(
                """
                INSERT INTO message_timings (
                    user_id,
                    session_id,
                    correlation_id,
                    rag_ms,
                    llm_ms,
                    lexical_ms,
                    memory_ms,
                    memory_mode,
                    queue_ms,
                    persist_ms,
                    send_ms,
                    e2e_ms,
                    total_ms,
                    status,
                    error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_id,
                    correlation_id,
                    rag_ms,
                    llm_ms,
                    lexical_ms,
                    memory_ms,
                    memory_mode,
                    queue_ms,
                    persist_ms,
                    send_ms,
                    e2e_ms,
                    total_ms,
                    status,
                    error,
                ),
            )
            conn.execute(
                """
                DELETE FROM message_timings
                WHERE id NOT IN (
                    SELECT id FROM message_timings ORDER BY id DESC LIMIT ?
                )
                """,
                (_MAX_ROWS,),
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to record message timing: %s", exc)


def read_recent_message_timings(limit: int = 30) -> Dict[str, Any]:
    """Load recent timing samples for admin UI."""

    limit = max(1, min(limit, 200))
    try:
        with db_rw() as conn:
            _ensure_table(conn)
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    created_at,
                    user_id,
                    session_id,
                    correlation_id,
                    queue_ms,
                    rag_ms,
                    llm_ms,
                    lexical_ms,
                    memory_ms,
                    memory_mode,
                    persist_ms,
                    send_ms,
                    e2e_ms,
                    total_ms,
                    status,
                    error
                FROM message_timings
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {"rows": [], "summary": _summary([])}
        raise

    rows_dict = [dict(r) for r in rows]
    return {"rows": rows_dict, "summary": _summary(rows_dict)}


def _summary(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {
            "count": 0,
            "avg_total_ms": None,
            "avg_queue_ms": None,
            "avg_rag_ms": None,
            "avg_llm_ms": None,
            "avg_memory_ms": None,
            "avg_persist_ms": None,
            "avg_send_ms": None,
            "avg_e2e_ms": None,
            "ok_count": 0,
            "error_count": 0,
        }

    def _vals(key: str) -> list[float]:
        out: list[float] = []
        for row in rows:
            value = row.get(key)
            if value is None:
                continue
            try:
                out.append(float(value))
            except (TypeError, ValueError):
                continue
        return out

    total_vals = _vals("total_ms")
    queue_vals = _vals("queue_ms")
    rag_vals = _vals("rag_ms")
    llm_vals = _vals("llm_ms")
    memory_vals = _vals("memory_ms")
    persist_vals = _vals("persist_ms")
    send_vals = _vals("send_ms")
    e2e_vals = _vals("e2e_ms")

    ok_count = sum(1 for row in rows if str(row.get("status", "")).startswith("ok"))

    def _avg(values: list[float]) -> float | None:
        if not values:
            return None
        return round(sum(values) / len(values), 1)

    return {
        "count": len(rows),
        "avg_total_ms": _avg(total_vals),
        "avg_queue_ms": _avg(queue_vals),
        "avg_rag_ms": _avg(rag_vals),
        "avg_llm_ms": _avg(llm_vals),
        "avg_memory_ms": _avg(memory_vals),
        "avg_persist_ms": _avg(persist_vals),
        "avg_send_ms": _avg(send_vals),
        "avg_e2e_ms": _avg(e2e_vals),
        "ok_count": ok_count,
        "error_count": len(rows) - ok_count,
    }
