#!/usr/bin/env python3
"""
Migration Script: Convert Old Psychological Profiles to Confidence Format

Mission: Migrate all existing psychological profiles from simple float format
to new {"value": X, "confidence": Y} format to prevent TypeError crashes
for existing users.

Old format: {"depression_likelihood": 0.7, "anxiety_likelihood": 0.6}
New format: {"depression_likelihood": {"value": 0.7, "confidence": 0.8}}

This ensures backward compatibility and allows new confidence-aware features.
"""

import json
import sqlite3
from pathlib import Path


def migrate_profile_metrics(profile_data):
    """Convert old-format metrics to new confidence format"""

    def convert_metric(value, default_confidence=0.7):
        """Convert a metric to confidence format if needed"""
        if isinstance(value, dict) and 'value' in value:
            # Already in new format
            return value
        elif isinstance(value, (int, float)):
            # Old format - convert
            return {
                "value": float(value),
                "confidence": default_confidence
            }
        else:
            # Unknown format - keep as is
            return value

    # Sections that should have confidence metrics
    sections_to_convert = [
        'mental_health_indicators',
        'dark_triad',
        'big_five',
        'emotional_intelligence',
        'cognitive_metrics',
        'attachment_style',
        'cognitive_distortions',
        'personality_traits',
        'communication_patterns'
    ]

    modified = False

    for section in sections_to_convert:
        if section in profile_data:
            section_data = profile_data[section]
            if isinstance(section_data, dict):
                for key, value in section_data.items():
                    # Skip non-metric keys
                    if key in ['primary_type', 'type', 'wing', 'integration_direction',
                               'disintegration_direction', 'primary_mechanisms',
                               'mature_mechanisms', 'immature_mechanisms', 'pathological_mechanisms']:
                        continue

                    converted = convert_metric(value)
                    if converted != value:
                        section_data[key] = converted
                        modified = True

    # Special handling for personality_typing (nested structure)
    if 'personality_typing' in profile_data:
        typing = profile_data['personality_typing']
        if isinstance(typing, dict):
            # Convert introversion_level if exists
            if 'introversion_level' in typing:
                converted = convert_metric(typing['introversion_level'])
                if converted != typing['introversion_level']:
                    typing['introversion_level'] = converted
                    modified = True

    return profile_data, modified


def main():
    """Run the migration"""
    db_path = Path('wellness_data') / 'telegram_wellness.db'

    if not db_path.exists():
        print(f"❌ Database not found at {db_path}")
        return

    print(f"🔄 Migrating psychological profiles in {db_path}")
    print("=" * 60)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        # Check if table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='psychological_profiles'"
        )
        if not cursor.fetchone():
            print("ℹ️  No psychological_profiles table found - nothing to migrate")
            return

        # Get all profiles
        profiles = conn.execute(
            "SELECT id, user_id, profile_data, created_at FROM psychological_profiles"
        ).fetchall()

        print(f"📊 Found {len(profiles)} profiles to check")
        print()

        migrated_count = 0
        error_count = 0

        for profile in profiles:
            profile_id = profile['id']
            user_id = profile['user_id']

            try:
                profile_data = json.loads(profile['profile_data'])

                # Migrate the profile
                migrated_data, was_modified = migrate_profile_metrics(profile_data)

                if was_modified:
                    # Update the database
                    conn.execute(
                        "UPDATE psychological_profiles SET profile_data = ? WHERE id = ?",
                        (json.dumps(migrated_data, indent=2), profile_id)
                    )
                    migrated_count += 1
                    print(f"✅ Migrated profile #{profile_id} for user {user_id}")

            except Exception as e:
                error_count += 1
                print(f"❌ Error migrating profile #{profile_id}: {e}")

        # Commit changes
        conn.commit()

        print()
        print("=" * 60)
        print(f"🎉 Migration complete!")
        print(f"   Migrated: {migrated_count} profiles")
        print(f"   Unchanged: {len(profiles) - migrated_count - error_count} profiles")
        print(f"   Errors: {error_count} profiles")

    except Exception as e:
        print(f"❌ Migration failed: {e}")
        conn.rollback()
        raise

    finally:
        conn.close()


if __name__ == '__main__':
    main()
