"""Registration helpers for profile import commands."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .handlers import (
    handle_cancel_profile_bulk,
    handle_document_upload,
    handle_import_profile,
    handle_import_profile_bulk,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime.interfaces import UnifiedWellnessBot


def register_feature(bot: "UnifiedWellnessBot", application: Application) -> None:
    application.add_handler(
        CommandHandler("importprofile", partial(handle_import_profile, bot), block=True)
    )
    application.add_handler(
        CommandHandler(
            "importprofilebulk", partial(handle_import_profile_bulk, bot), block=True
        )
    )
    application.add_handler(
        CommandHandler(
            "import_profile_bulk", partial(handle_import_profile_bulk, bot), block=True
        )
    )
    application.add_handler(
        CommandHandler(
            "cancelimport", partial(handle_cancel_profile_bulk, bot), block=True
        )
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL, partial(handle_document_upload, bot)),
        group=-1,
    )
    bot.log("Feature module enabled: multi_document_import")
