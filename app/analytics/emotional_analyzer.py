"""Emotional trend analysis utilities for nightly analytics."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from app.config import settings
from app.db import db_ro
from app.utils.time_utils import operator_now


@dataclass
class EmotionalSnapshot:
    avg_valence: float
    avg_arousal: float
    avg_dominance: float
    common_emotion: str | None
    sample_size: int


@dataclass
class EmotionalTrend:
    weekly_delta: float | None
    monthly_delta: float | None
    streak_direction: str


class EmotionalAnalyzer:
    """Generates emotional summaries and trend files for each user."""

    def __init__(self, lookback_days: int = 30) -> None:
        self.lookback_days = lookback_days

    def analyze_user(self, user_id: int, telegram_user_id: int) -> dict:
        """Return emotional summary data for the user."""

        snapshots = self._fetch_snapshots(user_id)
        summary = self._snapshot_summary(snapshots)
        trend = self._trend_summary(user_id)
        now = operator_now()
        output = {
            "generated_at": now.isoformat(),
            "lookback_days": self.lookback_days,
            "summary": summary.__dict__ if summary else None,
            "trend": trend.__dict__ if trend else None,
        }
        self._write_to_disk(telegram_user_id, output)
        return output

    def _fetch_snapshots(
        self, user_id: int
    ) -> list[tuple[datetime, float, float, float, str]]:
        cutoff = operator_now() - timedelta(days=self.lookback_days)
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT s.processed_at, s.valence, s.arousal, s.dominance, s.emotion_label
                FROM sentiments AS s
                JOIN messages AS m ON m.id = s.message_id
                WHERE m.user_id = ? AND COALESCE(m.scope, 'standard') = 'standard' AND s.processed_at >= ?
                ORDER BY s.processed_at ASC
                """,
                (user_id, cutoff.isoformat(sep=" ")),
            ).fetchall()
        return [
            (
                datetime.fromisoformat(row["processed_at"]),
                row["valence"],
                row["arousal"],
                row["dominance"],
                row["emotion_label"],
            )
            for row in rows
        ]

    def _snapshot_summary(
        self, snapshots: Iterable[tuple[datetime, float, float, float, str]]
    ) -> EmotionalSnapshot | None:
        data = list(snapshots)
        if not data:
            return None
        vals = [item[1] for item in data if item[1] is not None]
        arousals = [item[2] for item in data if item[2] is not None]
        dominances = [item[3] for item in data if item[3] is not None]
        emotions = [item[4] for item in data if item[4]]
        emotion_count = Counter(emotions)
        common_emotion = emotion_count.most_common(1)[0][0] if emotion_count else None
        return EmotionalSnapshot(
            avg_valence=sum(vals) / len(vals) if vals else 0.0,
            avg_arousal=sum(arousals) / len(arousals) if arousals else 0.0,
            avg_dominance=sum(dominances) / len(dominances) if dominances else 0.0,
            common_emotion=common_emotion,
            sample_size=len(data),
        )

    def _trend_summary(self, user_id: int) -> EmotionalTrend | None:
        with db_ro() as conn:
            rows = conn.execute(
                """
                SELECT s.valence, s.processed_at
                FROM sentiments AS s
                JOIN messages AS m ON m.id = s.message_id
                WHERE m.user_id = ? AND COALESCE(m.scope, 'standard') = 'standard'
                ORDER BY s.processed_at DESC
                LIMIT 60
                """,
                (user_id,),
            ).fetchall()
        if len(rows) < 10:
            return None

        entries = [
            (float(row["valence"]), datetime.fromisoformat(row["processed_at"]))
            for row in rows
            if row["valence"] is not None
        ]
        entries.reverse()

        now = operator_now()
        weekly_cutoff = now - timedelta(days=7)
        monthly_cutoff = now - timedelta(days=30)

        weekly_vals = [val for val, ts in entries if ts >= weekly_cutoff]
        monthly_vals = [val for val, ts in entries if ts >= monthly_cutoff]
        overall_vals = [val for val, _ in entries]

        weekly_delta = self._delta(weekly_vals)
        monthly_delta = self._delta(monthly_vals)
        streak_dir = self._streak_direction(overall_vals)
        return EmotionalTrend(weekly_delta, monthly_delta, streak_dir)

    def _delta(self, values: list[float]) -> float | None:
        if len(values) < 2:
            return None
        return values[-1] - values[0]

    def _streak_direction(self, values: list[float]) -> str:
        if len(values) < 2:
            return "stable"
        recent_avg = sum(values[-5:]) / min(len(values), 5)
        early_avg = sum(values[:5]) / min(len(values), 5)
        diff = recent_avg - early_avg
        if diff > 0.1:
            return "improving"
        if diff < -0.1:
            return "declining"
        return "stable"

    def _write_to_disk(self, telegram_user_id: int, payload: dict) -> None:
        base = (
            Path(settings().data_root)
            / "users"
            / str(telegram_user_id)
            / "derived"
            / "analytics"
        )
        base.mkdir(parents=True, exist_ok=True)
        filename = (
            base / f"emotional_summary_{operator_now().strftime('%Y-%m-%d')}.json"
        )
        filename.write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
