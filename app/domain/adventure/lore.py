"""Adventure "memory": an LLM-maintained canon sheet in ``adventures.lore``.

The lore is periodically refreshed by folding new turns into a persistent
canon sheet (player identity, setting, objective, people, facts, recent canon
changes, open threads). ``adventures.settings.last_lore_message_id`` tracks how
far it has been consolidated so only new material is folded in.

This mirrors the Telegram adapter's ``_refresh_adventure_lore`` but is decoupled
from Telegram so the Mini App shares one memory implementation. Adventure memory
stays isolated from the wellness/psych pipeline by design.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, List

from app.db import db_ro, db_rw
from app.utils.prompt_safety import sanitize_untrusted_text

logger = logging.getLogger(__name__)

# How many new turns must accumulate before a refresh is worthwhile.
LORE_REFRESH_EVERY = 6
_MAX_FOLD_MESSAGES = 18

ChatFn = Callable[..., Awaitable[Dict[str, Any]]]

_LORE_SYSTEM = (
    "You maintain the canon sheet for an interactive text adventure. Update the "
    "lore so important characters, decisions, locations, factions, retcons, and "
    "unresolved threads persist across long sessions. Fold canon-changing RETCON "
    "or OOC directives into the lore as if they were always true. Ignore purely "
    "social OOC chatter that does not alter canon. Write plain text only. Keep "
    "these sections exactly: PLAYER IDENTITY, SETTING, CURRENT OBJECTIVE, "
    "IMPORTANT PEOPLE / FACTIONS, ESTABLISHED FACTS, RECENT CANON CHANGES, "
    "OPEN THREADS."
)


def _load_settings(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return dict(data) if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def lore_refresh_due(adventure_id: int) -> bool:
    """True when enough new (unconsolidated) messages have accumulated."""
    with db_ro() as conn:
        row = conn.execute(
            "SELECT settings FROM adventures WHERE id = ?", (adventure_id,)
        ).fetchone()
        if row is None:
            return False
        last_id = int(_load_settings(row["settings"]).get("last_lore_message_id") or 0)
        new_count = conn.execute(
            "SELECT COUNT(*) FROM adventure_messages WHERE adventure_id = ? AND id > ?",
            (adventure_id, last_id),
        ).fetchone()[0]
    return new_count >= LORE_REFRESH_EVERY


async def refresh_adventure_lore(
    adventure_id: int, *, chat_fn: ChatFn, reason: str = "turn"
) -> bool:
    """Fold new turns into the adventure's canon sheet. Returns True if updated."""
    with db_ro() as conn:
        adv = conn.execute(
            "SELECT title, lore, settings FROM adventures WHERE id = ?",
            (adventure_id,),
        ).fetchone()
        if adv is None:
            return False
        settings = _load_settings(adv["settings"])
        last_id = int(settings.get("last_lore_message_id") or 0)
        chars = conn.execute(
            "SELECT c.display_name, c.emoji, ac.role "
            "FROM adventure_characters ac JOIN custom_characters c ON c.id = ac.character_id "
            "WHERE ac.adventure_id = ?",
            (adventure_id,),
        ).fetchall()
        new_msgs: List[Any] = conn.execute(
            "SELECT id, role, content FROM adventure_messages "
            "WHERE adventure_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (adventure_id, last_id, _MAX_FOLD_MESSAGES),
        ).fetchall()

    if not new_msgs:
        return False

    current_lore = str(adv["lore"] or "").strip() or "No lore has been consolidated yet."
    message_lines = "\n".join(
        f"{m['role'].upper()}: {sanitize_untrusted_text(m['content'], limit=500)}"
        for m in new_msgs
    )
    title = sanitize_untrusted_text(adv["title"], limit=120)
    char_lines = "\n".join(
        f"- {(c['emoji'] or '🎭')} {sanitize_untrusted_text(c['display_name'], limit=80)} ({c['role']})"
        for c in chars
    ) or "- None recorded yet"
    player_role = sanitize_untrusted_text(
        str(settings.get("player_role") or ""), limit=300
    ) or "Not specified yet."

    lore_text = ""
    try:
        resp = await chat_fn(
            messages=[
                {"role": "system", "content": _LORE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Adventure: {title}\n"
                        f"Refresh reason: {reason}\n"
                        f"Player identity: {player_role}\n"
                        f"Known characters:\n{char_lines}\n\n"
                        f"Current lore:\n{current_lore}\n\n"
                        f"New canonical material to fold in:\n{message_lines}"
                    ),
                },
            ],
            options={"temperature": 0.2, "num_predict": 900},
        )
        if isinstance(resp, dict):
            lore_text = str(resp.get("text") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Adventure lore refresh failed for %s: %s", adventure_id, exc)

    if not lore_text:
        # Fallback: append the new material as recent canon changes so nothing
        # is silently lost when the summarizer fails.
        recent = "\n".join(
            f"- {m['role']}: {sanitize_untrusted_text(m['content'], limit=220)}"
            for m in new_msgs[-6:]
        )
        lore_text = f"{current_lore}\n\nRECENT CANON CHANGES:\n{recent}".strip()

    settings["last_lore_message_id"] = int(new_msgs[-1]["id"])
    with db_rw() as conn:
        conn.execute(
            "UPDATE adventures SET lore = ?, settings = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (lore_text.strip(), json.dumps(settings), adventure_id),
        )
    return True
