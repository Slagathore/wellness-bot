"""Preference helpers for follow-ups and NSFW configuration."""

from __future__ import annotations

import copy
import json
import logging
import threading
from collections import OrderedDict
from pathlib import Path

from app.config import settings
from app.db import db_ro, db_rw
from app.utils.time_utils import format_operator_datetime, operator_now

DEFAULT_HARD_LIMITS: list[str] = [
    "Non-consensual scenarios",
    "Age play / infantilization",
    "Extreme violence",
    "Bodily waste / scat",
    "Bestiality",
    "Incest",
    "Degradation / slurs",
    "Pregnancy / breeding",
    "Feet content",
]

DEFAULT_NSFW_PREFERENCES: dict = {
    "nsfw_opt_in": False,
    "downbad_submode": "standard",
    "content_intensity": "moderate",
    "interaction_style": "playful_teasing",
    "roleplay_scenarios": [],
    "hard_limits": DEFAULT_HARD_LIMITS.copy(),
    "soft_limits": [],
    "safe_word": "Red",
    "user_gender": "unspecified",
    "bot_gender": "female",
    "dominance_preference": "balanced",
    "pacing": "medium",
    "verbosity": "medium",
    "allow_bot_actions": True,
    "allow_retcon": True,
    "kinks": [],
    "story_setting": "",
}


def _merge_defaults(existing: dict) -> dict:
    base = copy.deepcopy(DEFAULT_NSFW_PREFERENCES)
    for key, value in (existing or {}).items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            base[key].update(value)
        else:
            base[key] = value
    return base


class PreferenceService:
    """Encapsulates preference lookups and caching."""

    def __init__(
        self, *, followup_cache_size: int = 1000, logger: logging.Logger | None = None
    ) -> None:
        self._followup_cache: OrderedDict[int, bool] = OrderedDict()
        self._followup_cache_size = followup_cache_size
        self._followup_lock = threading.Lock()
        self.logger = logger or logging.getLogger(__name__)

    # Follow-up preferences -------------------------------------------------

    def get_followup_pref(self, user_id: int) -> bool:
        cached = self._followup_cache_get(user_id)
        if cached is not None:
            return cached
        enabled = True
        try:
            with db_ro() as conn:
                row = conn.execute(
                    "SELECT value FROM profile_context WHERE user_id = ? AND key = 'followup_reminders_enabled'",
                    (user_id,),
                ).fetchone()
                if row:
                    enabled = str(row["value"]).lower() not in {
                        "0",
                        "false",
                        "off",
                        "no",
                    }
        except Exception:  # noqa: BLE001
            enabled = True
        self._followup_cache_set(user_id, enabled)
        return enabled

    def set_followup_pref(self, user_id: int, enabled: bool) -> None:
        value = "true" if enabled else "false"
        timestamp_now = format_operator_datetime(operator_now())
        try:
            with db_rw() as conn:
                conn.execute(
                    """
                    INSERT INTO profile_context (user_id, key, value)
                    VALUES (?, 'followup_reminders_enabled', ?)
                    ON CONFLICT(user_id, key)
                    DO UPDATE SET value = excluded.value, updated_at = ?
                    """,
                    (user_id, value, timestamp_now),
                )
        except Exception:  # noqa: BLE001
            pass
        self._followup_cache_set(user_id, enabled)

    def should_allow_followup(self, message_lower: str) -> bool:
        followup_phrases = [
            "remind me",
            "please remind",
            "follow up",
            "check in on me",
            "check on me",
            "check in later",
            "ping me",
            "reach out",
            "ask me later",
            "see how i am later",
            "keep me accountable",
            "can you check",
        ]
        return any(phrase in message_lower for phrase in followup_phrases)

    def should_disable_followups(self, message_lower: str) -> bool:
        disable_phrases = [
            "no more reminders",
            "stop reminders",
            "stop the reminders",
            "too many reminders",
            "dont remind me",
            "don't remind me",
            "no follow up",
            "stop following up",
            "pause the reminders",
            "pause reminders",
            "you shouldnt be having so many reminders",
            "you shouldn't be having so many reminders",
        ]
        return any(phrase in message_lower for phrase in disable_phrases)

    def should_enable_followups(self, message_lower: str) -> bool:
        enable_phrases = [
            "its okay to follow up",
            "it's okay to follow up",
            "you can follow up",
            "resume reminders",
            "its ok to remind me later",
            "it's ok to remind me later",
            "please check in on me later",
            "feel free to check on me later",
        ]
        return any(phrase in message_lower for phrase in enable_phrases)

    def clear_cache(self) -> None:
        with self._followup_lock:
            self._followup_cache.clear()

    # NSFW preferences ------------------------------------------------------

    def get_nsfw_opt_in(self, user_id: int) -> bool:
        """Return True when the user has opted into NSFW/downbad content."""

        try:
            with db_ro() as conn:
                row = conn.execute(
                    "SELECT value FROM profile_context WHERE user_id = ? AND key = 'nsfw_opt_in'",
                    (user_id,),
                ).fetchone()
            if row:
                return str(row["value"]).lower() in {"1", "true", "yes", "on"}

            with db_ro() as conn:
                row = conn.execute(
                    "SELECT onboarding_data FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
            if row and row["onboarding_data"]:
                data = self._safe_json_loads(
                    row["onboarding_data"], {}, context="onboarding data"
                )
                value = data.get("nsfw_opt_in")
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    return value.lower() in {"1", "true", "yes", "on"}
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "NSFW preference lookup failed for user %s: %s",
                user_id,
                exc,
                exc_info=True,
            )
        return False

    def load_nsfw_preferences(self, user_id: int, telegram_id: int) -> dict:
        """Load NSFW preferences for the user from database or JSON backup."""

        prefs: dict = {}
        try:
            with db_ro() as conn:
                row = conn.execute(
                    "SELECT value FROM profile_context WHERE user_id = ? AND key = 'nsfw_preferences'",
                    (user_id,),
                ).fetchone()
            if row and row["value"]:
                prefs = json.loads(row["value"])
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(
                "NSFW preference DB lookup failed for user %s: %s", user_id, exc
            )
            prefs = {}

        if not prefs:
            data_root = Path(settings().data_root)
            file_path = data_root / "nsfw_preferences" / f"{telegram_id}.json"
            if file_path.exists():
                try:
                    prefs = json.loads(file_path.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    self.logger.debug(
                        "NSFW preference file read failed for user %s: %s", user_id, exc
                    )
                    prefs = {}

        merged = _merge_defaults(prefs)
        merged["nsfw_opt_in"] = self.get_nsfw_opt_in(user_id)
        return merged

    def format_nsfw_context(self, prefs: dict) -> str:
        """Format NSFW preferences into a heavily weighted system prompt section."""

        if not prefs or not prefs.get("nsfw_opt_in"):
            return ""

        sections: list[str] = []
        sections.append("=" * 80)
        sections.append("🔥 USER'S EXPLICIT NSFW PREFERENCES (MAXIMUM PRIORITY) 🔥")
        sections.append("=" * 80)
        sections.append("")
        sections.append("FLIRTY/NSFW MODE: ACTIVE")
        sections.append("")
        sections.append(
            "⚠️ IMPORTANT: These preferences OVERRIDE the default personality settings above."
        )
        sections.append(
            "The user has explicitly configured their ideal experience. Follow these exactly."
        )
        sections.append("")

        submode = str(prefs.get("downbad_submode", "standard")).lower().strip()
        if submode == "roleplay":
            sections.append("**Downbad Submode:** 🎭 ROLEPLAY")
            sections.append(
                "Stay fully in-character scene-by-scene while preserving consent, safeword, and configured limits."
            )
            sections.append(
                "Treat roleplay_scenarios and story_setting as active canon unless the user changes them."
            )
            sections.append("")
        else:
            sections.append("**Downbad Submode:** 🔥 STANDARD")
            sections.append(
                "Focus on direct flirtation and intimacy over sustained roleplay narrative unless the user asks for one."
            )
            sections.append("")

        intensity_map = {
            "mild": "🌸 MILD: Keep things suggestive and flirty. Innuendo over explicit language.",
            "moderate": "🔥 MODERATE: Be explicit and direct. Don't hold back, but keep some romance.",
            "intense": "💥 INTENSE: Raw, vulgar, no-holds-barred. The user wants MAXIMUM intensity.",
        }
        intensity = prefs.get("content_intensity", "moderate")
        sections.append(
            f"**Content Intensity:** {intensity_map.get(intensity, intensity)}"
        )
        sections.append("")

        style_map = {
            "romantic_sensual": "💕 ROMANTIC & SENSUAL: Emphasize emotional connection, tenderness, intimacy. Make it beautiful.",
            "playful_teasing": "😏 PLAYFUL & TEASING: Flirty banter, bratty energy, keep them on their toes.",
            "naive_shy": "🥺 NAIVE / SHY: Inexperienced, blushy, hesitant, and easily flustered. Let the nervous sweetness show.",
            "bratty": "😈 BRATTY: Mischievous push-pull, playful defiance, taunting little challenges, and teasing attitude.",
            "tsundere": "🔥 TSUNDERE: Alternate between denial and affection. Play tough, then let warmth leak through when the moment lands.",
            "dominant_assertive": "👑 DOMINANT & ASSERTIVE: Take charge confidently. Be directive. Show authority.",
            "submissive_eager": "🥺 SUBMISSIVE & EAGER: Focus on pleasing them. Be responsive. Let them lead.",
            "gentle_caregiver": "🫶 GENTLE CAREGIVER: Be soft, reassuring, affectionate, and attentive. Prioritize comfort and emotional closeness.",
            "worshipful_devoted": "🙏 WORSHIPFUL / DEVOTED: Adore them openly. Use praise, reverence, and eager devotion.",
            "explicit_raw": "💦 EXPLICIT & RAW: Direct, vulgar, hungry. No romance - pure lust.",
        }
        style = prefs.get("interaction_style", "playful_teasing")
        sections.append(f"**Interaction Style:** {style_map.get(style, style)}")
        sections.append("")

        dom_map = {
            "submissive": "🥺 LOW DOMINANCE: Mira follows the user's lead completely.",
            "balanced": "⚖️ BALANCED: Take turns leading. Read the room and match their energy.",
            "dominant": "👑 HIGH DOMINANCE: Mira is assertive and directive. Take control.",
        }
        dominance = prefs.get("dominance_preference", "balanced")
        sections.append(f"**Dominance Level:** {dom_map.get(dominance, dominance)}")
        sections.append("")

        pacing_map = {
            "fast": "⚡ FAST PACING: Jump right in. Don't waste time with buildup.",
            "medium": "🔥 MEDIUM PACING: Build some anticipation, but don't drag it out.",
            "slow": "🌹 SLOW BURN: Lots of buildup. Tease. Make them wait for it.",
        }
        pacing = prefs.get("pacing", "medium")
        sections.append(f"**Pacing:** {pacing_map.get(pacing, pacing)}")
        sections.append("")

        verb_map = {
            "concise": "📝 CONCISE: Short, punchy replies. Get to the point.",
            "medium": "📖 BALANCED: Mix of detail and brevity. Natural flow.",
            "detailed": "📚 DETAILED & DESCRIPTIVE: Paint a vivid picture. Lush, detailed prose.",
        }
        verbosity = prefs.get("verbosity", "medium")
        sections.append(f"**Response Style:** {verb_map.get(verbosity, verbosity)}")
        sections.append("")

        user_gender = prefs.get("user_gender", "unspecified")
        bot_gender = prefs.get("bot_gender", "female")
        if user_gender != "unspecified" or bot_gender != "female":
            sections.append("**Gender Presentation:**")
            if user_gender != "unspecified":
                sections.append(f"  - Refer to user as: {user_gender}")
            if bot_gender != "female":
                sections.append(f"  - Mira presents as: {bot_gender}")
            sections.append("")

        kinks = prefs.get("kinks", [])
        if kinks:
            sections.append("**Kinks & Interests (incorporate these naturally):**")
            for kink in kinks:
                sections.append(f"  ✓ {kink}")
            sections.append("")

        roleplay = prefs.get("roleplay_scenarios", [])
        if roleplay:
            sections.append("**Preferred Roleplay Scenarios:**")
            for scenario in roleplay:
                sections.append(f"  ✓ {scenario}")
            sections.append("")

        story = prefs.get("story_setting", "")
        if story:
            sections.append(f"**Current Scene/Story:** {story}")
            sections.append("")

        hard_limits = prefs.get("hard_limits", [])
        if hard_limits:
            sections.append("🚫 **HARD LIMITS - NEVER INCLUDE THESE:**")
            for limit in hard_limits:
                sections.append(f"  ✗ {limit}")
            sections.append("")

        soft_limits = prefs.get("soft_limits", [])
        if soft_limits:
            sections.append("⚠️ **SOFT LIMITS - Be cautious with these:**")
            for limit in soft_limits:
                sections.append(f"  ~ {limit}")
            sections.append("")

        safe_word = prefs.get("safe_word", "Red")
        sections.append(f"**Safe Word:** {safe_word}")
        sections.append(
            f"If the user says '{safe_word}', immediately stop and check in with them."
        )
        sections.append("")

        allow_bot_actions = prefs.get("allow_bot_actions", True)
        allow_retcon = prefs.get("allow_retcon", True)
        sections.append("**Scene Rules:**")
        if allow_bot_actions:
            sections.append(
                "  ✓ You MAY describe the user's actions (e.g., 'you reach out and...')"
            )
        else:
            sections.append(
                "  ✗ Do NOT describe the user's actions. Only describe Mira's actions."
            )
        if allow_retcon:
            sections.append("  ✓ User can retcon/rewind Mira's actions")
        sections.append("")

        sections.append("=" * 80)
        sections.append(
            "🎯 SUMMARY: Follow these preferences EXACTLY. They override the default prompt."
        )
        sections.append("=" * 80)
        sections.append("")
        return "\n".join(sections)

    # Internal helpers ------------------------------------------------------

    def _followup_cache_get(self, user_id: int) -> bool | None:
        with self._followup_lock:
            if user_id in self._followup_cache:
                value = self._followup_cache.pop(user_id)
                self._followup_cache[user_id] = value
                return value
            return None

    def _followup_cache_set(self, user_id: int, value: bool) -> None:
        with self._followup_lock:
            if user_id in self._followup_cache:
                self._followup_cache.pop(user_id)
            elif len(self._followup_cache) >= self._followup_cache_size:
                self._followup_cache.popitem(last=False)
            self._followup_cache[user_id] = value

    def _safe_json_loads(self, raw, default, *, context: str = ""):
        try:
            import json

            return json.loads(raw)
        except Exception:  # noqa: BLE001
            self.logger.debug("Failed to parse JSON for %s", context)
            return default
