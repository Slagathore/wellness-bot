"""E2E test for handling large historical sleep uploads."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta

from app.config import settings
from app.db import db_ro, db_rw


def test_large_historical_sleep_upload(test_config, test_user, tmp_path):
    user_id, telegram_user_id = test_user

    csv_path = tmp_path / "oura.csv"
    start_date = datetime.utcnow().date() - timedelta(days=365)
    with open(csv_path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Date", "Total", "Deep", "REM", "Score"])
        for i in range(365):
            day = start_date + timedelta(days=i)
            writer.writerow([day.isoformat(), 28800, 7200, 7200, 85])

    with open(csv_path, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = [row for row in reader]

    with db_rw() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO sleep_data(user_id, source, date, metrics)
                VALUES(?, 'oura', ?, ?)
                """,
                (
                    user_id,
                    row["Date"],
                    str(
                        {
                            "total_sleep_duration": int(row["Total"]),
                            "deep_sleep": int(row["Deep"]),
                            "rem_sleep": int(row["REM"]),
                            "sleep_score": int(row["Score"]),
                        }
                    ),
                ),
            )

        session = conn.execute(
            "SELECT id FROM sessions WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()
        if session:
            session_id = session["id"]
        else:
            conn.execute(
                "INSERT INTO sessions(user_id, status, ctx_token_budget) VALUES(?, 'active', ?)",
                (user_id, settings().ctx_token_budget),
            )
            session_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()[
                "id"
            ]

        instruction = (
            "TASK: SLEEP DATA INTEGRATION\n"
            f"User uploaded {len(rows) / 365:.1f} years of sleep data."
            "Instructions: Ask whether to integrate historical data or just recent trends."
        )
        conn.execute(
            "INSERT INTO messages(user_id, session_id, role, content) VALUES(?, ?, 'server_event', ?)",
            (user_id, session_id, instruction),
        )

    with db_ro() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM sleep_data WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]
        assert count == 365
        event = conn.execute(
            "SELECT content FROM messages WHERE user_id = ? AND role = 'server_event'",
            (user_id,),
        ).fetchone()
        assert event and "SLEEP DATA" in event["content"]
