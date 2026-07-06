"""Ensure NSFW preference context renders both preferences AND limitations.

Adventure mode now injects this same context (see TelegramAdapter._handle_
adventure_message), so safe words and hard/soft limits reach adventures, not
just downbad chat.
"""

from __future__ import annotations

from app.runtime.services.preferences import PreferenceService


def test_format_nsfw_context_includes_limits_and_safeword():
    svc = PreferenceService()
    prefs = {
        "nsfw_opt_in": True,
        "hard_limits": ["blood", "underage"],
        "soft_limits": ["public"],
        "safe_word": "pineapple",
        "kinks": ["teasing"],
    }
    ctx = svc.format_nsfw_context(prefs)

    # The "limitations" the user asked to always inject:
    assert "blood" in ctx and "underage" in ctx
    assert "HARD LIMITS" in ctx
    assert "public" in ctx  # soft limit
    assert "pineapple" in ctx  # safe word
    # And a "good one":
    assert "teasing" in ctx
