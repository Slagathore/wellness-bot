"""
NSFW Preferences - Inline Keyboard Builders

Builds Telegram inline keyboards for each menu.
Separated from handlers for clarity and testability.

Each function returns: InlineKeyboardMarkup
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from .models import NSFWPreferences
from . import menu_text


# ============================================================================
# MAIN MENU KEYBOARD
# ============================================================================


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Build the main NSFW preferences menu.

    Returns 6 category buttons + Done button.
    """
    keyboard = [
        [
            InlineKeyboardButton(
                "🔥 Content Intensity", callback_data="nsfw_menu_intensity"
            )
        ],
        [InlineKeyboardButton("💋 Preferred Style", callback_data="nsfw_menu_style")],
        [
            InlineKeyboardButton(
                "🎭 Roleplay Preferences", callback_data="nsfw_menu_roleplay"
            )
        ],
        [
            InlineKeyboardButton(
                "🚫 Boundaries & Limits", callback_data="nsfw_menu_boundaries"
            )
        ],
        [
            InlineKeyboardButton(
                "⚙️ Advanced Settings", callback_data="nsfw_menu_advanced"
            )
        ],
        [
            InlineKeyboardButton(
                "👤 Character & Setting", callback_data="nsfw_menu_character"
            )
        ],
        [InlineKeyboardButton("✅ Done & Save", callback_data="nsfw_done")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ============================================================================
# MENU 1: CONTENT INTENSITY KEYBOARD
# ============================================================================


def build_intensity_keyboard(current_intensity: str) -> InlineKeyboardMarkup:
    """
    Build keyboard for content intensity selection.

    Shows 3 options (mild/moderate/intense) with checkmark on current.
    """
    keyboard = []

    for option in menu_text.INTENSITY_OPTIONS:
        # Add checkmark if this is current selection
        check = "✓ " if option["name"] == current_intensity.lower() else ""
        button_text = f"{check}{option['emoji']} {option['title']}"

        keyboard.append(
            [
                InlineKeyboardButton(
                    button_text, callback_data=f"nsfw_intensity_{option['name']}"
                )
            ]
        )

    # Navigation buttons
    keyboard.append(
        [
            InlineKeyboardButton("← Back", callback_data="nsfw_menu_main"),
            InlineKeyboardButton("Save ✓", callback_data="nsfw_save_intensity"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)


# ============================================================================
# MENU 2: PREFERRED STYLE KEYBOARD
# ============================================================================


def build_style_keyboard(current_style: str) -> InlineKeyboardMarkup:
    """
    Build keyboard for interaction style selection.

    Shows 5 style options with checkmark on current.
    """
    keyboard = []

    for option in menu_text.STYLE_OPTIONS:
        check = "✓ " if option["name"] == current_style.lower() else ""
        button_text = f"{check}{option['emoji']} {option['title']}"

        keyboard.append(
            [
                InlineKeyboardButton(
                    button_text, callback_data=f"nsfw_style_{option['name']}"
                )
            ]
        )

    # Navigation
    keyboard.append(
        [
            InlineKeyboardButton("← Back", callback_data="nsfw_menu_main"),
            InlineKeyboardButton("Save ✓", callback_data="nsfw_save_style"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)


# ============================================================================
# MENU 3: ROLEPLAY PREFERENCES KEYBOARD
# ============================================================================


def build_roleplay_keyboard(prefs: NSFWPreferences) -> InlineKeyboardMarkup:
    """
    Build keyboard for roleplay preferences.

    Shows 3 categories with checkboxes. User can toggle multiple.
    This is the most complex keyboard.
    """
    keyboard = []

    # Add section header button (non-clickable info)
    keyboard.append(
        [InlineKeyboardButton("📋 Relationship Dynamics", callback_data="nsfw_noop")]
    )

    # Add relationship dynamics options
    for display_name, internal_key in menu_text.ROLEPLAY_OPTIONS[
        "relationship_dynamics"
    ]:
        check = "☑" if internal_key in prefs.relationship_dynamics else "☐"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{check} {display_name}",
                    callback_data=f"nsfw_roleplay_dyn_{internal_key}",
                )
            ]
        )

    # Fantasy settings header
    keyboard.append(
        [InlineKeyboardButton("🌟 Fantasy Settings", callback_data="nsfw_noop")]
    )

    # Add fantasy settings options
    for display_name, internal_key in menu_text.ROLEPLAY_OPTIONS["fantasy_settings"]:
        check = "☑" if internal_key in prefs.fantasy_settings else "☐"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{check} {display_name}",
                    callback_data=f"nsfw_roleplay_set_{internal_key}",
                )
            ]
        )

    # Character types header
    keyboard.append(
        [InlineKeyboardButton("🎭 Character Types I Play", callback_data="nsfw_noop")]
    )

    # Add character type options
    for display_name, internal_key in menu_text.ROLEPLAY_OPTIONS["character_types"]:
        check = "☑" if internal_key in prefs.character_types else "☐"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{check} {display_name}",
                    callback_data=f"nsfw_roleplay_char_{internal_key}",
                )
            ]
        )

    # Navigation and action buttons
    keyboard.append(
        [
            InlineKeyboardButton("← Back", callback_data="nsfw_menu_main"),
            InlineKeyboardButton("Clear All", callback_data="nsfw_roleplay_clear"),
        ]
    )
    keyboard.append(
        [InlineKeyboardButton("Save ✓", callback_data="nsfw_save_roleplay")]
    )

    return InlineKeyboardMarkup(keyboard)


# ============================================================================
# MENU 4: BOUNDARIES KEYBOARD
# ============================================================================


def build_boundaries_keyboard(prefs: NSFWPreferences) -> InlineKeyboardMarkup:
    """
    Build keyboard for boundaries and limits.

    Shows checkboxes for common hard limits.
    """
    keyboard = []

    # Add boundary options as checkboxes
    for display_name, internal_key in menu_text.BOUNDARY_OPTIONS:
        check = "☑" if internal_key in prefs.hard_limits else "☐"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{check} {display_name}",
                    callback_data=f"nsfw_boundary_{internal_key}",
                )
            ]
        )

    # Safe word button
    keyboard.append(
        [
            InlineKeyboardButton(
                f"🛑 Safe Word: {prefs.safe_word}", callback_data="nsfw_edit_safeword"
            )
        ]
    )

    # Navigation
    keyboard.append(
        [
            InlineKeyboardButton("← Back", callback_data="nsfw_menu_main"),
            InlineKeyboardButton("Save ✓", callback_data="nsfw_save_boundaries"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)


# ============================================================================
# MENU 5: ADVANCED SETTINGS KEYBOARD
# ============================================================================


def build_advanced_keyboard(prefs: NSFWPreferences) -> InlineKeyboardMarkup:
    """
    Build keyboard for advanced settings.

    Shows toggles for bot actions, retcon, emojis, and verbosity slider.
    """
    keyboard = []

    # Bot actions toggle
    bot_actions_status = "ON ✓" if prefs.allow_bot_actions else "OFF"
    keyboard.append(
        [
            InlineKeyboardButton(
                f"Bot Actions: {bot_actions_status}",
                callback_data="nsfw_toggle_botactions",
            )
        ]
    )

    # Retcon toggle
    retcon_status = "ON ✓" if prefs.allow_retcon else "OFF"
    keyboard.append(
        [
            InlineKeyboardButton(
                f"Retcon Allowed: {retcon_status}", callback_data="nsfw_toggle_retcon"
            )
        ]
    )

    # Emoji toggle
    emoji_status = "ON ✓" if prefs.use_emojis else "OFF"
    keyboard.append(
        [
            InlineKeyboardButton(
                f"Use Emojis: {emoji_status}", callback_data="nsfw_toggle_emojis"
            )
        ]
    )

    # Verbosity slider (0-10)
    # Show as: [- - - -] 6/10 [+ + + +]
    verbosity_bar = "━" * prefs.verbosity + "░" * (10 - prefs.verbosity)
    keyboard.append(
        [
            InlineKeyboardButton(
                f"Verbosity: {verbosity_bar} {prefs.verbosity}/10",
                callback_data="nsfw_noop",
            )
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton("--", callback_data="nsfw_verbosity_minus2"),
            InlineKeyboardButton("-", callback_data="nsfw_verbosity_minus1"),
            InlineKeyboardButton("+", callback_data="nsfw_verbosity_plus1"),
            InlineKeyboardButton("++", callback_data="nsfw_verbosity_plus2"),
        ]
    )

    # Navigation
    keyboard.append(
        [
            InlineKeyboardButton("← Back", callback_data="nsfw_menu_main"),
            InlineKeyboardButton("Save ✓", callback_data="nsfw_save_advanced"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)


# ============================================================================
# MENU 6: CHARACTER & SETTING KEYBOARD
# ============================================================================


def build_character_keyboard(prefs: NSFWPreferences) -> InlineKeyboardMarkup:
    """
    Build keyboard for character and setting configuration.

    Shows gender selection and links to character builder.
    """
    keyboard = []

    # Bot gender selection
    keyboard.append(
        [InlineKeyboardButton("My Gender/Identity:", callback_data="nsfw_noop")]
    )

    # First row of genders
    gender_row_1 = []
    for display, value in menu_text.GENDER_OPTIONS[:3]:
        check = "✓ " if prefs.bot_gender == value else ""
        gender_row_1.append(
            InlineKeyboardButton(
                f"{check}{display}", callback_data=f"nsfw_botgender_{value}"
            )
        )
    keyboard.append(gender_row_1)

    # Second row of genders
    gender_row_2 = []
    for display, value in menu_text.GENDER_OPTIONS[3:]:
        check = "✓ " if prefs.bot_gender == value else ""
        gender_row_2.append(
            InlineKeyboardButton(
                f"{check}{display}", callback_data=f"nsfw_botgender_{value}"
            )
        )
    keyboard.append(gender_row_2)

    # User gender selection
    keyboard.append(
        [InlineKeyboardButton("Your Gender/Identity:", callback_data="nsfw_noop")]
    )

    # User genders (same layout)
    user_gender_row_1 = []
    for display, value in menu_text.GENDER_OPTIONS[:3]:
        check = "✓ " if prefs.user_gender == value else ""
        user_gender_row_1.append(
            InlineKeyboardButton(
                f"{check}{display}", callback_data=f"nsfw_usergender_{value}"
            )
        )
    keyboard.append(user_gender_row_1)

    user_gender_row_2 = []
    for display, value in menu_text.GENDER_OPTIONS[3:]:
        check = "✓ " if prefs.user_gender == value else ""
        user_gender_row_2.append(
            InlineKeyboardButton(
                f"{check}{display}", callback_data=f"nsfw_usergender_{value}"
            )
        )
    keyboard.append(user_gender_row_2)

    # Link to character builder
    keyboard.append(
        [
            InlineKeyboardButton(
                "✨ Use Custom Character Builder",
                callback_data="nsfw_launch_character_builder",
            )
        ]
    )

    # Navigation
    keyboard.append(
        [
            InlineKeyboardButton("← Back", callback_data="nsfw_menu_main"),
            InlineKeyboardButton("Save ✓", callback_data="nsfw_save_character"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def build_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Simple Yes/No confirmation keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("✓ Yes", callback_data="nsfw_confirm_yes"),
            InlineKeyboardButton("✗ No", callback_data="nsfw_confirm_no"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_back_button() -> InlineKeyboardMarkup:
    """Single back button to main menu."""
    keyboard = [
        [InlineKeyboardButton("← Back to Main Menu", callback_data="nsfw_menu_main")]
    ]
    return InlineKeyboardMarkup(keyboard)
