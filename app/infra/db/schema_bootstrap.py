"""Database schema bootstrap/compat helpers.

Ensures latest tables from ``schema/init_db.sql`` exist and patches a few legacy
column differences seen in long-lived local SQLite files.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from threading import Lock

from app.config import settings

_SCHEMA_LOCK = Lock()
_SCHEMA_READY = False
logger = logging.getLogger(__name__)


def ensure_schema_current(force: bool = False) -> None:
    """Apply baseline schema + compatibility patches once per process."""
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and not force:
            return
        db_path = Path(settings().database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            _apply_compat_patches(conn)
            _apply_baseline_schema(conn)
            _apply_performance_indexes(conn)
            _validate_required_schema(conn)
            conn.commit()
            _SCHEMA_READY = True
        finally:
            conn.close()


def _apply_baseline_schema(conn: sqlite3.Connection) -> None:
    schema_path = Path(__file__).resolve().parents[3] / "schema" / "init_db.sql"
    if not schema_path.exists():
        # Backward-compatible fallback for unusual launch layouts.
        schema_path = Path("schema/init_db.sql")
    if not schema_path.exists():
        return
    sql_text = schema_path.read_text(encoding="utf-8")
    # Execute statement-by-statement so legacy drift in one index/column does not
    # prevent the rest of the schema from being applied.
    for raw_stmt in sql_text.split(";"):
        stmt = raw_stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "no such column" in msg or "duplicate column name" in msg:
                logger.debug("Skipping incompatible schema statement: %s (%s)", stmt, exc)
                continue
            raise


def _apply_compat_patches(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "users", "personality", "TEXT DEFAULT 'friendly'")
    _ensure_column(conn, "sessions", "scope", "TEXT DEFAULT 'standard'")
    _ensure_column(conn, "messages", "timestamp", "TEXT")
    _ensure_column(conn, "messages", "created_at", "TEXT")
    _ensure_column(conn, "messages", "correlation_id", "TEXT")
    _ensure_column(conn, "messages", "scope", "TEXT DEFAULT 'standard'")
    _ensure_column(conn, "conversation_embeddings", "importance_score", "REAL DEFAULT 5.0")
    _ensure_column(conn, "conversation_embeddings", "context_window", "TEXT")
    _ensure_column(conn, "conversation_embeddings", "emotional_salience", "REAL DEFAULT 0.0")
    _ensure_column(conn, "conversation_embeddings", "user_value_score", "REAL DEFAULT 0.0")
    _ensure_column(conn, "conversation_embeddings", "context_score", "REAL DEFAULT 0.0")
    _ensure_column(conn, "conversation_embeddings", "reference_count", "INTEGER DEFAULT 0")
    _ensure_column(conn, "conversation_embeddings", "last_referenced_at", "TEXT")
    _ensure_column(conn, "conversation_embeddings", "scope", "TEXT DEFAULT 'standard'")
    _ensure_column(conn, "psychological_profiles", "confidence_score", "REAL DEFAULT 0.0")
    _ensure_column(conn, "psychological_profiles", "mental_health_indicators", "TEXT")
    _ensure_column(conn, "psychological_profiles", "big_five", "TEXT")
    _ensure_column(conn, "psychological_profiles", "cognitive_metrics", "TEXT")
    _ensure_column(conn, "psychological_profiles", "updated_at", "TEXT")
    _ensure_column(conn, "user_feedback", "updated_at", "TEXT")
    _ensure_column(conn, "profile_context", "updated_at", "TEXT")
    _ensure_column(conn, "checkin_configs", "updated_at", "TEXT")
    _ensure_column(conn, "adventures", "settings", "TEXT")
    _ensure_column(conn, "turn_audit_log", "updated_at", "TEXT")
    _ensure_column(conn, "turn_audit_log", "plan_json", "TEXT DEFAULT '{}'")
    _ensure_column(conn, "turn_audit_log", "route_json", "TEXT DEFAULT '[]'")
    _ensure_column(conn, "turn_audit_log", "followup_json", "TEXT DEFAULT '{}'")
    _ensure_column(conn, "profile_fact_candidates", "metadata", "TEXT DEFAULT '{}'")


def _apply_performance_indexes(conn: sqlite3.Connection) -> None:
    statements = [
        "CREATE INDEX IF NOT EXISTS idx_messages_user_session_id ON messages(user_id, session_id, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_user_scope_status ON sessions(user_id, scope, status)",
        "CREATE INDEX IF NOT EXISTS idx_messages_user_scope_time ON messages(user_id, scope, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_conv_embeddings_user_lastref ON conversation_embeddings(user_id, last_referenced_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_conv_embeddings_user_importance ON conversation_embeddings(user_id, importance_score DESC)",
        "CREATE INDEX IF NOT EXISTS idx_conv_embeddings_user_scope_lastref ON conversation_embeddings(user_id, scope, last_referenced_at DESC)",
    ]
    for stmt in statements:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            logger.debug("Skipping index statement %s (%s)", stmt, exc)


def _validate_required_schema(conn: sqlite3.Connection) -> None:
    required: dict[str, set[str]] = {
        "users": {
            "id",
            "telegram_user_id",
            "onboarding_completed",
            "last_active_at",
            "personality",
        },
        "sessions": {"id", "user_id", "scope", "status", "message_count", "token_count", "ctx_token_budget", "summary"},
        "messages": {"id", "user_id", "session_id", "scope", "role", "content", "timestamp"},
        "reminders": {"id", "user_id", "kind", "payload", "next_run_at", "enabled"},
        "profile_context": {"id", "user_id", "key", "value"},
        "conversation_embeddings": {
            "id",
            "user_id",
            "message_id",
            "scope",
            "role",
            "content",
            "embedding",
            "importance_score",
            "context_window",
            "emotional_salience",
            "user_value_score",
            "context_score",
            "reference_count",
            "last_referenced_at",
        },
        "generated_media": {"id", "user_id", "media_type", "prompt", "status", "created_at"},
        "turn_audit_log": {"id", "user_id", "plan_json", "route_json", "status"},
        "profile_fact_candidates": {"id", "user_id", "key", "value", "confidence"},
    }
    missing: list[str] = []
    for table, columns in required.items():
        if not _table_exists(conn, table):
            missing.append(f"missing table '{table}'")
            continue
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column in sorted(columns):
            if column not in existing:
                missing.append(f"missing column '{table}.{column}'")
    if missing:
        raise RuntimeError(
            "Database schema compatibility check failed: "
            + "; ".join(missing)
            + ". Run schema bootstrap/migrations before starting runtime."
        )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if not _table_exists(conn, table):
        return
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
