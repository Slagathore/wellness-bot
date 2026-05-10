"""Guided onboarding flow for new Telegram users."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.db import db_ro, db_rw
from app.domain.reminders.timezone import normalize_user_local_reminder_time
from app.feature_flags import enabled
from app.utils.ollama import generate
from app.utils.time_utils import (
    OPERATOR_TZ_NAME,
    operator_now,
    operator_offset_minutes,
    to_operator_string,
    to_user_time,
)

WELCOME_MESSAGE = (
    "Welcome to Mira, your personal wellness companion!\n\n"
    "I'm here to help you:\n"
    "- Track your emotional well-being\n"
    "- Build healthier habits\n"
    "- Remember important tasks\n"
    "- Provide support when you need it\n\n"
    "Use /help anytime to see all commands.\n\n"
    "Let's get you set up."
)

CHECK_IN_PROMPT = (
    "How often would you like me to check in?\n"
    "1) Daily\n"
    "2) Every other day\n"
    "3) Weekly\n"
    "4) No automatic check-ins\n\n"
    "Reply with the number that fits you best."
)

REMINDER_TYPES_PROMPT = (
    "What would you like reminders for? (Select all that apply)\n"
    "1) Meals & nutrition\n"
    "2) Medication\n"
    "3) Hydration\n"
    "4) Exercise & movement\n"
    "5) Sleep schedule\n"
    "6) Social connections\n"
    "7) Self-care & wellness\n"
    "8) Custom reminders\n\n"
    "Send the numbers or keywords (e.g., 1,3,5). Reply with 'none' if you don't want reminders right now."
)

FEATURE_PROMPT = (
    "Great! Which features would you like active?\n\n"
    "Options:\n"
    "1) Mood tracking\n"
    "2) Journaling prompts\n"
    "3) Sleep tracking\n"
    "4) Medication tracking\n"
    "5) Wellness goals\n\n"
    "Reply with 'all', 'none', or list the ones you want (e.g., 1,3,5 or mood, sleep)."
)

PERSONALITY_PROMPT = (
    "How would you like me to interact with you?\n\n"
    "1) Supportive Friend - warm, encouraging, emotionally engaged\n"
    "2) Gentle Coach - positive with actionable guidance\n"
    "3) Casual Buddy - laid-back, conversational, low pressure\n"
    "4) Professional Assistant - efficient, organized, to the point\n\n"
    "Pick your vibe (1-4)."
)

NAME_PROMPT = "What name should I use for you?"

PRONOUNS_PROMPT = (
    "Do you have preferred pronouns you'd like me to use?\n"
    "Share anything you're comfortable with (e.g., she/her, they/them) or say 'skip'."
)

TIMEZONE_PROMPT = (
    "What's your timezone?"
    "\nTry one of these if you're in the United States:"
    "\n• Central Time (CST/CDT) — Chicago, Dallas, Houston"
    "\n• Eastern Time (EST/EDT) — New York, Atlanta, Miami"
    "\n• Mountain Time (MST/MDT) — Denver, Phoenix, Salt Lake City"
    "\n• Pacific Time (PST/PDT) — Los Angeles, Seattle, San Francisco"
    "\nYou can also share a CST offset like CST-6 or tell me your city if you're elsewhere."
)

GOALS_PROMPT = (
    "What are you hoping to improve or focus on?\n"
    "Share anything that feels relevant."
)

SUPPORT_STYLE_PROMPT = (
    "How can I support you best?\n"
    "For example: gentle encouragement, direct advice, regular accountability, or say 'skip'."
)

SLEEP_SCHEDULE_PROMPT = (
    "What time do you usually go to sleep and wake up?\n"
    "A natural reply is fine, like 'Usually around 11:30pm and I wake up at 7am.' "
    "You can also say 'skip'."
)

NSFW_PROMPT = (
    "Would you like to unlock the spicy/NSFW personality mode (Downbad)?\n"
    "1) Yes - unlock it\n"
    "2) No thanks (default)\n\n"
    "You can change this anytime later with /nsfwpref."
)

REMINDER_TYPE_CHOICES: List[Tuple[str, str]] = [
    ("meals", "Meals & nutrition"),
    ("medication", "Medication"),
    ("hydration", "Hydration"),
    ("exercise", "Exercise & movement"),
    ("sleep", "Sleep schedule"),
    ("social", "Social connections"),
    ("self_care", "Self-care & wellness"),
    ("custom", "Custom reminders"),
]

REMINDER_INDEX_MAP = {
    str(idx + 1): key for idx, (key, _) in enumerate(REMINDER_TYPE_CHOICES)
}
REMINDER_KEYWORD_MAP: Dict[str, str] = {
    "meal": "meals",
    "meals": "meals",
    "food": "meals",
    "nutrition": "meals",
    "med": "medication",
    "medication": "medication",
    "medications": "medication",
    "pill": "medication",
    "pills": "medication",
    "hydration": "hydration",
    "water": "hydration",
    "drink": "hydration",
    "exercise": "exercise",
    "movement": "exercise",
    "workout": "exercise",
    "sleep": "sleep",
    "rest": "sleep",
    "bed": "sleep",
    "social": "social",
    "friends": "social",
    "family": "social",
    "self-care": "self_care",
    "selfcare": "self_care",
    "wellness": "self_care",
    "custom": "custom",
}

FEATURE_CHOICES: List[Tuple[str, str]] = [
    ("mood_tracking", "Mood tracking"),
    ("journaling", "Journaling prompts"),
    ("sleep_tracking", "Sleep tracking"),
    ("medication_tracking", "Medication tracking"),
    ("wellness_goals", "Wellness goals"),
]

FEATURE_INDEX_MAP = {str(idx + 1): key for idx, (key, _) in enumerate(FEATURE_CHOICES)}
FEATURE_KEYWORD_MAP: Dict[str, str] = {
    "mood": "mood_tracking",
    "mood tracking": "mood_tracking",
    "journaling": "journaling",
    "journal": "journaling",
    "sleep": "sleep_tracking",
    "sleep tracking": "sleep_tracking",
    "medication": "medication_tracking",
    "medication tracking": "medication_tracking",
    "wellness": "wellness_goals",
    "goals": "wellness_goals",
}

FEATURE_FLAG_DEFAULTS: Dict[str, bool] = {
    "mood_journaling": False,
    "journaling_prompts": False,
    "sleep_tracking": False,
    "medication_tracking": False,
    "wellness_goals": False,
    "social_reminders": False,
    "hydration_tracking": False,
}

PERSONALITY_CHOICES: List[Tuple[str, str]] = [
    ("friend", "Supportive friend"),
    ("coach", "Gentle coach"),
    ("buddy", "Casual buddy"),
    ("assistant", "Professional assistant"),
]

PERSONALITY_INDEX_MAP = {
    str(idx + 1): key for idx, (key, _) in enumerate(PERSONALITY_CHOICES)
}
PERSONALITY_KEYWORD_MAP: Dict[str, str] = {
    "friend": "friend",
    "supportive": "friend",
    "coach": "coach",
    "gentle": "coach",
    "buddy": "buddy",
    "casual": "buddy",
    "assistant": "assistant",
    "professional": "assistant",
}

DAY_NAME_MAP: Dict[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

_SKIP_RESPONSES = {
    "",
    "skip",
    "none",
    "no",
    "n/a",
    "nah",
    "pass",
    "no thanks",
    "no thank you",
    "nope",
    "later",
    "maybe later",
}

TIME_PATTERN = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)

DEFAULT_REMINDER_SETTINGS: Dict[str, Dict[str, Any]] = {
    "meals": {
        "defaults": [(8, 0, "Breakfast"), (12, 30, "Lunch"), (18, 0, "Dinner")],
        "frequency": "daily",
    },
    "hydration": {
        "defaults": [(9, 0, "Hydration"), (14, 0, "Hydration"), (20, 0, "Hydration")],
        "frequency": "daily",
    },
    "medication": {
        "defaults": [(8, 0, "Medication"), (20, 0, "Medication")],
        "frequency": "daily",
    },
    "exercise": {
        "defaults": [(18, 0, "Exercise")],
        "frequency": "daily",
    },
    "sleep": {
        "defaults": [(22, 0, "Bedtime"), (7, 0, "Wake up")],
        "frequency": "daily",
    },
    "social": {
        "defaults": [(17, 0, "Social connection")],
        "frequency": "weekly",
        "day_of_week": 6,
    },
    "self_care": {
        "defaults": [(20, 0, "Self-care")],
        "frequency": "daily",
    },
    "custom": {
        "defaults": [],
        "frequency": "daily",
    },
}


def _normalize_tokens(reply: str) -> List[str]:
    tokens = [
        token.strip().lower() for token in re.split(r"[\s,]+", reply) if token.strip()
    ]
    return tokens


def _parse_check_in_frequency(reply: str) -> Optional[str]:
    normalized = reply.strip().lower()
    mapping = {
        "1": "daily",
        "daily": "daily",
        "2": "every_other_day",
        "every other day": "every_other_day",
        "every-other-day": "every_other_day",
        "3": "weekly",
        "weekly": "weekly",
        "4": "none",
        "none": "none",
        "skip": "none",
        "no": "none",
        "no check-ins": "none",
        "no checkins": "none",
        "off": "none",
        "0": "none",
    }
    return mapping.get(normalized)


def _parse_reminder_types(reply: str) -> Optional[List[str]]:
    tokens = _normalize_tokens(reply)
    if not tokens:
        return None
    skip_keywords = {
        "none",
        "no",
        "skip",
        "nothing",
        "nah",
        "pass",
        "0",
        "nope",
        "later",
    }
    if any(token in skip_keywords for token in tokens):
        return []
    selections: List[str] = []
    for token in tokens:
        if token in REMINDER_INDEX_MAP:
            selections.append(REMINDER_INDEX_MAP[token])
        elif token in REMINDER_KEYWORD_MAP:
            selections.append(REMINDER_KEYWORD_MAP[token])
    seen = set()
    ordered: List[str] = []
    for item in selections:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered if ordered else None


def _parse_feature_selection(reply: str) -> Optional[List[str]]:
    text = reply.strip().lower()
    if not text:
        return None
    if text in {"all", "everything"}:
        return [key for key, _ in FEATURE_CHOICES]
    if text in {"none", "no", "skip"}:
        return []

    selections: List[str] = []
    tokens = _normalize_tokens(reply)
    for token in tokens:
        if token in FEATURE_INDEX_MAP:
            selections.append(FEATURE_INDEX_MAP[token])
        elif token in FEATURE_KEYWORD_MAP:
            selections.append(FEATURE_KEYWORD_MAP[token])
    for keyword, value in FEATURE_KEYWORD_MAP.items():
        if keyword in text:
            selections.append(value)
    if not selections:
        return None
    seen = set()
    ordered: List[str] = []
    for item in selections:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _parse_personality(reply: str) -> Optional[str]:
    token = reply.strip().lower()
    if token in PERSONALITY_INDEX_MAP:
        return PERSONALITY_INDEX_MAP[token]
    for keyword, value in PERSONALITY_KEYWORD_MAP.items():
        if keyword in token:
            return value
    return None


def _parse_nsfw_preference(reply: str) -> Optional[bool]:
    token = reply.strip().lower()
    yes_tokens = {"1", "yes", "y", "enable", "enabled", "unlock", "allow", "true"}
    no_tokens = {
        "2",
        "no",
        "n",
        "disable",
        "disabled",
        "keep off",
        "no thanks",
        "nope",
        "false",
        "skip",
    }
    if token in yes_tokens:
        return True
    if token in no_tokens:
        return False
    return None


def _parse_times(text: str) -> List[Tuple[int, int]]:
    times: List[Tuple[int, int]] = []
    for match in TIME_PATTERN.finditer(text):
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3)
        if meridiem:
            meridiem = meridiem.lower()
            hour = hour % 12
            if meridiem == "pm":
                hour += 12
        times.append((hour % 24, minute % 60))
    return times


def _is_skip_response(text: str) -> bool:
    return text.strip().lower() in _SKIP_RESPONSES


def _format_clock_value(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _parse_sleep_schedule(text: str) -> Optional[Dict[str, Any]]:
    if _is_skip_response(text):
        return {"raw_text": text.strip(), "times": [], "bedtime": None, "wake_time": None}

    times = _parse_times(text)
    if len(times) < 2:
        return None

    bedtime = times[0]
    wake_time = times[1]
    return {
        "raw_text": text.strip(),
        "times": [bedtime, wake_time],
        "bedtime": bedtime,
        "wake_time": wake_time,
    }


def _classify_time_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _format_time(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    hour_12 = hour % 12
    if hour_12 == 0:
        hour_12 = 12
    return f"{hour_12}:{minute:02d} {suffix}"


def _parse_timezone_offset(raw: Optional[str]) -> Tuple[int, str]:
    base_offset = operator_offset_minutes()
    base_label = f"{OPERATOR_TZ_NAME}"
    if not raw:
        return base_offset, base_label
    text = raw.strip()
    lower = text.lower()
    match = re.search(r"(utc|gmt)\s*([+-]?\d{1,2})(?::(\d{2}))?", lower)
    if match:
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        total_minutes = hours * 60 + (minutes if hours >= 0 else -minutes)
        label = f"Offset{hours:+03d}:{minutes:02d}"
        return total_minutes, label
    tz_map = {
        "pst": (-8 * 60, "America/Los_Angeles"),
        "pdt": (-7 * 60, "America/Los_Angeles"),
        "pacific": (-8 * 60, "America/Los_Angeles"),
        "pacific daylight": (-7 * 60, "America/Los_Angeles"),
        "pacific standard": (-8 * 60, "America/Los_Angeles"),
        "los angeles": (-8 * 60, "America/Los_Angeles"),
        "la": (-8 * 60, "America/Los_Angeles"),
        "seattle": (-8 * 60, "America/Los_Angeles"),
        "san francisco": (-8 * 60, "America/Los_Angeles"),
        "mst": (-7 * 60, "America/Denver"),
        "mdt": (-6 * 60, "America/Denver"),
        "mountain": (-7 * 60, "America/Denver"),
        "mountain daylight": (-6 * 60, "America/Denver"),
        "mountain standard": (-7 * 60, "America/Denver"),
        "denver": (-7 * 60, "America/Denver"),
        "phoenix": (-7 * 60, "America/Phoenix"),
        "cst": (-6 * 60, OPERATOR_TZ_NAME),
        "cdt": (-5 * 60, OPERATOR_TZ_NAME),
        "central": (-6 * 60, OPERATOR_TZ_NAME),
        "central daylight": (-5 * 60, OPERATOR_TZ_NAME),
        "central standard": (-6 * 60, OPERATOR_TZ_NAME),
        "chicago": (-6 * 60, OPERATOR_TZ_NAME),
        "dallas": (-6 * 60, OPERATOR_TZ_NAME),
        "houston": (-6 * 60, OPERATOR_TZ_NAME),
        "est": (-5 * 60, "America/New_York"),
        "edt": (-4 * 60, "America/New_York"),
        "eastern": (-5 * 60, "America/New_York"),
        "eastern daylight": (-4 * 60, "America/New_York"),
        "eastern standard": (-5 * 60, "America/New_York"),
        "new york": (-5 * 60, "America/New_York"),
        "nyc": (-5 * 60, "America/New_York"),
        "atlanta": (-5 * 60, "America/New_York"),
        "miami": (-5 * 60, "America/New_York"),
        "bst": (1 * 60, "Europe/London"),
        "cet": (1 * 60, "Europe/Berlin"),
        "cest": (2 * 60, "Europe/Berlin"),
        "ist": (5 * 60 + 30, "Asia/Kolkata"),
        "nzt": (12 * 60, "Pacific/Auckland"),
    }
    token = lower.replace("standard", "").replace("time", "").strip()
    token = " ".join(token.split())
    # todo plug in a geolocation lookup to dynamically resolve cities to accurate DST-aware offsets
    if token in tz_map:
        offset, label = tz_map[token]
        return offset, label
    return base_offset, text


def _detect_day_of_week(text: str) -> Optional[int]:
    lower = text.lower()
    for keyword, idx in DAY_NAME_MAP.items():
        if keyword in lower:
            return idx
    return None


class OnboardingFlow:
    """Encapsulates onboarding state transitions."""

    def _initial_state(self) -> Dict[str, Any]:
        return {
            "current_step": "check_in_frequency",
            "responses": {},
            "pending_reminder_types": [],
            "current_reminder_type": None,
        }

    def start(self, db_user_id: int) -> str:
        state = self._initial_state()
        self._save_state(db_user_id, state)
        return f"{WELCOME_MESSAGE}\n{CHECK_IN_PROMPT}"

    def handle_user_message(
        self,
        telegram_user_id: int,
        db_user_id: int,
        message: str,
    ) -> Optional[str]:
        state = self._load_state(db_user_id)
        if state is None:
            return self.start(db_user_id)

        step = state.get("current_step", "check_in_frequency")
        responses = state.setdefault("responses", {})

        if step == "check_in_frequency":
            parsed = _parse_check_in_frequency(message)
            if not parsed:
                return (
                    "Please reply with 1 for Daily, 2 for Every other day, "
                    "3 for Weekly, or 4 for no automatic check-ins."
                )
            responses["check_in_frequency"] = parsed
            state["current_step"] = "reminder_types"
            self._save_state(db_user_id, state)
            return REMINDER_TYPES_PROMPT

        if step == "reminder_types":
            selections = _parse_reminder_types(message)
            if selections is None:
                return (
                    "Please send the numbers or keywords for the reminder types you want, "
                    "or reply with 'none' if you'd prefer to skip reminders for now."
                )
            responses["reminder_types"] = selections
            state["pending_reminder_types"] = selections.copy()
            state["current_step"] = "feature_activation"
            self._save_state(db_user_id, state)
            return FEATURE_PROMPT

        if step == "feature_activation":
            parsed_features = _parse_feature_selection(message)
            if parsed_features is None:
                return "Reply with 'all', 'none', or list the features you want (e.g., 1,3,5 or mood, sleep)."
            responses["features"] = parsed_features
            state["current_step"] = "personality"
            self._save_state(db_user_id, state)
            return PERSONALITY_PROMPT

        if step == "personality":
            personality = _parse_personality(message)
            if not personality:
                return "Please choose 1-4 so I know the tone you prefer."
            responses["personality_mode"] = personality
            if enabled("nsfw_preferences"):
                state["current_step"] = "nsfw_preference"
                self._save_state(db_user_id, state)
                return NSFW_PROMPT
            state["current_step"] = "preferred_name"
            self._save_state(db_user_id, state)
            return NAME_PROMPT
        if step == "nsfw_preference":
            preference = _parse_nsfw_preference(message)
            if preference is None:
                return (
                    "Let me know if you want NSFW/downbad mode unlocked.\n"
                    "Reply with 1 (yes) or 2 (no), or say 'skip' to keep it locked."
                )
            responses["nsfw_opt_in"] = bool(preference)
            state["current_step"] = "preferred_name"
            self._save_state(db_user_id, state)
            return NAME_PROMPT

        if step == "preferred_name":
            name = message.strip()
            if not name:
                return "Let me know what I should call you."
            responses["preferred_name"] = name
            state["current_step"] = "pronouns"
            self._save_state(db_user_id, state)
            return PRONOUNS_PROMPT

        if step == "pronouns":
            pronouns = message.strip()
            if _is_skip_response(pronouns) or not pronouns:
                responses["pronouns"] = ""
            else:
                responses["pronouns"] = pronouns
            state["current_step"] = "timezone"
            self._save_state(db_user_id, state)
            return TIMEZONE_PROMPT

        if step == "timezone":
            timezone_value = message.strip()
            if not timezone_value:
                return (
                    "Please share a timezone so reminders arrive at the right time "
                    "(e.g., Central Time, EST, PST, or CST-6)."
                )
            responses["timezone"] = timezone_value
            state["current_step"] = "sleep_schedule"
            self._save_state(db_user_id, state)
            return SLEEP_SCHEDULE_PROMPT

        if step == "sleep_schedule":
            sleep_details = _parse_sleep_schedule(message)
            if sleep_details is None:
                return (
                    "I did not catch both a usual bedtime and wake time. "
                    "Try something like '11pm and 7am', or say 'skip'."
                )
            bedtime = sleep_details.get("bedtime")
            wake_time = sleep_details.get("wake_time")
            if bedtime and wake_time:
                responses["usual_bedtime"] = _format_clock_value(*bedtime)
                responses["usual_wake_time"] = _format_clock_value(*wake_time)
            else:
                responses["usual_bedtime"] = None
                responses["usual_wake_time"] = None
            state["current_step"] = "support_preferences"
            self._save_state(db_user_id, state)
            return SUPPORT_STYLE_PROMPT

        if step == "support_preferences":
            support_preference = message.strip()
            if _is_skip_response(support_preference) or not support_preference:
                responses["support_preferences"] = ""
            else:
                responses["support_preferences"] = support_preference
            state["current_step"] = "wellness_goals"
            self._save_state(db_user_id, state)
            return GOALS_PROMPT

        if step == "wellness_goals":
            responses["wellness_goals"] = message.strip()
            pending = state.get("pending_reminder_types", [])
            if pending:
                next_type = pending.pop(0)
                state["current_reminder_type"] = next_type
                state["current_step"] = "reminder_times"
                self._save_state(db_user_id, state)
                return self._prompt_for_reminder_type(next_type)
            state["current_step"] = "finalize"
            self._save_state(db_user_id, state)
            return self._finalize_onboarding(telegram_user_id, db_user_id, state)

        if step == "reminder_times":
            current_type = state.get("current_reminder_type")
            if not current_type:
                state["current_step"] = "finalize"
                self._save_state(db_user_id, state)
                return self._finalize_onboarding(telegram_user_id, db_user_id, state)

            details = self._parse_reminder_details(current_type, message)
            if details is None:
                return self._retry_prompt_for_type(current_type)

            reminder_details = responses.setdefault("reminder_details", {})
            reminder_details[current_type] = details

            pending = state.get("pending_reminder_types", [])
            if pending:
                next_type = pending.pop(0)
                state["current_reminder_type"] = next_type
                self._save_state(db_user_id, state)
                return self._prompt_for_reminder_type(next_type)

            state["current_step"] = "finalize"
            self._save_state(db_user_id, state)
            return self._finalize_onboarding(telegram_user_id, db_user_id, state)

        if step == "finalize":
            return self._finalize_onboarding(telegram_user_id, db_user_id, state)

        return self.start(db_user_id)

    def _load_state(self, db_user_id: int) -> Optional[Dict[str, Any]]:
        with db_ro() as conn:
            row = conn.execute(
                "SELECT onboarding_completed, onboarding_data FROM users WHERE id = ?",
                (db_user_id,),
            ).fetchone()
        if not row:
            return None
        if row["onboarding_completed"]:
            return None
        data = row["onboarding_data"] or "{}"
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return None
        if "current_step" not in parsed:
            return None
        return parsed

    def _save_state(self, db_user_id: int, state: Dict[str, Any]) -> None:
        with db_rw() as conn:
            conn.execute(
                "UPDATE users SET onboarding_data = ?, last_active_at = datetime('now') WHERE id = ?",
                (json.dumps(state), db_user_id),
            )

    def _prompt_for_reminder_type(self, reminder_type: str) -> str:
        prompts = {
            "meals": (
                "When should I remind you about meals?"
                "\nShare times for breakfast, lunch, and dinner (e.g., '8am, 12:30pm, 6pm')."
            ),
            "medication": (
                "Tell me about your medication schedule."
                "\nList each medication with times (e.g., 'Sertraline at 8am and 8pm')."
            ),
            "hydration": (
                "When would you like hydration reminders?"
                "\nShare times across the day (e.g., '9am, 2pm, 8pm')."
            ),
            "exercise": (
                "When should I nudge you to move?"
                "\nShare preferred times or days (e.g., 'Weekdays at 7am')."
            ),
            "sleep": (
                "What does your sleep schedule look like?"
                "\nShare bedtime and wake-up times (e.g., 'Bed at 10:30pm, wake at 6:30am')."
            ),
            "social": (
                "When should I remind you to connect with friends or family?"
                "\nInclude a day if you have one (e.g., 'Sundays at 4pm')."
            ),
            "self_care": (
                "What time should I remind you to take a moment for self-care?"
            ),
            "custom": (
                "Describe the reminder and when it should happen (e.g., 'Water the plants on Mondays at 7pm')."
            ),
        }
        return prompts.get(reminder_type, "Let me know the schedule you prefer.")

    def _retry_prompt_for_type(self, reminder_type: str) -> str:
        return "I did not catch clear times. Could you share specific times (like '8am' or '20:00')?"

    def _parse_reminder_details(
        self, reminder_type: str, message: str
    ) -> Optional[Dict[str, Any]]:
        text = message.strip()
        if not text:
            return None

        if reminder_type == "sleep":
            return _parse_sleep_schedule(text)

        parsed_times = _parse_times(text)
        details: Dict[str, Any] = {
            "raw_text": text,
            "times": parsed_times,
        }

        if reminder_type == "social":
            day = _detect_day_of_week(text)
            if day is not None:
                details["day_of_week"] = day
        return details

    def _finalize_onboarding(
        self,
        telegram_user_id: int,
        db_user_id: int,
        state: Dict[str, Any],
    ) -> str:
        responses = state.get("responses", {})
        tz_offset_minutes, timezone_label = _parse_timezone_offset(
            responses.get("timezone")
        )
        responses["timezone_offset_minutes"] = tz_offset_minutes
        responses["timezone_label"] = timezone_label
        if "nsfw_opt_in" not in responses:
            responses["nsfw_opt_in"] = False

        feature_flags = self._build_feature_flags(responses)
        reminder_summary = self._create_reminders(
            db_user_id, responses, tz_offset_minutes
        )

        profile_entries = self._generate_profile_entries(db_user_id, responses)
        self._store_profile_entries(
            db_user_id, responses, profile_entries, timezone_label, tz_offset_minutes
        )

        onboarding_summary = self._build_onboarding_summary(responses, reminder_summary)

        with db_rw() as conn:
            conn.execute(
                """
                UPDATE users
                   SET onboarding_completed = 1,
                       onboarding_data = ?,
                       feature_flags = ?,
                       last_active_at = datetime('now')
                 WHERE id = ?
                """,
                (
                    json.dumps(onboarding_summary),
                    json.dumps(feature_flags),
                    db_user_id,
                ),
            )
            preferred_name = responses.get("preferred_name", "").strip()
            if preferred_name:
                conn.execute(
                    "UPDATE users SET display_name = ? WHERE id = ?",
                    (preferred_name, db_user_id),
                )

        confirmation = self._build_confirmation_message(
            responses, feature_flags, reminder_summary
        )
        return confirmation

    def _build_feature_flags(self, responses: Dict[str, Any]) -> Dict[str, bool]:
        flags = FEATURE_FLAG_DEFAULTS.copy()
        selected_features: List[str] = responses.get("features", [])
        for feature in selected_features:
            if feature == "mood_tracking":
                flags["mood_journaling"] = True
            elif feature == "journaling":
                flags["journaling_prompts"] = True
            elif feature == "sleep_tracking":
                flags["sleep_tracking"] = True
            elif feature == "medication_tracking":
                flags["medication_tracking"] = True
            elif feature == "wellness_goals":
                flags["wellness_goals"] = True
        reminder_types = responses.get("reminder_types", [])
        if "hydration" in reminder_types:
            flags["hydration_tracking"] = True
        if "social" in reminder_types:
            flags["social_reminders"] = True
        return flags

    def _create_reminders(
        self,
        db_user_id: int,
        responses: Dict[str, Any],
        tz_offset_minutes: int,
    ) -> List[str]:
        reminder_types = responses.get("reminder_types", [])
        details_map: Dict[str, Dict[str, Any]] = responses.get("reminder_details", {})
        sleep_window = self._resolve_sleep_window_from_responses(responses, details_map)
        summary: List[str] = []

        with db_rw() as conn:
            for reminder_type in reminder_types:
                defaults = DEFAULT_REMINDER_SETTINGS.get(
                    reminder_type, {"defaults": [], "frequency": "daily"}
                )
                user_details = details_map.get(reminder_type, {})
                summary.extend(
                    self._create_reminders_for_type(
                        conn,
                        db_user_id,
                        reminder_type,
                        defaults,
                        user_details,
                        tz_offset_minutes,
                        sleep_window,
                    )
                )
        return summary

    def _create_reminders_for_type(
        self,
        conn,
        user_id: int,
        reminder_type: str,
        defaults: Dict[str, Any],
        user_details: Dict[str, Any],
        tz_offset_minutes: int,
        sleep_window: Optional[Tuple[Tuple[int, int], Tuple[int, int]]],
    ) -> List[str]:
        user_times: List[Tuple[int, int]] = user_details.get("times") or []
        has_user_times = bool(user_times)
        times: List[Tuple[int, int]] = user_times.copy()
        if not times:
            times = [(h, m) for h, m, _ in defaults.get("defaults", [])]
        day_of_week = user_details.get("day_of_week") or defaults.get("day_of_week")
        raw_text = user_details.get("raw_text", "")

        summary: List[str] = []

        if reminder_type == "meals":
            meal_labels = ["Breakfast", "Lunch", "Dinner"]
            for idx, (hour, minute) in enumerate(times[:3]):
                label = meal_labels[idx] if idx < len(meal_labels) else "Meal"
                text = f"Time for {label.lower()}"
                summary.append(
                    self._insert_reminder(
                        conn,
                        user_id,
                        text,
                        hour,
                        minute,
                        frequency="daily",
                        tz_offset_minutes=tz_offset_minutes,
                        label=label,
                        allow_jitter=not has_user_times,
                        base_hour=hour,
                        base_minute=minute,
                        sleep_window=sleep_window,
                    )
                )
            return summary

        if reminder_type == "hydration":
            hydration_times = times[:4] or [(9, 0), (14, 0), (20, 0)]
            for hour, minute in hydration_times:
                text = "Drink some water"
                summary.append(
                    self._insert_reminder(
                        conn,
                        user_id,
                        text,
                        hour,
                        minute,
                        frequency="daily",
                        tz_offset_minutes=tz_offset_minutes,
                        label="Hydration",
                        allow_jitter=not has_user_times,
                        base_hour=hour,
                        base_minute=minute,
                        sleep_window=sleep_window,
                    )
                )
            return summary

        if reminder_type == "medication":
            display = raw_text if raw_text else "your medication"
            medication_times = times[:4] or [(8, 0), (20, 0)]
            for hour, minute in medication_times:
                text = f"Take your medication ({display})"
                summary.append(
                    self._insert_reminder(
                        conn,
                        user_id,
                        text,
                        hour,
                        minute,
                        frequency="daily",
                        tz_offset_minutes=tz_offset_minutes,
                        label="Medication",
                        allow_jitter=not has_user_times,
                        base_hour=hour,
                        base_minute=minute,
                        sleep_window=sleep_window,
                    )
                )
            return summary

        if reminder_type == "exercise":
            exercise_times = times[:3] or [(18, 0)]
            for hour, minute in exercise_times:
                text = "Time to move your body"
                summary.append(
                    self._insert_reminder(
                        conn,
                        user_id,
                        text,
                        hour,
                        minute,
                        frequency="daily",
                        tz_offset_minutes=tz_offset_minutes,
                        label="Exercise",
                        allow_jitter=not has_user_times,
                        base_hour=hour,
                        base_minute=minute,
                        sleep_window=sleep_window,
                    )
                )
            return summary

        if reminder_type == "sleep":
            if not times and sleep_window:
                times = [sleep_window[0], sleep_window[1]]
            if times:
                hour, minute = times[0]
                summary.append(
                    self._insert_reminder(
                        conn,
                        user_id,
                        "Start winding down for bed",
                        hour,
                        minute,
                        frequency="daily",
                        tz_offset_minutes=tz_offset_minutes,
                        label="Bedtime",
                        allow_jitter=not has_user_times,
                        base_hour=hour,
                        base_minute=minute,
                        respect_sleep_window=False,
                    )
                )
            if len(times) >= 2:
                hour, minute = times[1]
                summary.append(
                    self._insert_reminder(
                        conn,
                        user_id,
                        "Good morning! How did you sleep?",
                        hour,
                        minute,
                        frequency="daily",
                        tz_offset_minutes=tz_offset_minutes,
                        label="Wake-up",
                        allow_jitter=not has_user_times,
                        base_hour=hour,
                        base_minute=minute,
                        respect_sleep_window=False,
                    )
                )
            return summary

        if reminder_type == "social":
            target_day = day_of_week if day_of_week is not None else 6
            social_times = times[:1] or [(17, 0)]
            for hour, minute in social_times:
                text = "Reach out to someone you care about"
                summary.append(
                    self._insert_reminder(
                        conn,
                        user_id,
                        text,
                        hour,
                        minute,
                        frequency="weekly",
                        tz_offset_minutes=tz_offset_minutes,
                        label="Social",
                        day_of_week=target_day,
                        allow_jitter=not has_user_times,
                        base_hour=hour,
                        base_minute=minute,
                        sleep_window=sleep_window,
                    )
                )
            return summary

        if reminder_type == "self_care":
            self_care_times = times[:2] or [(20, 0)]
            for hour, minute in self_care_times:
                text = "Pause for a moment of self-care"
                summary.append(
                    self._insert_reminder(
                        conn,
                        user_id,
                        text,
                        hour,
                        minute,
                        frequency="daily",
                        tz_offset_minutes=tz_offset_minutes,
                        label="Self-care",
                        allow_jitter=not has_user_times,
                        base_hour=hour,
                        base_minute=minute,
                        sleep_window=sleep_window,
                    )
                )
            return summary

        if reminder_type == "custom":
            description = raw_text or "Custom reminder"
            if not times:
                times = [(9, 0)]
            for hour, minute in times[:4]:
                summary.append(
                    self._insert_reminder(
                        conn,
                        user_id,
                        description,
                        hour,
                        minute,
                        frequency="daily",
                        tz_offset_minutes=tz_offset_minutes,
                        label="Custom",
                        allow_jitter=not has_user_times,
                        base_hour=hour,
                        base_minute=minute,
                        sleep_window=sleep_window,
                    )
                )
            return summary

        for hour, minute in times[:2]:
            summary.append(
                self._insert_reminder(
                    conn,
                    user_id,
                    "Friendly reminder",
                    hour,
                    minute,
                    frequency=defaults.get("frequency", "daily"),
                    tz_offset_minutes=tz_offset_minutes,
                    label="Reminder",
                    day_of_week=day_of_week,
                    allow_jitter=not has_user_times,
                    base_hour=hour,
                    base_minute=minute,
                    sleep_window=sleep_window,
                )
            )
        return summary

    def _insert_reminder(
        self,
        conn,
        user_id: int,
        text: str,
        hour: int,
        minute: int,
        frequency: str,
        tz_offset_minutes: int,
        label: Optional[str] = None,
        day_of_week: Optional[int] = None,
        *,
        allow_jitter: bool,
        base_hour: Optional[int] = None,
        base_minute: Optional[int] = None,
        sleep_window: Optional[Tuple[Tuple[int, int], Tuple[int, int]]] = None,
        respect_sleep_window: bool = True,
    ) -> str:
        time_of_day = _classify_time_of_day(hour)
        next_run_at = self._next_run_at(
            hour,
            minute,
            tz_offset_minutes,
            frequency,
            day_of_week,
            sleep_window=sleep_window,
            respect_sleep_window=respect_sleep_window,
        )
        base_hour = hour if base_hour is None else base_hour
        base_minute = minute if base_minute is None else base_minute
        payload = {
            "text": text,
            "frequency": frequency,
            "time_of_day": time_of_day,
            "base_hour": base_hour,
            "base_minute": base_minute,
            "allow_jitter": bool(allow_jitter),
            "specific_hour": hour if not allow_jitter else None,
            "specific_minute": minute if not allow_jitter else None,
            "respect_sleep_window": bool(respect_sleep_window),
        }
        if label:
            payload["label"] = label
        if day_of_week is not None:
            payload["day_of_week"] = day_of_week
        conn.execute(
            """
            INSERT INTO reminders (user_id, kind, payload, next_run_at, cadence_cron, enabled)
            VALUES (?, 'custom_reminder', ?, ?, ?, 1)
            """,
            (
                user_id,
                json.dumps(payload),
                next_run_at,
                frequency,
            ),
        )
        schedule_note = _format_time(hour, minute)
        if day_of_week is not None:
            day_name = [
                name for name, idx in DAY_NAME_MAP.items() if idx == day_of_week
            ]
            if day_name:
                schedule_note = f"{day_name[0].capitalize()} at {schedule_note}"
        return f"{text} ({schedule_note})"

    def _next_run_at(
        self,
        hour: int,
        minute: int,
        tz_offset_minutes: int,
        frequency: str,
        day_of_week: Optional[int],
        *,
        sleep_window: Optional[Tuple[Tuple[int, int], Tuple[int, int]]] = None,
        respect_sleep_window: bool = True,
    ) -> str:
        reference = operator_now()
        now_local = to_user_time(reference, tz_offset_minutes, reference=reference)
        next_local = now_local.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

        if frequency == "weekly":
            target_day = day_of_week if day_of_week is not None else now_local.weekday()
            days_ahead = (target_day - next_local.weekday()) % 7
            if days_ahead == 0 and next_local <= now_local:
                days_ahead = 7
            next_local = next_local + timedelta(days=days_ahead)
        else:
            if next_local <= now_local:
                next_local += timedelta(days=1)

        if respect_sleep_window:
            next_local = normalize_user_local_reminder_time(
                next_local,
                reference_local=now_local,
                time_of_day=_classify_time_of_day(hour),
                sleep_window=sleep_window,
                min_lead_minutes=30,
            )

        return to_operator_string(next_local, tz_offset_minutes, reference=reference)

    def _resolve_sleep_window_from_responses(
        self,
        responses: Dict[str, Any],
        details_map: Dict[str, Dict[str, Any]],
    ) -> Optional[Tuple[Tuple[int, int], Tuple[int, int]]]:
        sleep_details = details_map.get("sleep") or {}
        bedtime = sleep_details.get("bedtime")
        wake_time = sleep_details.get("wake_time")
        if bedtime and wake_time:
            return bedtime, wake_time

        bedtime_raw = responses.get("usual_bedtime")
        wake_raw = responses.get("usual_wake_time")
        if bedtime_raw and wake_raw:
            try:
                bed_hour, bed_minute = str(bedtime_raw).split(":", 1)
                wake_hour, wake_minute = str(wake_raw).split(":", 1)
                return (
                    (int(bed_hour), int(bed_minute)),
                    (int(wake_hour), int(wake_minute)),
                )
            except Exception:
                return None
        return None

    def _generate_profile_entries(
        self,
        db_user_id: int,
        responses: Dict[str, Any],
    ) -> List[Dict[str, str]]:
        structured_payload = {
            "check_in_frequency": responses.get("check_in_frequency"),
            "reminder_types": responses.get("reminder_types", []),
            "features": responses.get("features", []),
            "personality_mode": responses.get("personality_mode"),
            "preferred_name": responses.get("preferred_name"),
            "timezone": responses.get("timezone_label"),
            "usual_bedtime": responses.get("usual_bedtime"),
            "usual_wake_time": responses.get("usual_wake_time"),
            "wellness_goals": responses.get("wellness_goals"),
        }
        prompt = (
            "A new user just completed onboarding. Based on the information below, create 3-5 concise profile entries "
            "that will help personalize future conversations.\n\n"
            f"User responses (JSON):\n{json.dumps(structured_payload, indent=2)}\n\n"
            "Return a JSON array like:\n"
            "[\n"
            '  {"key": "sleep_goal", "value": "Wants to improve sleep routine"},\n'
            '  {"key": "timezone", "value": "CST-6"}\n'
            "]"
        )
        try:
            result = generate(prompt, format="json", options={"temperature": 0.2})
            raw = result.get("text", "{}")
            data = json.loads(raw)
            if isinstance(data, list):
                entries = []
                for item in data:
                    if isinstance(item, dict) and "key" in item and "value" in item:
                        entries.append(
                            {
                                "key": str(item["key"]),
                                "value": str(item["value"]),
                            }
                        )
                return entries
        except Exception:
            return []
        return []

    def _store_profile_entries(
        self,
        db_user_id: int,
        responses: Dict[str, Any],
        llm_entries: List[Dict[str, str]],
        timezone_label: str,
        tz_offset_minutes: int,
    ) -> None:
        base_entries = [
            ("preferred_name", responses.get("preferred_name", "")),
            ("pronouns", responses.get("pronouns", "")),
            ("timezone", timezone_label),
            ("timezone_offset_minutes", str(tz_offset_minutes)),
            ("usual_bedtime", responses.get("usual_bedtime")),
            ("usual_wake_time", responses.get("usual_wake_time")),
            ("personality_mode", responses.get("personality_mode", "")),
            ("check_in_frequency", responses.get("check_in_frequency", "")),
            ("wellness_goals", responses.get("wellness_goals")),
            ("support_preferences", responses.get("support_preferences", "")),
        ]
        nsfw_pref = responses.get("nsfw_opt_in")
        if nsfw_pref is not None:
            base_entries.append(("nsfw_opt_in", "true" if nsfw_pref else "false"))
        reminder_types = responses.get("reminder_types", [])
        if reminder_types:
            base_entries.append(("reminder_preferences", ", ".join(reminder_types)))
        features = responses.get("features", [])
        if features:
            base_entries.append(("feature_preferences", ", ".join(features)))

        with db_rw() as conn:
            for key, value in base_entries:
                if not value:
                    continue
                conn.execute(
                    """
                    INSERT INTO profile_context (user_id, key, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, key)
                    DO UPDATE SET value = excluded.value, updated_at = datetime('now')
                    """,
                    (db_user_id, key, value),
                )
            for entry in llm_entries:
                conn.execute(
                    """
                    INSERT INTO profile_context (user_id, key, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, key)
                    DO UPDATE SET value = excluded.value, updated_at = datetime('now')
                    """,
                    (db_user_id, entry["key"], entry["value"]),
                )

    def _build_onboarding_summary(
        self,
        responses: Dict[str, Any],
        reminder_summary: List[str],
    ) -> Dict[str, Any]:
        summary = {
            "check_in_frequency": responses.get("check_in_frequency"),
            "focus_areas": responses.get("reminder_types", []),
            "features": responses.get("features", []),
            "personality_mode": responses.get("personality_mode"),
            "preferred_name": responses.get("preferred_name"),
            "pronouns": responses.get("pronouns"),
            "timezone": responses.get("timezone_label"),
            "timezone_offset_minutes": responses.get("timezone_offset_minutes"),
            "usual_bedtime": responses.get("usual_bedtime"),
            "usual_wake_time": responses.get("usual_wake_time"),
            "wellness_goals": responses.get("wellness_goals"),
            "support_preferences": responses.get("support_preferences"),
            "nsfw_opt_in": responses.get("nsfw_opt_in"),
            "reminders_created": reminder_summary,
        }
        return summary

    def _build_confirmation_message(
        self,
        responses: Dict[str, Any],
        feature_flags: Dict[str, bool],
        reminder_summary: List[str],
    ) -> str:
        check_in_raw = responses.get("check_in_frequency")
        if check_in_raw in (None, "", "none"):
            check_in = "Off (I'll wait for you to ask)"
        else:
            check_in = str(check_in_raw).replace("_", " ")
        active_features = [
            name
            for key, name in FEATURE_CHOICES
            if feature_flags.get(self._feature_flag_key(key))
        ]
        if (
            feature_flags.get("hydration_tracking")
            and "Hydration tracking" not in active_features
        ):
            active_features.append("Hydration tracking")
        if (
            feature_flags.get("social_reminders")
            and "Social reminders" not in active_features
        ):
            active_features.append("Social reminders")
        feature_text = ", ".join(active_features) if active_features else "None"
        personality_map = {
            "friend": "Supportive friend",
            "coach": "Gentle coach",
            "buddy": "Casual buddy",
            "assistant": "Professional assistant",
        }
        personality_key = responses.get("personality_mode")
        if not isinstance(personality_key, str):
            personality_key = ""
        personality = personality_map.get(personality_key, "Supportive friend")
        pronouns = responses.get("pronouns")
        pronouns_line = f"- Pronouns: {pronouns}\n" if pronouns else ""
        support_pref = responses.get("support_preferences")
        support_line = (
            f"- Support preferences: {support_pref}\n" if support_pref else ""
        )
        nsfw_enabled = bool(responses.get("nsfw_opt_in"))
        nsfw_line = "- NSFW (Downbad) access: {}\n".format(
            "Unlocked" if nsfw_enabled else "Locked (use /nsfwpref to unlock)"
        )
        reminder_lines = (
            "\n".join(f"  - {item}" for item in reminder_summary)
            if reminder_summary
            else "  - None yet"
        )
        commands = (
            "You can change any of this anytime with:\n"
            "/reminders - Manage reminders\n"
            "/profile - View or edit your profile\n"
            "/settings - Adjust preferences\n"
            "/help - See all commands\n"
        )
        message = (
            "Perfect! Here's your setup:\n\n"
            f"- Check-ins: {check_in}\n"
            f"{pronouns_line}"
            f"{support_line}"
            f"{nsfw_line}"
            f"- Active features: {feature_text}\n"
            f"- Preferred interaction style: {personality}\n"
            "- Reminders scheduled:\n"
            f"{reminder_lines}\n\n"
            f"{commands}\n"
            "Ready to start? How are you feeling today?"
        )
        return message

    def _feature_flag_key(self, feature: str) -> str:
        mapping = {
            "mood_tracking": "mood_journaling",
            "journaling": "journaling_prompts",
            "sleep_tracking": "sleep_tracking",
            "medication_tracking": "medication_tracking",
            "wellness_goals": "wellness_goals",
        }
        return mapping.get(feature, feature)


# Singleton instance used by the consumer
onboarding_flow = OnboardingFlow()
