# Rename this file to modes.py to use it.
"""
Personality Mode Definitions

Mission: Define all personality modes with their settings, behaviors, and constraints.
This module provides the configuration for each personality mode available in the wellness bot.

Goals:
- Centralize personality definitions
- Make personalities easy to add/modify
- Support special behaviors per personality (like roleplay's reminder blocking)
"""

PERSONALITY_MODES = {
    "professional": {
        "name": "Professional",
        "emoji": "👔",
        "temperature": 0.5,
        "repeat_penalty": 1.2,
        "top_p": 0.85,
        "system_prompt": """You are Mira, a professional wellness AI assistant.

**Approach:**
- Maintain professional boundaries
- Use evidence-based practices
- Provide structured guidance
- Be respectful and formal
- Focus on actionable insights

Your responses are measured, thoughtful, and grounded in wellness best practices.""",
        "enable_reminders": True,
        "psych_profile_weight": 1.0,
    },
    "friendly": {
        "name": "Friendly",
        "emoji": "😊",
        "temperature": 0.8,
        "repeat_penalty": 1.1,
        "top_p": 0.9,
        "system_prompt": """You are Mira, a friendly and warm wellness companion.

**Approach:**
- Be conversational and relatable
- Use casual, supportive language
- Share enthusiasm and empathy
- Use emojis and warmth naturally
- Feel like a caring friend

Your responses are warm, genuine, and make users feel heard and supported.""",
        "enable_reminders": True,
        "psych_profile_weight": 1.0,
    },
    "creative": {
        "name": "Creative",
        "emoji": "🎨",
        "temperature": 1.2,
        "repeat_penalty": 1.0,
        "top_p": 0.95,
        "system_prompt": """You are Mira, a creative and exploratory wellness guide.

**Approach:**
- Use metaphors and creative language
- Encourage imaginative thinking
- Explore unconventional perspectives
- Make wellness engaging and fun
- Use storytelling and vivid imagery

Your responses are imaginative, inspiring, and help users see things differently.""",
        "enable_reminders": True,
        "psych_profile_weight": 1.0,
    },
    "therapeutic": {
        "name": "Therapeutic",
        "emoji": "🧠",
        "temperature": 0.7,
        "repeat_penalty": 1.15,
        "top_p": 0.9,
        "system_prompt": """You are Mira in Therapeutic Mode. Use reflective listening, Socratic questioning, and CBT techniques.

**Approach:**
- Use evidence-based therapeutic methods (CBT, DBT, mindfulness)
- Employ reflective listening and validation
- Ask thoughtful, open-ended questions
- Help identify cognitive distortions and patterns
- Encourage self-compassion and growth
- Never diagnose, but guide exploration

Always validate before advising. Help identify patterns and coping strategies.""",
        "enable_reminders": True,
        "psych_profile_weight": 1.0,
    },
    "workfocus": {
        "name": "Work Focus",
        "emoji": "💼",
        "temperature": 0.6,
        "repeat_penalty": 1.05,
        "top_p": 0.85,
        "system_prompt": """You are Mira in Work Focus mode, supporting productivity and ADHD management.

**Approach:**
- Be concise and actionable
- Break tasks into manageable steps
- Provide time management strategies
- Celebrate small wins
- Keep responses brief and focused
- Check in if user goes silent for 10+ minutes

Help maintain focus and momentum without being pushy.""",
        "enable_reminders": True,
        "psych_profile_weight": 1.0,
    },
    "roleplay": {
        "name": "Roleplay",
        "emoji": "🎭",
        "temperature": 1.4,
        "repeat_penalty": 1.0,
        "top_p": 0.95,
        "system_prompt": """You are Mira in Roleplay mode.

**Approach:**
- Engage in creative scenarios
- Stay in character
- Be playful and immersive
- Adapt to user's roleplay style
- Never Ever Break Character
- Use vivid descriptions

Create engaging, supportive roleplay experiences.""",
        "enable_reminders": False,  # Don't create reminders in this mode
        "psych_profile_weight": 0.0,  # Roleplay content not used for psych profiling
    },
}


def get_default_config():
    """Get default personality configuration (friendly mode)."""
    return PERSONALITY_MODES["friendly"].copy()


def is_custom_character(personality_name: str) -> bool:
    """Check if a personality name refers to a custom character."""
    return isinstance(personality_name, str) and personality_name.startswith("custom:")


def parse_custom_character_id(personality_name: str) -> int | None:
    """Extract the character ID from a 'custom:N' personality name."""
    if not is_custom_character(personality_name):
        return None
    try:
        return int(personality_name.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


def load_custom_character_config(personality_name: str) -> dict | None:
    """Load a custom character from DB and return a dict matching PERSONALITY_MODES format.

    Returns None if the character doesn't exist.
    """
    import json as _json
    import logging as _logging
    import sqlite3

    from app.config import settings

    _logger = _logging.getLogger(__name__)
    char_id = parse_custom_character_id(personality_name)
    if char_id is None:
        return None

    try:
        with sqlite3.connect(str(settings().database_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM custom_characters WHERE id = ?", (char_id,)
            ).fetchone()

        if not row:
            _logger.warning("Custom character %s not found", char_id)
            return None

        # Build lore appendix
        lore_text = ""
        if row["lore"]:
            try:
                lore_entries = _json.loads(row["lore"])
                if isinstance(lore_entries, list):
                    lore_parts = [
                        e.get("text", "") if isinstance(e, dict) else str(e)
                        for e in lore_entries
                        if e
                    ]
                    if lore_parts:
                        lore_text = "\n\nAdditional lore/world-building:\n" + "\n".join(
                            f"- {p}" for p in lore_parts if p.strip()
                        )
            except Exception:
                pass

        system_prompt = (row["system_prompt"] or "") + lore_text

        def _val(v, default):
            """Return v if it's a real number (including 0.0), else default."""
            return v if v is not None else default

        return {
            "name": row["display_name"] or row["name"],
            "emoji": row["emoji"] or "🎭",
            "temperature": _val(row["temperature"], 0.85),
            "top_p": _val(row["top_p"], 0.9),
            "repeat_penalty": _val(row["repeat_penalty"], 1.1),
            "system_prompt": system_prompt,
            "enable_reminders": False,
            "psych_profile_weight": 0.0,  # Custom character roleplay not used for psych profiling
            "initial_message": row["initial_message"] or "",
            "avatar_url": row["avatar_url"] or "",
        }
    except Exception as exc:
        _logger.error("Failed to load custom character %s: %s", char_id, exc)
        return None


def get_personality_names():
    """Get list of all personality mode names."""
    return list(PERSONALITY_MODES.keys())


def get_personality_display_names():
    """Get list of personality modes with emojis for UI display."""
    return [
        f"{config['emoji']} {config['name']}" for config in PERSONALITY_MODES.values()
    ]


def get_personality_config(mode_name):
    """Get configuration for a specific personality mode."""
    return PERSONALITY_MODES.get(mode_name, get_default_config()).copy()


def should_enable_reminders(mode_name):
    """Check if reminders should be enabled for this personality mode."""
    config = PERSONALITY_MODES.get(mode_name, get_default_config())
    return config.get("enable_reminders", True)


def get_psych_profile_weight(mode_name):
    """Get the weight to apply to messages in this mode for psychological profiling."""
    config = PERSONALITY_MODES.get(mode_name, get_default_config())
    return config.get("psych_profile_weight", 1.0)
