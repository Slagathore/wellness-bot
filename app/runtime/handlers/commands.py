"""Command handlers for the runtime bot."""

from __future__ import annotations


from telegram.ext import Application, CommandHandler

from app.runtime.context import RuntimeDeps


def _wrap(deps: RuntimeDeps, method_name: str):
    async def _handler(update, context):
        method = getattr(deps.app, method_name)
        return await method(update, context)

    return _handler


COMMAND_MAP = {
    "help": "handle_help",
    "helpmodes": "handle_helpmodes",
    "onboard": "handle_onboard",
    "mood": "handle_mood",
    "journal": "handle_journal",
    "personality": "handle_personality",
    "reminders": "handle_reminders_command",
    "export": "handle_export",
    "streak": "handle_streak",
    "setmodel": "handle_setmodel",
    "mymodel": "handle_mymodel",
    "models": "handle_models",
    "generate_image": "handle_generate_image",
}


def register(app: Application, deps: RuntimeDeps) -> None:
    """Register command handlers on the Application."""

    for command, method_name in COMMAND_MAP.items():
        app.add_handler(CommandHandler(command, _wrap(deps, method_name)))
