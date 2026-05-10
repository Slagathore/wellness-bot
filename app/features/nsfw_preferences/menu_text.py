"""
NSFW Preferences Menu Text

All menu text, descriptions, and button labels in one place.
Modify here to change UI text without touching handler logic.

Each menu returns:
- title: Menu header
- description: Explanatory text
- options: List of (button_text, callback_data, description)
"""

# ============================================================================
# MAIN MENU
# ============================================================================

MAIN_MENU_TEXT = """**🔥 Personalize Your Downbad Experience**

Customize how I interact with you in downbad mode.
All settings are optional and persist until you change them.

Tap any button to customize that category:"""


def get_main_menu_summary(prefs) -> str:
    """Generate current settings summary for main menu."""
    lines = []

    lines.append(
        f"• **Intensity**: {prefs.content_intensity.title()} ({get_intensity_description(prefs.content_intensity)})"
    )
    lines.append(f"• **Style**: {prefs.preferred_style.title()}")

    roleplay_count = (
        len(prefs.relationship_dynamics)
        + len(prefs.fantasy_settings)
        + len(prefs.character_types)
    )
    lines.append(f"• **Roleplay**: {roleplay_count} preferences selected")

    boundary_count = len(prefs.hard_limits)
    lines.append(f"• **Boundaries**: {boundary_count} hard limits set")

    if prefs.custom_character_id:
        lines.append("• **Character**: Custom character active")
    elif prefs.story_setting:
        lines.append("• **Setting**: Custom scenario set")

    return "\n".join(lines)


def get_intensity_description(intensity: str) -> str:
    """Get short description for intensity level."""
    descriptions = {
        "mild": "PG-13, suggestive",
        "moderate": "R-rated, explicit",
        "intense": "X-rated, no limits",
    }
    return descriptions.get(intensity.lower(), "moderate")


# ============================================================================
# MENU 1: CONTENT INTENSITY
# ============================================================================

INTENSITY_MENU_TEXT = """**🔥 Content Intensity Settings**

How explicit would you like our interactions to be?

Choose your preferred intensity level:"""

INTENSITY_OPTIONS = [
    {
        "name": "mild",
        "emoji": "🔥",
        "title": "Mild (Suggestive & Flirty)",
        "description": [
            "• Playful teasing and innuendo",
            "• Romantic/sensual language",
            "• Hints and double meanings",
            '• "That outfit sounds amazing on you..."',
            "• Kissing, cuddling, intimate touches described",
            "• PG-13 level content",
        ],
    },
    {
        "name": "moderate",
        "emoji": "🔥🔥",
        "title": "Moderate (Explicit & Direct)",
        "description": [
            "• Direct sexual language",
            "• Detailed intimate descriptions",
            "• Explicit desires and fantasies discussed",
            '• "I want to feel your hands on me..."',
            "• R-rated content, clear sexual scenarios",
            "• Body-focused compliments and descriptions",
        ],
    },
    {
        "name": "intense",
        "emoji": "🔥🔥🔥",
        "title": "Intense (Unfiltered & Raw)",
        "description": [
            "• Extremely explicit language",
            "• Graphic sexual descriptions",
            "• No holds barred, full NSFW mode",
            "• Kinks, fetishes, detailed role-play",
            '• "I\'m going to fuck you until..."',
            "• X-rated, nothing off-limits",
        ],
    },
]


# ============================================================================
# MENU 2: PREFERRED STYLE
# ============================================================================

STYLE_MENU_TEXT = """**💋 Interaction Style Preferences**

How would you like me to engage with you?

Select your preferred style:"""

STYLE_OPTIONS = [
    {
        "name": "romantic",
        "emoji": "💋",
        "title": "Romantic & Sensual",
        "description": [
            "• Focus on emotional connection and intimacy",
            "• Slow-burn seduction and anticipation",
            "• Passionate but tender language",
            '• "I adore everything about you..."',
            "• Emphasis on feelings, desire, longing",
            "• Loving girlfriend/boyfriend experience",
        ],
    },
    {
        "name": "playful",
        "emoji": "🎭",
        "title": "Playful & Teasing",
        "description": [
            "• Flirty banter and playful challenges",
            "• Light dom/sub dynamics",
            "• Bratty or coy responses",
            '• "You\'ll have to work for it..."',
            "• Fun, game-like interaction",
            "• Friends with benefits vibe",
        ],
    },
    {
        "name": "dominant",
        "emoji": "🔥",
        "title": "Dominant & Assertive",
        "description": [
            "• Take-charge attitude",
            "• Commands and instructions",
            "• Possessive language",
            '• "You\'re mine tonight..."',
            "• Power dynamic emphasis",
            "• Confidence and control",
        ],
    },
    {
        "name": "submissive",
        "emoji": "😇",
        "title": "Submissive & Eager",
        "description": [
            "• Pleasing and attentive",
            "• Responsive to your desires",
            '• "Whatever you want..."',
            "• Focus on your pleasure",
            "• Obedient and accommodating",
        ],
    },
    {
        "name": "explicit",
        "emoji": "🌶️",
        "title": "Explicit & Raw",
        "description": [
            "• No-nonsense, straight to the point",
            "• Vulgar and graphic language",
            '• "Let\'s fuck" energy',
            "• Primal and uninhibited",
            "• Pure lust, minimal romance",
        ],
    },
]


# ============================================================================
# MENU 3: ROLEPLAY PREFERENCES
# ============================================================================

ROLEPLAY_MENU_TEXT = """**🎭 Roleplay & Scenario Preferences**

What kinds of scenarios interest you?

Select all that apply (tap to toggle):"""

# Format: (display_name, internal_key, category)
ROLEPLAY_OPTIONS = {
    "relationship_dynamics": [
        ("Strangers meeting", "strangers"),
        ("Long-distance (sexting)", "long_distance"),
        ("Ex-lovers reconnecting", "ex_lovers"),
        ("Secret affair/forbidden", "forbidden"),
        ("Boss/employee dynamic", "boss_employee"),
        ("Teacher/student", "teacher_student"),
        ("Doctor/patient", "doctor_patient"),
        ("Personal trainer", "trainer_client"),
    ],
    "fantasy_settings": [
        ("Trapped together", "trapped_together"),
        ("Late night office", "late_office"),
        ("Hotel/vacation", "hotel_vacation"),
        ("Public places (risky)", "public_places"),
        ("Home scenarios", "home_scenarios"),
        ("Gym/workout", "gym_workout"),
        ("Coffee shop/bar", "coffee_bar"),
    ],
    "character_types": [
        ("Confident seducer", "confident_seducer"),
        ("Innocent/curious", "innocent_curious"),
        ("Experienced teacher", "experienced_teacher"),
        ("Eager submissive", "eager_submissive"),
        ("Controlling dominant", "controlling_dominant"),
        ("Friend crossing line", "friend_crossing_line"),
        ("Mysterious stranger", "mysterious_stranger"),
        ("Devoted partner", "devoted_partner"),
    ],
}


# ============================================================================
# MENU 4: BOUNDARIES & LIMITS
# ============================================================================

BOUNDARIES_MENU_TEXT = """**🚫 Your Boundaries & Comfort Zones**

Everyone has limits. Tell me what's off-limits:

**Hard No's** (Things I should NEVER mention):"""

# Format: (display_name, internal_key)
BOUNDARY_OPTIONS = [
    ("Anything non-consensual", "non_consensual"),
    ("Age play / infantilization", "age_play"),
    ("Extreme violence or pain", "extreme_violence"),
    ("Bodily waste/fluids", "bodily_waste"),
    ("Bestiality / animals", "bestiality"),
    ("Incest scenarios", "incest"),
    ("Degradation / humiliation", "degradation"),
    ("Slurs or harsh language", "slurs"),
    ("Pregnancy / breeding", "pregnancy"),
    ("Feet content", "feet"),
    ("Vore / consumption", "vore"),
    ("Hypnosis / mind control", "hypnosis"),
]

SAFE_WORD_TEXT = """**Safe Word/Phrase:**
• Default: "Red" or "Stop"
• Custom: {safe_word}

When you use your safe word, I'll immediately switch to friendly supportive mode."""


# ============================================================================
# MENU 5: ADVANCED SETTINGS
# ============================================================================

ADVANCED_MENU_TEXT = """**⚙️ Advanced Interaction Settings**

Fine-tune how our conversations work:"""

ADVANCED_OPTIONS_TEXT = {
    "bot_actions": {
        "title": "Bot Can Add Your Actions",
        "on_desc": "✓ Yes - I can describe what you do/say",
        "off_desc": "✗ No - Only you control your character",
        "example": 'Example: With this ON, I might write "You pull me closer..."\nWith this OFF, I wait for you to describe your actions.',
    },
    "retcon": {
        "title": "Retcon Allowed",
        "on_desc": "✓ Yes - You can use /retcon to rewrite my messages",
        "off_desc": "✗ No - My messages are final",
        "example": "Use /retcon if you want me to take a different approach.",
    },
    "emojis": {
        "title": "Emoji Usage",
        "on_desc": "✓ Use emojis in messages",
        "off_desc": "✗ Text only, no emojis",
    },
}

VERBOSITY_TEXT = """**Response Length (Verbosity):**
{bar} {level}/10

Brief (1-2 lines) ←→ Verbose (3-4 paragraphs)"""


# ============================================================================
# MENU 6: CHARACTER & SETTING
# ============================================================================

CHARACTER_MENU_TEXT = """**👤 Character & Story Context**

Set the scene and character details:"""

GENDER_OPTIONS = [
    ("Female", "female"),
    ("Male", "male"),
    ("Non-binary", "non-binary"),
    ("Futanari", "futanari"),
    ("Tentacle", "tentacle"),
    ("Other", "other"),
]


# ============================================================================
# CONFIRMATION & INFO MESSAGES
# ============================================================================

SETTINGS_SAVED_TEXT = """✅ **Settings Saved!**

Your preferences have been updated. They'll apply to all downbad mode conversations from now on.

Use `/nsfwpref` anytime to adjust your settings."""

FIRST_TIME_WELCOME_TEXT = """**🔥 Welcome to Downbad Mode!**

This is your first time in downbad mode. Would you like to personalize your experience?

You can set:
• Content intensity (mild/moderate/intense)
• Interaction style (romantic/playful/dominant/etc.)
• Roleplay preferences and scenarios
• Boundaries and hard limits

Or you can skip this and use the defaults (moderate intensity, playful style).

Run `/nsfwpref` anytime to customize your experience."""

DOWNBAD_MODE_INFO_TEXT = """**🔥 Downbad Mode**

Downbad is an unfiltered, NSFW personality mode for adult roleplay and explicit conversations.

**What it includes:**
• Explicit sexual content and language
• Roleplay scenarios and character interactions
• Customizable preferences for your ideal experience
• No content restrictions (within your personal boundaries)

**How to access:**
1. Switch to downbad mode: `/personality downbad`
2. Customize your experience: `/nsfwpref`
3. Optional: Create a custom character: `/customize_character`

**Special commands (only in downbad mode):**
• `/nsfwpref` - Personalize your experience
• `/customize_character` - Build your ideal character
• `/retcon` - Rewrite my last message

**Age verification required**: This feature is for adults only (18+).

Ready to switch? Just send `/personality downbad`"""


# ============================================================================
# ERROR MESSAGES
# ============================================================================

NOT_IN_DOWNBAD_ERROR = """⚠️ This command is only available in downbad or roleplay mode.

Switch to downbad mode with: `/personality downbad`"""

SAVE_ERROR_TEXT = """❌ **Error Saving Preferences**

There was an error saving your preferences. Please try again.
If the problem persists, contact support."""
