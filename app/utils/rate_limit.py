"""Anti-abuse safeguards and rate limiting helpers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone as dt_timezone

from app.db import db_ro, db_rw
from app.utils.time_utils import (
    format_operator_datetime,
    normalize_operator,
    operator_now,
)


def check_and_enforce_rate_limit(user_id: int) -> bool:
    """Return True if the user is currently blocked or newly penalised."""

    now = operator_now()

    with db_ro() as conn:
        record = conn.execute(
            """
            SELECT status, blocked_until, window_end
            FROM rate_limits
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()

    last_window_end = None

    if record:
        status = record["status"]
        blocked_until = record["blocked_until"]
        if hasattr(record, "keys") and "window_end" in record.keys():
            window_end = record["window_end"]
        else:
            window_end = None
        if window_end:
            last_window_end = _parse_operator_iso(window_end)
        if status == "banned":
            return True
        if status == "blocked" and blocked_until:
            blocked_until_dt = _parse_operator_iso(blocked_until)
            if blocked_until_dt and blocked_until_dt > now:
                return True

    window_start = format_operator_datetime(now - timedelta(seconds=60))
    with db_ro() as conn:
        recent = conn.execute(
            """
            SELECT id, timestamp
            FROM messages
            WHERE user_id = ? AND role = 'user' AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (user_id, window_start),
        ).fetchall()

    filtered_recent: list[tuple[dict, datetime]] = []
    for row in recent:
        ts = _parse_operator_iso(row["timestamp"])
        if ts is None:
            continue
        if last_window_end and ts <= last_window_end:
            continue
        filtered_recent.append((row, ts))

    if len(filtered_recent) < 10:
        return False

    first_ts = filtered_recent[0][1]
    last_ts = filtered_recent[-1][1]
    span = max((last_ts - first_ts).total_seconds(), 1)
    rate = len(filtered_recent) / span
    first_ts_str = filtered_recent[0][0]["timestamp"]
    last_ts_str = filtered_recent[-1][0]["timestamp"]

    with db_ro() as conn:
        replies = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM messages
            WHERE user_id = ? AND role = 'assistant' AND timestamp >= ?
            """,
            (user_id, first_ts_str),
        ).fetchone()["c"]

    burst_without_reply = replies == 0
    excessive_rate = rate > 1.0

    if burst_without_reply or excessive_rate:
        apply_rate_limit_penalty(user_id, first_ts_str, last_ts_str)
        return True

    return False


def apply_rate_limit_penalty(user_id: int, window_start: str, window_end: str) -> None:
    """Escalate the penalty ladder for abusive behaviour."""

    with db_ro() as conn:
        previous = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM rate_limits
            WHERE user_id = ? AND status IN ('warned', 'blocked')
            """,
            (user_id,),
        ).fetchone()["c"]

    if previous:
        with db_rw() as conn:
            conn.execute(
                """
                INSERT INTO rate_limits(user_id, window_start, window_end, status, violations)
                VALUES(?, ?, ?, 'banned', 2)
                """,
                (user_id, window_start, window_end),
            )
            conn.execute(
                """
                INSERT INTO moderation_events(user_id, event_type, severity, details)
                VALUES(?, 'rate_limit_ban', 5, '{"reason": "repeat_violation"}')
                """,
                (user_id,),
            )
        return

    blocked_until = format_operator_datetime(operator_now() + timedelta(minutes=5))
    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO rate_limits(user_id, window_start, window_end, status, blocked_until, violations)
            VALUES(?, ?, ?, 'blocked', ?, 1)
            """,
            (user_id, window_start, window_end, blocked_until),
        )
        conn.execute(
            """
            INSERT INTO moderation_events(user_id, event_type, severity, details)
            VALUES(?, 'rate_limit_warning', 3, '{"block_duration": "5min"}')
            """,
            (user_id,),
        )


def check_spam_patterns(user_id: int, text: str) -> str | None:
    """Run inexpensive spam heuristics against the text."""

    with db_ro() as conn:
        identical = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM messages
            WHERE user_id = ? AND content = ? AND timestamp >= datetime('now', '-5 minutes')
            """,
            (user_id, text),
        ).fetchone()["c"]
    if identical >= 3:
        return "identical_spam"

    url_pattern = re.compile(r"http[s]?://[\\w./?=&%-]+", re.IGNORECASE)
    urls = url_pattern.findall(text)
    if len(urls) > 5:
        return "link_spam"

    if len(text) > 20:
        caps_ratio = sum(1 for c in text if c.isupper()) / len(text)
        if caps_ratio > 0.7:
            return "caps_spam"

    return None


def check_malicious_code(text: str) -> bool:
    """Detect obviously dangerous code-injection attempts."""

    dangerous = [
        r"exec\s*\(",
        r"eval\s*\(",
        r"__import__",
        r"subprocess\\.",
        r"os\\.system",
        r"open\s*\(",
        r"\.read\(",
        r"\.write\(",
        r"pickle\\.loads",
        r"yaml\\.load\(",
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in dangerous)


def _parse_operator_iso(value: str | None) -> datetime | None:
    """Parse ISO timestamp strings and normalise to the operator timezone."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return normalize_operator(parsed)

    operator_candidate = normalize_operator(parsed)
    utc_candidate = normalize_operator(parsed.replace(tzinfo=dt_timezone.utc))
    now = operator_now()
    if abs((operator_candidate - now).total_seconds()) <= abs(
        (utc_candidate - now).total_seconds()
    ):
        return operator_candidate
    return utc_candidate
