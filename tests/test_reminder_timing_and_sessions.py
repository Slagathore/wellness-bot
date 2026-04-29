from __future__ import annotations

from datetime import datetime, timedelta

from app.config import settings
from app.db import db_ro, db_rw
from app.domain.reminders.timezone import normalize_user_local_reminder_time
from app.runtime.services.user_sessions import UserSessionStore


def test_normalize_user_local_reminder_time_respects_sleep_window() -> None:
    reference_local = datetime(2026, 3, 21, 20, 0)
    candidate_local = datetime(2026, 3, 22, 2, 0)

    adjusted = normalize_user_local_reminder_time(
        candidate_local,
        reference_local=reference_local,
        sleep_window=((23, 0), (7, 0)),
        min_lead_minutes=30,
    )

    assert adjusted == datetime(2026, 3, 22, 7, 45)


def test_user_session_store_archives_previous_active_session(
    test_config, test_user
) -> None:
    user_id, _ = test_user
    cfg = settings()
    store = UserSessionStore(
        data_root=cfg.data_root,
        ctx_token_budget=cfg.ctx_token_budget,
    )

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO sessions(user_id, scope, status, started_at, ctx_token_budget)
            VALUES(?, 'roleplay', 'active', CURRENT_TIMESTAMP, ?)
            """,
            (user_id, cfg.ctx_token_budget),
        )
        prior_session_id = conn.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]

    new_session_id = store.get_or_create_session(user_id)

    with db_ro() as conn:
        prior_status = conn.execute(
            "SELECT status FROM sessions WHERE id = ?",
            (prior_session_id,),
        ).fetchone()["status"]
        new_row = conn.execute(
            "SELECT scope, status FROM sessions WHERE id = ?",
            (new_session_id,),
        ).fetchone()

    assert prior_status == "archived"
    assert new_row["scope"] == "standard"
    assert new_row["status"] == "active"
