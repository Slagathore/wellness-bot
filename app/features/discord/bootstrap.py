"""Discord bot integration with shared wellness pipeline."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, cast

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime.interfaces import UnifiedWellnessBot
    from discord import Client as DiscordClientBase  # type: ignore[reportAttributeAccessIssue]
    from discord import Intents as DiscordIntents  # type: ignore[reportAttributeAccessIssue]
    from discord import Member as DiscordMember  # type: ignore[reportAttributeAccessIssue]
    from discord import Message as DiscordMessage  # type: ignore[reportAttributeAccessIssue]
    from discord import User as DiscordUser  # type: ignore[reportAttributeAccessIssue]
else:  # pragma: no cover
    UnifiedWellnessBot = Any
    DiscordClientBase = Any
    DiscordIntents = Any
    DiscordMember = Any
    DiscordMessage = Any
    DiscordUser = Any

DiscordClientBaseRuntime: type[Any] = object
DiscordIntentsRuntime: Any = None

try:
    import discord  # type: ignore[reportMissingImports]
except ImportError:  # pragma: no cover - handled gracefully at runtime
    discord = None
else:
    DiscordClientBaseRuntime = cast(type[Any], getattr(discord, "Client"))
    DiscordIntentsRuntime = getattr(discord, "Intents")

from app.config import settings
from app.db import db_rw
from app.history_scope import history_scope_for_user
from app.memory import ConversationMemoryIndexer
from app.orchestrator.pipeline import generate_response
from app.utils.fs import ensure_user_dirs
from app.utils.time_utils import operator_now

LOGGER = logging.getLogger(__name__)
DISCORD_ID_OFFSET = 2_000_000_000_000
_RUNNER: "DiscordRunner | None" = None
MEMORY_INDEXER = ConversationMemoryIndexer()


def start_feature(bot: "UnifiedWellnessBot") -> None:
    """Kick off the Discord bot in a background thread when enabled."""

    global _RUNNER  # noqa: PLW0603

    if discord is None:
        bot.log(
            "Discord feature requested but discord.py is not installed. Skipping startup."
        )
        return

    token = settings().discord_bot_token
    if not token:
        bot.log(
            "Discord feature flag is enabled but DISCORD_BOT_TOKEN is not configured. Skipping startup."
        )
        return

    if _RUNNER and _RUNNER.is_running:
        bot.log("Discord bridge already running; skipping duplicate startup.")
        return

    _RUNNER = DiscordRunner(token=token, log_callback=bot.log)
    _RUNNER.start()
    bot.log("Discord bridge started in background thread.")


def stop_feature(bot: "UnifiedWellnessBot | None" = None) -> None:
    """Stop the Discord bridge if it is currently running."""

    global _RUNNER  # noqa: PLW0603

    if _RUNNER is None:
        return

    _RUNNER.stop()
    if bot is not None:
        bot.log("Discord bridge stopped.")
    else:  # pragma: no cover - fallback logging when bot not available
        LOGGER.info("Discord bridge stopped.")
    _RUNNER = None


def _map_discord_user_id(discord_user_id: int) -> int:
    """Map a Discord user ID onto the integer namespace used for Telegram."""

    return -(DISCORD_ID_OFFSET + int(discord_user_id))


def _ensure_user(discord_user: DiscordUser | DiscordMember) -> int:
    """Ensure a user record exists for the Discord user and return internal ID."""

    mapped_id = _map_discord_user_id(discord_user.id)
    username_hint = f"discord:{discord_user.name}"
    display_name = getattr(discord_user, "display_name", discord_user.name)

    with db_rw() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_user_id = ?",
            (mapped_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET telegram_username = ?, display_name = ?, last_active_at = CURRENT_TIMESTAMP WHERE id = ?",
                (username_hint, display_name, row["id"]),
            )
            user_id = row["id"]
        else:
            cursor = conn.execute(
                """INSERT INTO users (
                    telegram_user_id,
                    telegram_username,
                    display_name,
                    onboarding_completed,
                    last_active_at,
                    personality
                ) VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, 'friendly')""",
                (mapped_id, username_hint, display_name),
            )
            user_id = cursor.lastrowid

    if user_id is None:
        raise RuntimeError("Failed to create Discord user record")
    user_id_int = int(user_id)
    ensure_user_dirs(user_id_int, username_hint)
    return user_id_int


def _get_session(user_id: int) -> int:
    """Fetch an active session or create a new one."""

    target_scope = history_scope_for_user(user_id)
    with db_rw() as conn:
        row = conn.execute(
            "SELECT id, scope FROM sessions WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row and str(row["scope"] or "standard") == target_scope:
            return int(row["id"])
        if row:
            conn.execute(
                "UPDATE sessions SET status = 'archived' WHERE user_id = ? AND status = 'active'",
                (user_id,),
            )
        cursor = conn.execute(
            """INSERT INTO sessions (user_id, scope, status, started_at, ctx_token_budget)
               VALUES (?, ?, 'active', CURRENT_TIMESTAMP, ?)""",
            (user_id, target_scope, settings().ctx_token_budget),
        )
        session_id = cursor.lastrowid
        if session_id is None:
            raise RuntimeError("Failed to create Discord session")
        return int(session_id)


def _store_message(session_id: int, user_id: int, role: str, content: str) -> int:
    """Persist a message row and return its identifier."""

    with db_rw() as conn:
        scope_row = conn.execute(
            "SELECT scope FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        scope = (
            str(scope_row["scope"])
            if scope_row and scope_row["scope"]
            else history_scope_for_user(user_id)
        )
        cursor = conn.execute(
            """INSERT INTO messages (session_id, user_id, scope, role, content, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                user_id,
                scope,
                role,
                content,
                operator_now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        message_id = cursor.lastrowid
        if message_id is None:
            raise RuntimeError("Failed to store Discord message")
        return int(message_id)


def _process_user_message(author: DiscordUser | DiscordMember, content: str) -> str:
    """Synchronous pipeline for handling a Discord DM."""

    user_id = _ensure_user(author)
    session_id = _get_session(user_id)
    user_message_id = _store_message(session_id, user_id, "user", content)
    MEMORY_INDEXER.index_message(
        message_id=user_message_id,
        user_id=user_id,
        scope=history_scope_for_user(user_id),
        role="user",
        content=content,
    )

    response = generate_response(user_id, session_id, content)
    text = response.get("text") or "I'm here with you."
    assistant_message_id = _store_message(session_id, user_id, "assistant", text)
    MEMORY_INDEXER.index_message(
        message_id=assistant_message_id,
        user_id=user_id,
        scope=history_scope_for_user(user_id),
        role="assistant",
        content=text,
    )
    return text


class DiscordWellnessClient(DiscordClientBaseRuntime):
    """Minimal Discord client that mirrors Telegram behaviour for DMs."""

    def __init__(self, *, log_callback, **kwargs):
        super().__init__(**kwargs)
        self._log_callback = log_callback

    async def on_ready(self) -> None:
        user = self.user
        if user is None:
            return
        self._log_callback(f"Discord bridge logged in as {user} (id={user.id})")

    async def on_message(self, message: DiscordMessage) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            # Only respond to direct messages for now.
            return

        content = (message.content or "").strip()
        if not content:
            return

        if content.lower() in {"!help", "/help"}:
            await message.channel.send(_build_help_text())
            return

        try:
            reply = await asyncio.to_thread(
                _process_user_message, message.author, content
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Discord message handling failed: %s", exc)
            await message.channel.send(
                "Sorry, I hit an error. Please try again in a moment."
            )
            return

        if reply:
            await message.channel.send(reply)


def _build_help_text() -> str:
    """Return a condensed help message suitable for Discord."""

    sections = [
        "**Wellness Bot (Discord)**",
        "",
        "I mirror the Telegram experience here in DMs:",
        "- Chat naturally about anything on your mind",
        "- I'll remember context and keep supporting you",
        "",
        "Commands:",
        "- `!help` — Show this overview",
        "- `/reportbug <details>` — Mention issues (requires Telegram link)",
        "",
        "Tip: Real-time reminders and onboarding live on Telegram right now, ",
        "but all conversation support works here too.",
    ]
    return "\n".join(sections)


@dataclass
class DiscordRunner:
    """Thin runner that owns the background Discord client thread."""

    token: str
    log_callback: Callable[[str], None]
    thread: threading.Thread | None = None
    loop: asyncio.AbstractEventLoop | None = None
    client: DiscordWellnessClient | None = None
    _ready: threading.Event = field(default_factory=threading.Event, init=False)

    @property
    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._ready.clear()
        self.thread = threading.Thread(
            target=self._run, name="discord-bot", daemon=True
        )
        self.thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        if not self.is_running:
            return

        self._ready.wait(timeout=timeout)
        loop = self.loop
        forced_stop = False

        if loop and loop.is_running():

            async def _shutdown() -> None:
                if self.client and not self.client.is_closed():
                    await self.client.close()

            future = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            try:
                future.result(timeout=timeout)
            except asyncio.TimeoutError:
                LOGGER.warning(
                    "Discord shutdown timed out after %.1fs; forcing close.", timeout
                )
                if not future.done():
                    loop_running = loop and loop.is_running() and not loop.is_closed()
                    if loop_running:
                        loop.call_soon_threadsafe(future.cancel)
                forced_stop = True
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.exception("Discord shutdown error: %s", exc)
                forced_stop = True

        if loop and loop.is_running() and forced_stop:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)

        thread_alive = False
        if self.thread:
            self.thread.join(timeout=timeout)
            thread_alive = self.thread.is_alive()
            if thread_alive:
                LOGGER.warning(
                    "Discord worker thread did not exit within %.1fs.", timeout
                )

        if loop and not loop.is_closed() and not thread_alive:
            with contextlib.suppress(RuntimeError):
                loop.close()

        self.thread = None
        self.loop = None
        self.client = None
        self._ready.clear()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)

        if discord is None:
            raise RuntimeError("Discord client not available")

        intents = DiscordIntentsRuntime.default()
        intents.message_content = True
        self.client = DiscordWellnessClient(
            intents=intents, log_callback=self.log_callback
        )
        self._ready.set()

        try:
            client = self.client
            if client is None:
                raise RuntimeError("Discord client failed to initialize")
            loop.run_until_complete(client.start(self.token))
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Discord client stopped unexpectedly: %s", exc)
            self.log_callback(f"Discord bridge stopped unexpectedly: {exc}")
        finally:
            if self.client and not self.client.is_closed():
                with contextlib.suppress(Exception):
                    loop.run_until_complete(self.client.close())
            self.client = None
            with contextlib.suppress(RuntimeError):
                loop.close()
            self.loop = None
