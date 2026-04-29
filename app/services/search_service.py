"""Background web search follow-up service."""

from __future__ import annotations

import asyncio
import logging

from app.monitoring import WORKER_ERRORS

logger = logging.getLogger(__name__)


class SearchService:
    """Runs background web searches and sends follow-up messages."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def run_background_search(self, chat_id, msg_text, personality_name):
        """Execute search logic; intended to run inside a thread pool."""

        try:
            from app.utils.web_search import enhance_response_with_search

            self.bot.log(f"🔍 Running background web search for: {msg_text[:50]}...")

            search_results = enhance_response_with_search(
                msg_text,
                model=None,
                use_llm_decision=False,
                personality=personality_name,
            )

            if search_results:
                self.bot.log("✅ Web search found relevant info, sending follow-up")
                loop = self.bot.bot_event_loop
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._send_search_followup(chat_id, search_results),
                        loop,
                    )
                else:
                    logger.warning(
                        "Cannot send search follow-up: bot event loop not available"
                    )
            else:
                self.bot.log("🔍 Web search: No relevant info needed")

        except Exception as exc:  # noqa: BLE001
            WORKER_ERRORS.labels(component="web_search").inc()
            logger.error("Background web search error: %s", exc, exc_info=True)
            self.bot.log(f"⚠ Background web search failed: {str(exc)[:100]}")

    async def _send_search_followup(self, chat_id, search_info):
        """Send web search results as a follow-up message."""

        try:
            if not self.bot.telegram_app:
                logger.warning("Cannot send search follow-up: telegram_app is None")
                return
            followup = f"🔍 *Here's what I found online:*\n\n{search_info}"
            await self.bot.telegram_app.bot.send_message(
                chat_id=chat_id,
                text=followup,
                parse_mode="Markdown",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Error sending search follow-up: %s", exc, exc_info=True)
