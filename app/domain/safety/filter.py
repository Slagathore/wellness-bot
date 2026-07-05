"""Inbound rate-limit gate and crisis-keyword detection.

Rate-limiting and crisis detection are two *unrelated* concerns and are kept
apart here. Previously ``allow()`` returned a single bool for both, so a crisis
message was indistinguishable from spam and callers replied with the throttle
text instead of crisis resources. ``evaluate()`` now returns a structured
decision so callers can react correctly to each reason; a detected crisis never
blocks the message.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.monitoring import SAFETY_BLOCKS
from app.utils.rate_limit import check_and_enforce_rate_limit

logger = logging.getLogger(__name__)

# Crisis detection deliberately runs in every history scope (standard, roleplay,
# downbad). Suppressing it in roleplay/NSFW modes previously left the most
# vulnerable users invisible to every safety mechanism.
CRISIS_TERMS = (
    r"kill myself",
    r"suicide",
    r"end my life",
    r"want to die",
    r"hurt myself",
)


def matches_crisis(text: str) -> bool:
    """Return True if the text contains a crisis keyword (scope-independent)."""
    lowered = (text or "").lower()
    return any(re.search(term, lowered) for term in CRISIS_TERMS)


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """Outcome of the inbound safety gate.

    - ``rate_limited``: the message should be throttled (a "slow down" reply).
    - ``crisis``: a crisis keyword matched; the message is NOT blocked, but the
      caller should surface crisis resources and ensure the event is logged.
    """

    rate_limited: bool = False
    crisis: bool = False

    @property
    def allowed(self) -> bool:
        """Whether normal processing should proceed (only rate-limit blocks)."""
        return not self.rate_limited


class SafetyFilter:
    """Applies rate-limit checks and flags crisis language."""

    def evaluate(self, user_id: int, text: str) -> SafetyDecision:
        rate_limited = False
        try:
            if check_and_enforce_rate_limit(user_id):
                SAFETY_BLOCKS.labels(reason="rate_limit").inc()
                rate_limited = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rate-limit check failed open for user %s: %s", user_id, exc)

        crisis = matches_crisis(text)
        if crisis:
            SAFETY_BLOCKS.labels(reason="crisis_keyword").inc()

        return SafetyDecision(rate_limited=rate_limited, crisis=crisis)

    def allow(self, user_id: int, text: str) -> bool:
        """Backwards-compatible gate: only a rate-limit blocks the message.

        A crisis keyword no longer causes a block here — crisis handling is the
        responsibility of :class:`~app.domain.safety.service.SafetyService`,
        which always runs and logs the event.
        """
        return self.evaluate(user_id, text).allowed
