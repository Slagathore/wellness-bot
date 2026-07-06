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
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.db import db_ro, db_rw
from app.domain.adventure.lore import (_load_settings, lore_refresh_due,
                                       refresh_adventure_lore)
from app.services.dm_image import generate_image
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

    # -- characters --------------------------------------------------------
    _CHAR_BLOCK_RE = re.compile(r"\[CHARACTER\](.*?)\[/CHARACTER\]", re.DOTALL | re.IGNORECASE)
    _VALID_ROLES = ("npc", "protagonist", "antagonist", "companion")
    _CHAR_SYSTEM = (
        "You are a character creation assistant for a roleplay adventure. From the "
        "user's brief, output EXACTLY one [CHARACTER] block and nothing else — no "
        "reasoning, no commentary. Use this format:\n"
        "[CHARACTER]\n"
        "Name: (character name)\n"
        "Emoji: (a single emoji)\n"
        "Greeting: (the character's first line, in character)\n"
        "Temperature: (a number 0.7-1.4)\n"
        "System Prompt: (150-400 words of instructions for playing this character: "
        "personality, appearance, mannerisms, speech patterns, scenario context)\n"
        "[/CHARACTER]"
    )

    @classmethod
    def _parse_character_block(cls, text: str) -> Dict[str, Any]:
        match = cls._CHAR_BLOCK_RE.search(text or "")
        block = match.group(1) if match else (text or "")
        key_map = {"name": "name", "emoji": "emoji", "greeting": "greeting",
                   "temperature": "temperature", "system prompt": "system_prompt"}
        result: Dict[str, Any] = {}
        cur: Optional[str] = None
        buf: List[str] = []
        for line in block.splitlines():
            hit = False
            low = line.lower().strip()
            for prefix, key in key_map.items():
                if low.startswith(prefix + ":"):
                    if cur:
                        result[cur] = "\n".join(buf).strip()
                    cur, buf, hit = key, [line.split(":", 1)[1].strip()], True
                    break
            if not hit and cur:
                buf.append(line)
        if cur:
            result[cur] = "\n".join(buf).strip()
        return result

    def _character_accessible(self, conn, db_user_id: int, character_id: int):
        return conn.execute(
            "SELECT id, display_name, emoji FROM custom_characters "
            "WHERE id = ? AND (is_global = 1 OR creator_user_id = ?)",
            (character_id, db_user_id),
        ).fetchone()

    async def create_character(
        self, db_user_id: int, *, name: str = "", description: str = ""
    ) -> Dict[str, Any]:
        name = sanitize_untrusted_text(name, limit=60).strip()
        description = sanitize_untrusted_text(description, limit=1500).strip()
        if not name and not description:
            raise ValueError("name or description required")

        response = await chat_async(
            messages=[
                {"role": "system", "content": self._CHAR_SYSTEM},
                {"role": "user", "content": f"Name: {name or '(you choose one)'}\nDescription: {description or '(invent something evocative)'}"},
            ],
            model=self._resolve_model(db_user_id),
            options={"temperature": 0.9, "num_predict": 1500},
        )
        raw = str(response.get("text") or "") if isinstance(response, dict) else ""
        parsed = self._parse_character_block(raw)

        final_name = (parsed.get("name") or name or "New Character").strip()[:60]
        emoji = (str(parsed.get("emoji") or "").strip() or "🎭")[:8]
        system_prompt = sanitize_untrusted_text(
            parsed.get("system_prompt") or description or final_name, limit=6000
        )
        greeting = sanitize_untrusted_text(parsed.get("greeting") or "", limit=1000)
        try:
            temperature = max(0.1, min(2.0, float(parsed.get("temperature") or 0.85)))
        except (TypeError, ValueError):
            temperature = 0.85
        slug = re.sub(r"[^a-z0-9]+", "_", final_name.lower()).strip("_") or "character"

        with db_rw() as conn:
            conn.execute(
                "INSERT INTO custom_characters(name, display_name, emoji, system_prompt, "
                "temperature, initial_message, creator_user_id, is_global) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (slug, final_name, emoji, system_prompt, temperature, greeting, db_user_id),
            )
            cid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        return {"id": cid, "display_name": final_name, "emoji": emoji}

    def list_adventure_characters(self, db_user_id: int, adventure_id: int) -> List[Dict[str, Any]]:
        with db_ro() as conn:
            self._owned_adventure(conn, db_user_id, adventure_id)
            rows = conn.execute(
                "SELECT c.id, c.display_name, c.emoji, ac.role FROM adventure_characters ac "
                "JOIN custom_characters c ON c.id = ac.character_id "
                "WHERE ac.adventure_id = ? ORDER BY c.display_name",
                (adventure_id,),
            ).fetchall()
        return [
            {"id": r["id"], "display_name": r["display_name"], "emoji": r["emoji"] or "🎭", "role": r["role"]}
            for r in rows
        ]

    def attach_character(
        self, db_user_id: int, adventure_id: int, character_id: int, role: str = "npc"
    ) -> Dict[str, Any]:
        role = role if role in self._VALID_ROLES else "npc"
        with db_rw() as conn:
            self._owned_adventure(conn, db_user_id, adventure_id)
            if not self._character_accessible(conn, db_user_id, character_id):
                raise ValueError("character not accessible")
            conn.execute(
                "INSERT OR REPLACE INTO adventure_characters(adventure_id, character_id, role) "
                "VALUES (?, ?, ?)",
                (adventure_id, character_id, role),
            )
        return {"ok": True}

    def detach_character(
        self, db_user_id: int, adventure_id: int, character_id: int
    ) -> Dict[str, Any]:
        with db_rw() as conn:
            self._owned_adventure(conn, db_user_id, adventure_id)
            conn.execute(
                "DELETE FROM adventure_characters WHERE adventure_id = ? AND character_id = ?",
                (adventure_id, character_id),
            )
        return {"ok": True}

    # -- memory (canon sheet) ----------------------------------------------
    def get_memory(self, db_user_id: int, adventure_id: int) -> Dict[str, Any]:
        with db_ro() as conn:
            adv = self._owned_adventure(conn, db_user_id, adventure_id)
        settings = _load_settings(adv["settings"])
        return {
            "lore": adv["lore"] or "",
            "player_role": settings.get("player_role") or "",
            "objective": settings.get("objective") or "",
        }

    def update_memory(
        self,
        db_user_id: int,
        adventure_id: int,
        *,
        lore: Optional[str] = None,
        player_role: Optional[str] = None,
        objective: Optional[str] = None,
    ) -> Dict[str, Any]:
        with db_rw() as conn:
            adv = self._owned_adventure(conn, db_user_id, adventure_id)
            settings = _load_settings(adv["settings"])
            if player_role is not None:
                settings["player_role"] = sanitize_untrusted_text(player_role, limit=300)
            if objective is not None:
                settings["objective"] = sanitize_untrusted_text(objective, limit=300)
            new_lore = adv["lore"] if lore is None else sanitize_untrusted_text(lore, limit=8000)
            conn.execute(
                "UPDATE adventures SET lore = ?, settings = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_lore, json.dumps(settings), adventure_id),
            )
        return self.get_memory(db_user_id, adventure_id)

    # -- images ------------------------------------------------------------
    def _nsfw_opt_in(self, db_user_id: int) -> bool:
        try:
            from app.runtime.services.preferences import PreferenceService

            return bool(PreferenceService().get_nsfw_opt_in(db_user_id))
        except Exception:  # noqa: BLE001
            return False

    async def _to_image_prompt(self, db_user_id: int, text: str) -> str:
        """Distil narrative text into a concise, comma-separated image prompt.

        The DM recipes expect a tight subject, not prose, so we summarize first;
        on any failure we fall back to the raw (truncated) text.
        """
        text = (text or "").strip()
        if not text:
            return ""
        try:
            resp = await chat_async(
                messages=[
                    {"role": "system", "content": (
                        "Convert the scene into an image-generation prompt: ONLY a "
                        "comma-separated list of vivid visual details (subject, setting, "
                        "lighting, mood, composition), under 60 words. No sentences, no "
                        "reasoning, no quotes."
                    )},
                    {"role": "user", "content": sanitize_untrusted_text(text, limit=1500)},
                ],
                model=self._resolve_model(db_user_id),
                options={"temperature": 0.4, "num_predict": 300},
            )
            raw = str(resp.get("text") or "").strip() if isinstance(resp, dict) else ""
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            prompt = sanitize_untrusted_text(lines[-1] if lines else "", limit=400)
            return prompt or sanitize_untrusted_text(text, limit=400)
        except Exception:  # noqa: BLE001
            return sanitize_untrusted_text(text, limit=400)

    async def illustrate_scene(
        self, db_user_id: int, adventure_id: int, *, subject: Optional[str] = None, style: str = "scene"
    ) -> bytes:
        with db_ro() as conn:
            self._owned_adventure(conn, db_user_id, adventure_id)
            if not subject:
                last = conn.execute(
                    "SELECT content FROM adventure_messages WHERE adventure_id = ? "
                    "AND role IN ('narrator', 'character') ORDER BY id DESC LIMIT 1",
                    (adventure_id,),
                ).fetchone()
                subject = last["content"] if last else ""
        prompt = await self._to_image_prompt(db_user_id, subject or "")
        if not prompt:
            raise ValueError("nothing to illustrate yet")
        return await generate_image(prompt, style=style, nsfw=self._nsfw_opt_in(db_user_id))

    async def character_portrait(
        self, db_user_id: int, character_id: int, *, style: str = "portrait"
    ) -> bytes:
        with db_ro() as conn:
            if not self._character_accessible(conn, db_user_id, character_id):
                raise ValueError("character not accessible")
            row = conn.execute(
                "SELECT display_name, system_prompt FROM custom_characters WHERE id = ?",
                (character_id,),
            ).fetchone()
        subject = f"character portrait of {row['display_name']}. {str(row['system_prompt'] or '')[:400]}"
        prompt = await self._to_image_prompt(db_user_id, subject)
        return await generate_image(
            prompt or subject[:400], style=style, nsfw=self._nsfw_opt_in(db_user_id)
        )

    # -- creation ----------------------------------------------------------
    async def create_adventure(
        self,
        db_user_id: int,
        *,
        title: str,
        premise: str = "",
        player_role: str = "",
        character_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Create a new adventure from a premise and generate its opening scene."""
        title = sanitize_untrusted_text(title, limit=80).strip() or "Untitled Adventure"
        premise = sanitize_untrusted_text(premise, limit=2000).strip()
        player_role = sanitize_untrusted_text(player_role, limit=300).strip()

        settings = {
            "reply_length": "moderate",
            "choice_mode": False,
            "player_role": player_role,
            "objective": "",
            "setting": "",
            "tone_notes": "",
            "last_lore_message_id": 0,
        }
        lore = premise or "A new story begins."
        with db_rw() as conn:
            conn.execute(
                "INSERT INTO adventures(user_id, title, description, lore, status, settings) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (db_user_id, title, premise, lore, json.dumps(settings)),
            )
            adventure_id = int(
                conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            )

        # Attach any starring characters the player picked (accessible ones only).
        char_names: List[str] = []
        for cid in (character_ids or []):
            try:
                self.attach_character(db_user_id, adventure_id, int(cid), "companion")
            except (ValueError, TypeError, AdventureNotFound):
                continue
        for c in self.list_adventure_characters(db_user_id, adventure_id):
            char_names.append(f"{c['emoji']} {c['display_name']}")

        opening = await self._opening_scene(db_user_id, title, premise, player_role, char_names)
        self._insert_message(adventure_id, "narrator", opening)
        return {"id": adventure_id, "title": title, "opening": opening}

    async def _opening_scene(
        self, db_user_id: int, title: str, premise: str, player_role: str,
        char_names: Optional[List[str]] = None,
    ) -> str:
        system = (
            "You are a creative roleplay narrator opening a brand-new interactive "
            "text adventure. Write an immersive opening scene of 2 to 3 short "
            "paragraphs in second person ('you'). Establish the setting, mood, and "
            "an immediate hook, then end at a moment that invites the player's first "
            "action. Do not explain or summarize; begin the story directly."
        )
        cast = ", ".join(char_names) if char_names else ""
        user = (
            f"Adventure title: {title}\n"
            f"Premise: {premise or '(none given; invent something evocative)'}\n"
            f"The player is: {player_role or 'an unnamed protagonist'}\n"
            + (f"Feature these characters: {sanitize_untrusted_text(cast, limit=300)}\n" if cast else "")
            + "\nOpening scene:"
        )
        response = await chat_async(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=self._resolve_model(db_user_id),
        )
        text = str(response.get("text") or "").strip() if isinstance(response, dict) else ""
        return sanitize_untrusted_text(text, limit=8000) or "The story begins. What do you do?"

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
