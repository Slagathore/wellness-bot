"""Conversation-related Telegram handlers."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters

from app.runtime.context import RuntimeDeps


async def handle_start(
    deps: RuntimeDeps, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Entry point for /start; delegates to app implementation."""

    await deps.app._handle_start_impl(update, context)


async def handle_message(
    deps: RuntimeDeps, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Entry point for regular text messages."""

    await deps.app._handle_message_impl(update, context)


def register(app, deps: RuntimeDeps) -> None:
    """Register conversation handlers on the application."""

    app.add_handler(
        CommandHandler(
            "start", lambda update, context: handle_start(deps, update, context)
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            lambda update, context: handle_message(deps, update, context),
        )
    )
