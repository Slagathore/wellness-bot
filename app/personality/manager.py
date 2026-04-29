"""
Personality Manager

Mission: Manage PER-USER personality configurations, state, and persistence.
Handles loading, saving, switching personalities on a per-user basis.
PREVENTS global personality changes that affect all users.

Goals:
- Per-user personality tracking (NOT global)
- Separate preview from active personality (for admin GUI)
- Auto-save settings changes
- Persist configurations to config.json
- Store active personality per user in database

#todo Add personality scheduling (auto-switch based on time)
#todo Add personality analytics (track which personalities users prefer)
#todo Add custom user-created personalities
#todo Add personality inheritance (base + overrides)
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict

from .modes import (PERSONALITY_MODES, get_default_config,
                    get_personality_names, is_custom_character,
                    load_custom_character_config, parse_custom_character_id)

logger = logging.getLogger(__name__)


class PersonalityManager:
    """Manages personality configurations and PER-USER personality state."""

    def __init__(self, config_path: str | Path, db_path: str | Path):
        """Initialize personality manager.

        Args:
            config_path: Path to config.json file (stores personality configs)
            db_path: Path to SQLite database (stores per-user personality)
        """
        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path)

        # State (for admin GUI preview mode only)
        self.selected_personality = "friendly"  # Preview mode (settings tab)
        self.personality_configs: Dict[str, Dict[str, Any]] = (
            {}
        )  # Cache of all configurations

        # Load existing config or create default
        self._load_config()

        # Ensure database has personality column
        self._ensure_db_schema()

    def _ensure_db_schema(self):
        """Ensure database has personality column."""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                cur = conn.cursor()

                # Check if personality column exists
                cur.execute("PRAGMA table_info(users)")
                columns = [row[1] for row in cur.fetchall()]

                if "personality" not in columns:
                    logger.info("[Personality] Adding personality column to users table")
                    cur.execute(
                        "ALTER TABLE users ADD COLUMN personality TEXT DEFAULT 'friendly'"
                    )
                    cur.execute(
                        "UPDATE users SET personality = 'friendly' WHERE personality IS NULL"
                    )
                    conn.commit()
                    logger.info("[Personality] Schema migration complete")
        except Exception as e:
            logger.error(f"[Personality] Error ensuring schema: {e}", exc_info=True)

    def _load_config(self):
        """Load personality configurations from config.json."""
        try:
            if self.config_path.exists():
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)

                # Load personality configurations (NOT active personality - that's per-user now)
                if "personalities" in config:
                    self.personality_configs = config["personalities"]
                else:
                    # Initialize with defaults
                    self.personality_configs = {
                        name: PERSONALITY_MODES[name].copy()
                        for name in PERSONALITY_MODES
                    }
                    self._save_config(config)
            else:
                # Create default config
                self.personality_configs = {
                    name: PERSONALITY_MODES[name].copy() for name in PERSONALITY_MODES
                }
                self._save_config({})

            logger.info(
                f"[Personality] Loaded {len(self.personality_configs)} personality configs"
            )

        except Exception as e:
            logger.error(f"[Personality] Error loading config: {e}", exc_info=True)
            # Fall back to defaults
            self.personality_configs = {
                name: PERSONALITY_MODES[name].copy() for name in PERSONALITY_MODES
            }

    def _save_config(self, base_config: Dict[str, Any]):
        """Save personality configurations to config.json.

        NOTE: Does NOT save active_personality - that's per-user in database now.

        Args:
            base_config: Existing config dict to merge with
        """
        try:
            # Merge with existing config (remove old active_personality if present)
            if "active_personality" in base_config:
                del base_config["active_personality"]

            base_config["personalities"] = self.personality_configs

            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(base_config, f, indent=2, ensure_ascii=False)

            logger.info("[Personality] Config saved")

        except Exception as e:
            logger.error(f"[Personality] Error saving config: {e}", exc_info=True)

    def get_user_personality(self, user_id: int) -> str:
        """Get personality name for a specific user.

        Args:
            user_id: Database user ID

        Returns:
            Personality name (e.g., 'friendly', 'downbad')
        """
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                cur = conn.cursor()
                cur.execute("SELECT personality FROM users WHERE id = ?", (user_id,))
                row = cur.fetchone()

            if row and row[0]:
                return row[0]
            return "friendly"  # Default
        except Exception as e:
            logger.error(
                f"[Personality] Error getting user personality: {e}", exc_info=True
            )
            return "friendly"

    def set_user_personality(self, user_id: int, personality_name: str) -> bool:
        """Set personality for a specific user.

        Args:
            user_id: Database user ID
            personality_name: Name of personality to set (e.g. 'friendly' or 'custom:17')

        Returns:
            True if successful, False otherwise
        """
        # Accept built-in modes and custom:N references
        if personality_name not in PERSONALITY_MODES and not is_custom_character(personality_name):
            logger.warning(f"[Personality] Unknown personality: {personality_name}")
            return False

        # Validate custom character exists and user has access
        if is_custom_character(personality_name):
            char_id = parse_custom_character_id(personality_name)
            if char_id is None:
                return False
            try:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT id, is_global, creator_user_id FROM custom_characters WHERE id = ?",
                        (char_id,),
                    ).fetchone()
                    if not row:
                        logger.warning(f"[Personality] Custom character {char_id} not found")
                        return False
                    # Check access: global, creator, or explicit grant
                    if not row["is_global"] and row["creator_user_id"] != user_id:
                        access = conn.execute(
                            "SELECT 1 FROM user_character_access WHERE user_id = ? AND character_id = ?",
                            (user_id, char_id),
                        ).fetchone()
                        if not access:
                            logger.warning(f"[Personality] User {user_id} has no access to character {char_id}")
                            return False
            except Exception as e:
                logger.error(f"[Personality] Error validating custom character: {e}", exc_info=True)
                return False

        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE users SET personality = ? WHERE id = ?",
                    (personality_name, user_id),
                )
                conn.commit()
            logger.info(f"[Personality] User {user_id} set to {personality_name}")
            return True
        except Exception as e:
            logger.error(
                f"[Personality] Error setting user personality: {e}", exc_info=True
            )
            return False

    def get_active_config(self, user_id: int) -> Dict[str, Any]:
        """Get configuration for a specific user's active personality.

        Args:
            user_id: Database user ID

        Returns:
            Dictionary with personality settings
        """
        personality_name = self.get_user_personality(user_id)

        # Handle custom characters
        if is_custom_character(personality_name):
            config = load_custom_character_config(personality_name)
            if config:
                return config
            # Character deleted or invalid — fall back to friendly
            return get_default_config()

        return self.personality_configs.get(
            personality_name, get_default_config()
        ).copy()

    def get_available_characters(self, user_id: int) -> list[Dict[str, Any]]:
        """Get all custom characters available to a user.

        Returns characters the user created, has explicit access to, or that are global.
        """
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT DISTINCT c.id, c.name, c.display_name, c.emoji, c.avatar_url
                    FROM custom_characters c
                    LEFT JOIN user_character_access a ON a.character_id = c.id AND a.user_id = ?
                    WHERE c.is_global = 1
                       OR c.creator_user_id = ?
                       OR a.user_id IS NOT NULL
                    ORDER BY c.display_name
                    """,
                    (user_id, user_id),
                ).fetchall()
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "display_name": row["display_name"],
                    "emoji": row["emoji"] or "🎭",
                    "avatar_url": row["avatar_url"] or "",
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"[Personality] Error loading available characters: {e}", exc_info=True)
            return []

    def get_selected_config(self) -> Dict[str, Any]:
        """Get configuration for the currently selected (preview) personality.

        NOTE: This is for admin GUI preview mode only.

        Returns:
            Dictionary with personality settings
        """
        return self.personality_configs.get(
            self.selected_personality, get_default_config()
        ).copy()

    def select_personality(self, personality_name: str):
        """Select a personality for preview (settings tab).

        This does NOT change the bot's behavior, only what's shown in settings.
        Use apply_personality() to actually switch the bot.

        Args:
            personality_name: Name of personality to preview
        """
        if personality_name in PERSONALITY_MODES:
            self.selected_personality = personality_name
            logger.info(f"[Personality] Selected for preview: {personality_name}")
        else:
            logger.warning(f"[Personality] Unknown personality: {personality_name}")

    def apply_personality(self, user_id: int):
        """Apply the selected personality for a specific user.

        NOTE: This is for admin GUI only. Use set_user_personality() for programmatic changes.

        Args:
            user_id: Database user ID to apply personality to
        """
        return self.set_user_personality(user_id, self.selected_personality)

    def update_setting(self, personality_name: str, setting_key: str, value: Any):
        """Update a specific setting for a personality.

        Args:
            personality_name: Name of personality to update
            setting_key: Setting to update (e.g., 'temperature', 'system_prompt')
            value: New value
        """
        if personality_name not in self.personality_configs:
            logger.warning(f"[Personality] Unknown personality: {personality_name}")
            return

        self.personality_configs[personality_name][setting_key] = value

        # Auto-save
        try:
            if self.config_path.exists():
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}

            self._save_config(config)
            logger.debug(
                f"[Personality] Updated {personality_name}.{setting_key} = {value}"
            )

        except Exception as e:
            logger.error(f"[Personality] Error saving setting: {e}", exc_info=True)

    def update_selected_setting(self, setting_key: str, value: Any):
        """Update a setting for the currently selected (preview) personality.

        Args:
            setting_key: Setting to update
            value: New value
        """
        self.update_setting(self.selected_personality, setting_key, value)

    def should_enable_reminders(self, user_id: int) -> bool:
        """Check if reminders should be enabled for a user's personality.

        Args:
            user_id: Database user ID

        Returns:
            Boolean indicating if reminders are enabled
        """
        config = self.get_active_config(user_id)
        return config.get("enable_reminders", True)

    def get_psych_profile_weight(self, user_id: int) -> float:
        """Get psychological profile weight for a user's personality.

        Args:
            user_id: Database user ID

        Returns:
            Float weight (1.0 = full, 0.25 = 25%, etc.)
        """
        config = self.get_active_config(user_id)
        return config.get("psych_profile_weight", 1.0)

    def get_personality_names(self):
        """Get list of all available personality names."""
        return get_personality_names()

    def get_all_configs(self) -> Dict[str, Dict[str, Any]]:
        """Get all personality configurations.

        Returns:
            Dictionary mapping personality names to their configs
        """
        return self.personality_configs.copy()
