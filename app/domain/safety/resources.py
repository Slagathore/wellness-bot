"""Crisis-response copy and resource links shared across delivery paths.

Kept in one place so every path that reacts to a detected crisis (the Telegram
fast path and the event-bus safety handler) sends identical, reviewed wording
rather than each hand-rolling its own.
"""

from __future__ import annotations

# Sent to a user the moment a crisis signal is detected, ahead of the normal
# conversational reply. Warm, brief, and it always names concrete resources —
# the previous behaviour sent the generic rate-limit throttle text instead.
CRISIS_RESOURCE_MESSAGE = (
    "I hear you, and I'm really glad you told me. What you're feeling matters, "
    "and you don't have to carry it alone right now.\n\n"
    "If you might be in danger or thinking about hurting yourself, please reach "
    "out to people who can help immediately:\n"
    "• Call or text 988 — Suicide & Crisis Lifeline (US, 24/7)\n"
    "• Text HOME to 741741 — Crisis Text Line (US/Canada)\n"
    "• Trans Lifeline: 877-565-8860\n"
    "• If you're outside the US: https://findahelpline.com\n"
    "• If you're in immediate danger, please call your local emergency "
    "number (911 in the US).\n\n"
    "I'm still here with you. Do you want to keep talking about what's going on?"
)
