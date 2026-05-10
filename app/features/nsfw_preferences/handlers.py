"""NSFW preference handlers and callback logic.

Mission: Keep the `/nsfwpref` experience configurable, safe, and persistent so
downbad mode aligns with the wellness bot's broader support goals. These
handlers power the Telegram menu flow, write preferences to SQLite + JSON, and
serve as the feature-module implementation of the legacy logic.

#todo Add inline safe-word editing instead of requiring slash commands.
#todo Integrate the future custom-character workflow once shipped.
"""

from __future__ import annotations

import copy
import json
import logging
import asyncio
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.db import db_ro, db_rw
from app.runtime.interfaces import UnifiedWellnessBot
from app.utils.fs import ensure_directory


def _ensure_bot(bot: "UnifiedWellnessBot"):
    """Return the active telegram bot instance."""
    application = getattr(bot, "telegram_app", None)
    if application is None:
        raise RuntimeError("Telegram application is not running.")
    return application.bot


def _run_bot_coroutine(bot: "UnifiedWellnessBot", coroutine):
    """Run a Telegram coroutine on the bot's event loop and wait for completion."""
    loop = getattr(bot, "bot_event_loop", None)
    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).error(
                "NSFW pref telegram call failed: %s", exc, exc_info=True
            )
            raise
    # Fallback for situations where the loop isn't available (e.g., unit tests)
    logging.getLogger(__name__).warning(
        "NSFW pref falling back to asyncio.run for %s", coroutine
    )
    return asyncio.run(coroutine)


def _reply_text(
    bot: "UnifiedWellnessBot", message: Message, text: str, **kwargs
) -> None:
    _run_bot_coroutine(bot, message.reply_text(text, **kwargs))


def _query_answer(bot: "UnifiedWellnessBot", query, *args, **kwargs) -> None:
    _run_bot_coroutine(bot, query.answer(*args, **kwargs))


class _TelegramBotProxy:
    """Synchronous facade around the async Telegram bot used by the NSFW module."""

    def __init__(self, owner: "UnifiedWellnessBot") -> None:
        self._owner = owner

    def send_message(self, **kwargs):
        bot = _ensure_bot(self._owner)
        return _run_bot_coroutine(self._owner, bot.send_message(**kwargs))

    def edit_message_text(self, **kwargs):
        bot = _ensure_bot(self._owner)
        return _run_bot_coroutine(self._owner, bot.edit_message_text(**kwargs))

    def edit_message_reply_markup(self, **kwargs):
        bot = _ensure_bot(self._owner)
        return _run_bot_coroutine(self._owner, bot.edit_message_reply_markup(**kwargs))

    def delete_message(self, **kwargs):
        bot = _ensure_bot(self._owner)
        return _run_bot_coroutine(self._owner, bot.delete_message(**kwargs))


TRUE_WORDS: Iterable[str] = {
    "on",
    "yes",
    "y",
    "true",
    "enable",
    "enabled",
    "unlock",
    "optin",
    "open",
    "allow",
}
FALSE_WORDS: Iterable[str] = {
    "off",
    "no",
    "n",
    "false",
    "disable",
    "disabled",
    "lock",
    "lockdown",
    "optout",
    "close",
    "deny",
}

INTENSITY_OPTIONS: List[Tuple[str, str, str]] = [
    ("mild", "Mild", "Suggestive & flirty"),
    ("moderate", "Moderate", "Explicit and direct"),
    ("intense", "Intense", "Raw and no-holds-barred"),
]

STYLE_OPTIONS: List[Tuple[str, str, str]] = [
    ("romantic_sensual", "Romantic & Sensual", "Emotional connection and tenderness"),
    ("playful_teasing", "Playful & Teasing", "Flirty banter with bratty energy"),
    ("naive_shy", "Naive / Shy", "Inexperienced, blushy, and hesitant energy"),
    ("bratty", "Bratty", "Defiant teasing with mischievous push-pull"),
    ("tsundere", "Tsundere", "Hot-cold denial with flustered softness underneath"),
    ("dominant_assertive", "Dominant & Assertive", "Mira takes charge confidently"),
    ("submissive_eager", "Submissive & Eager", "Mira focuses on pleasing you"),
    ("gentle_caregiver", "Gentle Caregiver", "Soft reassurance, affection, and attentive warmth"),
    ("worshipful_devoted", "Worshipful / Devoted", "Adoring praise, devotion, and eager focus"),
    ("explicit_raw", "Explicit & Raw", "Direct, vulgar, and hungry"),
]

SUBMODE_OPTIONS: List[Tuple[str, str, str]] = [
    ("standard", "Standard Downbad", "Flirty/explicit without strict scene continuity"),
    ("roleplay", "Roleplay Downbad", "Stay in-character with narrative continuity"),
]

ROLEPLAY_OPTIONS: List[Tuple[str, str]] = [
    ("strangers", "Strangers meeting for the first time"),
    ("long_distance", "Long-distance sexting"),
    ("exes", "Ex-lovers reconnecting"),
    ("forbidden", "Secret affair / forbidden romance"),
    ("boss_employee", "Boss / employee power dynamic"),
    ("teacher_student", "Professor / student fantasy"),
    ("friends_to_lovers", "Friends-to-lovers slow burn"),
    ("fantasy", "Fantasy worlds (e.g. elves, magic)"),
    ("other", "Custom scenarios"),
]

KINK_PRESET_OPTIONS: List[Tuple[str, str]] = [
    ("bondage", "Bondage"),
    ("praise", "Praise & worship"),
    ("impact", "Impact play"),
    ("voyeur", "Exhibition / voyeur"),
    ("public", "Public teasing"),
    ("role_reversal", "Role reversal / gender play"),
    ("dirty_talk", "Dirty talk"),
    ("aftercare", "Aftercare focus"),
]

BOUNDARY_OPTIONS: List[Tuple[str, str]] = [
    ("Non-consensual scenarios", "Non-consensual scenarios"),
    ("Age play / infantilization", "Age play / infantilization"),
    ("Extreme violence", "Extreme violence or pain"),
    ("Bodily waste / scat", "Bodily waste / scat"),
    ("Bestiality", "Bestiality"),
    ("Incest", "Incest"),
    ("Degradation / slurs", "Degradation / slurs"),
    ("Pregnancy / breeding", "Pregnancy / breeding"),
    ("Feet content", "Feet content"),
]

GENDER_OPTIONS: List[Tuple[str, str]] = [
    ("unspecified", "Keep it flexible"),
    ("female", "Female"),
    ("male", "Male"),
    ("non_binary", "Non-binary"),
    ("other", "Something custom"),
]

DOMINANCE_OPTIONS: List[Tuple[str, str, str]] = [
    ("submissive", "Low", "Mira follows your lead"),
    ("balanced", "Balanced", "Take turns leading"),
    ("dominant", "High", "Mira is assertive and directive"),
]

PACING_OPTIONS: List[Tuple[str, str]] = [
    ("fast", "Fast - jump right in"),
    ("medium", "Medium - build anticipation"),
    ("slow", "Slow burn - lots of buildup"),
]

VERBOSITY_OPTIONS: List[Tuple[str, str]] = [
    ("concise", "Concise replies"),
    ("medium", "Balanced detail"),
    ("detailed", "Detailed & descriptive"),
]

DEFAULT_HARD_LIMITS: List[str] = [
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

DEFAULT_PREFERENCES: Dict[str, Any] = {
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

CALLBACK_PREFIX = "nsfw|"


def _preferences_dir(bot: UnifiedWellnessBot) -> Path:
    data_root = getattr(bot.cfg, "data_root", "wellness_data")
    return Path(data_root) / "nsfw_preferences"


def _merge_defaults(existing: Dict[str, Any]) -> Dict[str, Any]:
    base = copy.deepcopy(DEFAULT_PREFERENCES)
    for key, value in existing.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            base[key].update(value)
        else:
            base[key] = value
    return base


def load_preferences(
    bot: UnifiedWellnessBot, db_user_id: int, telegram_id: int
) -> Dict[str, Any]:
    prefs: Dict[str, Any] = {}

    with db_ro() as conn:
        row = conn.execute(
            "SELECT value FROM profile_context WHERE user_id = ? AND key = 'nsfw_preferences'",
            (db_user_id,),
        ).fetchone()
        if row and row["value"]:
            try:
                prefs = json.loads(row["value"])
            except json.JSONDecodeError:
                prefs = {}

    if not prefs:
        pref_dir = _preferences_dir(bot)
        file_path = pref_dir / f"{telegram_id}.json"
        if file_path.exists():
            try:
                prefs = json.loads(file_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                prefs = {}

    merged = _merge_defaults(prefs)
    merged["nsfw_opt_in"] = bool(bot._get_nsfw_opt_in(db_user_id))
    return merged


def _persist_opt_in(bot: UnifiedWellnessBot, user_id: int, enabled: bool) -> None:
    value_str = "true" if enabled else "false"
    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO profile_context (user_id, key, value)
            VALUES (?, 'nsfw_opt_in', ?)
            ON CONFLICT(user_id, key)
            DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, value_str),
        )

        onboarding_row = conn.execute(
            "SELECT onboarding_data FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        onboarding_data: Dict[str, Any] = {}
        if onboarding_row and onboarding_row["onboarding_data"]:
            try:
                onboarding_data = json.loads(onboarding_row["onboarding_data"])
            except json.JSONDecodeError:
                onboarding_data = {}
        onboarding_data["nsfw_opt_in"] = enabled
        conn.execute(
            "UPDATE users SET onboarding_data = ? WHERE id = ?",
            (json.dumps(onboarding_data), user_id),
        )


def save_preferences(
    bot: UnifiedWellnessBot,
    db_user_id: int,
    telegram_id: int,
    preferences: Dict[str, Any],
) -> None:
    pref_dir = _preferences_dir(bot)
    ensure_directory(pref_dir)
    file_path = pref_dir / f"{telegram_id}.json"
    file_path.write_text(
        json.dumps(preferences, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    with db_rw() as conn:
        conn.execute(
            """
            INSERT INTO profile_context (user_id, key, value)
            VALUES (?, 'nsfw_preferences', ?)
            ON CONFLICT(user_id, key)
            DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (db_user_id, json.dumps(preferences)),
        )

    _persist_opt_in(bot, db_user_id, preferences.get("nsfw_opt_in", False))
    bot.invalidate_profile_cache(db_user_id)


def _label_for(
    options: Iterable[Tuple[str, str]], value: str, default: str = "Not set"
) -> str:
    for option in options:
        if option[0] == value:
            return option[1]
    return default


def _multi_label(
    options: Iterable[Tuple[str, str]], values: Iterable[str]
) -> List[str]:
    lookup: Dict[str, str] = {}
    for option in options:
        key, label = option[:2]
        lookup[key] = label
    result: List[str] = []
    for value in values:
        if value in lookup:
            result.append(lookup[value])
        else:
            result.append(value)
    return result


def render_summary(prefs: Dict[str, Any]) -> str:
    # todo Localize summary copy once the translation system ships.
    status = "Enabled ✅" if prefs.get("nsfw_opt_in") else "Disabled 🚫"
    content_intensity = str(prefs.get("content_intensity") or "")
    interaction_style = str(prefs.get("interaction_style") or "")
    dominance_pref = str(prefs.get("dominance_preference") or "")
    pacing = str(prefs.get("pacing") or "")
    verbosity = str(prefs.get("verbosity") or "")
    downbad_submode = str(prefs.get("downbad_submode") or "standard")
    user_gender = str(prefs.get("user_gender") or "")
    bot_gender = str(prefs.get("bot_gender") or "")

    lines = [
        f"NSFW Access: {status}",
        "",
        "Scene Style:",
        f"- Intensity: {_label_for([(value, label) for value, label, _ in INTENSITY_OPTIONS], content_intensity)}",
        f"- Interaction Style: {_label_for([(value, label) for value, label, _ in STYLE_OPTIONS], interaction_style)}",
        f"- Dominance: {_label_for([(value, label) for value, label, _ in DOMINANCE_OPTIONS], dominance_pref)}",
        f"- Pacing: {_label_for(PACING_OPTIONS, pacing)}",
        f"- Verbosity: {_label_for(VERBOSITY_OPTIONS, verbosity)}",
        f"- Downbad Submode: {_label_for([(value, label) for value, label, _ in SUBMODE_OPTIONS], downbad_submode)}",
        "",
        "Identity Preferences:",
        f"- Refer to you as: {_label_for(GENDER_OPTIONS, user_gender)}",
        f"- Mira presents as: {_label_for(GENDER_OPTIONS, bot_gender)}",
        "",
    ]

    roleplay = prefs.get("roleplay_scenarios", [])
    roleplay_display = _multi_label(ROLEPLAY_OPTIONS, roleplay) or ["None"]
    kinks = prefs.get("kinks", [])
    kink_display = _multi_label(KINK_PRESET_OPTIONS, kinks) or ["None"]

    lines.extend(
        [
            "Roleplay Themes:",
            "- " + ", ".join(roleplay_display),
            "",
            "Kinks & Interests:",
            "- " + ", ".join(kink_display),
            "",
            "Boundaries:",
            "- Hard limits: " + (", ".join(prefs.get("hard_limits", [])) or "None"),
            "- Soft limits: " + (", ".join(prefs.get("soft_limits", [])) or "None"),
            f"- Safe word: {prefs.get('safe_word') or 'Red'}",
            "",
        ]
    )

    story = prefs.get("story_setting") or "Not set"
    lines.extend(
        [
            "Story / Setting:",
            f"- {story}",
            "",
            "Quick commands:",
            "- /nsfwpref enable | disable",
            "- /nsfwpref reset",
            "- /nsfwpref kinks add <text>",
            "- /nsfwpref kinks remove <text>",
            "- /nsfwpref limit add|remove <text>",
            "- /nsfwpref soft add|remove <text>",
            "- /nsfwpref safeword <word>",
            "- /nsfwpref story <description>",
            "- /nsfwpref mode standard|roleplay",
        ]
    )

    return "\n".join(lines)


def build_root_keyboard(prefs: Dict[str, Any]) -> InlineKeyboardMarkup:
    toggle_label = "Lock Access" if prefs.get("nsfw_opt_in") else "Unlock Access"
    rows = [
        [
            InlineKeyboardButton(
                "Downbad Submode", callback_data=f"{CALLBACK_PREFIX}menu|submode"
            ),
            InlineKeyboardButton(
                "Interaction Style", callback_data=f"{CALLBACK_PREFIX}menu|style"
            ),
        ],
        [
            InlineKeyboardButton(
                "Content Intensity", callback_data=f"{CALLBACK_PREFIX}menu|intensity"
            ),
        ],
        [
            InlineKeyboardButton(
                "Roleplay Themes", callback_data=f"{CALLBACK_PREFIX}menu|roleplay"
            ),
            InlineKeyboardButton(
                "Kinks & Interests", callback_data=f"{CALLBACK_PREFIX}menu|kinks"
            ),
        ],
        [
            InlineKeyboardButton(
                "Boundaries", callback_data=f"{CALLBACK_PREFIX}menu|boundaries"
            ),
            InlineKeyboardButton(
                "Gender Preferences", callback_data=f"{CALLBACK_PREFIX}menu|gender"
            ),
        ],
        [
            InlineKeyboardButton(
                "Dynamics & Pacing", callback_data=f"{CALLBACK_PREFIX}menu|dynamics"
            ),
            InlineKeyboardButton(
                "Scene Rules", callback_data=f"{CALLBACK_PREFIX}menu|rules"
            ),
        ],
        [
            InlineKeyboardButton(
                "Character Hub", callback_data=f"{CALLBACK_PREFIX}shortcut|characters"
            ),
            InlineKeyboardButton(
                "Adventure Hub", callback_data=f"{CALLBACK_PREFIX}shortcut|adventures"
            ),
        ],
        [
            InlineKeyboardButton(
                "Add Character", callback_data=f"{CALLBACK_PREFIX}shortcut|add_character"
            ),
            InlineKeyboardButton(
                "Chat -> Adventure", callback_data=f"{CALLBACK_PREFIX}shortcut|fromchat"
            ),
        ],
        [
            InlineKeyboardButton(
                toggle_label, callback_data=f"{CALLBACK_PREFIX}toggle|access"
            ),
            InlineKeyboardButton(
                "Reset", callback_data=f"{CALLBACK_PREFIX}reset|confirm"
            ),
        ],
        [InlineKeyboardButton("Close", callback_data=f"{CALLBACK_PREFIX}menu|close")],
    ]
    return InlineKeyboardMarkup(rows)


def _single_choice_keyboard(
    category: str, options: List[Tuple[str, str, str]], current: str
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for value, label, _desc in options:
        prefix = "✅ " if value == current else "⚪ "
        rows.append(
            [
                InlineKeyboardButton(
                    prefix + label,
                    callback_data=f"{CALLBACK_PREFIX}set|{category}|{value}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("Back", callback_data=f"{CALLBACK_PREFIX}menu|summary")]
    )
    return InlineKeyboardMarkup(rows)


def _multi_choice_keyboard(
    category: str, options: List[Tuple[str, str]], selected: Iterable[str]
) -> InlineKeyboardMarkup:
    selected_set = {value for value in selected}
    rows: List[List[InlineKeyboardButton]] = []
    for value, label in options:
        prefix = "✅ " if value in selected_set else "⚪ "
        rows.append(
            [
                InlineKeyboardButton(
                    prefix + label,
                    callback_data=f"{CALLBACK_PREFIX}toggle_multi|{category}|{value}",
                )
            ]
        )

    if category == "roleplay":
        rows.append(
            [
                InlineKeyboardButton(
                    "Suggest new theme",
                    callback_data=f"{CALLBACK_PREFIX}prompt|roleplay",
                )
            ]
        )
    elif category == "kinks":
        rows.append(
            [
                InlineKeyboardButton(
                    "Add custom kink",
                    callback_data=f"{CALLBACK_PREFIX}prompt|kinks",
                )
            ]
        )

    rows.append(
        [InlineKeyboardButton("Back", callback_data=f"{CALLBACK_PREFIX}menu|summary")]
    )
    return InlineKeyboardMarkup(rows)


def _limits_keyboard(
    category: str,
    options: List[Tuple[str, str]],
    selected: Iterable[str],
    prompt_action: str,
) -> InlineKeyboardMarkup:
    selected_set = {value for value in selected}
    rows: List[List[InlineKeyboardButton]] = []

    for value, label in options:
        prefix = "✅ " if value in selected_set else "⚪ "
        rows.append(
            [
                InlineKeyboardButton(
                    prefix + label,
                    callback_data=f"{CALLBACK_PREFIX}toggle_limit|{category}|{value}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "Add custom limit",
                callback_data=f"{CALLBACK_PREFIX}prompt|{prompt_action}",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton("Back", callback_data=f"{CALLBACK_PREFIX}menu|summary")]
    )
    return InlineKeyboardMarkup(rows)


def _rules_keyboard(prefs: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                ("✅ " if prefs.get("allow_bot_actions", True) else "⚪ ")
                + "Allow Mira actions",
                callback_data=f"{CALLBACK_PREFIX}toggle|allow_bot_actions",
            )
        ],
        [
            InlineKeyboardButton(
                ("✅ " if prefs.get("allow_retcon", True) else "⚪ ")
                + "Allow retcon tool",
                callback_data=f"{CALLBACK_PREFIX}toggle|allow_retcon",
            )
        ],
        [
            InlineKeyboardButton(
                "Set safe word",
                callback_data=f"{CALLBACK_PREFIX}prompt|safe_word",
            )
        ],
    ]
    rows.append(
        [InlineKeyboardButton("Back", callback_data=f"{CALLBACK_PREFIX}menu|summary")]
    )
    return InlineKeyboardMarkup(rows)


def _story_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                "Set story focus",
                callback_data=f"{CALLBACK_PREFIX}prompt|story_setting",
            )
        ],
        [InlineKeyboardButton("Back", callback_data=f"{CALLBACK_PREFIX}menu|summary")],
    ]
    return InlineKeyboardMarkup(rows)


def _confirm_reset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Reset to defaults",
                    callback_data=f"{CALLBACK_PREFIX}reset|confirm_yes",
                )
            ],
            [
                InlineKeyboardButton(
                    "Cancel", callback_data=f"{CALLBACK_PREFIX}menu|summary"
                )
            ],
        ]
    )


def _send_or_edit_message(
    bot: UnifiedWellnessBot,
    chat_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
    message_id: Optional[int],
) -> None:
    telegram_bot = _ensure_bot(bot)
    if message_id:
        try:
            _run_bot_coroutine(
                bot,
                telegram_bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN,
                ),
            )
            return
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.getLogger(__name__).debug("Failed to edit message: %s", exc)

    _run_bot_coroutine(
        bot,
        telegram_bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
        ),
    )
    logging.getLogger(__name__).debug(
        "NSFW pref message dispatched to chat %s", chat_id
    )


def _handle_menu(
    bot: UnifiedWellnessBot,
    chat_id: int,
    prefs: Dict[str, Any],
    section: str,
    message_id: Optional[int],
) -> None:
    if section == "summary":
        _send_or_edit_message(
            bot,
            chat_id,
            render_summary(prefs),
            build_root_keyboard(prefs),
            message_id,
        )
        return

    if section == "close":
        if message_id:
            try:
                _run_bot_coroutine(
                    bot,
                    _ensure_bot(bot).delete_message(
                        chat_id=chat_id, message_id=message_id
                    ),
                )
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        return

    menu_map: Dict[str, Tuple[str, InlineKeyboardMarkup]] = {
        "intensity": (
            "Choose your preferred intensity:",
            _single_choice_keyboard(
                "content_intensity",
                INTENSITY_OPTIONS,
                prefs.get("content_intensity", "moderate"),
            ),
        ),
        "style": (
            "How should Mira engage?",
            _single_choice_keyboard(
                "interaction_style",
                STYLE_OPTIONS,
                prefs.get("interaction_style", "playful_teasing"),
            ),
        ),
        "submode": (
            "Choose your downbad submode:",
            _single_choice_keyboard(
                "downbad_submode",
                SUBMODE_OPTIONS,
                prefs.get("downbad_submode", "standard"),
            ),
        ),
        "roleplay": (
            "Pick your favorite scenarios (toggle on/off):",
            _multi_choice_keyboard(
                "roleplay", ROLEPLAY_OPTIONS, prefs.get("roleplay_scenarios", [])
            ),
        ),
        "kinks": (
            "Select interests (toggle on/off):",
            _multi_choice_keyboard(
                "kinks", KINK_PRESET_OPTIONS, prefs.get("kinks", [])
            ),
        ),
        "boundaries": (
            "Set your hard limits:",
            _limits_keyboard(
                "hard_limits",
                BOUNDARY_OPTIONS,
                prefs.get("hard_limits", []),
                "hard_limit",
            ),
        ),
        "gender": (
            "Who are we roleplaying as?",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            ("✅ " if prefs.get("user_gender") == value else "⚪ ")
                            + label,
                            callback_data=f"{CALLBACK_PREFIX}set|user_gender|{value}",
                        )
                    ]
                    for value, label in GENDER_OPTIONS
                ]
                + [
                    [
                        InlineKeyboardButton(
                            (
                                "✅ "
                                if prefs.get("bot_gender", "female") == value
                                else "⚪ "
                            )
                            + f"Mira as {label}",
                            callback_data=f"{CALLBACK_PREFIX}set|bot_gender|{value}",
                        )
                    ]
                    for value, label in GENDER_OPTIONS
                ]
                + [
                    [
                        InlineKeyboardButton(
                            "Back", callback_data=f"{CALLBACK_PREFIX}menu|summary"
                        )
                    ]
                ]
            ),
        ),
        "dynamics": (
            "Set the dynamic and pacing:",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            (
                                "✅ "
                                if prefs.get("dominance_preference") == value
                                else "⚪ "
                            )
                            + f"Dominance: {label}",
                            callback_data=f"{CALLBACK_PREFIX}set|dominance_preference|{value}",
                        )
                    ]
                    for value, label, _desc in DOMINANCE_OPTIONS
                ]
                + [
                    [
                        InlineKeyboardButton(
                            ("✅ " if prefs.get("pacing", "medium") == value else "⚪ ")
                            + f"Pacing: {label}",
                            callback_data=f"{CALLBACK_PREFIX}set|pacing|{value}",
                        )
                    ]
                    for value, label in PACING_OPTIONS
                ]
                + [
                    [
                        InlineKeyboardButton(
                            (
                                "✅ "
                                if prefs.get("verbosity", "medium") == value
                                else "⚪ "
                            )
                            + f"Verbosity: {label}",
                            callback_data=f"{CALLBACK_PREFIX}set|verbosity|{value}",
                        )
                    ]
                    for value, label in VERBOSITY_OPTIONS
                ]
                + [
                    [
                        InlineKeyboardButton(
                            "Back", callback_data=f"{CALLBACK_PREFIX}menu|summary"
                        )
                    ]
                ]
            ),
        ),
        "rules": (
            "Scene rules & safety:",
            _rules_keyboard(prefs),
        ),
        "story": (
            "Set the overall story arc:",
            _story_keyboard(),
        ),
        "soft_limits": (
            "Tag softer boundaries:",
            _limits_keyboard(
                "soft_limits",
                BOUNDARY_OPTIONS,
                prefs.get("soft_limits", []),
                "soft_limit",
            ),
        ),
        "reset": (
            "Reset everything to default?",
            _confirm_reset_keyboard(),
        ),
    }

    if section in menu_map:
        text, keyboard = menu_map[section]
        _send_or_edit_message(bot, chat_id, text, keyboard, message_id)
    else:
        _send_or_edit_message(
            bot,
            chat_id,
            "Not sure what menu that is. Sending you home.",
            build_root_keyboard(prefs),
            message_id,
        )


def _toggle_access(
    bot: UnifiedWellnessBot, prefs: Dict[str, Any], db_user_id: int, telegram_id: int
) -> Dict[str, Any]:
    prefs = copy.deepcopy(prefs)
    prefs["nsfw_opt_in"] = not prefs.get("nsfw_opt_in", False)
    save_preferences(bot, db_user_id, telegram_id, prefs)
    return prefs


def _handle_toggle(
    bot: UnifiedWellnessBot,
    chat_id: int,
    prefs: Dict[str, Any],
    category: str,
    db_user_id: int,
    telegram_id: int,
    message_id: Optional[int],
) -> Dict[str, Any]:
    updated = copy.deepcopy(prefs)

    if category == "access":
        updated = _toggle_access(bot, prefs, db_user_id, telegram_id)
        _send_or_edit_message(
            bot,
            chat_id,
            render_summary(updated),
            build_root_keyboard(updated),
            message_id,
        )
        return updated

    updated[category] = not prefs.get(category, False)
    save_preferences(bot, db_user_id, telegram_id, updated)
    _send_or_edit_message(
        bot,
        chat_id,
        render_summary(updated),
        build_root_keyboard(updated),
        message_id,
    )
    return updated


def _handle_set(
    bot: UnifiedWellnessBot,
    chat_id: int,
    prefs: Dict[str, Any],
    category: str,
    value: str,
    db_user_id: int,
    telegram_id: int,
    message_id: Optional[int],
) -> Dict[str, Any]:
    updated = copy.deepcopy(prefs)
    updated[category] = value
    save_preferences(bot, db_user_id, telegram_id, updated)

    _send_or_edit_message(
        bot,
        chat_id,
        render_summary(updated),
        build_root_keyboard(updated),
        message_id,
    )
    return updated


def _handle_toggle_multi(
    bot: UnifiedWellnessBot,
    chat_id: int,
    prefs: Dict[str, Any],
    category: str,
    value: str,
    db_user_id: int,
    telegram_id: int,
    message_id: Optional[int],
) -> Dict[str, Any]:
    updated = copy.deepcopy(prefs)
    values = updated.setdefault(category, [])
    if value in values:
        values.remove(value)
    else:
        values.append(value)
    save_preferences(bot, db_user_id, telegram_id, updated)

    _handle_menu(
        bot,
        chat_id,
        updated,
        "roleplay" if category == "roleplay_scenarios" else category,
        message_id,
    )
    return updated


def _handle_toggle_limit(
    bot: UnifiedWellnessBot,
    chat_id: int,
    prefs: Dict[str, Any],
    category: str,
    value: str,
    db_user_id: int,
    telegram_id: int,
    message_id: Optional[int],
) -> Dict[str, Any]:
    updated = copy.deepcopy(prefs)
    values = updated.setdefault(category, [])
    if value in values:
        values.remove(value)
    else:
        values.append(value)
    save_preferences(bot, db_user_id, telegram_id, updated)

    _handle_menu(
        bot,
        chat_id,
        updated,
        "boundaries" if category == "hard_limits" else "soft_limits",
        message_id,
    )
    return updated


def _handle_prompt(
    bot: UnifiedWellnessBot,
    chat_id: int,
    prefs: Dict[str, Any],
    category: str,
    db_user_id: int,
    telegram_id: int,
    message_id: Optional[int],
    prompt: str,
) -> None:
    _run_bot_coroutine(
        bot,
        _ensure_bot(bot).send_message(
            chat_id=chat_id,
            text=f"Send the {prompt} now. Reply here so I can keep track.",
        ),
    )

    pending_inputs = getattr(bot, "_pending_inputs", None)
    if pending_inputs is None:
        pending_inputs = {}
        setattr(bot, "_pending_inputs", pending_inputs)
    pending_inputs[(chat_id, db_user_id)] = {
        "category": category,
        "telegram_id": telegram_id,
        "message_id": message_id,
    }


def _handle_reset(
    bot: UnifiedWellnessBot,
    chat_id: int,
    db_user_id: int,
    telegram_id: int,
    message_id: Optional[int],
) -> Dict[str, Any]:
    prefs = copy.deepcopy(DEFAULT_PREFERENCES)
    prefs["nsfw_opt_in"] = bool(bot._get_nsfw_opt_in(db_user_id))
    save_preferences(bot, db_user_id, telegram_id, prefs)
    _send_or_edit_message(
        bot,
        chat_id,
        "Reset complete. Back to defaults.",
        build_root_keyboard(prefs),
        message_id,
    )
    return prefs


def _nsfw_pref_command_impl(
    bot: UnifiedWellnessBot,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    logging.getLogger(__name__).debug(
        "NSFW pref command invoked for chat_id=%s",
        getattr(update.effective_chat, "id", None),
    )
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    if not user:
        return

    db_user_id = bot.get_user_id(user.id)
    if not db_user_id:
        _reply_text(
            bot, message, "You need to set up your profile before adjusting this."
        )
        return

    prefs = load_preferences(bot, db_user_id, user.id)

    args = (message.text or "").split()[1:]
    if args:
        _handle_text_command(
            bot,
            message,
            prefs,
            db_user_id,
            user.id,
            args,
        )
        return

    summary = render_summary(prefs)
    keyboard = build_root_keyboard(prefs)

    _reply_text(
        bot, message, summary, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
    )


def _handle_text_command(
    bot: UnifiedWellnessBot,
    message: Message,
    prefs: Dict[str, Any],
    db_user_id: int,
    telegram_id: int,
    args: List[str],
) -> None:
    if not args:
        _reply_text(bot, message, "Try /nsfwpref to open the menu.")
        return

    command = args[0].lower()

    if command in TRUE_WORDS:
        prefs = copy.deepcopy(prefs)
        prefs["nsfw_opt_in"] = True
        save_preferences(bot, db_user_id, telegram_id, prefs)
        _reply_text(bot, message, "NSFW content enabled for this relationship.")
        return

    if command in FALSE_WORDS:
        prefs = copy.deepcopy(prefs)
        prefs["nsfw_opt_in"] = False
        save_preferences(bot, db_user_id, telegram_id, prefs)
        _reply_text(bot, message, "NSFW content locked. Stay cozy!")
        return

    if command == "reset":
        prefs = copy.deepcopy(DEFAULT_PREFERENCES)
        save_preferences(bot, db_user_id, telegram_id, prefs)
        _reply_text(bot, message, "Preferences reset to defaults.")
        return

    if command == "kinks":
        _handle_text_kinks(bot, message, prefs, db_user_id, telegram_id, args[1:])
        return

    if command in {"limit", "limits"}:
        _handle_text_limits(bot, message, prefs, db_user_id, telegram_id, args[1:])
        return

    if command == "soft":
        _handle_text_soft_limits(bot, message, prefs, db_user_id, telegram_id, args[1:])
        return

    if command == "safeword":
        if len(args) < 2:
            _reply_text(bot, message, "Usage: /nsfwpref safeword <word>")
            return
        prefs = copy.deepcopy(prefs)
        prefs["safe_word"] = args[1]
        save_preferences(bot, db_user_id, telegram_id, prefs)
        _reply_text(bot, message, f"Safe word set to {args[1]}.")
        return

    if command == "story":
        if len(args) < 2:
            _reply_text(bot, message, "Usage: /nsfwpref story <description>")
            return
        prefs = copy.deepcopy(prefs)
        prefs["story_setting"] = " ".join(args[1:])
        save_preferences(bot, db_user_id, telegram_id, prefs)
        _reply_text(bot, message, "Story preference updated.")
        return

    if command == "mode":
        if len(args) < 2:
            _reply_text(bot, message, "Usage: /nsfwpref mode standard|roleplay")
            return
        mode = args[1].strip().lower()
        valid = {value for value, _label, _desc in SUBMODE_OPTIONS}
        if mode not in valid:
            _reply_text(
                bot,
                message,
                "Invalid mode. Use /nsfwpref mode standard or /nsfwpref mode roleplay",
            )
            return
        prefs = copy.deepcopy(prefs)
        prefs["downbad_submode"] = mode
        save_preferences(bot, db_user_id, telegram_id, prefs)
        _reply_text(
            bot,
            message,
            "Downbad submode set to Roleplay." if mode == "roleplay" else "Downbad submode set to Standard.",
        )
        return

    _reply_text(bot, message, "Not sure what you mean. Try /nsfwpref for options.")


def _handle_text_kinks(
    bot: UnifiedWellnessBot,
    message: Message,
    prefs: Dict[str, Any],
    db_user_id: int,
    telegram_id: int,
    args: List[str],
) -> None:
    if not args:
        _reply_text(
            bot,
            message,
            "Usage: /nsfwpref kinks add <text> or /nsfwpref kinks remove <text>",
        )
        return

    action = args[0].lower()
    value = " ".join(args[1:]) if len(args) > 1 else ""
    if not value:
        _reply_text(bot, message, "Please specify the kink to add or remove.")
        return

    updated = copy.deepcopy(prefs)
    kinks = updated.setdefault("kinks", [])

    if action == "add":
        if value not in kinks:
            kinks.append(value)
            save_preferences(bot, db_user_id, telegram_id, updated)
            _reply_text(bot, message, f"Added kink: {value}")
        else:
            _reply_text(bot, message, "Already there!")
        return

    if action == "remove":
        if value in kinks:
            kinks.remove(value)
            save_preferences(bot, db_user_id, telegram_id, updated)
            _reply_text(bot, message, f"Removed kink: {value}")
        else:
            _reply_text(bot, message, "Couldn't find that one.")
        return

    _reply_text(bot, message, "Usage: /nsfwpref kinks add|remove <text>")


def _handle_text_limits(
    bot: UnifiedWellnessBot,
    message: Message,
    prefs: Dict[str, Any],
    db_user_id: int,
    telegram_id: int,
    args: List[str],
) -> None:
    if len(args) < 2:
        _reply_text(bot, message, "Usage: /nsfwpref limit add|remove <text>")
        return

    action = args[0].lower()
    value = " ".join(args[1:])
    updated = copy.deepcopy(prefs)
    hard_limits = updated.setdefault("hard_limits", [])

    if action == "add":
        if value not in hard_limits:
            hard_limits.append(value)
            save_preferences(bot, db_user_id, telegram_id, updated)
            _reply_text(bot, message, f"Added hard limit: {value}")
        else:
            _reply_text(bot, message, "Already a hard limit.")
        return

    if action == "remove":
        if value in hard_limits:
            hard_limits.remove(value)
            save_preferences(bot, db_user_id, telegram_id, updated)
            _reply_text(bot, message, f"Removed hard limit: {value}")
        else:
            _reply_text(bot, message, "That wasn't on the list.")
        return

    _reply_text(bot, message, "Usage: /nsfwpref limit add|remove <text>")


def _handle_text_soft_limits(
    bot: UnifiedWellnessBot,
    message: Message,
    prefs: Dict[str, Any],
    db_user_id: int,
    telegram_id: int,
    args: List[str],
) -> None:
    if len(args) < 2:
        _reply_text(bot, message, "Usage: /nsfwpref soft add|remove <text>")
        return

    action = args[0].lower()
    value = " ".join(args[1:])
    updated = copy.deepcopy(prefs)
    soft_limits = updated.setdefault("soft_limits", [])

    if action == "add":
        if value not in soft_limits:
            soft_limits.append(value)
            save_preferences(bot, db_user_id, telegram_id, updated)
            _reply_text(bot, message, f"Added soft limit: {value}")
        else:
            _reply_text(bot, message, "Already a soft limit.")
        return

    if action == "remove":
        if value in soft_limits:
            soft_limits.remove(value)
            save_preferences(bot, db_user_id, telegram_id, updated)
            _reply_text(bot, message, f"Removed soft limit: {value}")
        else:
            _reply_text(bot, message, "That wasn't on the list.")
        return

    _reply_text(bot, message, "Usage: /nsfwpref soft add|remove <text>")


def _nsfw_pref_callback_impl(
    bot: UnifiedWellnessBot,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    logging.getLogger(__name__).debug(
        "NSFW pref callback received with data=%s",
        getattr(update.callback_query, "data", None),
    )
    query = update.callback_query
    if not query:
        return

    user = query.from_user
    if not user:
        return

    db_user_id = bot.get_user_id(user.id)
    if not db_user_id:
        _query_answer(bot, query, "Please finish onboarding first.", show_alert=True)
        return

    prefs = load_preferences(bot, db_user_id, user.id)
    data = query.data or ""

    if not data.startswith(CALLBACK_PREFIX):
        _query_answer(bot, query)
        return

    payload = data[len(CALLBACK_PREFIX) :]
    parts = payload.split("|")

    action = parts[0]
    rest = parts[1:]

    chat_id = getattr(query.message, "chat_id", None) if query.message else None
    if chat_id is None:
        chat_id = user.id
    message_id = getattr(query.message, "message_id", None) if query.message else None

    try:
        if action == "menu":
            section = rest[0] if rest else "summary"
            _handle_menu(bot, chat_id, prefs, section, message_id)
            _query_answer(bot, query)
            return

        if action == "toggle":
            category = rest[0] if rest else "access"
            updated = _handle_toggle(
                bot,
                chat_id,
                prefs,
                category,
                db_user_id,
                user.id,
                message_id,
            )
            _query_answer(bot, query, "Updated")
            prefs.update(updated)
            return

        if action == "set":
            category = rest[0]
            value = rest[1]
            updated = _handle_set(
                bot,
                chat_id,
                prefs,
                category,
                value,
                db_user_id,
                user.id,
                message_id,
            )
            _query_answer(bot, query, "Preference saved")
            prefs.update(updated)
            return

        if action == "toggle_multi":
            category = rest[0]
            value = rest[1]
            updated = _handle_toggle_multi(
                bot,
                chat_id,
                prefs,
                category,
                value,
                db_user_id,
                user.id,
                message_id,
            )
            _query_answer(bot, query, "Updated")
            prefs.update(updated)
            return

        if action == "toggle_limit":
            category = rest[0]
            value = rest[1]
            updated = _handle_toggle_limit(
                bot,
                chat_id,
                prefs,
                category,
                value,
                db_user_id,
                user.id,
                message_id,
            )
            _query_answer(bot, query, "Updated limits")
            prefs.update(updated)
            return

        if action == "prompt":
            category = rest[0]
            prompt_lookup = {
                "roleplay": "roleplay scenario",
                "kinks": "kink or interest",
                "hard_limit": "hard limit",
                "soft_limit": "soft limit",
                "safe_word": "new safe word",
                "story_setting": "story description",
            }
            prompt = prompt_lookup.get(category, "preference")
            _handle_prompt(
                bot,
                chat_id,
                prefs,
                category,
                db_user_id,
                user.id,
                message_id,
                prompt,
            )
            _query_answer(bot, query, "Waiting for your reply…")
            return

        if action == "reset":
            sub_action = rest[0]
            if sub_action == "confirm":
                _send_or_edit_message(
                    bot,
                    chat_id,
                    "Reset everything?",
                    _confirm_reset_keyboard(),
                    message_id,
                )
                _query_answer(bot, query)
                return
            if sub_action == "confirm_yes":
                prefs = _handle_reset(bot, chat_id, db_user_id, user.id, message_id)
                _query_answer(bot, query, "Reset done")
                return

        _query_answer(bot, query, "Not sure what that button does.")
    except Exception as exc:  # pragma: no cover - runtime guard
        logging.getLogger(__name__).exception("Error handling NSFW callback: %s", exc)
        _query_answer(bot, query, "Something went wrong; try again.", show_alert=True)


def handle_pending_input(
    bot: UnifiedWellnessBot,
    message: Message,
    db_user_id: int,
    telegram_id: int,
) -> bool:
    pending_inputs = getattr(bot, "_pending_inputs", None) or {}
    pending = pending_inputs.get((message.chat_id, db_user_id))
    if not pending:
        return False

    category = pending["category"]
    prefs = load_preferences(bot, db_user_id, telegram_id)
    updated = copy.deepcopy(prefs)
    text = (message.text or "").strip()
    if not text:
        return False

    if category == "roleplay":
        updated.setdefault("roleplay_scenarios", []).append(text)
        _reply_text(bot, message, f"Added custom scenario: {text}")
    elif category == "kinks":
        updated.setdefault("kinks", []).append(text)
        _reply_text(bot, message, f"Added custom kink: {text}")
    elif category == "hard_limit":
        updated.setdefault("hard_limits", []).append(text)
        _reply_text(bot, message, f"Hard limit noted: {text}")
    elif category == "soft_limit":
        updated.setdefault("soft_limits", []).append(text)
        _reply_text(bot, message, f"Soft limit noted: {text}")
    elif category == "safe_word":
        updated["safe_word"] = text
        _reply_text(bot, message, f"Safe word set to {text}")
    elif category == "story_setting":
        updated["story_setting"] = text
        _reply_text(bot, message, "Story preference updated.")
    else:
        _reply_text(bot, message, "Not sure how to save that.")
        pending_inputs.pop((message.chat_id, db_user_id), None)
        return True

    save_preferences(bot, db_user_id, telegram_id, updated)
    pending_inputs.pop((message.chat_id, db_user_id), None)

    pending_message_id = pending.get("message_id")
    if pending_message_id:
        try:
            _run_bot_coroutine(
                bot,
                _ensure_bot(bot).edit_message_reply_markup(
                    chat_id=message.chat_id,
                    message_id=pending_message_id,
                    reply_markup=build_root_keyboard(updated),
                ),
            )
        except Exception:  # pragma: no cover - best effort refresh
            pass
    return True


async def nsfw_pref_command(
    bot: "UnifiedWellnessBot",
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _nsfw_pref_command_impl, bot, update, context)


async def nsfw_pref_callback(
    bot: "UnifiedWellnessBot",
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _nsfw_pref_callback_impl, bot, update, context)


__all__ = ["nsfw_pref_command", "nsfw_pref_callback", "handle_pending_input"]
