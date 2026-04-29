"""Feature module bootstrap helpers."""

from __future__ import annotations

from app.feature_flags import enabled
from app.runtime.interfaces import UnifiedWellnessBot


def bootstrap_features(bot: "UnifiedWellnessBot") -> None:
    """Activate optional feature modules for the running bot instance."""
    application = getattr(bot, "telegram_app", None)
    if application is None:
        return

    if enabled("user_feedback"):
        from .feedback.bootstrap import register_feature as _register_feedback

        _register_feedback(bot, application)

    if enabled("nsfw_preferences"):
        from .nsfw_preferences.bootstrap import register_feature as _register_nsfw

        _register_nsfw(bot, application)

    if enabled("adaptive_psych_tests"):
        from .adaptive_psych_tests import register_feature as _register_psych

        _register_psych(bot, application)

    if enabled("profile_personalization_agent"):
        from .personalization_agent import register_feature as _register_personalization

        _register_personalization(bot)

    if enabled("multi_document_import"):
        from .profile_import import register_feature as _register_profile_import

        _register_profile_import(bot, application)

    if enabled("discord_bot"):
        from .discord import start_feature as _start_discord

        _start_discord(bot)
