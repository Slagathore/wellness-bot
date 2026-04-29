"""
Admin/ops HTTP surface with UI shell, live feed, trust cookie, and core controls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, StreamingResponse)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:  # optional system metrics
    import psutil  # type: ignore

    _PSUTIL = True
except Exception:
    psutil = None  # type: ignore
    _PSUTIL = False

from app.config import settings
from app.core.container import container
from app.core.events import event_bus
from app.domain import events
from app.domain.reminders.commands import CreateReminderCommand
from app.domain.reminders.service import ReminderService
from app.infra.db.reminders_repo import SqliteReminderRepository
from app.infra.db.schema_bootstrap import ensure_schema_current
from app.infra.db.session import db_ro, db_rw
from app.infra.llm.client import default_llm_client
from app.infra.vector.client import default_vector_client
from app.interfaces.admin.llm_console_tools import (TOOL_DEFINITIONS,
                                                    LLMConsoleTools)
from app.monitoring_latency import read_recent_message_timings
from app.services.media_generation_service import (MediaGenerationService,
                                                   get_media_service)

logger = logging.getLogger(__name__)

security = HTTPBasic()
app = FastAPI(title="Wellness Admin", version="1.0.0")
_LIVE_FEED_MAX = 200

# Mount static files for the new extracted frontend
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


class LiveFeed:
    """In-memory rolling buffer + async stream for live updates."""

    def __init__(self, max_items: int = 200, listener_queue_max: int = 200) -> None:
        self._buffer: deque[str] = deque(maxlen=max_items)
        self._listener_queue_max = max(1, listener_queue_max)
        self._listeners: list[asyncio.Queue[str]] = []

    def append(self, message: str) -> None:
        entry = f"{datetime.utcnow().isoformat()} {message}"
        self._buffer.append(entry)
        for q in list(self._listeners):
            try:
                if q.full():
                    q.get_nowait()
                q.put_nowait(entry)
            except asyncio.QueueFull:
                continue
            except asyncio.QueueEmpty:
                continue

    async def stream(self):
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._listener_queue_max)
        self._listeners.append(queue)
        try:
            for item in self._buffer:
                yield f"data: {item}\n\n"
            while True:
                msg = await queue.get()
                yield f"data: {msg}\n\n"
        finally:
            self._listeners.remove(queue)


live_feed = LiveFeed(_LIVE_FEED_MAX, listener_queue_max=_LIVE_FEED_MAX)


@app.on_event("startup")
async def _admin_startup() -> None:
    """Ensure latest DB schema exists when admin runs standalone."""
    ensure_schema_current()


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex)
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def _is_trusted(request: Request) -> bool:
    cfg = settings()
    token = cfg.admin_trust_token
    if token is None or token == "":
        return False
    cookie = request.cookies.get("admin_trust")
    if not cookie:
        return False
    return secrets.compare_digest(cookie, token)


def _require_admin(
    request: Request, credentials: HTTPBasicCredentials = Depends(security)
) -> str:
    # Trust cookie shortcut
    if _is_trusted(request):
        return "trusted-cookie"
    cfg = settings()
    if not cfg.admin_username or not cfg.admin_password:
        raise HTTPException(status_code=503, detail="Admin console disabled")
    if not secrets.compare_digest(
        credentials.username, cfg.admin_username
    ) or not secrets.compare_digest(credentials.password, cfg.admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    return credentials.username


def _mask_user(row: sqlite3.Row) -> Dict[str, Any]:
    """Return a PII-reduced view of a user row."""
    row_dict = dict(row)
    return {
        "id": row_dict.get("id"),
        "display_name": row_dict.get("display_name") or f"user_{row_dict.get('id')}",
        "created_at": row_dict.get("created_at"),
        "last_active_at": row_dict.get("last_active_at"),
        "personality": row_dict.get("personality"),
    }


def _audit(
    actor: str,
    action: str,
    target_user_id: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist an audit record."""
    try:
        with db_rw() as conn:
            conn.execute(
                "INSERT INTO audit_log (actor, action, target_user_id, details) VALUES (?, ?, ?, ?)",
                (actor, action, target_user_id, json.dumps(details or {})),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to audit action %s: %s", action, exc)


def _resolve_scheduler():
    """Return scheduler instance if this process owns one."""
    try:
        return container.resolve("scheduler")
    except Exception:
        return None


def _ollama_base_url() -> str:
    return settings().ollama_host.rstrip("/")


def _fetch_ollama_models() -> list[dict[str, Any]]:
    url = f"{_ollama_base_url()}/api/tags"
    req = urlrequest.Request(url, method="GET")
    with urlrequest.urlopen(req, timeout=8) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8"))
    models = payload.get("models") or []
    result: list[dict[str, Any]] = []
    for m in models:
        name = m.get("name") or ""
        result.append(
            {
                "name": name,
                "size": m.get("size"),
                "modified_at": m.get("modified_at"),
                "digest": m.get("digest"),
            }
        )
    return sorted(result, key=lambda x: x["name"])


def _as_utc(ts: str) -> datetime:
    raw = str(ts).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        # Fallback for SQLite-style timestamps without timezone
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_json_decode(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {"raw": text}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


def _fetch_message_context(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    pivot_ts: str | None,
    before: int = 3,
    after: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Return surrounding conversation snippets for moderation/crisis inspection."""
    if not pivot_ts:
        return {"before": [], "after": []}
    before_rows = conn.execute(
        """
        SELECT role, content, timestamp
        FROM messages
        WHERE user_id = ? AND timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (user_id, pivot_ts, before),
    ).fetchall()
    after_rows = conn.execute(
        """
        SELECT role, content, timestamp
        FROM messages
        WHERE user_id = ? AND timestamp > ?
        ORDER BY timestamp ASC
        LIMIT ?
        """,
        (user_id, pivot_ts, after),
    ).fetchall()
    return {
        "before": [dict(r) for r in reversed(before_rows)],
        "after": [dict(r) for r in after_rows],
    }


@app.get("/healthz", response_class=PlainTextResponse)
async def health(request: Request, admin: str = Depends(_require_admin)) -> str:
    live_feed.append("healthz check ok")
    return "ok"


@app.get("/auth/trust", response_class=JSONResponse)
async def set_trust(token: str, response: Response) -> Dict[str, Any]:
    cfg = settings()
    if not cfg.admin_trust_token or not secrets.compare_digest(
        token, cfg.admin_trust_token
    ):
        raise HTTPException(status_code=401, detail="invalid token")
    response.set_cookie(
        "admin_trust", token, max_age=7 * 24 * 3600, httponly=True, samesite="lax"
    )
    live_feed.append("trust cookie set")
    return {"status": "trusted"}


@app.get("/readyz", response_class=JSONResponse)
async def ready(
    request: Request, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    checks: Dict[str, str] = {}
    try:
        with db_ro() as conn:
            conn.execute("SELECT 1")
        checks["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["db"] = f"error: {exc}"

    try:
        vec = default_vector_client()
        _ = getattr(vec, "search", None)
        checks["vector"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["vector"] = f"error: {exc}"

    try:
        from app.infra.llm.client import default_llm_client

        _ = default_llm_client()
        checks["llm"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["llm"] = f"error: {exc}"

    status = "ready" if all(v == "ok" for v in checks.values() if v) else "degraded"
    live_feed.append(f"readyz status={status}")
    return {"status": status, "checks": checks}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(_: str = Depends(_require_admin)) -> str:
    live_feed.append("metrics scrape requested")
    return "# metrics to be implemented\n"


class ModelUpdateRequest(BaseModel):
    chat_model: Optional[str] = None
    embed_model: Optional[str] = None
    vision_model: Optional[str] = None


def _update_env_setting(key: str, value: str) -> None:
    # Update os.environ so pydantic BaseSettings picks up the new value
    # when the lru_cache is cleared (it reads os.environ before .env file)
    os.environ[key.upper()] = value

    env_path = Path(".env")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")


@app.get("/models", response_class=JSONResponse)
async def get_models(admin: str = Depends(_require_admin)) -> Dict[str, Any]:
    cfg = settings()
    return {
        "chat_model": cfg.chat_model,
        "embed_model": cfg.embed_model,
        "vision_model": cfg.vision_model,
        "note": "Updates persist to .env; restart runtime to apply everywhere.",
    }


@app.post("/models", response_class=JSONResponse)
async def update_models(
    payload: ModelUpdateRequest, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    if payload.chat_model:
        _update_env_setting("CHAT_MODEL", payload.chat_model)
    if payload.embed_model:
        _update_env_setting("EMBED_MODEL", payload.embed_model)
    if payload.vision_model:
        _update_env_setting("VISION_MODEL", payload.vision_model)
    settings.cache_clear()
    cfg = settings()
    live_feed.append("models updated via admin")
    return {
        "status": "updated",
        "chat_model": cfg.chat_model,
        "embed_model": cfg.embed_model,
        "vision_model": cfg.vision_model,
        "note": "Persisted to .env; restart runtime to propagate to other processes.",
    }


@app.get("/models/ollama", response_class=JSONResponse)
async def list_ollama_models(_: str = Depends(_require_admin)) -> Dict[str, Any]:
    try:
        return {"models": _fetch_ollama_models()}
    except urlerror.URLError as exc:
        raise HTTPException(
            status_code=502, detail=f"Ollama unreachable at {_ollama_base_url()}: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/models/pull/stream")
async def pull_ollama_model_stream(
    model: str, admin: str = Depends(_require_admin)
):
    model_name = (model or "").strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="model is required")

    async def _events():
        yield f"data: {json.dumps({'status': 'starting', 'progress': 0, 'message': f'Pulling {model_name}...'})}\n\n"
        proc = subprocess.Popen(
            ["ollama", "pull", model_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        progress = 0
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                pct = re.search(r"(\d{1,3})%", line)
                if pct:
                    try:
                        progress = max(progress, min(100, int(pct.group(1))))
                    except Exception:
                        pass
                payload = {"status": "running", "progress": progress, "message": line}
                yield f"data: {json.dumps(payload)}\n\n"
                await asyncio.sleep(0)
            rc = proc.wait()
            if rc == 0:
                yield f"data: {json.dumps({'status': 'completed', 'progress': 100, 'message': f'{model_name} ready'})}\n\n"
            else:
                yield f"data: {json.dumps({'status': 'error', 'progress': progress, 'message': f'ollama pull exited with code {rc}'})}\n\n"
        finally:
            if proc.poll() is None:
                proc.terminate()

    _audit(admin, "models.pull", details={"model": model_name})
    return StreamingResponse(_events(), media_type="text/event-stream")


@app.get("/stats/db", response_class=JSONResponse)
async def db_stats(_: str = Depends(_require_admin)) -> Dict[str, Any]:
    stats: Dict[str, Any] = {}
    with db_ro() as conn:
        try:
            stats["users"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        except Exception:
            stats["users"] = "unknown"
        for name, query in {
            "messages": "SELECT COUNT(*) FROM messages",
            "reminders_enabled": "SELECT COUNT(*) FROM reminders WHERE enabled = 1",
            "sentiments": "SELECT COUNT(*) FROM sentiments",
            "crises_open": "SELECT COUNT(*) FROM moderation_events WHERE resolved = 0",
            "images": "SELECT COUNT(*) FROM image_uploads",
        }.items():
            try:
                stats[name] = conn.execute(query).fetchone()[0]
            except Exception as exc:  # noqa: BLE001
                stats[name] = f"error: {exc}"
    return stats


class TrustRequest(BaseModel):
    token: str


@app.post("/auth/trust")
async def set_trust_token(
    payload: TrustRequest, response: Response, admin: str = Depends(_require_admin)
) -> Dict[str, str]:
    cfg = settings()
    tok = cfg.admin_trust_token
    if not tok:
        raise HTTPException(status_code=400, detail="Trust token not configured")
    if not secrets.compare_digest(payload.token, tok):
        raise HTTPException(status_code=401, detail="Invalid trust token")
    response.set_cookie(
        "admin_trust", tok, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30
    )
    live_feed.append("admin trust cookie set")
    return {"status": "trusted"}


@app.post("/actions/restart")
async def restart_bot(
    request: Request, admin: str = Depends(_require_admin)
) -> Dict[str, str]:
    try:
        req_id = getattr(request.state, "request_id", None)
        logger.info("Admin restart requested", extra={"request_id": req_id})
        event_bus.publish(events.EVENT_ADMIN_RESTART, {})
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to publish restart event: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to request restart"
        ) from exc
    live_feed.append("restart requested")
    return {"status": "queued"}


class BroadcastRequest(BaseModel):
    text: str
    dry_run: bool = False


class DeleteUsersRequest(BaseModel):
    user_ids: List[int]


@app.post("/actions/broadcast")
async def broadcast(
    payload: BroadcastRequest, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    cfg = settings()
    if not cfg.enable_dangerous_tools:
        raise HTTPException(status_code=403, detail="Dangerous tools disabled")
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty broadcast")
    with db_ro() as conn:
        rows = conn.execute(
            "SELECT id, telegram_user_id FROM users WHERE telegram_user_id IS NOT NULL"
        ).fetchall()
    if payload.dry_run:
        return {"status": "dry_run", "targets": len(rows)}
    count = 0
    with db_rw() as conn:
        for row in rows:
            chat_id = row["telegram_user_id"]
            conn.execute(
                "INSERT INTO telegram_outbox (user_id, chat_id, message_text) VALUES (?, ?, ?)",
                (row["id"], chat_id, text),
            )
            count += 1
    live_feed.append(f"broadcast sent to {count} users")
    return {"status": "sent", "targets": count}


@app.post("/actions/disable_bot")
async def disable_bot(admin: str = Depends(_require_admin)) -> Dict[str, str]:
    """Signal the runtime to disable interactions (scaffold)."""
    event_bus.publish(events.EVENT_ADMIN_DISABLE, {})
    live_feed.append("bot disable requested")
    return {"status": "queued"}


@app.post("/actions/enable_bot")
async def enable_bot(admin: str = Depends(_require_admin)) -> Dict[str, str]:
    """Signal the runtime to re-enable interactions (scaffold)."""
    event_bus.publish(events.EVENT_ADMIN_ENABLE, {})
    live_feed.append("bot enable requested")
    return {"status": "queued"}


@app.post("/actions/shutdown_admin")
async def shutdown_admin(admin: str = Depends(_require_admin)) -> Dict[str, str]:
    """Stop the admin process."""
    _audit(admin, "admin.shutdown")
    live_feed.append("admin shutdown requested")

    async def _exit_soon() -> None:
        await asyncio.sleep(0.4)
        os._exit(0)

    asyncio.create_task(_exit_soon())
    return {"status": "shutting_down"}


@app.get("/users")
async def list_users(
    limit: int = 25, offset: int = 0, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT id, telegram_user_id, telegram_username, display_name, last_active_at
            FROM users
            ORDER BY last_active_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    users = [
        {
            "id": row["id"],
            "telegram_user_id": row["telegram_user_id"],
            "username": row["telegram_username"],
            "display_name": row["display_name"],
            "last_active_at": row["last_active_at"],
        }
        for row in rows
    ]
    return {"users": users, "limit": limit, "offset": offset}


@app.get("/users/names", response_class=JSONResponse)
async def list_user_names(admin: str = Depends(_require_admin)) -> Dict[str, Any]:
    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT id,
                   COALESCE(NULLIF(display_name, ''), NULLIF(telegram_username, ''), ('user_' || id)) AS name
            FROM users
            ORDER BY LOWER(name) ASC
            """
        ).fetchall()
    return {"users": [{"id": r["id"], "name": r["name"]} for r in rows]}


def _cascade_delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Explicitly delete all data for a user across all tables.

    Required because some tables (created by older migrate_db.py) may lack
    ON DELETE CASCADE, which causes IntegrityError on a bare DELETE FROM users.
    Deletes grandchild rows first, then child rows, then the user itself.
    Table names below are all hard-coded constants — no injection risk.
    """
    uid = (user_id,)

    # --- Grandchild tables (keyed on IDs from child tables, not user_id) ---
    _grandchild_deletes = [
        ("sentiments", "message_id IN (SELECT id FROM messages WHERE user_id = ?)"),
        ("embedding_links", "message_id IN (SELECT id FROM messages WHERE user_id = ?)"),
        ("medication_logs", "medication_id IN (SELECT id FROM medications WHERE user_id = ?)"),
        ("profile_assessment_responses", "session_id IN (SELECT id FROM profile_assessment_sessions WHERE user_id = ?)"),
        ("habit_logs", "goal_id IN (SELECT id FROM wellness_goals WHERE user_id = ?)"),
        ("adventure_messages", "adventure_id IN (SELECT id FROM adventures WHERE user_id = ?)"),
        ("adventure_characters", "adventure_id IN (SELECT id FROM adventures WHERE user_id = ?)"),
    ]
    for table, where in _grandchild_deletes:
        try:
            conn.execute(f"DELETE FROM {table} WHERE {where}", uid)
        except sqlite3.OperationalError:
            pass  # table may not exist in this DB revision

    # --- Child tables (direct user_id foreign key) ---
    _child_tables = [
        "sessions", "messages", "reminders", "medications", "sleep_data",
        "mood_journal", "moderation_events", "rate_limits",
        "profile_assessment_sessions", "user_feedback",
        "conversation_embeddings", "profile_import_documents",
        "wellness_goals", "social_connections", "profile_context",
        "user_streaks", "conversation_exports", "telegram_outbox",
        "transcript_shards", "psychological_profiles", "image_uploads",
        "checkin_configs", "generated_media", "user_character_access",
        "adventures",
        # Legacy tables from migrate_db.py (may or may not exist)
        "moods", "meditation_sessions", "goals",
    ]
    for table in _child_tables:
        try:
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", uid)
        except sqlite3.OperationalError:
            pass  # table may not exist

    # SET NULL for custom_characters creator (FK uses ON DELETE SET NULL)
    try:
        conn.execute(
            "UPDATE custom_characters SET creator_user_id = NULL WHERE creator_user_id = ?",
            uid,
        )
    except sqlite3.OperationalError:
        pass

    # Finally delete the user row itself
    conn.execute("DELETE FROM users WHERE id = ?", uid)


@app.delete("/users/{user_id}", response_class=JSONResponse)
async def delete_user(user_id: int, admin: str = Depends(_require_admin)) -> Dict[str, Any]:
    with db_rw() as conn:
        existing = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="User not found")
        _cascade_delete_user(conn, user_id)
    _audit(admin, "user.delete", target_user_id=user_id)
    live_feed.append(f"user {user_id} deleted by admin")
    return {"status": "deleted", "user_id": user_id}


@app.post("/users/delete_many", response_class=JSONResponse)
async def delete_users_many(
    payload: DeleteUsersRequest, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    ids = sorted({int(x) for x in payload.user_ids if int(x) > 0})
    if not ids:
        raise HTTPException(status_code=400, detail="No valid user_ids provided")
    with db_rw() as conn:
        for uid in ids:
            _cascade_delete_user(conn, uid)
    _audit(admin, "user.delete_many", details={"count": len(ids), "user_ids": ids[:200]})
    live_feed.append(f"{len(ids)} users deleted by admin")
    return {"status": "deleted", "count": len(ids), "user_ids": ids}


@app.get("/users/{user_id}")
async def user_detail(
    user_id: int, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    with db_ro() as conn:
        user = conn.execute(
            "SELECT id, telegram_user_id, telegram_username, display_name, personality,"
            " last_active_at, onboarding_completed, onboarding_data"
            " FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        reminders = conn.execute(
            "SELECT id, payload, next_run_at, enabled FROM reminders WHERE user_id = ? ORDER BY next_run_at DESC LIMIT 50",
            (user_id,),
        ).fetchall()
        messages = conn.execute(
            "SELECT role, content, timestamp FROM messages WHERE user_id = ? ORDER BY timestamp DESC LIMIT 100",
            (user_id,),
        ).fetchall()
        checkins = conn.execute(
            "SELECT personalized_prompt, next_checkin_at, frequency, is_active FROM checkin_configs WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        profile_context = conn.execute(
            "SELECT key, value, updated_at FROM profile_context WHERE user_id = ? ORDER BY key",
            (user_id,),
        ).fetchall()
    return {
        "user": dict(user),
        "reminders": [dict(r) for r in reminders],
        "messages": [dict(m) for m in messages],
        "checkins": [dict(c) for c in checkins],
        "profile_context": [dict(p) for p in profile_context],
    }


@app.get("/users/{user_id}/messages")
async def user_messages(
    user_id: int, limit: int = 100, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    limit = max(1, min(limit, 500))
    with db_ro() as conn:
        rows = conn.execute(
            "SELECT role, content, timestamp FROM messages WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return {"user_id": user_id, "messages": [dict(r) for r in rows], "limit": limit}


@app.get("/users/{user_id}/reminders")
async def user_reminders(
    user_id: int, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    try:
        service: ReminderService = container.resolve("reminder_service")
    except Exception:
        service = ReminderService(SqliteReminderRepository())  # type: ignore[arg-type]
    reminders = service.list_for_user(str(user_id), limit=100)
    return {
        "user_id": user_id,
        "reminders": [
            {
                "id": r.id,
                "text": r.text,
                "due_at": r.due_at.isoformat() if getattr(r, "due_at", None) else None,
                "next_run_at": (
                    getattr(r, "next_run_at").isoformat()
                    if getattr(r, "next_run_at", None)
                    else (r.due_at.isoformat() if getattr(r, "due_at", None) else None)
                ),
                "cadence_cron": getattr(r, "cadence_cron", None),
                "enabled": bool(getattr(r, "enabled", True)),
                "last_delivered_at": (
                    delivered.isoformat()
                    if (delivered := getattr(r, "last_delivered_at", None))
                    else None
                ),
                "metadata": r.metadata or {},
            }
            for r in reminders
        ],
    }


@app.get("/status/modules")
async def module_status(admin: str = Depends(_require_admin)) -> Dict[str, Any]:
    """Module status scaffold for Phase 3 (bots, scheduler, services)."""
    status: Dict[str, Any] = {
        "telegram": "unknown",
        "scheduler": "unknown",
        "vector": "unknown",
        "llm": "unknown",
        "db": "unknown",
    }
    try:
        with db_ro() as conn:
            conn.execute("SELECT 1")
        status["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        status["db"] = f"error: {exc}"
    try:
        vec = default_vector_client()
        _ = getattr(vec, "search", None)
        status["vector"] = "ok"
    except Exception as exc:  # noqa: BLE001
        status["vector"] = f"error: {exc}"
    try:
        from app.infra.llm.client import default_llm_client

        _ = default_llm_client()
        status["llm"] = "ok"
    except Exception as exc:  # noqa: BLE001
        status["llm"] = f"error: {exc}"
    sched = _resolve_scheduler()
    if sched is None:
        status["scheduler"] = "external (runtime process)"
    else:
        running = getattr(sched, "_started", False)
        status["scheduler"] = "running" if running else "stopped"
    # Telegram status inferred from recent messages
    try:
        with db_ro() as conn:
            row = conn.execute("SELECT MAX(timestamp) AS ts FROM messages").fetchone()
            last_ts = row["ts"] if row else None
        if last_ts:
            last_dt = _as_utc(last_ts)
            status["telegram"] = (
                "active"
                if (datetime.now(timezone.utc) - last_dt) <= timedelta(minutes=15)
                else "stale"
            )
            status["telegram_last_message"] = last_ts
        else:
            status["telegram"] = "unknown"
    except Exception as exc:  # noqa: BLE001
        status["telegram"] = f"error: {exc}"
    return status


@app.get("/status/scheduler")
async def scheduler_status(admin: str = Depends(_require_admin)) -> Dict[str, Any]:
    """Return scheduler jobs and next run times."""
    sched = _resolve_scheduler()
    if sched is None:
        # Infer scheduler health from DB evidence
        evidence: Dict[str, Any] = {}
        try:
            with db_ro() as conn:
                # Last message processed
                r = conn.execute("SELECT MAX(timestamp) AS ts FROM messages").fetchone()
                evidence["last_message"] = r["ts"] if r and r["ts"] else None
                # Last sentiment analysis
                r = conn.execute("SELECT MAX(analyzed_at) AS ts FROM message_sentiments").fetchone()
                evidence["last_sentiment_analysis"] = r["ts"] if r and r["ts"] else None
                # Last psych profile
                r = conn.execute("SELECT MAX(created_at) AS ts FROM psychological_profiles").fetchone()
                evidence["last_psych_profile"] = r["ts"] if r and r["ts"] else None
                # Active reminders
                r = conn.execute("SELECT COUNT(*) AS cnt FROM reminders WHERE enabled = 1").fetchone()
                evidence["active_reminders"] = r["cnt"] if r else 0
                # Open moderation events
                r = conn.execute("SELECT COUNT(*) AS cnt FROM moderation_events WHERE resolved = 0").fetchone()
                evidence["open_moderation_events"] = r["cnt"] if r else 0
        except Exception as exc:  # noqa: BLE001
            evidence["error"] = str(exc)
        return {
            "running": None,
            "mode": "external",
            "detail": "Scheduler managed by bot runtime process. Showing inferred activity below.",
            "jobs": [],
            "evidence": evidence,
        }
    base = getattr(sched, "_scheduler", None)
    jobs = base.get_jobs() if base else []
    return {
        "running": bool(getattr(sched, "_started", False)),
        "mode": "local",
        "jobs": [
            {
                "id": j.id,
                "next_run": (j.next_run_time.isoformat() if j.next_run_time else None),
                "trigger": str(j.trigger),
            }
            for j in jobs
        ],
    }


@app.get("/users/{user_id}/images", response_class=JSONResponse)
async def user_images(
    user_id: int, limit: int = 200, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    limit = max(1, min(limit, 500))
    images: list[dict[str, Any]] = []
    tg_user_id: int | None = None
    try:
        with db_ro() as conn:
            user = conn.execute(
                "SELECT id, telegram_user_id, display_name FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            tg_user_id = user["telegram_user_id"]
            try:
                rows = conn.execute(
                    """
                    SELECT id, file_path, caption, vision_analysis, uploaded_at, processed
                    FROM image_uploads
                    WHERE user_id = ?
                    ORDER BY uploaded_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []

        for row in rows:
            fp = Path(row["file_path"]) if row["file_path"] else None
            images.append(
                {
                    "id": row["id"],
                    "file_path": str(fp) if fp else None,
                    "caption": row["caption"],
                    "vision_analysis": row["vision_analysis"],
                    "uploaded_at": row["uploaded_at"],
                    "processed": row["processed"],
                    "preview_url": f"/users/uploaded-image/{row['id']}",
                    "source": "db",
                }
            )
    except HTTPException:
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning("image_uploads query failed: %s", exc)

    # Filesystem fallback for older runs that didn't populate image_uploads.
    if not images and tg_user_id:
        try:
            img_dir = Path(settings().data_root) / "users" / str(tg_user_id) / "images"
            if img_dir.exists():
                files = sorted(
                    [
                        p
                        for p in img_dir.iterdir()
                        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                    ],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:limit]
                for p in files:
                    images.append(
                        {
                            "id": None,
                            "file_path": str(p),
                            "caption": None,
                            "vision_analysis": None,
                            "uploaded_at": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
                            "processed": None,
                            "preview_url": f"/users/{user_id}/image-file?name={urlparse.quote(p.name)}",
                            "source": "filesystem",
                        }
                    )
        except Exception as exc:
            logging.getLogger(__name__).warning("Filesystem image scan failed: %s", exc)

    return {"user_id": user_id, "images": images, "count": len(images)}


@app.get("/users/uploaded-image/{image_id}")
async def serve_uploaded_image(
    image_id: int, _admin: str = Depends(_require_admin)
):
    with db_ro() as conn:
        try:
            row = conn.execute(
                "SELECT file_path FROM image_uploads WHERE id = ?",
                (image_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: image_uploads" in str(exc).lower():
                raise HTTPException(status_code=404, detail="Image store not initialized")
            raise
    if not row or not row["file_path"]:
        raise HTTPException(status_code=404, detail="Image not found")
    path = Path(row["file_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Image file not found")
    media_type = "image/png"
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        media_type = "image/jpeg"
    elif path.suffix.lower() == ".webp":
        media_type = "image/webp"
    return FileResponse(path, media_type=media_type)


@app.get("/users/{user_id}/image-file")
async def serve_user_image_file(
    user_id: int, name: str, _admin: str = Depends(_require_admin)
):
    safe_name = Path(name).name
    with db_ro() as conn:
        user = conn.execute(
            "SELECT telegram_user_id FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    tg_user_id = user["telegram_user_id"]
    if not tg_user_id:
        raise HTTPException(status_code=404, detail="User has no telegram id")
    path = Path(settings().data_root) / "users" / str(tg_user_id) / "images" / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Image file not found")
    media_type = "image/png"
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        media_type = "image/jpeg"
    elif path.suffix.lower() == ".webp":
        media_type = "image/webp"
    return FileResponse(path, media_type=media_type)


@app.get("/status/telegram")
async def telegram_status(admin: str = Depends(_require_admin)) -> Dict[str, Any]:
    """Telegram activity inferred from recent messages."""
    info: Dict[str, Any] = {"status": "unknown"}
    try:
        with db_ro() as conn:
            row = conn.execute("SELECT MAX(timestamp) AS ts FROM messages").fetchone()
            recent = conn.execute(
                """
                SELECT m.id,
                       m.user_id,
                       m.role,
                       m.content,
                       m.timestamp,
                       COALESCE(NULLIF(u.display_name, ''), NULLIF(u.telegram_username, ''), ('user_' || m.user_id)) AS user_name,
                       s.emotion_label,
                       s.valence,
                       s.arousal,
                       s.dominance,
                       s.confidence
                FROM messages m
                LEFT JOIN users u ON u.id = m.user_id
                LEFT JOIN sentiments s ON s.message_id = m.id
                ORDER BY m.timestamp DESC
                LIMIT 20
                """
            ).fetchall()
        last_ts = row["ts"] if row else None
        info["last_message_at"] = last_ts
        if last_ts:
            last_dt = _as_utc(last_ts)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            info["status"] = (
                "active"
                if (datetime.now(timezone.utc) - last_dt) <= timedelta(minutes=15)
                else "stale"
            )
        info["recent_messages"] = [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "user_name": r["user_name"],
                "role": r["role"],
                "content": (r["content"] or "")[:280],
                "timestamp": r["timestamp"],
                "emotion_label": r["emotion_label"],
                "valence": r["valence"],
                "arousal": r["arousal"],
                "dominance": r["dominance"],
                "confidence": r["confidence"],
            }
            for r in recent
        ]
    except Exception as exc:  # noqa: BLE001
        info["status"] = "error"
        info["error"] = str(exc)
    return info


@app.get("/analytics/alerts")
async def alerts(admin: str = Depends(_require_admin)) -> Dict[str, Any]:
    """Lightweight alerts for reminders and moderation anomalies."""
    data: Dict[str, Any] = {}
    try:
        with db_ro() as conn:
            data["reminders_enabled"] = conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE enabled = 1"
            ).fetchone()[0]
            data["reminders_past_due"] = conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE enabled = 1 AND next_run_at < datetime('now')"
            ).fetchone()[0]
            data["open_moderation"] = conn.execute(
                "SELECT COUNT(*) FROM moderation_events WHERE resolved = 0"
            ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        data["error"] = str(exc)
    return data


@app.get("/crisis/active", response_class=JSONResponse)
async def crisis_active(
    limit: int = 100, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    limit = max(1, min(limit, 500))
    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT me.id, me.user_id, me.timestamp, me.severity, me.event_type, me.details,
                   me.resolved, u.personality,
                   COALESCE(NULLIF(u.display_name, ''), NULLIF(u.telegram_username, ''), ('user_' || me.user_id)) AS user_name
            FROM moderation_events me
            LEFT JOIN users u ON u.id = me.user_id
            WHERE me.resolved = 0
              AND (me.event_type = 'crisis_detected' OR me.severity >= 4)
            ORDER BY me.timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    alerts: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["details_obj"] = _safe_json_decode(item.get("details"))
        alerts.append(item)
    # Keep both keys for compatibility with both old and new frontends.
    return {"alerts": alerts, "events": alerts, "count": len(alerts)}


@app.get("/moderation/events", response_class=JSONResponse)
async def list_moderation_events(
    resolved: Optional[bool] = None,
    limit: int = 100,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    limit = max(1, min(limit, 500))
    where = ""
    params: List[Any] = []
    if resolved is not None:
        where = "WHERE resolved = ?"
        params.append(1 if resolved else 0)
    with db_ro() as conn:
        rows = conn.execute(
            f"""
            SELECT me.id, me.user_id, me.timestamp, me.event_type, me.severity, me.details,
                   me.resolved, me.resolved_at, me.resolved_by, u.personality,
                   COALESCE(NULLIF(u.display_name, ''), NULLIF(u.telegram_username, ''), ('user_' || me.user_id)) AS user_name
            FROM moderation_events me
            LEFT JOIN users u ON u.id = me.user_id
            {where}
            ORDER BY me.timestamp DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    events_out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["details_obj"] = _safe_json_decode(item.get("details"))
        events_out.append(item)
    return {"events": events_out, "count": len(events_out)}


@app.get("/moderation/events/{event_id}/context", response_class=JSONResponse)
async def moderation_event_context(
    event_id: int, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    with db_ro() as conn:
        event_row = conn.execute(
            """
            SELECT me.id, me.user_id, me.timestamp, me.event_type, me.severity, me.details,
                   me.resolved, me.resolved_at, me.resolved_by, u.personality,
                   COALESCE(NULLIF(u.display_name, ''), NULLIF(u.telegram_username, ''), ('user_' || me.user_id)) AS user_name
            FROM moderation_events me
            LEFT JOIN users u ON u.id = me.user_id
            WHERE me.id = ?
            """,
            (event_id,),
        ).fetchone()
        if not event_row:
            raise HTTPException(status_code=404, detail="Moderation event not found")

        event = dict(event_row)
        details_obj = _safe_json_decode(event.get("details"))
        event["details_obj"] = details_obj

        trigger_msg = details_obj.get("message")
        # If the message text wasn't stored, fetch it by message_id
        if not trigger_msg and details_obj.get("message_id"):
            msg_row = conn.execute(
                "SELECT content FROM messages WHERE id = ?",
                (details_obj["message_id"],),
            ).fetchone()
            if msg_row:
                trigger_msg = msg_row["content"] if isinstance(msg_row, sqlite3.Row) else msg_row[0]
        message_context = _fetch_message_context(
            conn,
            user_id=int(event["user_id"]),
            pivot_ts=event.get("timestamp"),
            before=4,
            after=2,
        )
    return {
        "event": event,
        "trigger_message": trigger_msg,
        "context": message_context,
    }


class ResolveModerationRequest(BaseModel):
    notes: str = ""


class BulkModerationRequest(BaseModel):
    event_ids: List[int]
    notes: str = ""


@app.post("/moderation/events/{event_id}/resolve", response_class=JSONResponse)
async def resolve_moderation_event(
    event_id: int,
    payload: ResolveModerationRequest,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    with db_rw() as conn:
        conn.execute(
            """
            UPDATE moderation_events
            SET resolved = 1,
                resolved_at = CURRENT_TIMESTAMP,
                resolved_by = ?,
                notes = ?
            WHERE id = ?
            """,
            (admin, payload.notes, event_id),
        )
    _audit(admin, "moderation.resolve", details={"event_id": event_id})
    live_feed.append(f"moderation event {event_id} resolved")
    return {"status": "resolved", "event_id": event_id}


@app.post("/moderation/events/bulk-resolve", response_class=JSONResponse)
async def bulk_resolve_moderation_events(
    payload: BulkModerationRequest,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    event_ids = sorted({int(event_id) for event_id in (payload.event_ids or [])})
    if not event_ids:
        raise HTTPException(status_code=400, detail="No event_ids provided")
    placeholders = ",".join("?" for _ in event_ids)
    with db_rw() as conn:
        cursor = conn.execute(
            f"""
            UPDATE moderation_events
            SET resolved = 1,
                resolved_at = CURRENT_TIMESTAMP,
                resolved_by = ?,
                notes = ?
            WHERE id IN ({placeholders})
            """,
            [admin, payload.notes or "Bulk resolved selected", *event_ids],
        )
        count = cursor.rowcount
    _audit(admin, "moderation.bulk_resolve", details={"count": count, "event_ids": event_ids[:50]})
    live_feed.append(f"bulk resolved {count} moderation events by {admin}")
    return {"status": "resolved", "count": count, "event_ids": event_ids}


@app.post("/moderation/events/bulk-delete", response_class=JSONResponse)
async def bulk_delete_moderation_events(
    payload: BulkModerationRequest,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    event_ids = sorted({int(event_id) for event_id in (payload.event_ids or [])})
    if not event_ids:
        raise HTTPException(status_code=400, detail="No event_ids provided")
    placeholders = ",".join("?" for _ in event_ids)
    with db_rw() as conn:
        cursor = conn.execute(
            f"DELETE FROM moderation_events WHERE id IN ({placeholders})",
            event_ids,
        )
        count = cursor.rowcount
    _audit(admin, "moderation.bulk_delete", details={"count": count, "event_ids": event_ids[:50]})
    live_feed.append(f"bulk deleted {count} moderation events by {admin}")
    return {"status": "deleted", "count": count, "event_ids": event_ids}


@app.post("/moderation/resolve-all", response_class=JSONResponse)
async def resolve_all_moderation_events(
    payload: ResolveModerationRequest,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    with db_rw() as conn:
        cursor = conn.execute(
            """
            UPDATE moderation_events
            SET resolved = 1,
                resolved_at = CURRENT_TIMESTAMP,
                resolved_by = ?,
                notes = ?
            WHERE resolved = 0
            """,
            (admin, payload.notes or "Bulk resolved all"),
        )
        count = cursor.rowcount
    _audit(admin, "moderation.resolve_all", details={"count": count})
    live_feed.append(f"all {count} open moderation events resolved by {admin}")
    return {"status": "resolved", "count": count}


@app.get("/reminders/{user_id}", response_class=JSONResponse)
async def list_reminders(
    user_id: str, _: str = Depends(_require_admin)
) -> Dict[str, Any]:
    try:
        service: ReminderService = container.resolve("reminder_service")
    except Exception:
        service = ReminderService(SqliteReminderRepository())  # type: ignore[arg-type]
    reminders = service.list_for_user(user_id, limit=50)
    return {
        "user_id": user_id,
        "reminders": [
            {
                "id": r.id,
                "kind": getattr(r, "kind", None),
                "text": r.text,
                "due_at": r.due_at.isoformat() if getattr(r, "due_at", None) else None,
                "next_run_at": (
                    getattr(r, "next_run_at").isoformat()
                    if getattr(r, "next_run_at", None)
                    else (r.due_at.isoformat() if getattr(r, "due_at", None) else None)
                ),
                "cadence_cron": getattr(r, "cadence_cron", None),
                "timezone": r.timezone,
                "enabled": bool(getattr(r, "enabled", True)),
                "last_delivered_at": (
                    delivered.isoformat()
                    if (delivered := getattr(r, "last_delivered_at", None))
                    else None
                ),
                "metadata": r.metadata or {},
            }
            for r in reminders
        ],
    }


class CreateReminderRequest(BaseModel):
    user_id: str
    text: str
    next_run_at: str  # ISO string
    cadence_cron: Optional[str] = None
    timezone: Optional[str] = None
    metadata: Dict[str, Any] | None = None
    enabled: bool = True


class UpdateReminderRequest(BaseModel):
    text: Optional[str] = None
    next_run_at: Optional[str] = None
    enabled: Optional[bool] = None
    cadence_cron: Optional[str] = None
    metadata: Dict[str, Any] | None = None


@app.get("/metrics/system")
async def system_metrics(admin: str = Depends(_require_admin)) -> Dict[str, Any]:
    """Basic system metrics; uses psutil when available."""
    data: Dict[str, Any] = {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    }
    # Disk at data_root
    try:
        root = Path(settings().data_root).anchor or "/"
        usage = shutil.disk_usage(root)
        data["disk"] = {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
        }
    except Exception as exc:  # noqa: BLE001
        data["disk"] = f"error: {exc}"
    if _PSUTIL:
        assert psutil is not None
        try:
            data["cpu_percent"] = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            data["memory"] = {
                "total": vm.total,
                "available": vm.available,
                "percent": vm.percent,
                "used": vm.used,
            }
            load = psutil.getloadavg() if hasattr(psutil, "getloadavg") else None
            if load:
                data["loadavg"] = load
        except Exception as exc:  # noqa: BLE001
            data["memory"] = f"error: {exc}"
    else:
        data["cpu_percent"] = "unavailable"
    return data


@app.get("/metrics/app")
async def app_metrics(
    hours: int = 24, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    """Basic application metrics from DB for the given window (hours)."""
    hours = max(1, min(hours, 168))
    now = datetime.utcnow()
    window_start = now - timedelta(hours=hours)
    in_hour = now + timedelta(hours=1)
    data: Dict[str, Any] = {}
    try:
        with db_ro() as conn:
            data["messages_total"] = conn.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
            data["messages_24h"] = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE timestamp >= ?",
                (window_start.isoformat(),),
            ).fetchone()[0]
            data["reminders_due_next_hour"] = conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE enabled = 1 AND next_run_at <= ?",
                (in_hour.isoformat(),),
            ).fetchone()[0]
            data["reminders_total"] = conn.execute(
                "SELECT COUNT(*) FROM reminders"
            ).fetchone()[0]
            data["moderation_open"] = conn.execute(
                "SELECT COUNT(*) FROM moderation_events WHERE resolved = 0"
            ).fetchone()[0]
            data["window_hours"] = hours
    except Exception as exc:  # noqa: BLE001
        data["error"] = str(exc)
    return data


@app.get("/metrics/timeseries")
async def metrics_timeseries(
    hours: int = 48, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    """Return simple timeseries counts for messages and reminders."""
    hours = max(1, min(hours, 336))
    now = datetime.utcnow()
    window_start = now - timedelta(hours=hours)
    result: Dict[str, Any] = {"hours": hours, "messages": [], "reminders": []}
    try:
        with db_ro() as conn:
            msg_rows = conn.execute(
                """
                SELECT strftime('%Y-%m-%d %H:00', timestamp) AS bucket, COUNT(*) AS count
                FROM messages
                WHERE timestamp >= ?
                GROUP BY bucket
                ORDER BY bucket
                """,
                (window_start.isoformat(),),
            ).fetchall()
            result["messages"] = [
                {"bucket": row["bucket"], "count": row["count"]} for row in msg_rows
            ]
            rem_rows = conn.execute(
                """
                SELECT strftime('%Y-%m-%d %H:00', next_run_at) AS bucket, COUNT(*) AS count
                FROM reminders
                WHERE next_run_at >= ?
                GROUP BY bucket
                ORDER BY bucket
                """,
                (window_start.isoformat(),),
            ).fetchall()
            result["reminders"] = [
                {"bucket": row["bucket"], "count": row["count"]} for row in rem_rows
            ]
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


@app.get("/metrics/latency_live")
async def metrics_latency_live(
    limit: int = 30, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    """Return recent per-message latency samples and aggregate summary."""
    return read_recent_message_timings(limit=limit)


@app.post("/reminders", response_class=JSONResponse)
async def create_reminder(
    payload: CreateReminderRequest,
    request: Request,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    try:
        service: ReminderService = container.resolve("reminder_service")
    except Exception:
        service = ReminderService(SqliteReminderRepository())  # type: ignore[arg-type]

    try:
        next_run_dt = datetime.fromisoformat(payload.next_run_at)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"Invalid next_run_at: {exc}"
        ) from exc

    rid = service.create(
        CreateReminderCommand(
            user_id=payload.user_id,
            text=payload.text,
            next_run_at=next_run_dt,
            cadence_cron=payload.cadence_cron,
            enabled=payload.enabled,
            timezone=payload.timezone,
            metadata={
                **(payload.metadata or {}),
                "text": payload.text,
                **({"cadence_cron": payload.cadence_cron} if payload.cadence_cron else {}),
                "enabled": payload.enabled,
            },
        )
    )
    req_id = getattr(request.state, "request_id", None)
    logger.info(
        "Admin created reminder %s for user %s",
        rid,
        payload.user_id,
        extra={"request_id": req_id},
    )
    live_feed.append(f"reminder {rid} created for user {payload.user_id}")
    return {"status": "created", "id": rid, "request_id": req_id}


@app.post("/reminders/{reminder_id}/disable", response_class=JSONResponse)
async def disable_reminder(
    reminder_id: str, request: Request, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    try:
        service: ReminderService = container.resolve("reminder_service")
    except Exception:
        service = ReminderService(SqliteReminderRepository())  # type: ignore[arg-type]
    service.disable(reminder_id)
    req_id = getattr(request.state, "request_id", None)
    logger.info("Admin disabled reminder %s", reminder_id, extra={"request_id": req_id})
    live_feed.append(f"reminder {reminder_id} disabled by admin")
    return {"status": "disabled", "reminder_id": reminder_id, "request_id": req_id}


@app.post("/reminders/user/{user_id}/disable_all", response_class=JSONResponse)
async def disable_all_reminders(
    user_id: str, request: Request, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    try:
        service: ReminderService = container.resolve("reminder_service")
    except Exception:
        service = ReminderService(SqliteReminderRepository())  # type: ignore[arg-type]
    updated = service.disable_all_for_user(user_id)
    req_id = getattr(request.state, "request_id", None)
    logger.info(
        "Admin disabled all reminders for user %s (updated=%s)",
        user_id,
        updated,
        extra={"request_id": req_id},
    )
    live_feed.append(f"all reminders disabled for user {user_id} (count={updated})")
    return {
        "status": "disabled",
        "user_id": user_id,
        "updated": updated,
        "request_id": req_id,
    }


@app.post("/reminders/clear_all", response_class=JSONResponse)
async def clear_all_reminders(
    request: Request, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    """Delete every reminder across all users."""
    with db_rw() as conn:
        before = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        conn.execute("DELETE FROM reminders")
    req_id = getattr(request.state, "request_id", None)
    logger.warning(
        "Admin cleared all reminders (deleted=%s)",
        before,
        extra={"request_id": req_id},
    )
    _audit(admin, "reminders.clear_all", details={"deleted": before})
    live_feed.append(f"all reminders cleared by admin (deleted={before})")
    return {"status": "cleared", "deleted": before, "request_id": req_id}


@app.post("/reminders/{reminder_id}/enable", response_class=JSONResponse)
async def enable_reminder(
    reminder_id: str, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    with db_rw() as conn:
        conn.execute("UPDATE reminders SET enabled = 1 WHERE id = ?", (reminder_id,))
    live_feed.append(f"reminder {reminder_id} enabled by admin")
    return {"status": "enabled", "reminder_id": reminder_id}


@app.post("/reminders/{reminder_id}/delete", response_class=JSONResponse)
async def delete_reminder(
    reminder_id: str, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    with db_rw() as conn:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    live_feed.append(f"reminder {reminder_id} deleted by admin")
    return {"status": "deleted", "reminder_id": reminder_id}


@app.post("/reminders/{reminder_id}/update", response_class=JSONResponse)
async def update_reminder(
    reminder_id: str,
    payload: UpdateReminderRequest,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    with db_rw() as conn:
        row = conn.execute(
            "SELECT payload FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Reminder not found")
        meta: Dict[str, Any] = {}
        if row["payload"]:
            try:
                meta = json.loads(row["payload"])
            except Exception:
                meta = {"raw": row["payload"]}
        if payload.text is not None:
            meta["text"] = payload.text
        if payload.metadata:
            meta.update(payload.metadata)
        if payload.cadence_cron is not None:
            meta["cadence_cron"] = payload.cadence_cron
        if payload.enabled is not None:
            meta["enabled"] = bool(payload.enabled)
        updates: List[str] = ["payload = ?"]
        params: List[Any] = [json.dumps(meta)]
        if payload.next_run_at is not None:
            updates.append("next_run_at = ?")
            params.append(payload.next_run_at)
        if payload.cadence_cron is not None:
            updates.append("cadence_cron = ?")
            params.append(payload.cadence_cron)
        if payload.enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if payload.enabled else 0)
        params.append(reminder_id)
        conn.execute(
            f"UPDATE reminders SET {', '.join(updates)} WHERE id = ?",
            params,
        )
    live_feed.append(f"reminder {reminder_id} updated by admin")
    return {"status": "updated", "reminder_id": reminder_id}


@app.get("/feedback", response_class=JSONResponse)
async def list_feedback(
    status: Optional[str] = None, limit: int = 50, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    limit = max(1, min(limit, 200))
    with db_ro() as conn:
        conn.row_factory = sqlite3.Row
        params: List[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            params.append(status)
        rows = conn.execute(
            f"SELECT id, user_id, feedback_type, content, status, admin_notes, created_at, updated_at FROM user_feedback {where} ORDER BY created_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return {"feedback": [dict(r) for r in rows], "count": len(rows)}


class UpdateFeedbackRequest(BaseModel):
    status: Optional[str] = None
    admin_notes: Optional[str] = None


@app.post("/feedback/{feedback_id}/update", response_class=JSONResponse)
async def update_feedback(
    feedback_id: int,
    payload: UpdateFeedbackRequest,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    fields = []
    params: List[Any] = []
    if payload.status:
        fields.append("status = ?")
        params.append(payload.status)
    if payload.admin_notes is not None:
        fields.append("admin_notes = ?")
        params.append(payload.admin_notes)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    params.append(feedback_id)
    with db_rw() as conn:
        conn.execute(
            f"UPDATE user_feedback SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            params,
        )
    live_feed.append(f"feedback {feedback_id} updated")
    return {"status": "updated", "id": feedback_id}


@app.get("/psych/{user_id}", response_class=JSONResponse)
async def psych_profile(
    user_id: int,
    profile_id: Optional[int] = None,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    with db_ro() as conn:
        conn.row_factory = sqlite3.Row
        if profile_id is None:
            profile = conn.execute(
                "SELECT * FROM psychological_profiles WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        else:
            profile = conn.execute(
                "SELECT * FROM psychological_profiles WHERE id = ? AND user_id = ?",
                (profile_id, user_id),
            ).fetchone()
        sessions = conn.execute(
            "SELECT * FROM profile_assessment_sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
            (user_id,),
        ).fetchall()
        responses = conn.execute(
            "SELECT * FROM profile_assessment_responses WHERE session_id IN (SELECT id FROM profile_assessment_sessions WHERE user_id = ?)"
            " ORDER BY created_at DESC LIMIT 200",
            (user_id,),
        ).fetchall()
    return {
        "profile": dict(profile) if profile else None,
        "sessions": [dict(s) for s in sessions],
        "responses": [dict(r) for r in responses],
    }


@app.get("/psych/{user_id}/history", response_class=JSONResponse)
async def psych_profile_history(
    user_id: int, limit: int = 10, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    limit = max(1, min(limit, 20))
    with db_ro() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, created_at, updated_at, messages_analyzed
            FROM psychological_profiles
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return {"history": [dict(r) for r in rows], "count": len(rows)}


@app.post("/psych/{user_id}/reanalyze", response_class=JSONResponse)
async def psych_reanalyze(
    user_id: int, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    try:
        from app.workers.nightly import _analyze_user_psychological_profile
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Psych analyzer unavailable: {exc}")

    with db_ro() as conn:
        user = conn.execute(
            "SELECT id, telegram_user_id FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        new_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'user'",
            (user_id,),
        ).fetchone()[0]
    tg_user_id = user["telegram_user_id"]
    if tg_user_id is None:
        raise HTTPException(status_code=400, detail="User has no telegram id")

    job_id = uuid.uuid4().hex
    _evict_old_psych_jobs()
    _PSYCH_REANALYZE_JOBS[job_id] = {
        "job_id": job_id,
        "user_id": user_id,
        "status": "queued",
        "progress": 5,
        "detail": "Queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }

    async def _run() -> None:
        job = _PSYCH_REANALYZE_JOBS[job_id]
        job.update(status="running", progress=20, detail="Analyzing message history",
                   updated_at=datetime.now(timezone.utc).isoformat())
        ok = False
        try:
            ok = await asyncio.wait_for(
                asyncio.to_thread(
                    _analyze_user_psychological_profile,
                    int(user_id),
                    int(tg_user_id),
                    int(new_count),
                ),
                timeout=300.0,  # 5 minute cap
            )
            job.update(status="ok" if ok else "skipped", progress=100,
                       detail="Profile updated" if ok else "Skipped (not enough data)")
        except asyncio.TimeoutError:
            job.update(status="error", progress=100, detail="Timed out after 5 minutes")
            logger.error("Psych reanalyze timed out for user %s", user_id)
        except Exception as exc:  # noqa: BLE001
            job.update(status="error", progress=100, detail=f"Error: {exc}")
            logger.exception("Psych reanalyze failed for user %s: %s", user_id, exc)
        finally:
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _audit(
            admin,
            "psych.reanalyze",
            target_user_id=user_id,
            details={"ok": ok, "job_id": job_id},
        )

    _task = asyncio.create_task(_run())
    _background_tasks.add(_task)
    _task.add_done_callback(_background_tasks.discard)
    return {"status": "queued", "user_id": user_id, "job_id": job_id}


@app.get("/psych/reanalyze/{job_id}", response_class=JSONResponse)
async def psych_reanalyze_status(
    job_id: str, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    job = _PSYCH_REANALYZE_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Reanalysis job not found")
    return dict(job)


@app.get("/memory/search", response_class=JSONResponse)
async def memory_search(
    q: str,
    user_id: Optional[int] = None,
    role: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    limit = max(1, min(limit, 200))
    clauses = ["content LIKE ?"]
    params: List[Any] = [f"%{q}%"]
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if role:
        clauses.append("m.role = ?")
        params.append(role)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(until)
    where = " AND ".join(clauses)
    with db_ro() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT m.id, m.user_id, m.role, m.content, m.timestamp, u.display_name
            FROM messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE {where}
            ORDER BY m.timestamp DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    results = []
    for r in rows:
        masked = {
            "id": r["id"],
            "user_id": r["user_id"],
            "role": r["role"],
            "content": r["content"],
            "timestamp": r["timestamp"],
            "display_name": r["display_name"] or f"user_{r['user_id']}",
        }
        results.append(masked)
    return {"results": results, "count": len(results)}


@app.get("/export/user/{user_id}", response_class=JSONResponse)
async def export_user_history(
    user_id: int,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 500,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    limit = max(1, min(limit, 2000))
    clauses = ["user_id = ?"]
    params: List[Any] = [user_id]
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp <= ?")
        params.append(until)
    where = " AND ".join(clauses)
    with db_ro() as conn:
        conn.row_factory = sqlite3.Row
        user_row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        messages = conn.execute(
            f"SELECT id, user_id, role, content, timestamp FROM messages WHERE {where} ORDER BY timestamp DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        reminders = conn.execute(
            "SELECT id, user_id, kind, payload, next_run_at, enabled, created_at FROM reminders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, 200),
        ).fetchall()
    masked_user = _mask_user(user_row) if user_row else None
    return {
        "user": masked_user,
        "messages": [dict(m) for m in messages],
        "reminders": [dict(r) for r in reminders],
    }


class LlmConsoleRequest(BaseModel):
    prompt: str
    context: Optional[str] = None
    confirm: bool = False


@app.post("/highrisk/llm_console", response_class=JSONResponse)
async def highrisk_llm_console(
    payload: LlmConsoleRequest, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    cfg = settings()
    if not (cfg.enable_dangerous_tools and cfg.admin_llm_console_enabled):
        raise HTTPException(status_code=403, detail="LLM console disabled by config")
    _audit(
        admin,
        "highrisk.llm_console.request",
        details={"prompt_preview": payload.prompt[:120]},
    )
    if not payload.confirm:
        return {
            "status": "confirm_required",
            "detail": "Set confirm=true to run; currently only echoes prompt.",
        }
    client = default_llm_client()
    system_msg = "You are an admin console assistant. Be concise. Never output secrets."
    msgs = [{"role": "system", "content": system_msg}]
    if payload.context:
        msgs.append({"role": "system", "content": f"context: {payload.context[:8000]}"})
    msgs.append({"role": "user", "content": payload.prompt})
    try:
        reply = client.chat(
            messages=msgs, model=cfg.chat_model, options={"temperature": 0.2}
        )
    except Exception as exc:  # noqa: BLE001
        _audit(admin, "highrisk.llm_console.error", details={"error": str(exc)})
        raise HTTPException(status_code=502, detail=f"LLM error: {exc}")
    _audit(
        admin,
        "highrisk.llm_console.success",
        details={"prompt_len": len(payload.prompt), "reply_preview": str(reply)[:120]},
    )
    return {"status": "ok", "reply": reply}


class DbEditRequest(BaseModel):
    table: str
    where: str
    set: Dict[str, Any]
    confirm: bool = False
    dry_run: bool = True


@app.post("/highrisk/db_edit", response_class=JSONResponse)
async def highrisk_db_edit(
    payload: DbEditRequest, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    cfg = settings()
    if not (cfg.enable_dangerous_tools and cfg.admin_db_edit_enabled):
        raise HTTPException(status_code=403, detail="DB edit disabled by config")
    allowed: Dict[str, Tuple[set[str], str]] = {
        "users": (
            {
                "display_name",
                "onboarding_data",
                "feature_flags",
                "personality",
                "last_active_at",
            },
            "id",
        ),
        "reminders": (
            {"text", "payload", "next_run_at", "enabled", "cadence_cron"},
            "id",
        ),
        "moderation_events": ({"resolved", "admin_notes"}, "id"),
    }
    if payload.table not in allowed:
        raise HTTPException(status_code=400, detail="Table not allowed")
    allowed_cols, pk = allowed[payload.table]
    invalid = [k for k in payload.set.keys() if k not in allowed_cols]
    if invalid:
        raise HTTPException(
            status_code=400, detail=f"Columns not allowed: {', '.join(invalid)}"
        )
    # basic where safety
    if not payload.where or ";" in payload.where:
        raise HTTPException(status_code=400, detail="Invalid where clause")
    preview = {
        "table": payload.table,
        "where": payload.where,
        "set": payload.set,
        "sql_preview": f"UPDATE {payload.table} SET {', '.join(f'{k}=?' for k in payload.set.keys())} WHERE {payload.where}",
    }
    if not payload.confirm:
        return {"status": "confirm_required", **preview}
    with db_rw() as conn:
        cur = conn.execute(
            f"SELECT COUNT(*) FROM {payload.table} WHERE {payload.where}"
        )
        to_update = cur.fetchone()[0]
        if payload.dry_run:
            _audit(
                admin,
                "highrisk.db_edit.dry_run",
                details={**preview, "rows": to_update},
            )
            return {"status": "accepted", "dry_run": True, "rows": to_update, **preview}
        if to_update == 0:
            return {"status": "accepted", "updated": 0, **preview}
        params = list(payload.set.values())
        conn.execute(
            f"UPDATE {payload.table} SET {', '.join(f'{k}=?' for k in payload.set.keys())} WHERE {payload.where}",
            params,
        )
    _audit(
        admin,
        "highrisk.db_edit",
        details={**preview, "updated": to_update, "dry_run": False},
    )
    return {"status": "accepted", "updated": to_update, **preview}


class OmniBroadcastRequest(BaseModel):
    message: str
    channels: Optional[List[str]] = None
    confirm: bool = False
    dry_run: bool = True


@app.post("/highrisk/omni_broadcast", response_class=JSONResponse)
async def highrisk_omni_broadcast(
    payload: OmniBroadcastRequest, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    cfg = settings()
    if not (cfg.enable_dangerous_tools and cfg.admin_omni_broadcast_enabled):
        raise HTTPException(status_code=403, detail="Omni broadcast disabled by config")
    details = {
        "message_preview": payload.message[:200],
        "channels": payload.channels or ["telegram"],
        "dry_run": payload.dry_run,
    }
    if not payload.confirm:
        return {"status": "confirm_required", **details}
    if payload.dry_run:
        _audit(admin, "highrisk.omni_broadcast.dry_run", details=details)
        return {
            "status": "accepted",
            "detail": "Dry-run only; no messages sent",
            **details,
        }
    # Minimal routing: telegram only for now
    sent = 0
    failures: List[str] = []
    telegram_rows = []
    channels = details.get("channels")
    if not isinstance(channels, list):
        channels = []
    if "telegram" in channels:
        with db_ro() as conn:
            telegram_rows = conn.execute(
                "SELECT id, telegram_user_id FROM users WHERE telegram_user_id IS NOT NULL"
            ).fetchall()
    for ch in channels:
        try:
            if ch == "telegram":
                with db_rw() as conn:
                    for row in telegram_rows:
                        conn.execute(
                            "INSERT INTO telegram_outbox (user_id, chat_id, message_text) VALUES (?, ?, ?)",
                            (row["id"], row["telegram_user_id"], payload.message),
                        )
                sent += len(telegram_rows)
            else:
                failures.append(f"unsupported channel {ch}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{ch}: {exc}")
    _audit(
        admin,
        "highrisk.omni_broadcast",
        details={**details, "sent": sent, "failures": failures},
    )
    return {"status": "accepted", "sent": sent, "failures": failures, **details}


# ============================================================================
# ENHANCED LLM CONSOLE WITH TOOLS
# ============================================================================

# In-memory conversation history (upgrade to Redis for production)
_CONSOLE_SESSIONS_MAX = 50
_CONSOLE_MESSAGES_MAX = 40
_console_conversations: "OrderedDict[str, List[Dict[str, str]]]" = OrderedDict()
_console_tools = LLMConsoleTools()
_PSYCH_REANALYZE_JOBS: Dict[str, Dict[str, Any]] = {}
_PSYCH_JOBS_MAX = 200
_PSYCH_JOBS_TTL_SECONDS = 3600


def _evict_old_psych_jobs() -> None:
    """Evict finished psych-reanalyze jobs older than TTL and enforce the hard cap."""
    if not _PSYCH_REANALYZE_JOBS:
        return
    cutoff = time.time() - _PSYCH_JOBS_TTL_SECONDS
    to_delete = [
        job_id
        for job_id, info in list(_PSYCH_REANALYZE_JOBS.items())
        if info.get("finished_at") and _iso_to_ts(info["finished_at"]) < cutoff
    ]
    for job_id in to_delete:
        _PSYCH_REANALYZE_JOBS.pop(job_id, None)
    if len(_PSYCH_REANALYZE_JOBS) >= _PSYCH_JOBS_MAX:
        overflow = len(_PSYCH_REANALYZE_JOBS) - _PSYCH_JOBS_MAX + 1
        sorted_ids = sorted(
            _PSYCH_REANALYZE_JOBS,
            key=lambda k: _iso_to_ts(_PSYCH_REANALYZE_JOBS[k].get("created_at") or ""),
        )
        for job_id in sorted_ids[:overflow]:
            _PSYCH_REANALYZE_JOBS.pop(job_id, None)


# Module-level set to hold strong references to fire-and-forget background tasks
# so the GC does not collect them before they complete.
_background_tasks: set[asyncio.Task] = set()


class EnhancedConsoleRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    allow_external_files: bool = False
    tools_enabled: bool = True
    max_iterations: int = 5
    num_ctx: int = 8192  # context window size sent to model
    timeout_seconds: float = 180.0  # overall request timeout


# -- helpers ---------------------------------------------------------------

_MAX_TOOL_RESULT_CHARS = 4000  # prevent context-window blow-up from huge results


def _truncate_tool_result(text: str) -> str:
    """Trim tool output to a safe size before injecting into the LLM context."""
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    half = _MAX_TOOL_RESULT_CHARS // 2
    return (
        text[:half]
        + f"\n\n... [TRUNCATED {len(text) - _MAX_TOOL_RESULT_CHARS} chars] ...\n\n"
        + text[-half:]
    )


def _get_console_conversation(session_id: str) -> List[Dict[str, str]]:
    """Return an LRU-tracked console conversation with bounded retention."""
    conversation = _console_conversations.pop(session_id, None)
    if conversation is None:
        conversation = []
    _console_conversations[session_id] = conversation
    while len(_console_conversations) > _CONSOLE_SESSIONS_MAX:
        _console_conversations.popitem(last=False)
    return conversation


def _append_console_message(session_id: str, message: Dict[str, str]) -> List[Dict[str, str]]:
    """Append a console message while capping per-session history."""
    conversation = _get_console_conversation(session_id)
    conversation.append(message)
    overflow = len(conversation) - _CONSOLE_MESSAGES_MAX
    if overflow > 0:
        del conversation[:overflow]
    return conversation


@app.post("/highrisk/llm_console_enhanced", response_class=JSONResponse)
async def enhanced_llm_console(
    payload: EnhancedConsoleRequest, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    """
    Enhanced LLM console with tool execution capabilities.

    The LLM can use 11 tools to interact with the system:
    - File operations: read_file, edit_file, list_directory
    - Database: query_database, update_database
    - System: list_users, get_user_detail, search_messages, get_system_status
    - Edit management: confirm_edit, rollback_edit
    """
    cfg = settings()
    if not (cfg.enable_dangerous_tools and cfg.admin_llm_console_enabled):
        raise HTTPException(
            status_code=403, detail="Enhanced LLM console disabled by config"
        )

    # Check for auto-rollbacks
    rolled_back = _console_tools.check_rollbacks()
    if rolled_back:
        live_feed.append(f"Auto-rolled back {len(rolled_back)} edits")

    # Get or create session
    session_id = payload.session_id or f"session_{int(time.time())}"
    conversation = _append_console_message(
        session_id,
        {"role": "user", "content": payload.message},
    )

    # Update tools configuration
    _console_tools.allow_external_files = payload.allow_external_files

    # System prompt with tool definitions
    tools_desc = "\n".join(
        [f"- {name}: {info['description']}" for name, info in TOOL_DEFINITIONS.items()]
    )

    system_prompt = f"""You are an omniscient admin console assistant with full system access.

## Available Tools
{tools_desc}

## How to use tools
Respond with ONLY a JSON object (no markdown, no extra text):
{{"tool": "tool_name", "args": {{"param1": "value1"}}}}

After each tool result you can call another tool or give a plain-text final answer.

## File Editing Workflow
1. First use read_file to see the current content
2. Use edit_file with the exact old_content string and your new_content
3. If edit_file succeeds, use confirm_edit with the returned edit_id to make it permanent
4. If you skip confirm_edit, the edit auto-rolls back after 60 seconds

### edit_file example:
{{"tool": "edit_file", "args": {{"file_path": "app/config.py", "old_content": "debug = False", "new_content": "debug = True"}}}}

### confirm_edit example:
{{"tool": "confirm_edit", "args": {{"edit_id": "edit_abc123"}}}}

## Important
- Be concise and direct
- Use tools proactively -- don't just describe what you would do, actually do it
- Chain multiple tools when needed (read -> edit -> confirm)
- The old_content in edit_file must be an EXACT substring match of the file
- For database changes, use query_database for SELECT and update_database for writes
- Never expose secrets, tokens, or passwords in your responses

Tools enabled: {payload.tools_enabled}
External file access: {payload.allow_external_files}
"""

    messages = [{"role": "system", "content": system_prompt}] + conversation[
        -10:
    ]  # Last 10 messages

    llm_options = {"temperature": 0.2, "num_ctx": payload.num_ctx}

    iterations = 0
    tool_results: list[dict[str, Any]] = []

    async def _run_console_loop() -> Dict[str, Any]:
        """Inner async loop — wrapped by asyncio.wait_for for overall timeout."""
        nonlocal iterations

        client = default_llm_client()

        while iterations < payload.max_iterations:
            iterations += 1

            # ----------------------------------------------------------
            # Async LLM call — does NOT block the event loop
            # ----------------------------------------------------------
            try:
                response = await client.chat_async(
                    messages=messages,
                    model=cfg.chat_model,
                    options=llm_options,
                )
            except Exception as exc:
                _audit(admin, "llm_console_enhanced.error", details={"error": str(exc)})
                raise HTTPException(status_code=502, detail=f"LLM error: {exc}")

            if isinstance(response, dict):
                response_text = (
                    str(response.get("text"))
                    if response.get("text")
                    else str(response.get("content") or response)
                ).strip()
            else:
                response_text = str(response).strip()

            # Check if response is a tool call
            if not payload.tools_enabled:
                _append_console_message(
                    session_id,
                    {"role": "assistant", "content": response_text},
                )
                break

            # Try to parse as JSON tool call
            tool_call = None
            try:
                # Strip markdown code fences if present
                clean = response_text.strip()
                if clean.startswith("```"):
                    lines = clean.split("\n")
                    lines = [l for l in lines if not l.strip().startswith("```")]
                    clean = "\n".join(lines).strip()
                # Look for JSON object in response
                if "{" in clean and "}" in clean:
                    start = clean.index("{")
                    end = clean.rindex("}") + 1
                    json_str = clean[start:end]
                    parsed = json.loads(json_str)
                    if "tool" in parsed:
                        tool_call = parsed
            except (json.JSONDecodeError, ValueError):
                pass

            # If no tool call, this is the final answer
            if not tool_call:
                _append_console_message(
                    session_id,
                    {"role": "assistant", "content": response_text},
                )
                break

            # Execute tool
            tool_name = tool_call.get("tool")
            tool_args = tool_call.get("args", {})

            if tool_name not in TOOL_DEFINITIONS:
                error_msg = f"Unknown tool: {tool_name}"
                messages.append({"role": "assistant", "content": response_text})
                messages.append({"role": "system", "content": f"Error: {error_msg}"})
                continue

            # Execute the tool in a thread so DB/file I/O doesn't block
            try:
                tool_method = getattr(_console_tools, tool_name)
                result = await asyncio.to_thread(tool_method, **tool_args)

                tool_results.append(
                    {
                        "tool": tool_name,
                        "args": tool_args,
                        "success": result.success,
                        "data": result.data,
                        "error": result.error,
                        "metadata": result.metadata,
                    }
                )

                # Build a size-capped tool result string for the LLM context
                raw_result_str = json.dumps(
                    result.data if result.success else result.error, indent=2
                )
                capped = _truncate_tool_result(raw_result_str)
                tool_result_msg = {
                    "role": "system",
                    "content": f"Tool '{tool_name}' result:\n{capped}",
                }
                messages.append({"role": "assistant", "content": response_text})
                messages.append(tool_result_msg)

                # Audit tool execution
                _audit(
                    admin,
                    f"llm_console_tool.{tool_name}",
                    details={
                        "args": tool_args,
                        "success": result.success,
                        "session_id": session_id,
                    },
                )

                if not result.success:
                    continue

            except Exception as exc:
                logger.error(f"Tool execution error: {exc}", exc_info=True)
                error_msg = f"Tool execution failed: {exc}"
                messages.append({"role": "system", "content": error_msg})
                tool_results.append(
                    {
                        "tool": tool_name,
                        "args": tool_args,
                        "success": False,
                        "error": str(exc),
                    }
                )

        # Get final response after all tool executions.
        # If the loop ended on a tool/system message, ask once more for a final
        # plain-language answer so UI does not show "Tool execution completed".
        if messages and messages[-1]["role"] == "assistant":
            final_response = messages[-1]["content"]
        else:
            try:
                final_prompt = messages + [
                    {
                        "role": "system",
                        "content": "Summarize the result for the admin in plain language now.",
                    }
                ]
                final_raw = await client.chat_async(
                    messages=final_prompt,
                    model=cfg.chat_model,
                    options=llm_options,
                )
                if isinstance(final_raw, dict):
                    final_response = (
                        str(final_raw.get("text"))
                        if final_raw.get("text")
                        else str(final_raw)
                    )
                else:
                    final_response = str(final_raw)
            except Exception:
                if tool_results:
                    last_tool = tool_results[-1]
                    final_response = (
                        f"Completed {len(tool_results)} tool call(s). "
                        f"Last tool: {last_tool.get('tool')} "
                        f"({'ok' if last_tool.get('success') else 'failed'})."
                    )
                else:
                    final_response = "No tool call was executed. Please try a more specific request."
            _append_console_message(
                session_id,
                {"role": "assistant", "content": final_response},
            )

        return {
            "status": "ok",
            "session_id": session_id,
            "response": final_response,
            "tool_executions": tool_results,
            "iterations": iterations,
            "conversation_length": len(conversation),
            "rolled_back_edits": rolled_back,
        }

    # ------------------------------------------------------------------
    # Run with an overall timeout so the user never waits forever
    # ------------------------------------------------------------------
    try:
        return await asyncio.wait_for(
            _run_console_loop(),
            timeout=payload.timeout_seconds,
        )
    except asyncio.TimeoutError:
        partial_msg = (
            f"Request timed out after {payload.timeout_seconds}s "
            f"({iterations} iteration(s), {len(tool_results)} tool call(s) completed)."
        )
        _append_console_message(
            session_id,
            {"role": "assistant", "content": partial_msg},
        )
        _audit(
            admin,
            "llm_console_enhanced.timeout",
            details={"session_id": session_id, "iterations": iterations},
        )
        return {
            "status": "timeout",
            "session_id": session_id,
            "response": partial_msg,
            "tool_executions": tool_results,
            "iterations": iterations,
            "conversation_length": len(conversation),
            "rolled_back_edits": rolled_back,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Enhanced console error: {exc}", exc_info=True)
        _audit(
            admin,
            "llm_console_enhanced.error",
            details={"error": str(exc), "session_id": session_id},
        )
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/highrisk/llm_console_sessions", response_class=JSONResponse)
async def list_console_sessions(_: str = Depends(_require_admin)) -> Dict[str, Any]:
    """List active console sessions"""
    sessions = []
    for session_id, messages in _console_conversations.items():
        sessions.append(
            {
                "session_id": session_id,
                "message_count": len(messages),
                "last_message": messages[-1]["content"][:100] if messages else None,
            }
        )
    return {"sessions": sessions}


@app.post("/highrisk/llm_console_clear", response_class=JSONResponse)
async def clear_console_session(
    session_id: Optional[str] = None, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    """Clear conversation history for a session or all sessions"""
    if session_id:
        if session_id in _console_conversations:
            del _console_conversations[session_id]
            return {"status": "cleared", "session_id": session_id}
        return {"status": "not_found", "session_id": session_id}
    else:
        count = len(_console_conversations)
        _console_conversations.clear()
        return {"status": "cleared_all", "count": count}


# ============================================================================
# LLM DEFAULTS ENDPOINTS
# ============================================================================


_LLM_DEFAULTS_PATH = Path(
    getattr(settings(), "data_root", ".") or "."
) / "llm_defaults.json"


def _load_llm_defaults_file() -> Dict[str, Any]:
    try:
        if _LLM_DEFAULTS_PATH.exists():
            return json.loads(_LLM_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed reading LLM defaults file: %s", exc)
    return {"standard": {}, "downbad": {}}


def _save_llm_defaults_file(data: Dict[str, Any]) -> None:
    _LLM_DEFAULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LLM_DEFAULTS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


@app.get("/admin/llm-defaults", response_class=JSONResponse)
async def get_llm_defaults(admin: str = Depends(_require_admin)) -> Dict[str, Any]:
    """Get admin-configured LLM defaults for standard and downbad personalities."""
    return _load_llm_defaults_file()


class LLMDefaultsPayload(BaseModel):
    standard: Optional[Dict[str, Any]] = None
    downbad: Optional[Dict[str, Any]] = None


@app.post("/admin/llm-defaults", response_class=JSONResponse)
async def save_llm_defaults(
    payload: LLMDefaultsPayload, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    """Save admin-configured LLM defaults."""
    from app.domain.conversation.pipeline import LLM_PARAM_RANGES

    current = _load_llm_defaults_file()
    saved = {}
    for group in ("standard", "downbad"):
        data = getattr(payload, group, None)
        if data is None:
            continue
        clean: Dict[str, Any] = {}
        for param, val in data.items():
            if param in LLM_PARAM_RANGES and val is not None:
                try:
                    fv = float(val)
                    lo, hi = LLM_PARAM_RANGES[param]
                    clean[param] = max(lo, min(hi, fv))
                except (TypeError, ValueError):
                    continue
        current[group] = clean
        saved[group] = clean

    _save_llm_defaults_file(current)
    _audit(admin, "admin.save_llm_defaults", details=saved)
    return {"status": "ok", "saved": saved}


# ============================================================================
# MEDIA GENERATION ENDPOINTS
# ============================================================================


class GenerateImageRequest(BaseModel):
    prompt: str
    user_id: Optional[int] = None
    model: str = "flux2-klein"
    negative_prompt: Optional[str] = None
    width: int = 1024
    height: int = 1024
    steps: int = 30
    guidance_scale: float = 7.5
    seed: Optional[int] = None
    num_frames: Optional[int] = None
    fps: Optional[int] = None
    epoch: Optional[int] = None
    # ── Local SDXL extras ──────────────────────────────────────────────────────
    # Pony source tags, e.g. ["source_anime", "source_pony"]
    source_tags: Optional[List[str]] = None
    # LoRAs: [{"name": "char.safetensors", "family": "pony", "weight": 0.8}]
    loras: Optional[List[Dict[str, Any]]] = None
    animated: bool = False
    hires_upscale: Optional[bool] = None


_MEDIA_JOBS: Dict[str, Dict[str, Any]] = {}
_MEDIA_JOBS_MAX = 500          # hard cap on in-memory job records
_MEDIA_JOBS_TTL_SECONDS = 3600  # evict finished jobs older than 1 hour


def _evict_old_media_jobs() -> None:
    """Remove finished jobs older than TTL and enforce the hard cap.

    Called before every new job insertion so the dict never grows unboundedly.
    Entries without a ``finished_at`` timestamp are treated as still running
    and are only pruned when the hard cap is hit (oldest first).
    """
    if not _MEDIA_JOBS:
        return
    cutoff = time.time() - _MEDIA_JOBS_TTL_SECONDS
    to_delete = [
        job_id
        for job_id, info in list(_MEDIA_JOBS.items())
        if info.get("finished_at") and _iso_to_ts(info["finished_at"]) < cutoff
    ]
    for job_id in to_delete:
        _MEDIA_JOBS.pop(job_id, None)
    # Hard-cap: if still over limit, evict oldest by started_at ascending
    if len(_MEDIA_JOBS) >= _MEDIA_JOBS_MAX:
        overflow = len(_MEDIA_JOBS) - _MEDIA_JOBS_MAX + 1  # remove enough for 1 new slot
        sorted_ids = sorted(
            _MEDIA_JOBS,
            key=lambda k: _iso_to_ts(_MEDIA_JOBS[k].get("started_at") or ""),
        )
        for job_id in sorted_ids[:overflow]:
            _MEDIA_JOBS.pop(job_id, None)


def _iso_to_ts(iso_str: str) -> float:
    """Parse an ISO-8601 string to a Unix timestamp; return 0.0 on failure."""
    if not iso_str:
        return 0.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _resolve_media_owner_user_id(admin: str, requested_user_id: Optional[int]) -> int:
    if requested_user_id is not None and int(requested_user_id) > 0:
        return int(requested_user_id)

    cfg = settings()
    candidates: list[str] = []
    if admin and admin != "trusted-cookie":
        candidates.append(admin)
    admin_username = getattr(cfg, "admin_username", None)
    if admin_username and admin_username not in candidates:
        candidates.append(admin_username)

    with db_ro() as conn:
        for candidate in candidates:
            row = conn.execute(
                """
                SELECT id
                FROM users
                WHERE LOWER(COALESCE(telegram_username, '')) = LOWER(?)
                   OR LOWER(COALESCE(display_name, '')) = LOWER(?)
                ORDER BY last_active_at DESC, id ASC
                LIMIT 1
                """,
                (candidate, candidate),
            ).fetchone()
            if row:
                return int(row["id"])

    raise HTTPException(
        status_code=400,
        detail="No media owner selected and no admin-linked user record was found.",
    )


@app.post("/media/generate", response_class=JSONResponse)
async def generate_image(
    payload: GenerateImageRequest, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    """Kick off image generation as a background task and return a job_id."""
    effective_user_id = _resolve_media_owner_user_id(admin, payload.user_id)
    job_id = f"media_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    model_info = MediaGenerationService.SUPPORTED_MODELS.get(payload.model or "flux2-klein", {})
    est_time = model_info.get("generation_time_s", 45)

    _evict_old_media_jobs()
    _MEDIA_JOBS[job_id] = {
        "status": "starting",
        "progress": 0,
        "detail": "Initializing...",
        "est_seconds": est_time,
        "result": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    async def _run() -> None:
        job = _MEDIA_JOBS[job_id]
        is_video = model_info.get("media_type") == "video"
        try:
            job.update(status="loading_model", progress=10, detail="Loading model...")
            service = get_media_service()
            media_label = "video" if is_video else "image"

            # For image models, apply per-model optimised defaults so that the admin
            # panel doesn't inadvertently run SDXL/z-image at 1024×1024 / 30 steps
            # when the model was tuned for 768×768 / 10-18 steps.  Explicit non-default
            # values supplied by the caller always win.
            _IMG_DEFAULT_WIDTH = 1024
            _IMG_DEFAULT_HEIGHT = 1024
            _IMG_DEFAULT_STEPS = 30
            _IMG_DEFAULT_GUIDANCE = 7.5
            if not is_video:
                img_defaults = service.get_image_defaults(payload.model)
                eff_width = (
                    img_defaults["width"]
                    if payload.width == _IMG_DEFAULT_WIDTH
                    else payload.width
                )
                eff_height = (
                    img_defaults["height"]
                    if payload.height == _IMG_DEFAULT_HEIGHT
                    else payload.height
                )
                eff_steps = (
                    img_defaults["num_inference_steps"]
                    if payload.steps == _IMG_DEFAULT_STEPS
                    else payload.steps
                )
                eff_guidance = (
                    img_defaults["guidance_scale"]
                    if payload.guidance_scale == _IMG_DEFAULT_GUIDANCE
                    else payload.guidance_scale
                )
            else:
                eff_width = payload.width
                eff_height = payload.height
                eff_steps = payload.steps
                eff_guidance = payload.guidance_scale

            try:
                job.update(status="generating", progress=30,
                           detail=f"Generating {media_label}...")
                if is_video:
                    result = await asyncio.to_thread(
                        service.generate_video,
                        prompt=payload.prompt,
                        user_id=effective_user_id,
                        model_key=payload.model,
                        negative_prompt=payload.negative_prompt,
                        width=eff_width,
                        height=eff_height,
                        num_frames=payload.num_frames or model_info.get("default_frames", 33),
                        num_inference_steps=eff_steps,
                        guidance_scale=eff_guidance,
                        fps=payload.fps or model_info.get("default_fps", 16),
                        seed=payload.seed,
                        epoch=payload.epoch,
                    )
                else:
                    result = await asyncio.to_thread(
                        service.generate_image,
                        prompt=payload.prompt,
                        user_id=effective_user_id,
                        model_key=payload.model,
                        negative_prompt=payload.negative_prompt,
                        width=eff_width,
                        height=eff_height,
                        num_inference_steps=eff_steps,
                        guidance_scale=eff_guidance,
                        seed=payload.seed,
                        source_tags=payload.source_tags,
                        loras=payload.loras,
                        animated=payload.animated,
                        hires_upscale=payload.hires_upscale,
                    )
            except sqlite3.OperationalError as exc:
                if "no such table: generated_media" not in str(exc).lower():
                    raise
                ensure_schema_current(force=True)
                if is_video:
                    result = await asyncio.to_thread(
                        service.generate_video,
                        prompt=payload.prompt,
                        user_id=effective_user_id,
                        model_key=payload.model,
                        negative_prompt=payload.negative_prompt,
                        width=eff_width,
                        height=eff_height,
                        num_frames=payload.num_frames or model_info.get("default_frames", 33),
                        num_inference_steps=eff_steps,
                        guidance_scale=eff_guidance,
                        fps=payload.fps or model_info.get("default_fps", 16),
                        seed=payload.seed,
                        epoch=payload.epoch,
                    )
                else:
                    result = await asyncio.to_thread(
                        service.generate_image,
                        prompt=payload.prompt,
                        user_id=effective_user_id,
                        model_key=payload.model,
                        negative_prompt=payload.negative_prompt,
                        width=eff_width,
                        height=eff_height,
                        num_inference_steps=eff_steps,
                        guidance_scale=eff_guidance,
                        seed=payload.seed,
                        source_tags=payload.source_tags,
                        loras=payload.loras,
                        animated=payload.animated,
                        hires_upscale=payload.hires_upscale,
                    )
            job["result"] = result
            if result.get("status") == "success":
                gen_ms = result.get("generation_time_ms", 0)
                job.update(status="completed", progress=100,
                           detail=f"Done in {gen_ms / 1000:.1f}s")
            else:
                job.update(status="error", progress=100,
                           detail=result.get("error", "Generation failed"))
        except Exception as exc:
            job.update(status="error", progress=100, detail=f"Error: {exc}")
        finally:
            job["finished_at"] = datetime.now(timezone.utc).isoformat()

    _task = asyncio.create_task(_run())
    _background_tasks.add(_task)
    _task.add_done_callback(_background_tasks.discard)
    media_type = model_info.get("media_type", "image")
    _audit(
        admin,
        f"media.generate_{media_type}",
        details={"user_id": effective_user_id, "model": payload.model, "job_id": job_id},
    )
    return {
        "job_id": job_id,
        "est_seconds": est_time,
        "status": "started",
        "user_id": effective_user_id,
    }


@app.get("/media/generate/{job_id}/status", response_class=JSONResponse)
async def media_job_status(
    job_id: str, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    """Poll the status of an image generation job."""
    job = _MEDIA_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job["progress"],
        "detail": job["detail"],
        "est_seconds": job.get("est_seconds"),
        "result": job.get("result"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


@app.get("/media/history", response_class=JSONResponse)
async def get_media_history(
    user_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Get media generation history"""
    try:
        service = get_media_service()
        try:
            history = service.get_generation_history(
                user_id=user_id, limit=limit, offset=offset
            )
        except sqlite3.OperationalError as exc:
            if "no such table: generated_media" not in str(exc).lower():
                raise
            ensure_schema_current(force=True)
            history = service.get_generation_history(
                user_id=user_id, limit=limit, offset=offset
            )
        return {"history": history, "count": len(history)}
    except Exception as exc:
        logger.error(f"Get history error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/media/models", response_class=JSONResponse)
async def list_models(_: str = Depends(_require_admin)) -> Dict[str, Any]:
    """List available AI models"""
    try:
        service = get_media_service()
        models = service.get_available_models()
        return {"models": models}
    except Exception as exc:
        logger.error(f"List models error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/media/image/{image_id}")
async def serve_generated_image(image_id: int, _admin: str = Depends(_require_admin)):
    """Serve a generated image file"""
    try:
        from app.infra.db.session import db_rw

        with db_rw() as conn:
            try:
                row = conn.execute(
                    "SELECT file_path, media_type FROM generated_media WHERE id = ? AND status = 'completed'",
                    (image_id,),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table: generated_media" in str(exc).lower():
                    ensure_schema_current(force=True)
                    row = conn.execute(
                        "SELECT file_path, media_type FROM generated_media WHERE id = ? AND status = 'completed'",
                        (image_id,),
                    ).fetchone()
                else:
                    raise

        if not row:
            raise HTTPException(
                status_code=404, detail="Image not found or not completed"
            )

        file_path = Path(row[0])
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Image file not found on disk")

        # Determine media type
        suffix = file_path.suffix.lower()
        if suffix in (".jpg", ".jpeg"):
            media_type = "image/jpeg"
        elif suffix == ".webp":
            media_type = "image/webp"
        elif suffix == ".mp4":
            media_type = "video/mp4"
        elif suffix == ".gif":
            media_type = "image/gif"
        else:
            media_type = "image/png"

        return FileResponse(file_path, media_type=media_type)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Serve image error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/media/unload", response_class=JSONResponse)
async def unload_model(admin: str = Depends(_require_admin)) -> Dict[str, Any]:
    """Unload current model to free VRAM"""
    try:
        service = get_media_service()
        service.unload_model()
        _audit(admin, "media.unload_model", details={})
        return {"status": "ok", "message": "Model unloaded, VRAM freed"}
    except Exception as exc:
        logger.error(f"Unload model error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/media/loras", response_class=JSONResponse)
async def list_loras(
    model_key: str = "pony-xl-v6",
    _: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """List downloaded LoRA files available for a given model, grouped by family dir."""
    try:
        service = get_media_service()
        loras = service.list_loras(model_key)
        return {"model_key": model_key, "loras": loras}
    except Exception as exc:
        logger.error("list_loras error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


class LoraDownloadRequest(BaseModel):
    url: str
    family: str  # must be one of: sdxl, pony, illustrious
    filename: str  # e.g. "my_character.safetensors"


_ALLOWED_LORA_FAMILIES: frozenset[str] = frozenset({"sdxl", "pony", "illustrious"})


@app.post("/media/loras/download", response_class=JSONResponse)
async def download_lora(
    payload: LoraDownloadRequest,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Download a LoRA safetensors file from a URL into the correct family directory.

    Security guardrails:
     - Family must be one of the known allowed values (no path traversal).
     - Filename must end in .safetensors and must not contain path separators.
     - URL scheme must be http or https.
    """
    from app.services.media_generation_service import LORA_BASE_DIR

    if payload.family not in _ALLOWED_LORA_FAMILIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid family '{payload.family}'. Allowed: {sorted(_ALLOWED_LORA_FAMILIES)}",
        )

    safe_filename = Path(payload.filename).name  # strips any directory component
    if not safe_filename.lower().endswith(".safetensors"):
        raise HTTPException(status_code=400, detail="Filename must end with .safetensors")

    parsed_url = urlparse.urlparse(payload.url)
    if parsed_url.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="URL must use http or https")

    dest_dir = LORA_BASE_DIR / payload.family
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_filename

    if dest_path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"File already exists: {safe_filename}. Delete it first if you want to replace it.",
        )

    async def _do_download() -> None:
        req = urlrequest.Request(payload.url, headers={"User-Agent": "wellness-bot/1.0"})
        with urlrequest.urlopen(req, timeout=300) as resp:  # noqa: S310
            data = resp.read()
        dest_path.write_bytes(data)

    try:
        await _do_download()
    except Exception as exc:
        logger.error("LoRA download failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Download failed: {exc}")

    file_size = dest_path.stat().st_size
    _audit(
        admin,
        "media.lora_download",
        details={"family": payload.family, "filename": safe_filename, "url": payload.url},
    )
    return {
        "status": "ok",
        "family": payload.family,
        "filename": safe_filename,
        "path": str(dest_path),
        "size_bytes": file_size,
    }


@app.delete("/media/loras/{family}/{filename}", response_class=JSONResponse)
async def delete_lora(
    family: str,
    filename: str,
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Delete a downloaded LoRA file.  Path traversal is blocked."""
    from app.services.media_generation_service import LORA_BASE_DIR

    if family not in _ALLOWED_LORA_FAMILIES:
        raise HTTPException(status_code=400, detail=f"Invalid family: {family}")

    safe_filename = Path(filename).name
    if not safe_filename.lower().endswith(".safetensors"):
        raise HTTPException(status_code=400, detail="Filename must end with .safetensors")

    lora_path = LORA_BASE_DIR / family / safe_filename
    if not lora_path.exists():
        raise HTTPException(status_code=404, detail=f"LoRA not found: {safe_filename}")

    lora_path.unlink()
    _audit(
        admin,
        "media.lora_delete",
        details={"family": family, "filename": safe_filename},
    )
    return {"status": "deleted", "family": family, "filename": safe_filename}


@app.get("/media/vram", response_class=JSONResponse)
async def get_vram_usage(_: str = Depends(_require_admin)) -> Dict[str, Any]:
    """Get current VRAM usage"""
    try:
        service = get_media_service()
        return service.get_vram_usage()
    except Exception as exc:
        logger.error(f"Get VRAM error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/live/stream")
async def live_stream(admin: str = Depends(_require_admin)):
    return StreamingResponse(live_feed.stream(), media_type="text/event-stream")


@app.get("/metrics/user_analytics", response_class=JSONResponse)
async def user_analytics(
    user_id: Optional[int] = None, admin: str = Depends(_require_admin)
) -> Dict[str, Any]:
    """Aggregated user analytics for the User Analytics subtab."""
    data: Dict[str, Any] = {}
    now = datetime.utcnow()
    try:
        with db_ro() as conn:
            if user_id is not None:
                user_row = conn.execute(
                    """
                    SELECT id,
                           COALESCE(NULLIF(display_name, ''), NULLIF(telegram_username, ''), ('user_' || id)) AS user_name,
                           last_active_at,
                           created_at
                    FROM users WHERE id = ?
                    """,
                    (user_id,),
                ).fetchone()
                if not user_row:
                    raise HTTPException(status_code=404, detail="User not found")

                data["mode"] = "single_user"
                data["user"] = dict(user_row)
                data["messages_total"] = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0]
                data["messages_24h"] = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE user_id = ? AND timestamp >= ?",
                    (user_id, (now - timedelta(hours=24)).isoformat()),
                ).fetchone()[0]
                data["messages_7d"] = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE user_id = ? AND timestamp >= ?",
                    (user_id, (now - timedelta(days=7)).isoformat()),
                ).fetchone()[0]
                data["assistant_messages"] = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'assistant'",
                    (user_id,),
                ).fetchone()[0]
                data["user_messages"] = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'user'",
                    (user_id,),
                ).fetchone()[0]
                day_rows = conn.execute(
                    """
                    SELECT DATE(timestamp) AS date, COUNT(*) AS count
                    FROM messages
                    WHERE user_id = ? AND timestamp >= ?
                    GROUP BY date
                    ORDER BY date
                    """,
                    (user_id, (now - timedelta(days=14)).isoformat()),
                ).fetchall()
                data["messages_by_day"] = [
                    {"date": r["date"], "count": r["count"]} for r in day_rows
                ]
                sentiment_rows = conn.execute(
                    """
                    SELECT DATE(m.timestamp) AS date,
                           AVG(s.valence) AS avg_valence,
                           AVG(s.arousal) AS avg_arousal,
                           AVG(s.dominance) AS avg_dominance,
                           COUNT(*) AS sample_count
                    FROM sentiments s
                    JOIN messages m ON m.id = s.message_id
                    WHERE m.user_id = ? AND COALESCE(m.scope, 'standard') = 'standard' AND m.timestamp >= ?
                    GROUP BY date
                    ORDER BY date
                    """,
                    (user_id, (now - timedelta(days=14)).isoformat()),
                ).fetchall()
                data["sentiment_by_day"] = [dict(r) for r in sentiment_rows]
                data["latest_messages"] = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT role, content, timestamp
                        FROM messages
                        WHERE user_id = ?
                        ORDER BY timestamp DESC
                        LIMIT 20
                        """,
                        (user_id,),
                    ).fetchall()
                ]
                return data

            data["mode"] = "global"
            # Total users
            data["total_users"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

            # Active users by time window
            for label, hours in [("active_24h", 24), ("active_7d", 168), ("active_30d", 720)]:
                cutoff = (now - timedelta(hours=hours)).isoformat()
                data[label] = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE last_active_at >= ?", (cutoff,)
                ).fetchone()[0]

            # Onboarded users
            data["onboarded"] = conn.execute(
                "SELECT COUNT(*) FROM users WHERE onboarding_completed = 1"
            ).fetchone()[0]

            # Average messages per user
            row = conn.execute(
                "SELECT AVG(cnt) FROM (SELECT COUNT(*) AS cnt FROM messages GROUP BY user_id)"
            ).fetchone()
            data["avg_messages_per_user"] = round(row[0] or 0, 1)

            # Top users by message count
            top_rows = conn.execute(
                """SELECT user_id, COUNT(*) AS message_count
                   FROM messages GROUP BY user_id ORDER BY message_count DESC LIMIT 10"""
            ).fetchall()
            data["top_users_by_messages"] = [
                {"user_id": r["user_id"], "message_count": r["message_count"]} for r in top_rows
            ]

            # New users by day (last 30 days)
            cutoff_30d = (now - timedelta(days=30)).isoformat()
            day_rows = conn.execute(
                """SELECT DATE(created_at) AS date, COUNT(*) AS count
                   FROM users WHERE created_at >= ?
                   GROUP BY date ORDER BY date""",
                (cutoff_30d,),
            ).fetchall()
            data["new_users_by_day"] = [
                {"date": r["date"], "count": r["count"]} for r in day_rows
            ]

            # Retention / engagement breakdown
            retention: Dict[str, int] = {}
            retention["daily_active"] = data.get("active_24h", 0)
            cutoff_7d = (now - timedelta(hours=168)).isoformat()
            cutoff_24h = (now - timedelta(hours=24)).isoformat()
            cutoff_30d_ts = (now - timedelta(days=30)).isoformat()
            retention["weekly_only"] = conn.execute(
                "SELECT COUNT(*) FROM users WHERE last_active_at >= ? AND last_active_at < ?",
                (cutoff_7d, cutoff_24h),
            ).fetchone()[0]
            retention["monthly_only"] = conn.execute(
                "SELECT COUNT(*) FROM users WHERE last_active_at >= ? AND last_active_at < ?",
                (cutoff_30d_ts, cutoff_7d),
            ).fetchone()[0]
            retention["dormant"] = conn.execute(
                "SELECT COUNT(*) FROM users WHERE last_active_at < ?",
                (cutoff_30d_ts,),
            ).fetchone()[0]
            data["retention"] = retention

            # Mood distribution (from mood_journal)
            try:
                mood_rows = conn.execute(
                    """SELECT mood_label, COUNT(*) AS count
                       FROM mood_journal WHERE mood_label IS NOT NULL
                       GROUP BY mood_label ORDER BY count DESC LIMIT 10"""
                ).fetchall()
                data["mood_distribution"] = [
                    {"mood_label": r["mood_label"], "count": r["count"]} for r in mood_rows
                ]
            except Exception:
                data["mood_distribution"] = []
    except Exception as exc:
        data["error"] = str(exc)
    return data


@app.get("/metrics/graph_data", response_class=JSONResponse)
async def graph_data(
    user_id: Optional[int] = None,
    days: str = "14",
    admin: str = Depends(_require_admin),
) -> Dict[str, Any]:
    """Chart-ready data for the Graphs subtab."""
    if days.strip().lower() == "all":
        days_int = 0  # sentinel for all time
        cutoff = "1970-01-01T00:00:00"
    else:
        days_int = max(1, min(int(days), 3650))
        cutoff = (datetime.utcnow() - timedelta(days=days_int)).isoformat()
    result: Dict[str, Any] = {"days": days_int or "all", "user_id": user_id}

    try:
        with db_ro() as conn:
            uid_clause = "AND m.user_id = ?" if user_id else ""
            uid_params: tuple = (user_id,) if user_id else ()

            # 1) Messages per day (line chart)
            rows = conn.execute(
                f"""
                SELECT DATE(m.timestamp) AS date, COUNT(*) AS count
                FROM messages m
                WHERE m.timestamp >= ? {uid_clause}
                GROUP BY date ORDER BY date
                """,
                (cutoff, *uid_params),
            ).fetchall()
            result["messages_per_day"] = [{"date": r["date"], "count": r["count"]} for r in rows]

            # 2) Messages per hour-of-day (bar chart — rolling 24hr clock)
            rows = conn.execute(
                f"""
                SELECT CAST(strftime('%H', m.timestamp) AS INTEGER) AS hour, COUNT(*) AS count
                FROM messages m
                WHERE m.timestamp >= ? {uid_clause}
                GROUP BY hour ORDER BY hour
                """,
                (cutoff, *uid_params),
            ).fetchall()
            hourly = {r["hour"]: r["count"] for r in rows}
            result["messages_by_hour"] = [{"hour": h, "count": hourly.get(h, 0)} for h in range(24)]

            # 3) Sentiment over time (line chart — valence, arousal, dominance)
            rows = conn.execute(
                f"""
                SELECT DATE(m.timestamp) AS date,
                       AVG(s.valence) AS avg_valence,
                       AVG(s.arousal) AS avg_arousal,
                       AVG(s.dominance) AS avg_dominance,
                       COUNT(*) AS sample_count
                FROM sentiments s
                JOIN messages m ON m.id = s.message_id
                WHERE m.timestamp >= ? AND COALESCE(m.scope, 'standard') = 'standard' {uid_clause}
                GROUP BY date ORDER BY date
                """,
                (cutoff, *uid_params),
            ).fetchall()
            result["sentiment_over_time"] = [
                {
                    "date": r["date"],
                    "valence": round(r["avg_valence"] or 0, 3),
                    "arousal": round(r["avg_arousal"] or 0, 3),
                    "dominance": round(r["avg_dominance"] or 0, 3),
                    "samples": r["sample_count"],
                }
                for r in rows
            ]

            # 4) Role distribution (pie chart)
            rows = conn.execute(
                f"""
                SELECT m.role, COUNT(*) AS count
                FROM messages m
                WHERE m.timestamp >= ? {uid_clause}
                GROUP BY m.role
                """,
                (cutoff, *uid_params),
            ).fetchall()
            result["role_distribution"] = [{"role": r["role"], "count": r["count"]} for r in rows]

            # 5) Word cloud (top 80 words from user messages)
            rows = conn.execute(
                f"""
                SELECT m.content
                FROM messages m
                WHERE m.role = 'user' AND m.timestamp >= ? {uid_clause}
                ORDER BY m.timestamp DESC
                LIMIT 500
                """,
                (cutoff, *uid_params),
            ).fetchall()
            word_freq: Dict[str, int] = {}
            stop_words = {
                "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
                "of", "with", "by", "from", "is", "it", "this", "that", "was", "are",
                "be", "have", "has", "had", "do", "does", "did", "will", "would",
                "could", "should", "may", "might", "can", "not", "no", "so", "if",
                "my", "me", "i", "you", "your", "we", "he", "she", "they", "them",
                "its", "been", "being", "just", "about", "like", "what", "when",
                "how", "who", "where", "which", "there", "here", "than", "then",
                "also", "very", "really", "much", "more", "some", "any", "all",
                "im", "dont", "ive", "thats", "youre", "ill", "cant", "wont",
                "ok", "oh", "yeah", "yes", "yep", "hey", "hi", "hello",
                "get", "got", "go", "going", "went", "know", "think", "want",
                "need", "feel", "feeling", "one", "up", "out", "as", "am",
            }
            for row in rows:
                text = (row["content"] or "").lower()
                text = re.sub(r"[^a-z\s']", " ", text)
                for word in text.split():
                    word = word.strip("' ")
                    if len(word) >= 3 and word not in stop_words:
                        word_freq[word] = word_freq.get(word, 0) + 1
            top_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:80]
            result["word_cloud"] = [{"word": w, "count": c} for w, c in top_words]

            # 6) Messages by day-of-week (bar chart)
            rows = conn.execute(
                f"""
                SELECT CAST(strftime('%w', m.timestamp) AS INTEGER) AS dow, COUNT(*) AS count
                FROM messages m
                WHERE m.timestamp >= ? {uid_clause}
                GROUP BY dow ORDER BY dow
                """,
                (cutoff, *uid_params),
            ).fetchall()
            dow_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
            dow_map = {r["dow"]: r["count"] for r in rows}
            result["messages_by_dow"] = [
                {"day": dow_names[d], "count": dow_map.get(d, 0)} for d in range(7)
            ]

            # 7) Average message length over time (line chart)
            rows = conn.execute(
                f"""
                SELECT DATE(m.timestamp) AS date,
                       AVG(LENGTH(m.content)) AS avg_len,
                       AVG(CASE WHEN m.role = 'user' THEN LENGTH(m.content) END) AS avg_user_len,
                       AVG(CASE WHEN m.role = 'assistant' THEN LENGTH(m.content) END) AS avg_bot_len
                FROM messages m
                WHERE m.timestamp >= ? {uid_clause}
                GROUP BY date ORDER BY date
                """,
                (cutoff, *uid_params),
            ).fetchall()
            result["avg_msg_length"] = [
                {
                    "date": r["date"],
                    "avg_len": round(r["avg_len"] or 0),
                    "avg_user_len": round(r["avg_user_len"] or 0),
                    "avg_bot_len": round(r["avg_bot_len"] or 0),
                }
                for r in rows
            ]

            # 8) Mood journal entries over time (if table exists)
            try:
                rows = conn.execute(
                    f"""
                    SELECT DATE(mj.created_at) AS date, mj.mood_label, COUNT(*) AS count
                    FROM mood_journal mj
                    {'JOIN users u ON u.id = mj.user_id WHERE mj.created_at >= ? AND u.id = ?' if user_id else 'WHERE mj.created_at >= ?'}
                    GROUP BY date, mj.mood_label ORDER BY date
                    """,
                    (cutoff, user_id) if user_id else (cutoff,),
                ).fetchall()
                result["mood_timeline"] = [
                    {"date": r["date"], "mood": r["mood_label"], "count": r["count"]} for r in rows
                ]
            except Exception:
                result["mood_timeline"] = []

    except Exception as exc:
        result["error"] = str(exc)
    return result


@app.get("/", response_class=HTMLResponse)
async def landing_page(
    request: Request, admin: str = Depends(_require_admin)
) -> HTMLResponse:
    live_feed.append("landing page loaded")
    cfg = settings()
    dangerous_enabled = "true" if cfg.enable_dangerous_tools else "false"

    # Serve the new extracted frontend if available, else fall back to inline
    html_path = _STATIC_DIR / "admin.html"
    if html_path.is_file():
        template = html_path.read_text(encoding="utf-8")
        html = template.replace("{{VERSION}}", app.version).replace(
            "{{DANGEROUS_ENABLED}}", dangerous_enabled
        )
        return HTMLResponse(content=html)

    # ---- Legacy inline HTML fallback (kept for safety) ----
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Wellness Admin</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }}
    header {{ background: #111827; padding: 16px; display: flex; justify-content: space-between; align-items: center; }}
    .pill {{ padding: 6px 12px; border-radius: 999px; background: #1f2937; font-size: 12px; }}

    /* Tab Navigation */
    .tab-nav {{
      background: #111827;
      border-bottom: 2px solid #1f2937;
      position: sticky;
      top: 0;
      z-index: 100;
      display: flex;
      overflow-x: auto;
      padding: 0 16px;
    }}
    .tab-btn {{
      background: transparent;
      color: #94a3b8;
      border: none;
      padding: 14px 20px;
      cursor: pointer;
      font-size: 14px;
      font-weight: 500;
      border-bottom: 3px solid transparent;
      white-space: nowrap;
      transition: all 0.2s;
    }}
    .tab-btn:hover {{
      color: #e2e8f0;
      background: rgba(37, 99, 235, 0.1);
    }}
    .tab-btn.active {{
      color: #93c5fd;
      border-bottom-color: #2563eb;
      background: rgba(37, 99, 235, 0.15);
    }}

    /* Tab Content */
    .tab-content {{
      display: none;
      padding: 16px;
      animation: fadeIn 0.3s;
    }}
    .tab-content.active {{
      display: block;
    }}
    @keyframes fadeIn {{
      from {{ opacity: 0; }}
      to {{ opacity: 1; }}
    }}

    /* Grid Layouts */
    .grid-2col {{ display: grid; grid-template-columns: 2fr 1fr; gap: 16px; }}
    .grid-3col {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }}
    .grid-auto {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}

    section {{ background: #111827; border: 1px solid #1f2937; border-radius: 8px; padding: 12px; }}
    h2 {{ margin: 0 0 8px 0; font-size: 16px; color: #93c5fd; }}
    h3 {{ margin: 8px 0 6px 0; font-size: 14px; color: #93c5fd; }}
    button {{ background: #2563eb; color: #fff; border: none; padding: 8px 12px; border-radius: 6px; cursor: pointer; }}
    button.danger {{ background: #ef4444; }}
    button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; }}
    .card {{ background: #0b1222; padding: 10px; border-radius: 6px; border: 1px solid #1f2937; }}
    .feed-box {{ height: 240px; overflow-y: auto; background: #0b1222; padding: 8px; border-radius: 6px; font-family: monospace; font-size: 12px; }}
    label {{ font-size: 13px; }}
    input, textarea, select {{ width: 100%; padding: 6px; background: #0b1222; border: 1px solid #1f2937; color: #e2e8f0; border-radius: 6px; box-sizing: border-box; }}
    input[type="checkbox"] {{ width: auto; padding: 0; }}
    .user-row-label {{ display:flex; align-items:flex-start; gap:8px; justify-content:flex-start; }}
    .image-grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap:10px; }}
    pre {{ white-space: pre-wrap; background: #0b1222; padding: 8px; border-radius: 6px; min-height: 80px; margin: 0; }}
    .pagination {{ display: flex; gap: 8px; margin-top: 8px; }}
    /* Toast Notifications */
    #toast-container {{
      position: fixed;
      top: 20px;
      right: 20px;
      z-index: 10000;
      display: flex;
      flex-direction: column;
      gap: 10px;
      max-width: 400px;
    }}

    .toast {{
      padding: 16px 20px;
      border-radius: 8px;
      color: white;
      font-size: 14px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
      animation: slideIn 0.3s ease-out;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    .toast.success {{ background: #10b981; }}
    .toast.error {{ background: #ef4444; }}
    .toast.warning {{ background: #f59e0b; }}
    .toast.info {{ background: #3b82f6; }}

    @keyframes slideIn {{
      from {{ transform: translateX(400px); opacity: 0; }}
      to {{ transform: translateX(0); opacity: 1; }}
    }}

    @keyframes slideOut {{
      from {{ transform: translateX(0); opacity: 1; }}
      to {{ transform: translateX(400px); opacity: 0; }}
    }}

    .toast.removing {{
      animation: slideOut 0.3s ease-out forwards;
    }}
  </style>
</head>
<body>
  <div id="toast-container"></div>
  <header>
    <div><strong>Wellness Admin</strong> <span class="pill">v{app.version}</span></div>
    <div style="display:flex; gap:12px; align-items:center;">
      <label><input type="checkbox" id="danger-toggle" /> Enable dangerous tools</label>
      <form id="trust-form" style="display:flex; gap:6px; align-items:center;">
        <input type="text" id="trust-token" placeholder="Trust token" />
        <button type="submit">Trust this device</button>
      </form>
    </div>
  </header>

  <!-- Tab Navigation -->
  <nav class="tab-nav">
    <button class="tab-btn active" data-tab="dashboard">Dashboard</button>
    <button class="tab-btn" data-tab="users">Users</button>
    <button class="tab-btn" data-tab="moderation">Moderation</button>
    <button class="tab-btn" data-tab="analytics">Analytics</button>
    <button class="tab-btn" data-tab="psych">Psych Profile</button>
    <button class="tab-btn" data-tab="crisis">Crisis Alerts</button>
    <button class="tab-btn" data-tab="system">System</button>
    <button class="tab-btn" data-tab="tools">Tools</button>
    <button class="tab-btn" data-tab="highrisk">High-Risk</button>
    <button class="tab-btn" data-tab="media">Media</button>
    <button class="tab-btn" data-tab="feedback">Bugs/Feedback</button>
    <button class="tab-btn" data-tab="misc">Misc</button>
  </nav>

  <!-- Tab: Dashboard -->
  <div class="tab-content active" id="tab-dashboard">
    <div class="grid-2col">
      <section>
        <h2>Status</h2>
        <div class="grid" id="status-cards"></div>
      </section>
      <section>
        <h2>Controls</h2>
      <div class="grid">
        <button id="btn-restart" class="danger">Restart Bot</button>
        <button id="btn-refresh-status">Refresh Status</button>
        <button id="btn-disable" class="danger">Disable Bot</button>
        <button id="btn-enable">Enable Bot</button>
        <button id="btn-shutdown-admin" class="danger">Stop Admin App</button>
      </div>
      <div style="margin-top:12px;">
        <h3 style="margin:4px 0;">Broadcast (dangerous)</h3>
        <textarea id="broadcast-text" rows="3" placeholder="Message to all users"></textarea>
        <label><input type="checkbox" id="broadcast-dryrun" checked /> Dry run</label>
        <button id="btn-broadcast" class="danger" style="width:100%; margin-top:6px;">Send Broadcast</button>
      </div>
    </section>
      <section>
        <h2>Live Feed</h2>
        <div id="live-feed" class="feed-box"></div>
      </section>
    </div>
  </div>

  <!-- Tab: Users -->
  <div class="tab-content" id="tab-users">
    <div class="grid-2col">
      <section>
        <h2>Users</h2>
        <div style="display:flex; gap:8px; margin-bottom:8px;">
          <button id="btn-users-delete-selected" class="danger">Delete Selected</button>
          <button id="btn-users-select-all">Toggle All</button>
        </div>
        <div id="users-list"></div>
        <div class="pagination">
          <button id="btn-users-prev">← Previous</button>
          <button id="btn-users-next">Next →</button>
          <span id="users-page-info" style="margin-left: 12px; color: #94a3b8;"></span>
        </div>
        <div style="margin-top:12px;">
          <input type="number" id="user-id-input" placeholder="User ID" style="width:100%; margin-bottom:6px;" />
          <div class="grid">
            <button id="btn-load-user">Toggle Detail</button>
            <button id="btn-load-user-messages">Toggle Messages</button>
            <button id="btn-load-user-reminders">Toggle Reminders</button>
            <button id="btn-load-user-images">Toggle Images</button>
          </div>
          <button id="btn-delete-user" class="danger" style="width:100%; margin-top:8px;">Delete This User</button>
        </div>
      </section>
      <section>
        <h2>User Detail</h2>
        <pre id="user-detail" style="display:none;"></pre>
      </section>
    </div>
    <div class="grid-2col" style="margin-top: 16px;">
      <section>
        <h2>User Messages</h2>
        <pre id="user-messages" style="display:none;"></pre>
      </section>
      <section>
        <h2>User Reminders</h2>
        <pre id="user-reminders" style="display:none;"></pre>
      </section>
    </div>
    <div class="grid-2col" style="margin-top: 16px;">
      <section style="grid-column: span 2;">
        <h2>User Images</h2>
        <div id="user-images" class="image-grid" style="display:none;"></div>
      </section>
    </div>
  </div>

  <!-- Tab: Moderation -->
  <div class="tab-content" id="tab-moderation">
    <div class="grid-2col">
      <section>
        <h2>Moderation Events</h2>
        <div class="grid">
          <select id="moderation-filter-resolved">
            <option value="open">Open</option>
            <option value="all">All</option>
            <option value="resolved">Resolved</option>
          </select>
          <input type="number" id="moderation-limit" value="100" min="1" max="500" />
          <button id="btn-moderation-load">Load Events</button>
        </div>
        <pre id="moderation-results"></pre>
      </section>
      <section>
        <h2>Resolve Event</h2>
        <input type="number" id="moderation-event-id" placeholder="Event ID" />
        <textarea id="moderation-notes" rows="4" placeholder="Resolution notes"></textarea>
        <button id="btn-moderation-resolve">Resolve Event</button>
      </section>
    </div>
  </div>

  <!-- Tab: Analytics -->
  <div class="tab-content" id="tab-analytics">
    <div class="grid-2col">
      <section>
        <h2>System Metrics</h2>
        <pre id="system-metrics"></pre>
      </section>
      <section>
        <h2>App Metrics</h2>
        <pre id="app-metrics"></pre>
        <div style="margin-top:8px;">
          <label>Window (hours): <input type="number" id="window-hours" value="24" min="1" max="168" style="width:80px;" /></label>
        </div>
      </section>
    </div>
    <section style="margin-top: 16px;">
      <h2>Timeseries Charts</h2>
      <div id="app-metrics-chart"></div>
    </section>
    <section style="margin-top: 16px;">
      <h2>Live Message Latency</h2>
      <div class="grid">
        <label>Rows:
          <input type="number" id="latency-limit" value="20" min="1" max="200" style="width:90px;" />
        </label>
        <button id="btn-latency-refresh">Refresh Live Latency</button>
      </div>
      <pre id="latency-live"></pre>
    </section>
  </div>

  <!-- Tab: Psych Profile -->
  <div class="tab-content" id="tab-psych">
    <div class="grid-auto">
      <section>
        <h2>Psychological Profiles</h2>
        <div class="grid">
          <select id="psych-user"></select>
          <select id="psych-history"></select>
          <button id="btn-psych-load">Load Profile</button>
          <button id="btn-psych-reanalyze" class="danger">Force Reanalysis</button>
        </div>
        <div id="psych-results" class="card" style="white-space: pre-wrap; line-height: 1.55; padding: 14px;"></div>
      </section>
    </div>
  </div>

  <!-- Tab: Crisis Alerts -->
  <div class="tab-content" id="tab-crisis">
    <div class="grid-auto">
      <section>
        <h2>Crisis Alerts</h2>
        <div class="grid">
          <input type="number" id="crisis-limit" value="100" min="1" max="500" />
          <button id="btn-crisis-refresh">Refresh Crisis Alerts</button>
        </div>
        <pre id="alerts"></pre>
      </section>
    </div>
  </div>

  <!-- Tab: System -->
  <div class="tab-content" id="tab-system">
    <div class="grid-2col">
      <section>
        <h2>Module Status</h2>
        <div class="grid" id="module-status"></div>
      </section>
      <section>
        <h2>Scheduler</h2>
        <pre id="scheduler-status"></pre>
      </section>
      <section>
        <h2>Telegram Status</h2>
        <pre id="telegram-status"></pre>
      </section>
      <section>
        <h2>Model Overrides</h2>
        <div class="grid">
          <select id="model-chat"></select>
          <select id="model-embed"></select>
          <select id="model-vision"></select>
          <button id="btn-model-save">Save Models</button>
        </div>
        <div class="card" style="margin-top:10px;">
          <h3 style="margin-top:0;">Pull Model</h3>
          <div class="grid">
            <input type="text" id="model-pull-name" placeholder="example: qwen2.5:14b" />
            <button id="btn-model-pull">Pull from Ollama</button>
          </div>
          <progress id="model-pull-progress" value="0" max="100" style="width:100%; margin-top:8px;"></progress>
          <div id="model-pull-status" style="font-size:12px; color:#94a3b8; margin-top:6px;"></div>
        </div>
        <pre id="model-results"></pre>
      </section>
    </div>
  </div>

  <!-- Tab: Tools -->
  <div class="tab-content" id="tab-tools">
    <div class="grid-2col">
      <section>
        <h2>Reminder Controls</h2>
        <div style="display:flex; flex-direction:column; gap:6px;">
          <select id="reminder-user-select"></select>
          <button id="btn-reminder-load-user">Manage User Reminders</button>
          <button id="btn-reminder-clear-all" class="danger">Clear All Reminders (All Users)</button>
          <select id="reminder-id-input"></select>
          <input type="text" id="reminder-text-input" placeholder="New text (optional)" />
          <input type="datetime-local" id="reminder-time-input" placeholder="Next run (optional)" />
          <select id="reminder-mode-input">
            <option value="one_off_exact">One-off Exact Time</option>
            <option value="recurring_exact">Recurring Exact Time</option>
            <option value="one_off_fuzzy">One-off Fuzzy Window</option>
            <option value="recurring_fuzzy">Recurring Fuzzy Window</option>
          </select>
          <input type="text" id="reminder-cron-input" placeholder="Cron (for recurring)" />
          <input type="number" id="reminder-fuzz-minutes" placeholder="Fuzz minutes (optional)" />
          <label><input type="checkbox" id="reminder-enabled-input" /> Enabled</label>
          <div class="grid">
            <button id="btn-reminder-enable">Enable</button>
            <button id="btn-reminder-disable" class="danger">Disable</button>
            <button id="btn-reminder-update">Update</button>
            <button id="btn-reminder-create">Create</button>
            <button id="btn-reminder-delete" class="danger">Delete</button>
          </div>
          <pre id="reminder-list" style="max-height: 240px; overflow:auto;"></pre>
          <pre id="reminder-action-result"></pre>
        </div>
      </section>
      <section>
        <h2>Memory Search</h2>
        <div class="grid">
          <input type="text" id="memory-query" placeholder="Search text" />
          <input type="number" id="memory-user" placeholder="User ID (optional)" />
          <input type="text" id="memory-since" placeholder="Since (ISO optional)" />
          <input type="text" id="memory-until" placeholder="Until (ISO optional)" />
          <input type="number" id="memory-limit" placeholder="Limit" value="50" />
          <button id="btn-memory-search">Search</button>
        </div>
        <pre id="memory-results"></pre>
      </section>
    </div>
    <div class="grid-2col" style="margin-top: 16px;">
      <section>
        <h2>User Export (PII-masked)</h2>
        <div class="grid">
          <input type="number" id="export-user" placeholder="User ID" />
          <input type="text" id="export-since" placeholder="Since (ISO optional)" />
          <input type="text" id="export-until" placeholder="Until (ISO optional)" />
          <input type="number" id="export-limit" placeholder="Message limit" value="500" />
          <button id="btn-export-user">Export</button>
        </div>
        <pre id="export-results"></pre>
      </section>
    </div>
  </div>

  <!-- Tab: High-Risk -->
  <div class="tab-content" id="tab-highrisk">
    <div class="grid-2col">
      <!-- Enhanced LLM Console -->
      <section style="grid-column: span 2;">
        <h2>🤖 Enhanced LLM Console</h2>
        <p style="color: #f59e0b; margin-bottom: 12px;">
          ⚠️ Omniscient assistant with 11 tools: read/edit files, query/modify database, manage users, search messages, and more.
          File edits have automatic rollback safety.
        </p>

        <!-- Console Options -->
        <div style="display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap;">
          <label><input type="checkbox" id="console-tools-enabled" checked /> Enable Tools</label>
          <label><input type="checkbox" id="console-external-files" /> Allow External Files</label>
          <button id="btn-console-clear" style="background: #6b7280;">Clear History</button>
          <button id="btn-console-sessions" style="background: #6b7280;">View Sessions</button>
          <span id="console-session-info" style="color: #94a3b8; margin-left: auto;"></span>
        </div>

        <!-- Chat History -->
        <div id="console-chat-history" style="background: #0b1222; border: 1px solid #1f2937; border-radius: 6px; padding: 12px; min-height: 400px; max-height: 600px; overflow-y: auto; margin-bottom: 12px; font-family: monospace; font-size: 13px;">
          <div style="color: #94a3b8; font-style: italic;">Console ready. Type a message to begin...</div>
        </div>

        <!-- Input Area -->
        <div style="display: flex; gap: 8px;">
          <textarea id="console-input" placeholder="Ask me anything... I can read files, query database, edit code (with rollback), search messages, and more!" style="flex: 1; min-height: 80px; font-family: monospace;"></textarea>
          <div style="display: flex; flex-direction: column; gap: 8px;">
            <button id="btn-console-send" class="danger" style="height: 40px;">Send</button>
            <button id="btn-console-stop" class="danger" style="height: 40px; background: #6b7280;" disabled>Stop</button>
          </div>
        </div>
      </section>

      <!-- Legacy Tools -->
      <section>
        <h2>Legacy High-Risk Tools</h2>
        <p style="color: #94a3b8; font-size: 12px; margin-bottom: 8px;">These are the old stub tools. Use Enhanced LLM Console above instead.</p>
        <div class="grid">
          <button id="btn-llm-console" class="danger">LLM Console (Legacy)</button>
          <button id="btn-db-edit" class="danger">DB Edit (Legacy)</button>
          <button id="btn-omni-broadcast" class="danger">Omni Broadcast (Legacy)</button>
        </div>
        <pre id="highrisk-results"></pre>
      </section>
    </div>
  </div>

  <!-- Tab: Media -->
  <div class="tab-content" id="tab-media">
    <!-- Generation Controls -->
    <div class="grid-2col">
      <section>
        <h2>🎨 AI Image Generation</h2>
        <div style="display: flex; flex-direction: column; gap: 8px;">
          <label>Model:
            <select id="media-model">
              <option value="flux2-klein">FLUX.2 Klein 9B GGUF (Local API, default, SFW)</option>
              <option value="pony-xl-v6">Pony Diffusion XL v6 (Local, 7GB, ~45s, NSFW+anime)</option>
              <option value="wai-illustrious-xl">WAI Illustrious SDXL v1.60 (Local, 7GB, ~60s, NSFW+hires)</option>
              <option value="unholy-desire-v7">Unholy Desire Mix Sinister v7.0 (Local, 7GB, ~30s, NSFW)</option>
              <option value="sdxl">Stable Diffusion XL (8GB, ~45s)</option>
              <option value="sdxl-turbo">SDXL Turbo (6GB, ~20s)</option>
              <option value="flux">FLUX.1-dev (10GB, ~90s)</option>
              <option value="z-image-q8-gguf">Z-Image Q8 GGUF (8GB+, ~420s)</option>
              <option value="easydiffusion">EasyDiffusion (local API, current model)</option>
              <option value="perchance">Perchance (remote, Playwright-backed)</option>
              <option value="perchance_other">Perchance Other (direct URL API)</option>
              <option value="playground">Playground v2.5 (8GB, ~50s)</option>
              <option value="pixart">PixArt-Σ (7GB, ~35s)</option>
            </select>
          </label>

          <label>Prompt:
            <textarea id="media-prompt" rows="3" placeholder="Describe the image you want to generate..."></textarea>
          </label>

          <label>Negative Prompt (optional):
            <textarea id="media-negative" rows="2" placeholder="Things to avoid (e.g., blurry, low quality)"></textarea>
          </label>

          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
            <label>Steps: <input type="number" id="media-steps" value="4" min="1" max="100" /></label>
            <label>Guidance: <input type="number" id="media-guidance" value="4.0" step="0.1" min="1" max="20" /></label>
            <label>Width: <input type="number" id="media-width" value="1024" step="64" min="512" max="2048" /></label>
            <label>Height: <input type="number" id="media-height" value="1024" step="64" min="512" max="2048" /></label>
            <label>Seed (optional): <input type="number" id="media-seed" placeholder="Random if empty" /></label>
            <label>Owner (optional): <select id="media-user-id"></select></label>
          </div>

          <button id="btn-generate-image" class="danger" style="width: 100%;">Generate Image</button>
          <div id="media-generation-status" style="color: #94a3b8; font-size: 13px; min-height: 20px;"></div>
        </div>
      </section>

      <section>
        <h2>⚙️ VRAM Status</h2>
        <pre id="media-vram" style="min-height: 150px;">Loading...</pre>
        <div style="display: flex; gap: 8px; margin-top: 8px;">
          <button id="btn-refresh-vram">Refresh VRAM</button>
          <button id="btn-unload-model" class="danger">Unload Model</button>
        </div>
      </section>
    </div>

    <!-- Generation History / Gallery -->
    <section style="margin-top: 16px;">
      <h2>🖼️ Generation History</h2>
      <div style="display: flex; gap: 8px; margin-bottom: 12px;">
        <select id="media-history-user"></select>
        <button id="btn-load-history">Load History</button>
      </div>
      <div id="media-history" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px;">
        <div style="color: #94a3b8;">No images generated yet</div>
      </div>
    </section>
  </div>

  <!-- Tab: Feedback -->
  <div class="tab-content" id="tab-feedback">
    <div class="grid-auto">
      <section>
        <h2>Bugs / Feedback / Suggestions</h2>
        <div class="grid">
          <select id="feedback-status">
            <option value="">All</option>
            <option value="new">New</option>
            <option value="triage">Triage</option>
            <option value="resolved">Resolved</option>
          </select>
          <input type="number" id="feedback-limit" value="50" />
          <button id="btn-feedback-load">Load Feedback</button>
        </div>
        <div style="margin-top:8px;">
          <input type="number" id="feedback-id" placeholder="Feedback ID" />
          <input type="text" id="feedback-notes" placeholder="Admin notes" />
          <select id="feedback-new-status">
            <option value="">(keep status)</option>
            <option value="triage">Triage</option>
            <option value="resolved">Resolved</option>
          </select>
          <button id="btn-feedback-update">Update</button>
        </div>
        <pre id="feedback-results"></pre>
      </section>
    </div>
  </div>

  <!-- Tab: Misc -->
  <div class="tab-content" id="tab-misc">
    <div class="grid-auto">
      <section>
        <h2>Miscellaneous</h2>
        <div class="grid">
          <button id="btn-misc-db-stats">Load DB Stats</button>
          <button id="btn-misc-users">Load User Names</button>
        </div>
        <pre id="misc-results"></pre>
      </section>
    </div>
  </div>
  <script>
    // Toast Notification System
    function showToast(message, type = 'info', duration = 4000) {{
      const container = document.getElementById('toast-container');
      const toast = document.createElement('div');
      toast.className = `toast ${{type}}`;

      const icon = {{
        success: '✓',
        error: '✗',
        warning: '⚠',
        info: 'ℹ'
      }}[type] || 'ℹ';

      toast.innerHTML = `<span style="font-size: 18px;">${{icon}}</span><span>${{message}}</span>`;

      container.appendChild(toast);

      // Remove on click
      toast.addEventListener('click', () => {{
        toast.classList.add('removing');
        setTimeout(() => toast.remove(), 300);
      }});

      // Auto-remove after duration
      setTimeout(() => {{
        if (toast.parentElement) {{
          toast.classList.add('removing');
          setTimeout(() => toast.remove(), 300);
        }}
      }}, duration);
    }}

    const statusEl = document.getElementById('status-cards');
    const feedEl = document.getElementById('live-feed');
    const dangerToggle = document.getElementById('danger-toggle');
    const trustForm = document.getElementById('trust-form');
    const usersList = document.getElementById('users-list');
    const userDetail = document.getElementById('user-detail');
    const userIdInput = document.getElementById('user-id-input');
    const userMessages = document.getElementById('user-messages');
    const userReminders = document.getElementById('user-reminders');
    const userImages = document.getElementById('user-images');
    const moduleStatus = document.getElementById('module-status');
    const systemMetrics = document.getElementById('system-metrics');
    const appMetricsEl = document.getElementById('app-metrics');
    const appChartEl = document.getElementById('app-metrics-chart');
    const latencyLiveEl = document.getElementById('latency-live');
    const schedulerEl = document.getElementById('scheduler-status');
    const telegramEl = document.getElementById('telegram-status');
    const alertsEl = document.getElementById('alerts');
    const windowHoursInput = document.getElementById('window-hours');
    const modelChat = document.getElementById('model-chat');
    const modelEmbed = document.getElementById('model-embed');
    const modelVision = document.getElementById('model-vision');
    const modelResults = document.getElementById('model-results');
    const modelPullName = document.getElementById('model-pull-name');
    const modelPullProgress = document.getElementById('model-pull-progress');
    const modelPullStatus = document.getElementById('model-pull-status');
    const reminderIdInput = document.getElementById('reminder-id-input');
    const reminderTextInput = document.getElementById('reminder-text-input');
    const reminderTimeInput = document.getElementById('reminder-time-input');
    const reminderModeInput = document.getElementById('reminder-mode-input');
    const reminderCronInput = document.getElementById('reminder-cron-input');
    const reminderFuzzInput = document.getElementById('reminder-fuzz-minutes');
    const reminderUserSelect = document.getElementById('reminder-user-select');
    const reminderListEl = document.getElementById('reminder-list');
    const reminderEnabledInput = document.getElementById('reminder-enabled-input');
    const reminderResult = document.getElementById('reminder-action-result');
    const moderationResults = document.getElementById('moderation-results');
    const miscResults = document.getElementById('misc-results');
    let usersOffset = 0;
    const usersLimit = 25;
    let selectedUserId = null;
    let selectedUserIds = new Set();
    let userNameMap = {{}};

    // Tab Switching with Hash Routing
    function switchTab(tabName) {{
      // Update tab buttons
      document.querySelectorAll('.tab-btn').forEach(btn => {{
        btn.classList.remove('active');
        if (btn.dataset.tab === tabName) btn.classList.add('active');
      }});
      // Update tab content
      document.querySelectorAll('.tab-content').forEach(content => {{
        content.classList.remove('active');
      }});
      const targetTab = document.getElementById(`tab-${{tabName}}`);
      if (targetTab) targetTab.classList.add('active');
      // Update URL hash
      window.location.hash = tabName;
    }}

    // Handle tab button clicks
    document.querySelectorAll('.tab-btn').forEach(btn => {{
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    }});

    // Handle hash changes (back/forward navigation)
    window.addEventListener('hashchange', () => {{
      const hash = window.location.hash.slice(1);
      if (hash) switchTab(hash);
    }});

    // Initialize from hash on page load
    const initialHash = window.location.hash.slice(1);
    if (initialHash && document.getElementById(`tab-${{initialHash}}`)) {{
      switchTab(initialHash);
    }}
    const memoryQuery = document.getElementById('memory-query');
    const memoryUser = document.getElementById('memory-user');
    const memorySince = document.getElementById('memory-since');
    const memoryUntil = document.getElementById('memory-until');
    const memoryLimit = document.getElementById('memory-limit');
    const memoryResults = document.getElementById('memory-results');
    const psychUser = document.getElementById('psych-user');
    const psychResults = document.getElementById('psych-results');
    const exportUser = document.getElementById('export-user');
    const exportSince = document.getElementById('export-since');
    const exportUntil = document.getElementById('export-until');
    const exportLimit = document.getElementById('export-limit');
    const exportResults = document.getElementById('export-results');
    const feedbackStatus = document.getElementById('feedback-status');
    const feedbackLimit = document.getElementById('feedback-limit');
    const feedbackResults = document.getElementById('feedback-results');
    const feedbackId = document.getElementById('feedback-id');
    const feedbackNotes = document.getElementById('feedback-notes');
    const feedbackNewStatus = document.getElementById('feedback-new-status');
    const highriskResults = document.getElementById('highrisk-results');
    dangerToggle.checked = {dangerous_enabled};

    async function loadStatus() {{
      const res = await fetch('/readyz');
      const data = await res.json();
      statusEl.innerHTML = '';
      const checks = data.checks || {{}};
      Object.entries(checks).forEach(([k,v]) => {{
        const div = document.createElement('div');
        div.className = 'card';
        div.innerHTML = `<div style="font-weight:600">${{k}}</div><div>${{v}}</div>`;
        statusEl.appendChild(div);
      }});
    }}

    async function loadUsers() {{
      const res = await fetch(`/users?limit=${{usersLimit}}&offset=${{usersOffset}}`);
      const data = await res.json();
      usersList.innerHTML = '';
      data.users.forEach(u => {{
        const div = document.createElement('div');
        div.className = 'card';
        div.style.cursor = 'pointer';
        const checked = selectedUserIds.has(u.id) ? 'checked' : '';
        div.onclick = () => {{
          selectedUserId = u.id;
          loadUserDetail(u.id);
          userIdInput.value = u.id;
        }};
        div.innerHTML = `<label class="user-row-label" style="cursor:pointer;">
          <input type="checkbox" class="user-select-box" data-user-id="${{u.id}}" ${{checked}} />
          <span>${{u.id}} | ${{u.username || ''}} | ${{u.display_name || ''}}</span>
        </label>`;
        usersList.appendChild(div);
      }});
      document.querySelectorAll('.user-select-box').forEach(box => {{
        box.addEventListener('change', (e) => {{
          const uid = parseInt(e.target.dataset.userId, 10);
          if (e.target.checked) selectedUserIds.add(uid);
          else selectedUserIds.delete(uid);
        }});
      }});
      // Update pagination info
      const pageInfo = document.getElementById('users-page-info');
      if (pageInfo) {{
        const start = usersOffset + 1;
        const end = usersOffset + data.users.length;
        pageInfo.textContent = `Showing ${{start}}-${{end}}`;
      }}
      // Update button states
      document.getElementById('btn-users-prev').disabled = usersOffset === 0;
      document.getElementById('btn-users-next').disabled = data.users.length < usersLimit;
    }}

    async function loadUserDetail(id) {{
      const res = await fetch(`/users/${{id}}`);
      if (!res.ok) {{ userDetail.textContent = 'Failed to load user'; return; }}
      const data = await res.json();
      userDetail.textContent = JSON.stringify(data, null, 2);
    }}

    async function loadUserMessages(id) {{
      const res = await fetch(`/users/${{id}}/messages?limit=100`);
      if (!res.ok) {{ userMessages.textContent = 'Failed to load messages'; return; }}
      const data = await res.json();
      userMessages.textContent = JSON.stringify(data, null, 2);
    }}

    async function loadUserReminders(id) {{
      const res = await fetch(`/users/${{id}}/reminders`);
      if (!res.ok) {{ userReminders.textContent = 'Failed to load reminders'; return; }}
      const data = await res.json();
      userReminders.textContent = JSON.stringify(data, null, 2);
    }}

    async function loadUserImages(id) {{
      const res = await fetch(`/users/${{id}}/images?limit=200`);
      const data = await res.json();
      if (!res.ok) {{
        userImages.innerHTML = `<div class="card">Failed to load images: ${{data.detail || res.status}}</div>`;
        return;
      }}
      const items = data.images || [];
      if (!items.length) {{
        userImages.innerHTML = '<div class="card">No images found for this user.</div>';
        return;
      }}
      userImages.innerHTML = '';
      items.forEach(item => {{
        const card = document.createElement('div');
        card.className = 'card';
        const when = item.uploaded_at ? new Date(item.uploaded_at).toLocaleString() : '';
        const caption = item.caption ? item.caption : '';
        const source = item.source || 'unknown';
        const imgHtml = item.preview_url
          ? `<img src="${{item.preview_url}}" alt="user image" style="width:100%; height:150px; object-fit:cover; border-radius:6px; margin-bottom:6px;" onclick="window.open('${{item.preview_url}}','_blank')" />`
          : '';
        card.innerHTML = `${{imgHtml}}<div style="font-size:12px;">${{caption}}</div><div style="font-size:11px; color:#94a3b8;">${{when}} • ${{source}}</div>`;
        userImages.appendChild(card);
      }});
    }}

    function getActiveUserId() {{
      const raw = (userIdInput.value || '').trim();
      if (raw) return parseInt(raw, 10);
      if (selectedUserId) return selectedUserId;
      return null;
    }}

    function togglePanel(el, loader) {{
      if (!el) return;
      if (el.style.display === 'none') {{
        el.style.display = 'block';
        loader();
      }} else {{
        el.style.display = 'none';
      }}
    }}

    async function loadModuleStatus() {{
      const res = await fetch('/status/modules');
      const data = await res.json();
      moduleStatus.innerHTML = '';
      Object.entries(data).forEach(([k,v]) => {{
        const div = document.createElement('div');
        div.className = 'card';
        div.innerHTML = `<div style="font-weight:600">${{k}}</div><div>${{v}}</div>`;
        moduleStatus.appendChild(div);
      }});
    }}

    async function loadSystemMetrics() {{
      const res = await fetch('/metrics/system');
      if (!res.ok) {{ systemMetrics.textContent = 'Failed to load metrics'; return; }}
      const data = await res.json();
      systemMetrics.textContent = JSON.stringify(data, null, 2);
      // simple bars for CPU/memory if present
      if (typeof data.cpu_percent === 'number') {{
        const bar = `<div class="card"><div>CPU</div><div style="background:#1f2937;border-radius:4px;"><div style="width:${{Math.min(100, data.cpu_percent)}}%;background:#10b981;height:8px;border-radius:4px;"></div></div><div>${{data.cpu_percent}}%</div></div>`;
        systemMetrics.insertAdjacentHTML('afterend', bar);
      }}
    }}

    async function loadAppMetrics() {{
      const hrs = parseInt(windowHoursInput.value || '24', 10) || 24;
      const res = await fetch(`/metrics/app?hours=${{hrs}}`);
      if (!res.ok) {{ appMetricsEl.textContent = 'Failed to load app metrics'; return; }}
      const data = await res.json();
      appMetricsEl.textContent = JSON.stringify(data, null, 2);
      if (appChartEl) {{
        const bars = [];
        const entries = [
          ['messages_total', data.messages_total],
          ['messages_window', data.messages_24h],
          ['reminders_total', data.reminders_total],
          ['reminders_due_next_hour', data.reminders_due_next_hour],
          ['moderation_open', data.moderation_open]
        ];
        const maxVal = Math.max(...entries.map(([,v]) => (typeof v === 'number' ? v : 0)), 1);
        entries.forEach(([label, val]) => {{
          const width = typeof val === 'number' ? Math.min(100, (val / maxVal) * 100) : 0;
          bars.push(`<div class="card"><div>${{label}}</div><div style="background:#1f2937;border-radius:4px;"><div style="width:${{width}}%;background:#3b82f6;height:8px;border-radius:4px;"></div></div><div>${{val ?? 'n/a'}}</div></div>`);
        }});
        appChartEl.innerHTML = bars.join('');
      }}
      loadTimeseries(hrs);
    }}

    async function loadLatencyLive() {{
      if (!latencyLiveEl) return;
      const limitEl = document.getElementById('latency-limit');
      const limit = parseInt((limitEl && limitEl.value) || '20', 10) || 20;
      const res = await fetch(`/metrics/latency_live?limit=${{limit}}`);
      if (!res.ok) {{
        latencyLiveEl.textContent = 'Failed to load live latency metrics';
        return;
      }}
      const data = await res.json();
      const rows = data.rows || [];
      const summary = data.summary || {{}};
      const fmt = (v) => (v === null || v === undefined ? '-' : `${{Number(v).toFixed(1)}}ms`);

      const lines = [];
      lines.push(
        `Samples: ${{summary.count ?? 0}} | ` +
        `Avg Total: ${{fmt(summary.avg_total_ms)}} | ` +
        `Avg Queue: ${{fmt(summary.avg_queue_ms)}} | ` +
        `Avg RAG: ${{fmt(summary.avg_rag_ms)}} | ` +
        `Avg Memory: ${{fmt(summary.avg_memory_ms)}} | ` +
        `Avg LLM: ${{fmt(summary.avg_llm_ms)}} | ` +
        `Avg Persist: ${{fmt(summary.avg_persist_ms)}} | ` +
        `Avg Send: ${{fmt(summary.avg_send_ms)}} | ` +
        `Avg E2E: ${{fmt(summary.avg_e2e_ms)}} | ` +
        `OK: ${{summary.ok_count ?? 0}} | ERR: ${{summary.error_count ?? 0}}`
      );
      lines.push('----------------------------------------------------------------');
      if (!rows.length) {{
        lines.push('No timing samples yet. Send a message to the bot and refresh.');
      }} else {{
        rows.forEach(r => {{
          const ts = r.created_at ? new Date(r.created_at).toLocaleTimeString() : '-';
          lines.push(
            `${{ts}} | user=${{r.user_id ?? '-'}} | total=${{fmt(r.total_ms)}} | queue=${{fmt(r.queue_ms)}} | rag=${{fmt(r.rag_ms)}} | memory=${{fmt(r.memory_ms)}}(${{r.memory_mode || '-'}}) | llm=${{fmt(r.llm_ms)}} | persist=${{fmt(r.persist_ms)}} | send=${{fmt(r.send_ms)}} | e2e=${{fmt(r.e2e_ms)}} | status=${{r.status || '-'}}`
          );
          if (r.error) {{
            lines.push(`  error: ${{String(r.error).slice(0, 180)}}`);
          }}
        }});
      }}
      latencyLiveEl.textContent = lines.join('\\n');
    }}

    async function loadTimeseries(hours=48) {{
      const res = await fetch(`/metrics/timeseries?hours=${{hours}}`);
      if (!res.ok) return;
      const data = await res.json();
      // Simple inline chart using widths
      const msg = data.messages || [];
      const rem = data.reminders || [];
      const maxMsg = Math.max(...msg.map(m => m.count || 0), 1);
      const maxRem = Math.max(...rem.map(r => r.count || 0), 1);
      const msgBars = msg.slice(-24).map(m => `<div class="card"><div>${{m.bucket}}</div><div style="background:#1f2937;border-radius:4px;"><div style="width:${{Math.min(100,(m.count/maxMsg)*100)}}%;background:#a855f7;height:6px;border-radius:4px;"></div></div><div>${{m.count}}</div></div>`);
      const remBars = rem.slice(-24).map(r => `<div class="card"><div>${{r.bucket}}</div><div style="background:#1f2937;border-radius:4px;"><div style="width:${{Math.min(100,(r.count/maxRem)*100)}}%;background:#f59e0b;height:6px;border-radius:4px;"></div></div><div>${{r.count}}</div></div>`);
      appChartEl.innerHTML += `<h4>Messages (last 24 buckets)</h4>` + msgBars.join('') + `<h4>Reminders (last 24 buckets)</h4>` + remBars.join('');
    }}

    async function loadScheduler() {{
      const res = await fetch('/status/scheduler');
      if (!res.ok) {{ schedulerEl.textContent = 'Failed to load scheduler'; return; }}
      const data = await res.json();
      schedulerEl.textContent = JSON.stringify(data, null, 2);
    }}

    async function loadTelegram() {{
      const res = await fetch('/status/telegram');
      if (!res.ok) {{ telegramEl.textContent = 'Failed to load telegram status'; return; }}
      const data = await res.json();
      telegramEl.textContent = JSON.stringify(data, null, 2);
    }}

    async function loadAlerts() {{
      const res = await fetch('/analytics/alerts');
      if (!res.ok) {{ alertsEl.textContent = 'Failed to load alerts'; return; }}
      const data = await res.json();
      alertsEl.textContent = JSON.stringify(data, null, 2);
    }}

    async function loadCrisisAlerts() {{
      const limit = parseInt(document.getElementById('crisis-limit').value || '100', 10) || 100;
      const res = await fetch(`/crisis/active?limit=${{limit}}`);
      const data = await res.json();
      alertsEl.textContent = res.ok ? JSON.stringify(data, null, 2) : `Error: ${{data.detail || res.status}}`;
    }}

    async function loadModels() {{
      const [currentRes, availRes] = await Promise.all([
        fetch('/models'),
        fetch('/models/ollama')
      ]);
      const current = await currentRes.json();
      const available = await availRes.json();
      const names = (available.models || []).map(m => m.name);
      function hydrateModelSelect(el, selected) {{
        if (!el) return;
        el.innerHTML = '';
        if (selected && !names.includes(selected)) {{
          const opt = document.createElement('option');
          opt.value = selected;
          opt.textContent = selected + ' (current)';
          el.appendChild(opt);
        }}
        names.forEach(name => {{
          const opt = document.createElement('option');
          opt.value = name;
          opt.textContent = name;
          el.appendChild(opt);
        }});
        el.value = selected || '';
      }}
      hydrateModelSelect(modelChat, current.chat_model || '');
      hydrateModelSelect(modelEmbed, current.embed_model || '');
      hydrateModelSelect(modelVision, current.vision_model || '');
      modelResults.textContent = JSON.stringify({{ current, available_count: names.length }}, null, 2);
    }}

    async function saveModels() {{
      const body = {{
        chat_model: modelChat.value || undefined,
        embed_model: modelEmbed.value || undefined,
        vision_model: modelVision.value || undefined
      }};

      try {{
        const res = await fetch('/models', {{
          method:'POST',
          headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify(body)
        }});
        const data = await res.json();

        if (res.ok) {{
          modelResults.textContent = JSON.stringify(data, null, 2);
          showToast('Model configuration saved successfully! Restart bot to apply changes.', 'success');
        }} else {{
          modelResults.textContent = `Error: ${{data.detail || res.status}}`;
          showToast(`Failed to save models: ${{data.detail || res.status}}`, 'error');
        }}
      }} catch (err) {{
        modelResults.textContent = `Error: ${{err.message}}`;
        showToast(`Error saving models: ${{err.message}}`, 'error');
      }}
    }}

    function pullModel() {{
      const name = (modelPullName.value || '').trim();
      if (!name) {{
        alert('Enter a model name/tag to pull');
        return;
      }}
      modelPullProgress.value = 0;
      modelPullStatus.textContent = 'Starting pull...';
      const src = new EventSource(`/models/pull/stream?model=${{encodeURIComponent(name)}}`);
      src.onmessage = (evt) => {{
        try {{
          const msg = JSON.parse(evt.data);
          if (typeof msg.progress === 'number') {{
            modelPullProgress.value = msg.progress;
          }}
          modelPullStatus.textContent = msg.message || msg.status || '';
          if (msg.status === 'completed') {{
            src.close();
            showToast(`Model ${{name}} pulled successfully`, 'success');
            loadModels();
          }} else if (msg.status === 'error') {{
            src.close();
            showToast(`Pull failed: ${{msg.message}}`, 'error');
          }}
        }} catch (err) {{
          modelPullStatus.textContent = evt.data;
        }}
      }};
      src.onerror = () => {{
        src.close();
      }};
    }}

    async function reminderAction(url, body = {{}}) {{
      reminderResult.textContent = '';
      const res = await fetch(url, {{
        method:'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify(body)
      }});
      const data = await res.json().catch(() => ({{}}));
      reminderResult.textContent = res.ok ? JSON.stringify(data) : `Error: ${{data.detail || res.status}}`;
    }}

    async function clearAllReminders() {{
      if (!dangerToggle.checked) {{
        alert('Enable dangerous tools to perform this action.');
        return;
      }}
      if (!confirm('Delete ALL reminders for ALL users? This cannot be undone.')) return;
      const res = await fetch('/reminders/clear_all', {{ method: 'POST' }});
      const data = await res.json().catch(() => ({{}}));
      if (!res.ok) {{
        reminderResult.textContent = `Error: ${{data.detail || res.status}}`;
        return;
      }}
      reminderResult.textContent = JSON.stringify(data, null, 2);
      reminderListEl.textContent = 'No reminders loaded.';
      reminderIdInput.innerHTML = '';
      showToast(`Cleared ${{data.deleted || 0}} reminder(s)`, 'warning');
      await loadAppMetrics();
      await loadAlerts();
      if (reminderUserSelect.value) {{
        await loadRemindersForSelectedUser();
      }}
    }}

    async function loadRemindersForSelectedUser() {{
      if (!reminderUserSelect.value) {{
        reminderListEl.textContent = 'Select a user first.';
        return;
      }}
      const uid = reminderUserSelect.value;
      const res = await fetch(`/users/${{uid}}/reminders`);
      const data = await res.json();
      if (!res.ok) {{
        reminderListEl.textContent = `Error: ${{data.detail || res.status}}`;
        return;
      }}
      const reminders = data.reminders || [];
      reminderIdInput.innerHTML = '';
      reminders.forEach(r => {{
        const opt = document.createElement('option');
        const label = r.text || r.kind || 'Reminder';
        opt.value = r.id;
        opt.textContent = `${{label}} | due ${{r.due_at}} | ${{r.enabled ? 'enabled' : 'disabled'}}`;
        reminderIdInput.appendChild(opt);
      }});
      reminderListEl.textContent = JSON.stringify(reminders, null, 2);
    }}

    async function createReminderFromForm() {{
      if (!reminderUserSelect.value) {{
        alert('Select a user');
        return;
      }}
      if (!reminderTextInput.value) {{
        alert('Reminder text is required');
        return;
      }}
      const dt = reminderTimeInput.value ? new Date(reminderTimeInput.value).toISOString() : new Date().toISOString();
      const mode = reminderModeInput.value;
      const metadata = {{
        text: reminderTextInput.value,
        mode: mode
      }};
      if (reminderFuzzInput.value) metadata.fuzz_minutes = parseInt(reminderFuzzInput.value, 10);
      if (mode.includes('fuzzy')) metadata.fuzzy = true;
      const payload = {{
        user_id: String(reminderUserSelect.value),
        text: reminderTextInput.value,
        next_run_at: dt,
        cadence_cron: reminderCronInput.value || null,
        enabled: reminderEnabledInput.checked,
        metadata
      }};
      const res = await fetch('/reminders', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify(payload)
      }});
      const data = await res.json().catch(() => ({{}}));
      reminderResult.textContent = res.ok ? JSON.stringify(data, null, 2) : `Error: ${{data.detail || res.status}}`;
      if (res.ok) await loadRemindersForSelectedUser();
    }}

    async function memorySearch() {{
      const q = memoryQuery.value;
      if (!q) {{ alert('Query required'); return; }}
      const params = new URLSearchParams();
      params.set('q', q);
      if (memoryUser.value) params.set('user_id', memoryUser.value);
      if (memorySince.value) params.set('since', memorySince.value);
      if (memoryUntil.value) params.set('until', memoryUntil.value);
      if (memoryLimit.value) params.set('limit', memoryLimit.value);
      const res = await fetch(`/memory/search?${{params.toString()}}`);
      const data = await res.json();
      memoryResults.textContent = res.ok ? JSON.stringify(data, null, 2) : `Error: ${{data.detail || res.status}}`;
    }}

    function formatJsonPretty(value, indent = 0) {{
      const pad = ' '.repeat(indent);
      if (value === null || value === undefined) return String(value);
      if (typeof value !== 'object') return String(value);
      if (Array.isArray(value)) {{
        return value.map(v => `${{pad}}- ${{formatJsonPretty(v, indent + 2)}}`).join('\\n');
      }}
      return Object.entries(value).map(([k,v]) => {{
        if (typeof v === 'object' && v !== null) {{
          return `${{pad}}${{k}}:\n${{formatJsonPretty(v, indent + 2)}}`;
        }}
        return `${{pad}}${{k}}: ${{v}}`;
      }}).join('\\n');
    }}

    async function loadPsychHistory() {{
      if (!psychUser.value) return;
      const res = await fetch(`/psych/${{psychUser.value}}/history?limit=10`);
      const data = await res.json();
      const history = document.getElementById('psych-history');
      history.innerHTML = '';
      if (!res.ok || !data.history || data.history.length === 0) {{
        history.innerHTML = '<option value="">Latest</option>';
        return;
      }}
      const latest = document.createElement('option');
      latest.value = '';
      latest.textContent = 'Latest';
      history.appendChild(latest);
      data.history.forEach(item => {{
        const opt = document.createElement('option');
        opt.value = item.id;
        opt.textContent = `#${{item.id}} | ${{item.created_at}} | msgs=${{item.messages_analyzed ?? 'n/a'}}`;
        history.appendChild(opt);
      }});
    }}

    async function loadPsych() {{
      if (!psychUser.value) {{ alert('User required'); return; }}
      const historySel = document.getElementById('psych-history');
      const pid = historySel && historySel.value ? `?profile_id=${{encodeURIComponent(historySel.value)}}` : '';
      const res = await fetch(`/psych/${{psychUser.value}}${{pid}}`);
      const data = await res.json();
      if (!res.ok) {{
        psychResults.textContent = `Error: ${{data.detail || res.status}}`;
        return;
      }}
      const profile = data.profile ? JSON.parse(data.profile.profile_data || '{{}}') : null;
      if (!profile) {{
        psychResults.textContent = 'No profile found for this user.';
        return;
      }}
      const header = `Profile ID: ${{data.profile.id}}\\nCreated: ${{data.profile.created_at}}\\nMessages analyzed: ${{data.profile.messages_analyzed}}\\n`;
      psychResults.textContent = `${{header}}\\n${{formatJsonPretty(profile, 0)}}`;
    }}

    async function reanalyzePsych() {{
      if (!psychUser.value) {{ alert('User required'); return; }}
      const res = await fetch(`/psych/${{psychUser.value}}/reanalyze`, {{ method: 'POST' }});
      const data = await res.json();
      if (!res.ok) {{
        alert(`Reanalysis failed: ${{data.detail || res.status}}`);
        return;
      }}
      showToast(`Reanalysis ${{data.status}}`, 'success');
      await loadPsychHistory();
      await loadPsych();
    }}

    async function loadUserNames() {{
      const res = await fetch('/users/names');
      const data = await res.json();
      if (!res.ok) return;
      const users = data.users || [];
      userNameMap = {{}};
      users.forEach(u => {{ userNameMap[String(u.id)] = u.name; }});
      const targets = [
        document.getElementById('psych-user'),
        document.getElementById('reminder-user-select'),
        document.getElementById('media-user-id'),
        document.getElementById('media-history-user')
      ];
      targets.forEach(sel => {{
        if (!sel) return;
        const blankText = sel.id === 'media-history-user'
          ? 'All Users'
          : sel.id === 'media-user-id'
            ? 'Admin Preview (use admin-linked owner)'
            : '';
        sel.innerHTML = blankText ? `<option value="">${{blankText}}</option>` : '';
        users.forEach(u => {{
          const opt = document.createElement('option');
          opt.value = u.id;
          opt.textContent = u.name;
          sel.appendChild(opt);
        }});
      }});
      if (users.length > 0 && !document.getElementById('psych-user').value) {{
        document.getElementById('psych-user').value = users[0].id;
      }}
      await loadPsychHistory();
    }}

    async function exportUserData() {{
      if (!exportUser.value) {{ alert('User ID required'); return; }}
      const params = new URLSearchParams();
      if (exportSince.value) params.set('since', exportSince.value);
      if (exportUntil.value) params.set('until', exportUntil.value);
      if (exportLimit.value) params.set('limit', exportLimit.value);
      const res = await fetch(`/export/user/${{exportUser.value}}?${{params.toString()}}`);
      const data = await res.json();
      exportResults.textContent = res.ok ? JSON.stringify(data, null, 2) : `Error: ${{data.detail || res.status}}`;
    }}

    async function loadFeedback() {{
      const params = new URLSearchParams();
      if (feedbackStatus.value) params.set('status', feedbackStatus.value);
      if (feedbackLimit.value) params.set('limit', feedbackLimit.value);
      const res = await fetch(`/feedback?${{params.toString()}}`);
      const data = await res.json();
      feedbackResults.textContent = res.ok ? JSON.stringify(data, null, 2) : `Error: ${{data.detail || res.status}}`;
    }}

    async function updateFeedbackItem() {{
      if (!feedbackId.value) {{ alert('Feedback ID required'); return; }}
      const body = {{}};
      if (feedbackNotes.value) body.admin_notes = feedbackNotes.value;
      if (feedbackNewStatus.value) body.status = feedbackNewStatus.value;
      const res = await fetch(`/feedback/${{feedbackId.value}}/update`, {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify(body)
      }});
      const data = await res.json();
      feedbackResults.textContent = res.ok ? JSON.stringify(data, null, 2) : `Error: ${{data.detail || res.status}}`;
    }}

    async function loadModeration() {{
      const mode = document.getElementById('moderation-filter-resolved').value;
      const limit = parseInt(document.getElementById('moderation-limit').value || '100', 10) || 100;
      const q = new URLSearchParams();
      q.set('limit', String(limit));
      if (mode === 'open') q.set('resolved', 'false');
      if (mode === 'resolved') q.set('resolved', 'true');
      const res = await fetch(`/moderation/events?${{q.toString()}}`);
      const data = await res.json();
      moderationResults.textContent = res.ok ? JSON.stringify(data, null, 2) : `Error: ${{data.detail || res.status}}`;
    }}

    async function resolveModeration() {{
      const id = document.getElementById('moderation-event-id').value;
      if (!id) {{ alert('Event ID required'); return; }}
      const notes = document.getElementById('moderation-notes').value || '';
      const res = await fetch(`/moderation/events/${{id}}/resolve`, {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{notes}})
      }});
      const data = await res.json();
      if (!res.ok) {{
        alert(`Resolve failed: ${{data.detail || res.status}}`);
        return;
      }}
      showToast('Moderation event resolved', 'success');
      loadModeration();
      loadCrisisAlerts();
    }}

    async function deleteCurrentUser() {{
      const uid = getActiveUserId();
      if (!uid) {{ alert('Select a user first'); return; }}
      if (!confirm(`Delete user ${{uid}} and related data?`)) return;
      const res = await fetch(`/users/${{uid}}`, {{ method: 'DELETE' }});
      const data = await res.json();
      if (!res.ok) {{
        alert(`Delete failed: ${{data.detail || res.status}}`);
        return;
      }}
      selectedUserIds.delete(uid);
      selectedUserId = null;
      userIdInput.value = '';
      showToast(`Deleted user ${{uid}}`, 'warning');
      loadUsers();
      loadUserNames();
    }}

    async function deleteSelectedUsers() {{
      const ids = Array.from(selectedUserIds);
      if (ids.length === 0) {{ alert('No users selected'); return; }}
      if (!confirm(`Delete ${{ids.length}} selected user(s)?`)) return;
      const res = await fetch('/users/delete_many', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{ user_ids: ids }})
      }});
      const data = await res.json();
      if (!res.ok) {{
        alert(`Delete failed: ${{data.detail || res.status}}`);
        return;
      }}
      selectedUserIds = new Set();
      showToast(`Deleted ${{data.count}} users`, 'warning');
      loadUsers();
      loadUserNames();
    }}

    async function highriskCall(url, body) {{
      highriskResults.textContent = '';
      const res = await fetch(url, {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify(body)
      }});
      const data = await res.json().catch(() => ({{}}));
      highriskResults.textContent = res.ok ? JSON.stringify(data, null, 2) : `Error: ${{data.detail || res.status}}`;
    }}

    trustForm?.addEventListener('submit', async (e) => {{
      e.preventDefault();
      const token = document.getElementById('trust-token').value;
      if (!token) {{ alert('Enter trust token'); return; }}
      const res = await fetch(`/auth/trust?token=${{encodeURIComponent(token)}}`);
      if (res.ok) {{
        alert('Trust cookie set. Reload the page.');
      }} else {{
        alert('Invalid trust token');
      }}
    }});

    document.getElementById('btn-refresh-status').onclick = () => {{
      loadStatus();
      loadModuleStatus();
      loadSystemMetrics();
      loadAppMetrics();
      loadLatencyLive();
      loadScheduler();
      loadTelegram();
      loadAlerts();
      loadCrisisAlerts();
      loadModeration();
      loadUsers();
    }};
    document.getElementById('btn-users-next')?.addEventListener('click', () => {{ usersOffset += usersLimit; loadUsers(); }});
    document.getElementById('btn-users-prev')?.addEventListener('click', () => {{ usersOffset = Math.max(0, usersOffset - usersLimit); loadUsers(); }});
    document.getElementById('btn-load-user').onclick = () => {{
      const id = getActiveUserId();
      if (!id) {{ alert('Select a user first'); return; }}
      togglePanel(userDetail, () => loadUserDetail(id));
    }};
    document.getElementById('btn-load-user-messages').onclick = () => {{
      const id = getActiveUserId();
      if (!id) {{ alert('Select a user first'); return; }}
      togglePanel(userMessages, () => loadUserMessages(id));
    }};
    document.getElementById('btn-load-user-reminders').onclick = () => {{
      const id = getActiveUserId();
      if (!id) {{ alert('Select a user first'); return; }}
      togglePanel(userReminders, () => loadUserReminders(id));
    }};
    document.getElementById('btn-load-user-images').onclick = () => {{
      const id = getActiveUserId();
      if (!id) {{ alert('Select a user first'); return; }}
      togglePanel(userImages, () => loadUserImages(id));
    }};
    document.getElementById('btn-delete-user').onclick = () => deleteCurrentUser();
    document.getElementById('btn-users-delete-selected').onclick = () => deleteSelectedUsers();
    document.getElementById('btn-users-select-all').onclick = () => {{
      const boxes = document.querySelectorAll('.user-select-box');
      const shouldCheckAll = Array.from(boxes).some(b => !b.checked);
      boxes.forEach(box => {{
        box.checked = shouldCheckAll;
        const uid = parseInt(box.dataset.userId, 10);
        if (shouldCheckAll) selectedUserIds.add(uid);
        else selectedUserIds.delete(uid);
      }});
    }};
    document.getElementById('btn-memory-search').onclick = () => memorySearch();
    document.getElementById('btn-psych-load').onclick = () => loadPsych();
    document.getElementById('btn-psych-reanalyze').onclick = () => reanalyzePsych();
    document.getElementById('psych-user').onchange = async () => {{ await loadPsychHistory(); await loadPsych(); }};
    document.getElementById('psych-history').onchange = () => loadPsych();
    document.getElementById('btn-export-user').onclick = () => exportUserData();
    document.getElementById('btn-feedback-load').onclick = () => loadFeedback();
    document.getElementById('btn-feedback-update').onclick = () => updateFeedbackItem();
    document.getElementById('btn-latency-refresh').onclick = () => loadLatencyLive();
    document.getElementById('btn-model-save').onclick = () => saveModels();
    document.getElementById('model-chat').addEventListener('focus', loadModels);
    document.getElementById('model-embed').addEventListener('focus', loadModels);
    document.getElementById('model-vision').addEventListener('focus', loadModels);
    document.getElementById('model-chat').addEventListener('click', loadModels);
    document.getElementById('model-embed').addEventListener('click', loadModels);
    document.getElementById('model-vision').addEventListener('click', loadModels);
    document.getElementById('btn-model-pull').onclick = () => pullModel();
    document.getElementById('btn-moderation-load').onclick = () => loadModeration();
    document.getElementById('btn-moderation-resolve').onclick = () => resolveModeration();
    document.getElementById('btn-crisis-refresh').onclick = () => loadCrisisAlerts();
    document.getElementById('btn-reminder-load-user').onclick = () => loadRemindersForSelectedUser();
    document.getElementById('btn-reminder-clear-all').onclick = () => clearAllReminders();
    document.getElementById('reminder-user-select').onchange = () => loadRemindersForSelectedUser();
    document.getElementById('btn-reminder-create').onclick = () => createReminderFromForm();
    document.getElementById('btn-misc-db-stats').onclick = async () => {{
      const res = await fetch('/stats/db');
      const data = await res.json();
      miscResults.textContent = JSON.stringify(data, null, 2);
    }};
    document.getElementById('btn-misc-users').onclick = async () => {{
      const res = await fetch('/users/names');
      const data = await res.json();
      miscResults.textContent = JSON.stringify(data, null, 2);
    }};
    document.getElementById('btn-llm-console').onclick = () => {{
      if (!confirm('⚠️ WARNING: You are about to use the LLM Console (Legacy)\\n\\nThis tool is deprecated. Use Enhanced LLM Console instead.\\n\\nContinue?')) return;
      if (!confirm('Final confirmation: Execute LLM Console (Legacy)?')) return;
      highriskCall('/highrisk/llm_console', {{prompt:'(admin) stub prompt', confirm:true}});
      showToast('LLM Console (Legacy) executed', 'warning');
    }};
    document.getElementById('btn-db-edit').onclick = () => {{
      if (!confirm('⚠️ DANGER: You are about to directly edit the database\\n\\nThis tool is deprecated. Use Enhanced LLM Console instead.\\n\\nThis can PERMANENTLY modify data!\\n\\nContinue?')) return;
      if (!confirm('Second confirmation: Are you SURE you want to edit the database?')) return;
      if (!confirm('FINAL confirmation: Execute DB Edit?')) return;
      highriskCall('/highrisk/db_edit', {{table:'users', where:'1=0', set:{{display_name:'(admin noop)'}}, confirm:true, dry_run:false}});
      showToast('DB Edit (Legacy) executed', 'warning');
    }};
    document.getElementById('btn-omni-broadcast').onclick = () => {{
      if (!confirm('⚠️ WARNING: You are about to broadcast to ALL users\\n\\nThis tool is deprecated. Use Enhanced LLM Console instead.\\n\\nThis will send a message to ALL active channels!\\n\\nContinue?')) return;
      if (!confirm('Second confirmation: Send broadcast to ALL users?')) return;
      if (!confirm('FINAL confirmation: Execute Omni Broadcast?')) return;
      highriskCall('/highrisk/omni_broadcast', {{message:'(admin) stub broadcast', channels:['telegram','discord'], confirm:true, dry_run:false}});
      showToast('Omni Broadcast (Legacy) executed', 'warning');
    }};
    // periodic refresh for status panels
    setInterval(() => {{
      loadStatus();
      loadModuleStatus();
      loadScheduler();
      loadTelegram();
      loadAlerts();
      const analyticsTab = document.getElementById('tab-analytics');
      if (analyticsTab && analyticsTab.classList.contains('active')) {{
        loadLatencyLive();
      }}
    }}, 15000);

    document.getElementById('btn-restart').onclick = async () => {{
      if (!dangerToggle.checked) {{ alert('Enable dangerous tools to perform this action.'); return; }}
      const res = await fetch('/actions/restart', {{method:'POST'}});
      if (res.ok) alert('Restart requested'); else alert('Failed to request restart');
    }};

    // initial hydration on load
    document.addEventListener('DOMContentLoaded', () => {{
      loadStatus();
      loadModuleStatus();
      loadSystemMetrics();
      loadAppMetrics();
      loadLatencyLive();
      loadScheduler();
      loadTelegram();
      loadAlerts();
      loadCrisisAlerts();
      loadModeration();
      loadUsers();
      loadUserNames();
      loadModels();
    }});

    document.getElementById('btn-reminder-enable').onclick = () => {{
      const id = reminderIdInput.value;
      if (!id) {{ alert('Reminder ID required'); return; }}
      reminderAction(`/reminders/${{id}}/enable`);
    }};
    document.getElementById('btn-reminder-disable').onclick = () => {{
      const id = reminderIdInput.value;
      if (!id) {{ alert('Reminder ID required'); return; }}
      reminderAction(`/reminders/${{id}}/disable`);
    }};
    document.getElementById('btn-reminder-delete').onclick = () => {{
      const id = reminderIdInput.value;
      if (!id) {{ alert('Reminder ID required'); return; }}
      reminderAction(`/reminders/${{id}}/delete`);
    }};
    document.getElementById('btn-reminder-update').onclick = () => {{
      const id = reminderIdInput.value;
      if (!id) {{ alert('Reminder ID required'); return; }}
      const body = {{}};
      if (reminderTextInput.value) body.text = reminderTextInput.value;
      if (reminderTimeInput.value) body.next_run_at = new Date(reminderTimeInput.value).toISOString();
      if (reminderCronInput.value) body.cadence_cron = reminderCronInput.value;
      body.metadata = {{
        mode: reminderModeInput.value || 'one_off_exact',
        fuzz_minutes: reminderFuzzInput.value ? parseInt(reminderFuzzInput.value, 10) : undefined
      }};
      body.enabled = reminderEnabledInput.checked;
      reminderAction(`/reminders/${{id}}/update`, body);
    }};

    document.getElementById('btn-disable').onclick = async () => {{
      if (!dangerToggle.checked) {{ alert('Enable dangerous tools to perform this action.'); return; }}
      const res = await fetch('/actions/disable_bot', {{method:'POST'}});
      alert(res.ok ? 'Disable requested' : 'Failed to disable');
    }};

    document.getElementById('btn-enable').onclick = async () => {{
      const res = await fetch('/actions/enable_bot', {{method:'POST'}});
      alert(res.ok ? 'Enable requested' : 'Failed to enable');
    }};

    document.getElementById('btn-shutdown-admin').onclick = async () => {{
      if (!confirm('Stop admin server now?')) return;
      await fetch('/actions/shutdown_admin', {{method:'POST'}});
      showToast('Admin shutting down...', 'warning');
    }};

    document.getElementById('btn-broadcast').onclick = async () => {{
      if (!dangerToggle.checked) {{ alert('Enable dangerous tools to perform this action.'); return; }}
      const text = document.getElementById('broadcast-text').value;
      const dry = document.getElementById('broadcast-dryrun').checked;
      const res = await fetch('/actions/broadcast', {{
        method:'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{text, dry_run: dry}})
      }});
      const data = await res.json();
      alert(JSON.stringify(data));
    }};

    trustForm.onsubmit = async (e) => {{
      e.preventDefault();
      const token = document.getElementById('trust-token').value;
      const res = await fetch('/auth/trust', {{
        method:'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{token}})
      }});
      if (res.ok) {{ alert('Device trusted'); }} else {{ alert('Failed to trust device'); }}
    }};

    function startFeed() {{
      const evtSource = new EventSource('/live/stream');
      evtSource.onmessage = function(e) {{
        const div = document.createElement('div');
        div.textContent = e.data;
        feedEl.appendChild(div);
        feedEl.scrollTop = feedEl.scrollHeight;
      }};
      evtSource.onerror = function() {{
        const div = document.createElement('div');
        div.textContent = '[feed] disconnected';
        feedEl.appendChild(div);
      }};
    }}

    loadStatus();
    loadUsers();
    loadUserNames();
    loadModuleStatus();
    loadSystemMetrics();
    loadAppMetrics();
    loadLatencyLive();
    loadScheduler();
    loadTelegram();
    loadAlerts();
    loadCrisisAlerts();
    loadModeration();
    startFeed();

    // ========================================================================
    // ENHANCED LLM CONSOLE
    // ========================================================================

    let consoleSessionId = null;
    let consoleProcessing = false;

    const consoleHistory = document.getElementById('console-chat-history');
    const consoleInput = document.getElementById('console-input');
    const consoleSendBtn = document.getElementById('btn-console-send');
    const consoleStopBtn = document.getElementById('btn-console-stop');
    const consoleSessionInfo = document.getElementById('console-session-info');
    const consoleToolsEnabled = document.getElementById('console-tools-enabled');
    const consoleExternalFiles = document.getElementById('console-external-files');

    function addConsoleMessage(role, content, toolExecutions = []) {{
      const msgDiv = document.createElement('div');
      msgDiv.style.marginBottom = '12px';
      msgDiv.style.paddingBottom = '12px';
      msgDiv.style.borderBottom = '1px solid #1f2937';

      const roleLabel = document.createElement('div');
      roleLabel.style.fontWeight = '600';
      roleLabel.style.marginBottom = '4px';

      if (role === 'user') {{
        roleLabel.style.color = '#93c5fd';
        roleLabel.textContent = '👤 You:';
      }} else if (role === 'assistant') {{
        roleLabel.style.color = '#a78bfa';
        roleLabel.textContent = '🤖 Assistant:';
      }} else if (role === 'system') {{
        roleLabel.style.color = '#f59e0b';
        roleLabel.textContent = '⚙️ System:';
      }}

      const contentDiv = document.createElement('div');
      contentDiv.style.color = '#e2e8f0';
      contentDiv.style.whiteSpace = 'pre-wrap';
      contentDiv.style.wordBreak = 'break-word';
      contentDiv.textContent = content;

      msgDiv.appendChild(roleLabel);
      msgDiv.appendChild(contentDiv);

      // Show tool executions
      if (toolExecutions && toolExecutions.length > 0) {{
        const toolsDiv = document.createElement('div');
        toolsDiv.style.marginTop = '8px';
        toolsDiv.style.padding = '8px';
        toolsDiv.style.background = '#1f2937';
        toolsDiv.style.borderRadius = '4px';
        toolsDiv.style.fontSize = '12px';

        toolExecutions.forEach(tool => {{
          const toolLine = document.createElement('div');
          toolLine.style.marginBottom = '4px';
          const icon = tool.success ? '✓' : '✗';
          const color = tool.success ? '#10b981' : '#ef4444';
          toolLine.innerHTML = `<span style="color:${{color}}">${{icon}}</span> <span style="color:#94a3b8">${{tool.tool}}</span>`;
          toolsDiv.appendChild(toolLine);
        }});

        msgDiv.appendChild(toolsDiv);
      }}

      consoleHistory.appendChild(msgDiv);
      consoleHistory.scrollTop = consoleHistory.scrollHeight;
    }}

    async function sendConsoleMessage() {{
      const message = consoleInput.value.trim();
      if (!message || consoleProcessing) return;

      consoleProcessing = true;
      consoleSendBtn.disabled = true;
      consoleStopBtn.disabled = false;

      // Add user message to chat
      addConsoleMessage('user', message);
      consoleInput.value = '';

      try {{
        const body = {{
          message: message,
          session_id: consoleSessionId,
          tools_enabled: consoleToolsEnabled.checked,
          allow_external_files: consoleExternalFiles.checked,
          max_iterations: 5
        }};

        const res = await fetch('/highrisk/llm_console_enhanced', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(body)
        }});

        const data = await res.json();

        if (!res.ok) {{
          addConsoleMessage('system', `Error: ${{data.detail || res.statusText}}`);
          return;
        }}

        // Update session ID
        consoleSessionId = data.session_id;
        consoleSessionInfo.textContent = `Session: ${{data.session_id}} | Messages: ${{data.conversation_length}}`;

        // Add assistant response
        addConsoleMessage('assistant', data.response, data.tool_executions);

        // Show rollback notifications
        if (data.rolled_back_edits && data.rolled_back_edits.length > 0) {{
          addConsoleMessage('system', `⚠️ Auto-rolled back ${{data.rolled_back_edits.length}} expired edit(s)`);
        }}

      }} catch (err) {{
        addConsoleMessage('system', `Error: ${{err.message}}`);
      }} finally {{
        consoleProcessing = false;
        consoleSendBtn.disabled = false;
        consoleStopBtn.disabled = true;
      }}
    }}

    async function clearConsoleHistory() {{
      if (!confirm('Clear conversation history?')) return;

      try {{
        const res = await fetch(`/highrisk/llm_console_clear?session_id=${{consoleSessionId || ''}}`, {{
          method: 'POST'
        }});
        const data = await res.json();

        consoleHistory.innerHTML = '<div style="color: #94a3b8; font-style: italic;">Console cleared. Type a message to begin...</div>';
        consoleSessionId = null;
        consoleSessionInfo.textContent = '';
      }} catch (err) {{
        alert(`Failed to clear: ${{err.message}}`);
      }}
    }}

    async function viewConsoleSessions() {{
      try {{
        const res = await fetch('/highrisk/llm_console_sessions');
        const data = await res.json();
        const sessions = data.sessions || [];

        if (sessions.length === 0) {{
          alert('No active sessions');
          return;
        }}

        const sessionList = sessions.map(s =>
          `${{s.session_id}}: ${{s.message_count}} messages\\n  "${{s.last_message}}"`
        ).join('\\n\\n');

        alert(`Active Sessions:\\n\\n${{sessionList}}`);
      }} catch (err) {{
        alert(`Failed to load sessions: ${{err.message}}`);
      }}
    }}

    // Event listeners for Enhanced LLM Console
    consoleSendBtn?.addEventListener('click', sendConsoleMessage);
    consoleInput?.addEventListener('keydown', (e) => {{
      if (e.key === 'Enter' && e.ctrlKey) {{
        e.preventDefault();
        sendConsoleMessage();
      }}
    }});
    document.getElementById('btn-console-clear')?.addEventListener('click', clearConsoleHistory);
    document.getElementById('btn-console-sessions')?.addEventListener('click', viewConsoleSessions);

    // ========================================================================
    // MEDIA GENERATION
    // ========================================================================

    async function generateImage() {{
      const prompt = document.getElementById('media-prompt').value.trim();
      if (!prompt) {{
        alert('Please enter a prompt');
        return;
      }}

      const statusEl = document.getElementById('media-generation-status');
      const btn = document.getElementById('btn-generate-image');
      const ownerValue = document.getElementById('media-user-id').value || '';
      const ownerId = ownerValue ? parseInt(ownerValue) : undefined;
      if (ownerValue && (!Number.isFinite(ownerId) || ownerId <= 0)) {{
        alert('Please select a valid owner or leave it blank');
        return;
      }}

      try {{
        btn.disabled = true;
        statusEl.textContent = 'Generating image... This may take 30-90 seconds depending on model...';
        statusEl.style.color = '#f59e0b';

        const body = {{
          prompt: prompt,
          user_id: ownerId,
          model: document.getElementById('media-model').value,
          negative_prompt: document.getElementById('media-negative').value || undefined,
          width: parseInt(document.getElementById('media-width').value),
          height: parseInt(document.getElementById('media-height').value),
          steps: parseInt(document.getElementById('media-steps').value),
          guidance_scale: parseFloat(document.getElementById('media-guidance').value),
          seed: document.getElementById('media-seed').value ? parseInt(document.getElementById('media-seed').value) : undefined
        }};

        const res = await fetch('/media/generate', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(body)
        }});

        const data = await res.json();

        if (!res.ok) {{
          statusEl.textContent = `Error: ${{data.detail || 'Generation failed'}}`;
          statusEl.style.color = '#ef4444';
          showToast(`Image generation failed: ${{data.detail || 'Unknown error'}}`, 'error');
          return;
        }}

        if (data.status === 'success') {{
          statusEl.textContent = `✓ Image generated in ${{(data.generation_time_ms / 1000).toFixed(1)}}s - ${{(data.file_size / 1024 / 1024).toFixed(2)}}MB`;
          statusEl.style.color = '#10b981';
          showToast(`Image generated successfully in ${{(data.generation_time_ms / 1000).toFixed(1)}}s!`, 'success');
          loadMediaHistory();
          refreshVRAM();
        }} else {{
          statusEl.textContent = `Error: ${{data.error}}`;
          statusEl.style.color = '#ef4444';
          showToast(`Image generation error: ${{data.error}}`, 'error');
        }}

      }} catch (err) {{
        statusEl.textContent = `Error: ${{err.message}}`;
        statusEl.style.color = '#ef4444';
        showToast(`Unexpected error: ${{err.message}}`, 'error');
      }} finally {{
        btn.disabled = false;
      }}
    }}

    async function refreshVRAM() {{
      try {{
        const res = await fetch('/media/vram');
        const data = await res.json();

        if (data.available) {{
          const vramText = `VRAM Status:
Total: ${{data.total_gb}}GB
Allocated: ${{data.allocated_gb}}GB
Reserved: ${{data.reserved_gb}}GB
Free: ${{data.free_gb}}GB
Usage: ${{data.percent_used}}%

Model Loaded: ${{data.model_loaded || 'None'}}`;
          document.getElementById('media-vram').textContent = vramText;
        }} else {{
          document.getElementById('media-vram').textContent = `CUDA not available\\n${{data.message || data.error}}`;
        }}
      }} catch (err) {{
        document.getElementById('media-vram').textContent = `Error: ${{err.message}}`;
      }}
    }}

    async function unloadModel() {{
      if (!confirm('Unload current model to free VRAM?')) return;

      try {{
        const res = await fetch('/media/unload', {{ method: 'POST' }});
        const data = await res.json();
        alert(data.message || 'Model unloaded');
        refreshVRAM();
      }} catch (err) {{
        alert(`Error: ${{err.message}}`);
      }}
    }}

    async function loadMediaHistory() {{
      const userIdInput = document.getElementById('media-history-user').value;
      const userId = userIdInput ? parseInt(userIdInput) : undefined;

      try {{
        const url = userId ? `/media/history?user_id=${{userId}}&limit=50` : '/media/history?limit=50';
        const res = await fetch(url);
        const data = await res.json();

        const historyEl = document.getElementById('media-history');

        if (!data.history || data.history.length === 0) {{
          historyEl.innerHTML = '<div style="color: #94a3b8;">No images generated yet</div>';
          return;
        }}

        historyEl.innerHTML = '';

        data.history.forEach(item => {{
          const card = document.createElement('div');
          card.style.background = '#111827';
          card.style.border = '1px solid #1f2937';
          card.style.borderRadius = '8px';
          card.style.padding = '12px';
          card.style.overflow = 'hidden';

          const status = item.status === 'completed' ? '✓' : item.status === 'failed' ? '✗' : '⏳';
          const statusColor = item.status === 'completed' ? '#10b981' : item.status === 'failed' ? '#ef4444' : '#f59e0b';

          // Image preview for completed images
          const imagePreview = item.status === 'completed' && item.file_path ? `
            <img
              src="/media/image/${{item.id}}"
              alt="Generated image"
              style="width: 100%; height: 200px; object-fit: cover; border-radius: 6px; margin-bottom: 8px; cursor: pointer;"
              onclick="window.open('/media/image/${{item.id}}', '_blank')"
              onerror="this.style.display='none'; this.nextElementSibling.style.display='block';"
            />
            <div style="display: none; padding: 40px; text-align: center; background: #1f2937; border-radius: 6px; margin-bottom: 8px; color: #6b7280;">
              Image not available
            </div>
          ` : '';

          card.innerHTML = `
            ${{imagePreview}}
            <div style="font-weight: 600; color: ${{statusColor}}">${{status}} ${{item.model_used || 'Unknown'}}</div>
            <div style="font-size: 12px; color: #94a3b8; margin: 4px 0;" title="${{item.prompt}}">${{item.prompt.substring(0, 100)}}${{item.prompt.length > 100 ? '...' : ''}}</div>
            <div style="font-size: 11px; color: #6b7280;">
              ${{userNameMap[String(item.user_id)] || ('User ' + item.user_id)}} | ${{new Date(item.created_at).toLocaleString()}}
              ${{item.generation_time_ms ? ` | ${{(item.generation_time_ms / 1000).toFixed(1)}}s` : ''}}
            </div>
            ${{item.file_size ? `<div style="font-size: 11px; color: #6b7280; margin-top: 4px;">Size: ${{(item.file_size / 1024).toFixed(1)}} KB</div>` : ''}}
            ${{item.error_message ? `<div style="margin-top: 8px; color: #ef4444; font-size: 11px;">${{item.error_message}}</div>` : ''}}
          `;

          historyEl.appendChild(card);
        }});

      }} catch (err) {{
        alert(`Error loading history: ${{err.message}}`);
      }}
    }}

    // Event listeners for media generation
    document.getElementById('btn-generate-image')?.addEventListener('click', generateImage);
    document.getElementById('btn-refresh-vram')?.addEventListener('click', refreshVRAM);
    document.getElementById('btn-unload-model')?.addEventListener('click', unloadModel);
    document.getElementById('btn-load-history')?.addEventListener('click', loadMediaHistory);

    // Auto-refresh VRAM when switching to Media tab
    document.querySelectorAll('.tab-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        if (btn.dataset.tab === 'media') {{
          refreshVRAM();
          loadMediaHistory();
        }}
      }});
    }});

  </script>
</body>
</html>
    """
    return HTMLResponse(content=html)


def run(host: str = "0.0.0.0", port: int = 8200) -> None:
    import uvicorn

    logger.info("Starting admin server on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import os

    parser = argparse.ArgumentParser(
        description="Start the Wellness admin HTTP server."
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("ADMIN_HOST", "0.0.0.0"),
        help="bind host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ADMIN_PORT", "8200")),
        help="bind port (default: 8200)",
    )
    args = parser.parse_args()
    run(host=args.host, port=args.port)
