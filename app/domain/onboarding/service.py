"""
Onboarding wrapper to decouple from unified_bot.
"""

from __future__ import annotations

from typing import Optional

from app.onboarding.flow import onboarding_flow


class OnboardingService:
    """Thin wrapper delegating to existing onboarding flow."""

    def start(self, user_id: int) -> Optional[str]:
        return onboarding_flow.start(user_id)

    def handle_message(self, tg_user_id: int, user_id: int, text: str) -> Optional[str]:
        return onboarding_flow.handle_user_message(tg_user_id, user_id, text)
