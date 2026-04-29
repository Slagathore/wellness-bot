"""Profile context caching and quick-reference service."""

from __future__ import annotations

import threading

from app.db import db_ro


class ProfileContextService:
    """Caches profile context and quick reference snippets for Telegram replies."""

    def __init__(self, bot, *, cache, lock: threading.Lock) -> None:
        self.bot = bot
        self._cache = cache
        self._lock = lock

    # Cache helpers ---------------------------------------------------------

    def invalidate(self, user_id: int) -> None:
        with self._lock:
            self._cache.pop(f"quick_ref_{user_id}", None)
            self._cache.pop(f"profile_ctx_{user_id}", None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    # Profile context -------------------------------------------------------

    def get_user_profile_context(self, user_id: int) -> str:
        cache_key = f"profile_ctx_{user_id}"
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        profile = self.bot._get_psych_profile_data(user_id)
        if not profile:
            return ""

        mh = profile.get("mental_health_indicators", {})
        bf = profile.get("big_five", {})
        cog = profile.get("cognitive_metrics", {})
        att = profile.get("attachment_style", {})
        comm = profile.get("communication_patterns", {})

        depression = self.bot._extract_metric_value(mh, "depression_likelihood", 0)
        anxiety = self.bot._extract_metric_value(mh, "anxiety_likelihood", 0)
        extraversion = self.bot._extract_metric_value(bf, "extraversion", 0.5)
        neuroticism = self.bot._extract_metric_value(bf, "neuroticism", 0.5)
        est_iq = self.bot._extract_metric_value(cog, "estimated_iq", 100)
        vocab_complexity = self.bot._extract_metric_value(
            cog, "vocabulary_complexity", 0.5
        )
        formality = self.bot._extract_metric_value(comm, "formality_level", 0.5)

        attachment_type = (
            att.get("primary_type", "unknown") if isinstance(att, dict) else str(att)
        )

        context = f"""
PSYCHOLOGICAL PROFILE (CONFIDENTIAL - Do not mention to user):
- Mental Health: Depression:{depression:.2f} Anxiety:{anxiety:.2f}
- Personality: Extraversion:{extraversion:.2f} Neuroticism:{neuroticism:.2f}
- Cognitive: Est.IQ:{est_iq:.0f} Complexity:{vocab_complexity:.2f}
- Style: {attachment_type} attachment pattern, Formality:{formality:.2f}

Use this insight to tailor tone, complexity, and emotional support.
"""
        with self._lock:
            self._cache[cache_key] = context
        return context

    # Quick reference -------------------------------------------------------

    def get_user_quick_reference(self, user_id: int) -> str:
        cache_key = f"quick_ref_{user_id}"
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        quick_ref = ""
        with db_ro() as conn:
            user = conn.execute(
                "SELECT telegram_username, display_name, onboarding_data FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not user:
                return ""

            onboarding = self.bot._safe_json_loads(
                user["onboarding_data"], {}, context="onboarding quick ref"
            )
            quick_ref = "\n=== QUICK USER REFERENCE ===\n"
            quick_ref += f"Name: {user['display_name']}\n"
            if onboarding:
                quick_ref += f"Check-in Frequency: {onboarding.get('check_in_frequency', 'not set')}\n"
                pronouns = onboarding.get("pronouns")
                if pronouns:
                    quick_ref += f"Pronouns: {pronouns}\n"
                support_pref = onboarding.get("support_preference")
                if support_pref:
                    quick_ref += f"Preferred Support: {support_pref}\n"
                nsfw_enabled = onboarding.get("nsfw_opt_in")
                if nsfw_enabled is not None:
                    quick_ref += (
                        f"NSFW Access: {'enabled' if nsfw_enabled else 'disabled'}\n"
                    )
                focus = onboarding.get("focus_areas")
                if isinstance(focus, list) and focus:
                    quick_ref += f"Focus Areas: {', '.join(focus)}\n"

            latest_summary = self.bot._get_latest_assessment_summary(user_id)
            if latest_summary:
                quick_ref += f"Assessment Snapshot: {latest_summary}\n"

            memory_notes = self.bot._get_memory_notes(user_id)
            if memory_notes:
                quick_ref += "Personal Notes:\n"
                quick_ref += "".join(
                    f"- {note.get('summary')}\n"
                    for note in memory_notes
                    if note.get("summary")
                )

            imported_profile = self.bot._get_imported_profile_summary(user_id)
            if imported_profile:
                quick_ref += f"{imported_profile}\n"

            interests = (
                onboarding.get("interests", []) if isinstance(onboarding, dict) else []
            )
            quirks = (
                onboarding.get("quirks", []) if isinstance(onboarding, dict) else []
            )
            profile = self.bot._get_psych_profile_data(user_id)
            if profile:
                indicators = profile.get("mental_health_indicators", {})
                depression = self.bot._extract_metric_value(
                    indicators, "depression_likelihood", 0
                )
                anxiety = self.bot._extract_metric_value(
                    indicators, "anxiety_likelihood", 0
                )
                if depression >= 0.7:
                    quick_ref += "⚠ Elevated depression indicators\n"
                if anxiety >= 0.7:
                    quick_ref += "⚠ Elevated anxiety indicators\n"
            if interests:
                quick_ref += f"Interests: {', '.join(interests[:5])}\n"
            if quirks:
                quick_ref += f"Note: {', '.join(quirks[:2])}\n"

            quick_ref += "===========================\n"

        with self._lock:
            self._cache[cache_key] = quick_ref
        return quick_ref
