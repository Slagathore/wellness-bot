# Rename this file to persona_runtime.py to use it.
"""Resolve per-user personality and prompt runtime context."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.core.container import container
from app.db import db_ro
from app.orchestrator.context_builder import user_quick_reference
from app.orchestrator.prompt_builder import build_legacy_system_prompt
from app.personality.manager import PersonalityManager
from app.personality.modes import (
    PERSONALITY_MODES,
    get_default_config,
    is_custom_character,
    load_custom_character_config,
)
from app.runtime.services.preferences import PreferenceService

logger = logging.getLogger(__name__)

_PERSONALITY_MANAGER: PersonalityManager | None = None
_PERSONALITY_LOCK = threading.Lock()
_PREFERENCE_SERVICE: PreferenceService | None = None
_PREFERENCE_LOCK = threading.Lock()


@dataclass(slots=True)
class UserPersonaRuntime:
    personality_name: str
    personality_config: dict[str, Any]
    followups_enabled: bool
    quick_ref: str
    system_prompt: str


def resolve_user_model(user_id: int, requested_model: str | None = None) -> str | None:
    """Return explicit model override or user preferred model from onboarding data."""

    if requested_model:
        return requested_model

    try:
        with db_ro() as conn:
            row = conn.execute(
                "SELECT onboarding_data FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row and row["onboarding_data"]:
            data = _safe_json_loads(row["onboarding_data"], default={})
            preferred = data.get("preferred_model")
            if isinstance(preferred, str) and preferred.strip():
                return preferred.strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed resolving preferred model for user %s: %s", user_id, exc)

    return None


def get_user_personality_name(user_id: int) -> str:
    manager = _get_personality_manager()
    if manager is not None:
        try:
            name = manager.get_user_personality(user_id)
            if name in PERSONALITY_MODES or is_custom_character(name):
                return name
        except Exception as exc:  # noqa: BLE001
            logger.debug("Personality manager lookup failed for user %s: %s", user_id, exc)
    return _fallback_personality_name(user_id)


def get_user_personality_config(user_id: int) -> tuple[str, dict[str, Any]]:
    manager = _get_personality_manager()
    if manager is not None:
        try:
            name = manager.get_user_personality(user_id)

            # Handle custom characters
            if is_custom_character(name):
                config = load_custom_character_config(name)
                if config:
                    return name, config
                # Character missing — fall back
                name = "friendly"

            config = manager.get_active_config(user_id)
            if not name or name not in PERSONALITY_MODES:
                name = "friendly"
            if not isinstance(config, dict) or not config:
                config = PERSONALITY_MODES.get(name, get_default_config()).copy()
            return name, config
        except Exception as exc:  # noqa: BLE001
            logger.debug("Personality config lookup failed for user %s: %s", user_id, exc)

    name = _fallback_personality_name(user_id)
    return name, PERSONALITY_MODES.get(name, get_default_config()).copy()


def build_user_persona_runtime(
    *,
    user_id: int,
    profile_context: str | None,
) -> UserPersonaRuntime:
    """Build legacy-compatible prompt controls for a given user."""

    personality_name, personality_config = get_user_personality_config(user_id)

    pref_service = _get_preference_service()
    followups_enabled = True
    if pref_service is not None:
        try:
            followups_enabled = bool(pref_service.get_followup_pref(user_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Follow-up preference lookup failed for user %s: %s", user_id, exc)

    psych_weight = _safe_float(personality_config.get("psych_profile_weight"), default=1.0)
    quick_ref = user_quick_reference(user_id) if psych_weight > 0.5 else ""

    system_prompt = build_legacy_system_prompt(
        personality_name=personality_name,
        personality_config=personality_config,
        followups_enabled=followups_enabled,
        quick_ref=quick_ref,
        profile_context=profile_context,
        nsfw_context="",
    )

    return UserPersonaRuntime(
        personality_name=personality_name,
        personality_config=personality_config,
        followups_enabled=followups_enabled,
        quick_ref=quick_ref,
        system_prompt=system_prompt,
    )


def _get_personality_manager() -> PersonalityManager | None:
    global _PERSONALITY_MANAGER

    with _PERSONALITY_LOCK:
        if _PERSONALITY_MANAGER is not None:
            return _PERSONALITY_MANAGER

        try:
            resolved = container.resolve("personality_manager")
            if isinstance(resolved, PersonalityManager):
                _PERSONALITY_MANAGER = resolved
                return _PERSONALITY_MANAGER
        except Exception:
            pass

        try:
            cfg = settings()
            manager = PersonalityManager(
                config_path=Path(cfg.data_root) / "config.json",
                db_path=cfg.database_path,
            )
            _PERSONALITY_MANAGER = manager
            try:
                container.register("personality_manager", lambda: manager, singleton=True)
            except Exception:
                pass
            return _PERSONALITY_MANAGER
        except Exception as exc:  # noqa: BLE001
            logger.debug("Unable to initialize personality manager: %s", exc)
            return None


def _get_preference_service() -> PreferenceService | None:
    global _PREFERENCE_SERVICE

    with _PREFERENCE_LOCK:
        if _PREFERENCE_SERVICE is not None:
            return _PREFERENCE_SERVICE

        try:
            resolved = container.resolve("preference_service")
            if isinstance(resolved, PreferenceService):
                _PREFERENCE_SERVICE = resolved
                return _PREFERENCE_SERVICE
        except Exception:
            pass

        try:
            service = PreferenceService()
            _PREFERENCE_SERVICE = service
            try:
                container.register("preference_service", lambda: service, singleton=True)
            except Exception:
                pass
            return _PREFERENCE_SERVICE
        except Exception as exc:  # noqa: BLE001
            logger.debug("Unable to initialize preference service: %s", exc)
            return None


def _fallback_personality_name(user_id: int) -> str:
    with db_ro() as conn:
        try:
            row = conn.execute(
                "SELECT personality FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        except Exception:
            row = None

        if row and row[0]:
            value = str(row[0]).strip()
            if is_custom_character(value):
                return value
            value = value.lower()
            if value in PERSONALITY_MODES:
                return value

        row = conn.execute(
            "SELECT value FROM profile_context WHERE user_id = ? AND key = 'personality_mode'",
            (user_id,),
        ).fetchone()
        if row and row["value"]:
            value = str(row["value"]).strip().lower()
            if value in PERSONALITY_MODES:
                return value

    return "friendly"


def _safe_json_loads(raw: Any, *, default: dict[str, Any]) -> dict[str, Any]:
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
    except Exception:
        return default
    if isinstance(parsed, dict):
        return parsed
    return default


def _safe_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
