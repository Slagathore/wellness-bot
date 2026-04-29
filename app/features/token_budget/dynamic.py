"""Dynamic context window selection helpers."""

from __future__ import annotations

from app.config import settings
from app.feature_flags import enabled

DEFAULT_TARGET = 32000
MIN_FLOOR = 8000

PERSONALITY_TARGETS = {
    "therapeutic": 64000,
    "professional": 48000,
    "friendly": 32000,
    "creative": 36000,
    "workfocus": 24000,
    "downbad": 24000,
    "roleplay": 24000,
}


def model_context_cap(model: str | None) -> int:
    """Return the maximum safe context window for the supplied model name."""

    cfg = settings()
    cap = cfg.ctx_token_budget
    if not model:
        return cap

    model_lower = model.lower()

    if "kimi" in model_lower and "cloud" in model_lower:
        return min(cap, 128000)

    if "gemini" in model_lower and "cloud" in model_lower:
        return min(cap, 120000)

    if (
        "deepseek" in model_lower or "qwen3-coder" in model_lower
    ) and "cloud" in model_lower:
        return min(cap, 64000)

    if "cloud" in model_lower or ":cloud" in model_lower:
        return min(cap, 64000)

    # Local models are memory-constrained; cap by parameter count.
    if any(size in model_lower for size in ("70b", "72b", "90b", "110b", "120b")):
        return min(cap, 16000)

    if any(size in model_lower for size in ("13b", "14b", "20b", "27b", "30b")):
        return min(cap, 8000)

    if any(size in model_lower for size in ("7b", "8b", "6b", "5b", "3b", "2b")):
        return min(cap, 6000)

    return cap


def resolve_context_window(model: str | None, personality: str | None = None) -> int:
    """Return the effective context budget respecting feature flags and personality."""

    cap = model_context_cap(model)
    if not enabled("token_budget_dynamic"):
        return cap

    personality_key = (personality or "").lower()
    target = PERSONALITY_TARGETS.get(personality_key, DEFAULT_TARGET)

    # Enforce bounds: never exceed model cap, never drop below safe floor.
    resolved = min(cap, target)
    if cap >= MIN_FLOOR:
        resolved = max(resolved, MIN_FLOOR)

    return resolved
