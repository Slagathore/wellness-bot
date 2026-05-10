"""
NSFW Preferences Data Models

This module defines the complete data structure for user NSFW preferences.
All preferences are stored as JSON files per user in wellness_data/nsfw_preferences/

Data Structure:
- user_id.json contains all preferences
- Preferences overlay on top of base downbad system prompt
- Preferences persist until user runs /nsfwpref again
"""

from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
import json
from pathlib import Path

from app.utils.time_utils import operator_now


# ============================================================================
# CONSTANTS - Valid options for each preference
# ============================================================================

VALID_GENDERS = ["male", "female", "non-binary", "futanari", "tentacle", "other"]

VALID_INTENSITIES = ["mild", "moderate", "intense"]

VALID_STYLES = ["romantic", "playful", "dominant", "submissive", "explicit"]

VALID_PACING = ["slow", "medium", "fast"]

# Roleplay scenario categories
RELATIONSHIP_DYNAMICS = [
    "strangers",
    "long_distance",
    "ex_lovers",
    "forbidden",
    "boss_employee",
    "teacher_student",
    "doctor_patient",
    "trainer_client",
]

FANTASY_SETTINGS = [
    "trapped_together",
    "late_office",
    "hotel_vacation",
    "public_places",
    "home_scenarios",
    "gym_workout",
    "coffee_bar",
]

CHARACTER_TYPES = [
    "confident_seducer",
    "innocent_curious",
    "experienced_teacher",
    "eager_submissive",
    "controlling_dominant",
    "friend_crossing_line",
    "mysterious_stranger",
    "devoted_partner",
]

# Common hard limits (boundaries)
COMMON_HARD_LIMITS = [
    "non_consensual",
    "age_play",
    "extreme_violence",
    "bodily_waste",
    "bestiality",
    "incest",
    "degradation",
    "slurs",
    "pregnancy",
    "feet",
    "vore",
    "hypnosis",
]


# ============================================================================
# DATA MODEL
# ============================================================================


@dataclass
class NSFWPreferences:
    """
    Complete NSFW preferences for a single user.

    This combines preferences from:
    - FEATURE_ANALYSIS_AND_ROADMAP.md (technical preferences)
    - IMPLEMENTATION_BULK_DELETE_NSFW.md (UI-friendly categories)

    All fields have defaults so partial preferences are valid.
    """

    # === IDENTITY & GENDER ===
    user_gender: str = "male"  # User's gender/identity
    bot_gender: str = "female"  # Bot's gender/identity
    user_body_type: str = "athletic"  # User's body description
    bot_body_type: str = "curvy"  # Bot's body description

    # === CONTENT INTENSITY ===
    # intensity_level: 0-10 scale (technical)
    # content_intensity: "mild"/"moderate"/"intense" (user-friendly)
    intensity_level: int = 5  # 0=soft, 10=extreme
    content_intensity: str = (
        "moderate"  # Maps to: mild(0-3), moderate(4-7), intense(8-10)
    )

    # === INTERACTION STYLE ===
    preferred_style: str = "playful"  # romantic|playful|dominant|submissive|explicit
    dominance_level: int = 5  # 0=submissive, 10=dominant (granular control)

    # === RESPONSE SETTINGS ===
    verbosity: int = 6  # 0=brief, 10=verbose (response length)
    pacing: str = "medium"  # slow|medium|fast (how quickly to escalate)

    # === BOT BEHAVIOR ===
    allow_bot_actions: bool = True  # Can bot add user's actions to story?
    allow_retcon: bool = True  # Can user rewrite bot's messages?
    use_emojis: bool = True  # Include emojis in messages?

    # === ROLEPLAY PREFERENCES ===
    # Each is a list of selected options from the constants above
    relationship_dynamics: List[str] = field(
        default_factory=list
    )  # From RELATIONSHIP_DYNAMICS
    fantasy_settings: List[str] = field(default_factory=list)  # From FANTASY_SETTINGS
    character_types: List[str] = field(default_factory=list)  # From CHARACTER_TYPES

    # === KINKS & INTERESTS ===
    kinks: List[str] = field(default_factory=list)  # User-defined kinks (freeform)
    special_interests: str = ""  # Freeform text field for additional interests

    # === BOUNDARIES ===
    hard_limits: List[str] = field(
        default_factory=list
    )  # From COMMON_HARD_LIMITS + custom
    soft_boundaries: str = ""  # Freeform text describing soft limits
    safe_word: str = "red"  # Safe word to immediately stop/switch modes

    # === STORY/CHARACTER ===
    story_setting: str = ""  # Freeform scenario description
    custom_character_id: Optional[str] = (
        None  # References custom_characters/{id}.json if set
    )

    # === METADATA ===
    created_at: str = field(default_factory=lambda: operator_now().isoformat())
    updated_at: str = field(default_factory=lambda: operator_now().isoformat())
    version: int = 2  # Schema version for future migrations

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NSFWPreferences":
        """Create from dictionary loaded from JSON."""
        # Filter out any keys that aren't in the dataclass
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered_data)

    def update_timestamp(self):
        """Update the updated_at timestamp."""
        self.updated_at = operator_now().isoformat()


# ============================================================================
# STORAGE HELPERS
# ============================================================================


class NSFWPreferencesStorage:
    """
    Handles loading and saving NSFW preferences to/from JSON files.

    Storage location: wellness_data/nsfw_preferences/{user_id}.json

    Why JSON files instead of database:
    - User-specific data, not queried across users
    - Complex nested structure
    - Easy to backup/delete/encrypt per-user
    - Privacy isolation
    """

    BASE_DIR = Path("wellness_data/nsfw_preferences")

    @classmethod
    def _ensure_directory(cls):
        """Create storage directory if it doesn't exist."""
        cls.BASE_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _get_file_path(cls, user_id: int) -> Path:
        """Get the file path for a user's preferences."""
        return cls.BASE_DIR / f"{user_id}.json"

    @classmethod
    def load(cls, user_id: int) -> Optional[NSFWPreferences]:
        """
        Load user's NSFW preferences from file.

        Returns:
            NSFWPreferences object if file exists, None otherwise
        """
        file_path = cls._get_file_path(user_id)

        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return NSFWPreferences.from_dict(data)
        except Exception as e:
            # Log error but don't crash - return None means "no preferences set"
            import logging

            logging.error(f"Error loading NSFW preferences for user {user_id}: {e}")
            return None

    @classmethod
    def save(cls, user_id: int, preferences: NSFWPreferences) -> bool:
        """
        Save user's NSFW preferences to file.

        Returns:
            True if successful, False otherwise
        """
        cls._ensure_directory()
        file_path = cls._get_file_path(user_id)

        try:
            # Update timestamp before saving
            preferences.update_timestamp()

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(preferences.to_dict(), f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            import logging

            logging.error(f"Error saving NSFW preferences for user {user_id}: {e}")
            return False

    @classmethod
    def delete(cls, user_id: int) -> bool:
        """
        Delete user's NSFW preferences file.

        Returns:
            True if deleted or didn't exist, False on error
        """
        file_path = cls._get_file_path(user_id)

        try:
            if file_path.exists():
                file_path.unlink()
            return True
        except Exception as e:
            import logging

            logging.error(f"Error deleting NSFW preferences for user {user_id}: {e}")
            return False

    @classmethod
    def exists(cls, user_id: int) -> bool:
        """Check if user has saved preferences."""
        return cls._get_file_path(user_id).exists()


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def get_intensity_from_level(level: int) -> str:
    """
    Convert numeric intensity level (0-10) to friendly name.

    0-3: mild (PG-13, suggestive)
    4-7: moderate (R-rated, explicit)
    8-10: intense (X-rated, no limits)
    """
    if level <= 3:
        return "mild"
    elif level <= 7:
        return "moderate"
    else:
        return "intense"


def get_level_from_intensity(intensity: str) -> int:
    """
    Convert friendly intensity name to numeric level.

    Default to middle of range for each category.
    """
    mapping = {"mild": 2, "moderate": 5, "intense": 9}
    return mapping.get(intensity.lower(), 5)


def create_default_preferences() -> NSFWPreferences:
    """
    Create a new NSFWPreferences object with sensible defaults.

    Defaults are moderate/balanced to work for most users.
    User can customize from there.
    """
    return NSFWPreferences(
        user_gender="male",
        bot_gender="female",
        intensity_level=5,
        content_intensity="moderate",
        preferred_style="playful",
        dominance_level=5,
        verbosity=6,
        pacing="medium",
        allow_bot_actions=True,
        allow_retcon=True,
        use_emojis=True,
        safe_word="red",
    )


# ============================================================================
# VALIDATION
# ============================================================================


def validate_preferences(prefs: NSFWPreferences) -> List[str]:
    """
    Validate preferences and return list of error messages.

    Returns:
        Empty list if valid, list of error strings if invalid
    """
    errors = []

    # Validate genders
    if prefs.user_gender not in VALID_GENDERS:
        errors.append(f"Invalid user_gender: {prefs.user_gender}")
    if prefs.bot_gender not in VALID_GENDERS:
        errors.append(f"Invalid bot_gender: {prefs.bot_gender}")

    # Validate intensity
    if not 0 <= prefs.intensity_level <= 10:
        errors.append(f"intensity_level must be 0-10, got {prefs.intensity_level}")
    if prefs.content_intensity not in VALID_INTENSITIES:
        errors.append(f"Invalid content_intensity: {prefs.content_intensity}")

    # Validate style
    if prefs.preferred_style not in VALID_STYLES:
        errors.append(f"Invalid preferred_style: {prefs.preferred_style}")

    # Validate dominance level
    if not 0 <= prefs.dominance_level <= 10:
        errors.append(f"dominance_level must be 0-10, got {prefs.dominance_level}")

    # Validate verbosity
    if not 0 <= prefs.verbosity <= 10:
        errors.append(f"verbosity must be 0-10, got {prefs.verbosity}")

    # Validate pacing
    if prefs.pacing not in VALID_PACING:
        errors.append(f"Invalid pacing: {prefs.pacing}")

    # Validate roleplay selections (check against valid options)
    for dynamic in prefs.relationship_dynamics:
        if dynamic not in RELATIONSHIP_DYNAMICS:
            errors.append(f"Invalid relationship_dynamic: {dynamic}")

    for setting in prefs.fantasy_settings:
        if setting not in FANTASY_SETTINGS:
            errors.append(f"Invalid fantasy_setting: {setting}")

    for char_type in prefs.character_types:
        if char_type not in CHARACTER_TYPES:
            errors.append(f"Invalid character_type: {char_type}")

    return errors
