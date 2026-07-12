"""Lightweight feature flag registry for modular rollout."""

from __future__ import annotations

import json
import os
from threading import RLock
from typing import Dict

# Pydantic settings reads .env for known fields but does not push values into
# os.environ. APP_FEATURE_FLAGS is read directly via os.getenv below, so we
# must load .env into the process env. load_dotenv() will not overwrite vars
# that are already set by the OS, so this is safe.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from app.config import settings

_DEFAULT_FLAGS: Dict[str, bool] = {
    "user_feedback": False,
    "token_budget_dynamic": False,
    "nsfw_preferences": True,
    "adaptive_psych_tests": False,
    "profile_personalization_agent": False,
    "enhanced_text_cleaning": False,
    "prompt_layering": False,
    "multi_document_import": False,
    "conversation_memory_v2": True,
    "web_search_v2": False,
    "discord_bot": True,
    "llm_continuation_on_truncation": True,
    "llm_turn_planner_shadow": False,
    "llm_turn_planner_v1": False,
    # Legacy aliases kept for backward compatibility with older env payloads.
    "turn_planner_shadow": False,
    "turn_planner_llm_primary": False,
}

_runtime_overrides: Dict[str, bool] = {}
_runtime_lock = RLock()


def _parse_env_payload(raw: str | None) -> Dict[str, bool]:
    """Parse JSON or comma-separated overrides from environment."""
    if not raw:
        return {}

    raw = raw.strip()
    if not raw:
        return {}

    # Prefer JSON payloads: {"flag": true}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}

    if isinstance(parsed, dict):
        return {k: bool(v) for k, v in parsed.items()}

    overrides: Dict[str, bool] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        value = value.strip().lower()
        if not name:
            continue
        overrides[name] = value in {"1", "true", "yes", "on"}
    return overrides


def _merged_flags() -> Dict[str, bool]:
    """Create merged copy of defaults + config + env + runtime overrides."""
    cfg = settings()
    merged = dict(_DEFAULT_FLAGS)
    # Merge settings-level overrides
    for key, value in getattr(cfg, "feature_flags", {}).items():
        merged[key] = bool(value)
    # Merge env overrides
    env_var = getattr(cfg, "feature_flags_env_var", "APP_FEATURE_FLAGS")
    merged.update(_parse_env_payload(os.getenv(env_var)))
    # Merge runtime overrides last
    with _runtime_lock:
        merged.update(_runtime_overrides)
    return merged


def enabled(flag_name: str) -> bool:
    """Return True when flag is enabled."""
    return _merged_flags().get(flag_name, False)


def all_flags() -> Dict[str, bool]:
    """Expose current flag state for diagnostics."""
    return _merged_flags()


def set_runtime_flag(flag_name: str, value: bool) -> None:
    """Override a flag for the current process (useful in tests or admin UI)."""
    with _runtime_lock:
        _runtime_overrides[flag_name] = bool(value)
