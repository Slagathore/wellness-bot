"""Registration helpers for NSFW preference feature."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from .handlers import _TelegramBotProxy, nsfw_pref_callback, nsfw_pref_command

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime.interfaces import UnifiedWellnessBot


def register_feature(bot: "UnifiedWellnessBot", application: Application) -> None:
    """Register Telegram handlers for the NSFW preference commands."""

    application.add_handler(
        CommandHandler("nsfwpref", partial(nsfw_pref_command, bot), block=True)
    )
    application.add_handler(
        CallbackQueryHandler(partial(nsfw_pref_callback, bot), pattern=r"^nsfw\|")
    )
    if not hasattr(bot, "_bot") or not isinstance(
        getattr(bot, "_bot"), _TelegramBotProxy
    ):
        setattr(bot, "_bot", _TelegramBotProxy(bot))
    if not hasattr(bot, "_pending_inputs"):
        setattr(bot, "_pending_inputs", {})
    bot.log("Feature module enabled: nsfw_preferences")
