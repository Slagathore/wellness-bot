"""
Check-in repository for WorkFocus mode.
"""

from __future__ import annotations

from typing import Iterable, List

from app.domain.workfocus.service import WorkFocusCheckin
from app.infra.db.session import db_ro


class SqliteCheckinsRepository:
    def due_checkins(self, now_str: str) -> Iterable[WorkFocusCheckin]:
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT c.user_id, c.personalized_prompt, c.frequency, u.telegram_user_id
                FROM checkin_configs c
                JOIN users u ON c.user_id = u.id
                WHERE c.is_active = 1 AND c.next_checkin_at <= ?
                """,
                (now_str,),
            ).fetchall()
        items: List[WorkFocusCheckin] = []
        for row in rows:
            items.append(
                WorkFocusCheckin(
                    user_id=str(row["user_id"]),
                    telegram_user_id=row["telegram_user_id"],
                    prompt=row["personalized_prompt"] or "",
                    frequency=row["frequency"] or "",
                )
            )
        return items
