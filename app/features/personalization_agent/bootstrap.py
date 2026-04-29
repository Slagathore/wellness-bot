"""Initialization hook for personalization agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .service import PersonalizationAgent

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime.interfaces import UnifiedWellnessBot


def register_feature(bot: "UnifiedWellnessBot") -> None:
    agent = PersonalizationAgent(bot.log)
    setattr(bot, "_personalization_agent", agent)
    bot.log("Feature module enabled: profile_personalization_agent")
