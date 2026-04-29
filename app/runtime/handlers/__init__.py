"""Telegram handler registration for the runtime service."""

from __future__ import annotations

from telegram.ext import Application

from app.runtime.context import RuntimeDeps
from app.runtime.handlers import conversations, commands, media


def register_default_handlers(app: Application, *, deps: RuntimeDeps) -> None:
    """Register runtime handlers on the telegram Application."""

    conversations.register(app, deps)
    commands.register(app, deps)
    media.register(app, deps)
