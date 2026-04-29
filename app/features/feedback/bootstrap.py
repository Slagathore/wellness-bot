"""Registration helpers for the feedback feature."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from telegram.ext import Application, CommandHandler

from .handlers import list_my_feedback, report_bug, submit_suggestion

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime.interfaces import UnifiedWellnessBot


def register_feature(bot: "UnifiedWellnessBot", application: Application) -> None:
    """Register Telegram handlers for the feedback feature."""
    application.add_handler(
        CommandHandler("reportbug", partial(report_bug, bot), block=True)
    )
    application.add_handler(
        CommandHandler("suggestion", partial(submit_suggestion, bot), block=True)
    )
    application.add_handler(
        CommandHandler("myfeedback", partial(list_my_feedback, bot), block=True)
    )
    bot.log("Feature module enabled: user_feedback")
