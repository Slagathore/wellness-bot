"""Registration for adaptive psych test feature."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from telegram.ext import Application, CommandHandler

from .handlers import (
    cancel_profile_assessment,
    handle_profile_answer,
    start_profile_assessment,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime.interfaces import UnifiedWellnessBot


def register_feature(bot: "UnifiedWellnessBot", application: Application) -> None:
    """Hook Telegram commands for adaptive psych tests."""

    manager = get_manager(bot)
    application.add_handler(
        CommandHandler(
            "profiletest", partial(start_profile_assessment, bot, manager), block=True
        )
    )
    application.add_handler(
        CommandHandler(
            "profileanswer", partial(handle_profile_answer, bot, manager), block=True
        )
    )
    application.add_handler(
        CommandHandler(
            "profilecancel",
            partial(cancel_profile_assessment, bot, manager),
            block=True,
        )
    )
    bot.log("Feature module enabled: adaptive_psych_tests")


def get_manager(bot: "UnifiedWellnessBot"):
    existing = getattr(bot, "_profile_assessment_manager", None)
    if existing is not None:
        return existing
    from .service import ProfileAssessmentManager

    manager = ProfileAssessmentManager(bot.log)
    setattr(bot, "_profile_assessment_manager", manager)
    return manager
