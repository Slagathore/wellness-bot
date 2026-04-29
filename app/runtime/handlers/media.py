"""Media and callback handlers for the runtime bot."""

from __future__ import annotations

from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from app.runtime.context import RuntimeDeps


def register(app: Application, deps: RuntimeDeps) -> None:
    """Register media/callback handlers."""

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            lambda update, context: deps.app.handle_photo(update, context),
        )
    )
    app.add_handler(
        MessageHandler(
            filters.Document.IMAGE,
            lambda update, context: deps.app.handle_photo(update, context),
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            lambda update, context: deps.app.handle_callback_query(update, context)
        )
    )
