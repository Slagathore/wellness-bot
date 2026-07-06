"""Shared roleplay/adventure service backing the Mini App.

Reads and writes the same SQLite DB as the Telegram bot, and generates adventure
turns using the same LLM utilities and prompt-safety helpers. This is the
extraction point that lets a webapp drive adventures without duplicating the
Telegram adapter; the Telegram path can migrate onto it incrementally.

Adventure memory stays isolated to the ``adventure_messages`` table by design —
nothing here feeds the wellness/psych analysis pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.db import db_ro, db_rw
from app.domain.adventure.lore import (lore_refresh_due,
                                       refresh_adventure_lore)
from app.utils.ollama import chat_async
from app.utils.prompt_safety import sanitize_untrusted_text

logger = logging.getLogger(__name__)

_RECENT_TURNS = 24
_MAX_TURN_CHARS = 4000
_VALID_MODES = ("do", "say", "story")

# Keep references to fire-and-forget lore-refresh tasks so they aren't GC'd.
_LORE_TASKS: set = set()


class AdventureNotFound(Exception):
    """Raised when an adventure does not exist or is not owned by the user."""


class WebappService:
    """DB-backed operations for the roleplay/adventure Mini App."""

    # -- user resolution ---------------------------------------------------
    def ensure_user(
        self,
        telegram_user_id: int,
        *,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> int:
        with db_rw() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            if row:
                return int(row["id"])
            conn.execute(
                "INSERT INTO users(telegram_user_id, telegram_username, display_name) "
                "VALUES (?, ?, ?)",
                (telegram_user_id, username, first_name or username or "Player"),
            )
            return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    # -- reads -------------------------------------------------------------
    def list_characters(self, db_user_id: int) -> List[Dict[str, Any]]:
        with db_ro() as conn:
            rows = conn.execute(
                "SELECT id, display_name, emoji FROM custom_characters "
                "WHERE is_global = 1 OR creator_user_id = ? "
                "ORDER BY display_name ASC",
                (db_user_id,),
            ).fetchall()
        return [
            {"id": r["id"], "display_name": r["display_name"], "emoji": r["emoji"] or "🎭"}
            for r in rows
        ]

    def list_adventures(
        self, db_user_id: int, *, offset: int = 0, limit: int = 20
    ) -> Dict[str, Any]:
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 50))
        with db_ro() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM adventures WHERE user_id = ?", (db_user_id,)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT id, title, status, updated_at FROM adventures "
                "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (db_user_id, limit, offset),
            ).fetchall()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "status": r["status"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ],
        }

    def _owned_adventure(self, conn, db_user_id: int, adventure_id: int):
        row = conn.execute(
            "SELECT id, user_id, title, description, lore, status, settings "
            "FROM adventures WHERE id = ? AND user_id = ?",
            (adventure_id, db_user_id),
        ).fetchone()
        if row is None:
            raise AdventureNotFound(f"adventure {adventure_id} not found for user")
        return row

    def get_adventure(self, db_user_id: int, adventure_id: int) -> Dict[str, Any]:
        with db_ro() as conn:
            row = self._owned_adventure(conn, db_user_id, adventure_id)
            return {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "lore": row["lore"] or "",
            }

    def list_messages(
        self, db_user_id: int, adventure_id: int, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with db_ro() as conn:
            self._owned_adventure(conn, db_user_id, adventure_id)
            rows = conn.execute(
                "SELECT role, content, timestamp FROM adventure_messages "
                "WHERE adventure_id = ? ORDER BY id DESC LIMIT ?",
                (adventure_id, limit),
            ).fetchall()
        return [
            {"role": r["role"], "content": r["content"], "timestamp": r["timestamp"]}
            for r in reversed(rows)
        ]

    # -- turn generation ---------------------------------------------------
    def _nsfw_context(self, db_user_id: int) -> str:
        try:
            from app.orchestrator.persona_runtime import \
                _telegram_user_id_for_db_user
            from app.runtime.services.preferences import PreferenceService

            pref = PreferenceService()
            if not pref.get_nsfw_opt_in(db_user_id):
                return ""
            telegram_id = _telegram_user_id_for_db_user(db_user_id) or db_user_id
            prefs = pref.load_nsfw_preferences(user_id=db_user_id, telegram_id=telegram_id)
            return pref.format_nsfw_context(prefs) or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("webapp NSFW context load failed for %s: %s", db_user_id, exc)
            return ""

    def _build_system_prompt(self, adv, chars_text: str, nsfw_ctx: str) -> str:
        title = sanitize_untrusted_text(adv["title"], limit=120)
        lore = sanitize_untrusted_text(adv["lore"] or "", limit=_MAX_TURN_CHARS)
        prompt = (
            f"You are a creative roleplay narrator for an adventure called '{title}'.\n\n"
            f"WORLD LORE:\n{lore}\n\n"
            f"CHARACTERS IN THIS ADVENTURE:\n{sanitize_untrusted_text(chars_text)}\n\n"
            "INSTRUCTIONS:\n"
            "- Narrate in second person ('you') for the player.\n"
            "- Voice each character distinctly; advance the plot in response to the player.\n"
            "- Stay consistent with established lore and characters.\n"
            "- End at a natural pause that invites the player's next action.\n"
        )
        if nsfw_ctx.strip():
            prompt += f"\n\n{nsfw_ctx.strip()}"
        return prompt

    @staticmethod
    def _format_player_input(text: str, mode: str) -> tuple[str, str]:
        """Return (adventure_role, stored_content) for an AI-Dungeon-style input.

        - do:    an action ("I raise my lantern") -> role 'user'
        - say:   dialogue -> role 'user', quoted so it reads as speech
        - story: player-authored narration inserted directly -> role 'narrator'
        """
        text = text.strip()
        if mode == "say":
            body = text.strip().strip('"').strip()
            return "user", f'You say, "{body}"'
        if mode == "story":
            return "narrator", text
        return "user", text  # do

    def _resolve_model(self, db_user_id: int) -> Optional[str]:
        try:
            from app.orchestrator.persona_runtime import resolve_user_model

            return resolve_user_model(db_user_id)
        except Exception:  # noqa: BLE001
            return None

    def _load_turn_context(self, db_user_id: int, adventure_id: int):
        with db_ro() as conn:
            adv = self._owned_adventure(conn, db_user_id, adventure_id)
            char_rows = conn.execute(
                "SELECT c.display_name, c.emoji FROM adventure_characters ac "
                "JOIN custom_characters c ON c.id = ac.character_id "
                "WHERE ac.adventure_id = ?",
                (adventure_id,),
            ).fetchall()
            recent = conn.execute(
                "SELECT role, content FROM adventure_messages "
                "WHERE adventure_id = ? ORDER BY id DESC LIMIT ?",
                (adventure_id, _RECENT_TURNS),
            ).fetchall()
        chars_text = "\n".join(
            f"{(r['emoji'] or '🎭')} {r['display_name']}" for r in char_rows
        ) or "(No named characters yet.)"
        system_prompt = self._build_system_prompt(
            adv, chars_text, self._nsfw_context(db_user_id)
        )
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for r in reversed(recent):
            role = "assistant" if r["role"] in ("narrator", "character") else "user"
            messages.append({"role": role, "content": r["content"]})
        return messages

    async def _narrate(
        self, db_user_id: int, adventure_id: int, extra: Optional[Dict[str, str]] = None
    ) -> str:
        """Generate one narrator turn from the current adventure state."""
        messages = self._load_turn_context(db_user_id, adventure_id)
        if extra:
            messages.append(extra)
        response = await chat_async(messages=messages, model=self._resolve_model(db_user_id))
        reply = ""
        if isinstance(response, dict):
            reply = str(response.get("text") or "").strip()
        return sanitize_untrusted_text(reply, limit=8000) or "*The story pauses momentarily...*"

    def _schedule_lore_refresh(self, adventure_id: int) -> None:
        if not lore_refresh_due(adventure_id):
            return
        try:
            task = asyncio.create_task(
                refresh_adventure_lore(adventure_id, chat_fn=chat_async, reason="webapp turn")
            )
        except RuntimeError:
            return  # no running loop (e.g. sync test context)
        _LORE_TASKS.add(task)
        task.add_done_callback(_LORE_TASKS.discard)

    @staticmethod
    def _insert_message(adventure_id: int, role: str, content: str) -> None:
        with db_rw() as conn:
            conn.execute(
                "INSERT INTO adventure_messages (adventure_id, role, content) VALUES (?, ?, ?)",
                (adventure_id, role, content),
            )
            conn.execute(
                "UPDATE adventures SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (adventure_id,),
            )

    async def generate_turn(
        self, db_user_id: int, adventure_id: int, text: str, *, mode: str = "do"
    ) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty turn")
        if mode not in _VALID_MODES:
            mode = "do"

        # Verify ownership BEFORE writing anything.
        with db_ro() as conn:
            self._owned_adventure(conn, db_user_id, adventure_id)

        role, stored = self._format_player_input(text, mode)
        self._insert_message(adventure_id, role, stored)
        reply = await self._narrate(db_user_id, adventure_id)
        self._insert_message(adventure_id, "narrator", reply)
        self._schedule_lore_refresh(adventure_id)
        return {"reply": reply, "player": {"role": role, "content": stored}}

    async def continue_story(self, db_user_id: int, adventure_id: int) -> Dict[str, Any]:
        """Generate more narration with no new player input."""
        reply = await self._narrate(
            db_user_id,
            adventure_id,
            extra={"role": "user", "content": "(Continue the story from where it left off.)"},
        )
        self._insert_message(adventure_id, "narrator", reply)
        self._schedule_lore_refresh(adventure_id)
        return {"reply": reply}

    async def retry_last(self, db_user_id: int, adventure_id: int) -> Dict[str, Any]:
        """Regenerate the most recent narrator turn."""
        with db_rw() as conn:
            self._owned_adventure(conn, db_user_id, adventure_id)
            last = conn.execute(
                "SELECT id, role FROM adventure_messages "
                "WHERE adventure_id = ? ORDER BY id DESC LIMIT 1",
                (adventure_id,),
            ).fetchone()
            if last and last["role"] in ("narrator", "character"):
                conn.execute("DELETE FROM adventure_messages WHERE id = ?", (last["id"],))
        reply = await self._narrate(db_user_id, adventure_id)
        self._insert_message(adventure_id, "narrator", reply)
        return {"reply": reply}

    def erase_last(self, db_user_id: int, adventure_id: int) -> Dict[str, Any]:
        """Remove the last exchange (narrator reply + preceding player action)."""
        removed = 0
        with db_rw() as conn:
            self._owned_adventure(conn, db_user_id, adventure_id)
            rows = conn.execute(
                "SELECT id, role FROM adventure_messages "
                "WHERE adventure_id = ? ORDER BY id DESC LIMIT 2",
                (adventure_id,),
            ).fetchall()
            if rows and rows[0]["role"] in ("narrator", "character"):
                conn.execute("DELETE FROM adventure_messages WHERE id = ?", (rows[0]["id"],))
                removed += 1
                if len(rows) > 1 and rows[1]["role"] == "user":
                    conn.execute("DELETE FROM adventure_messages WHERE id = ?", (rows[1]["id"],))
                    removed += 1
        return {"removed": removed}
