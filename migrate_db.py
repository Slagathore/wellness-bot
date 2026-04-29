"""Database migration script to add missing tables"""
import sqlite3
from pathlib import Path


def migrate_database():
    db_path = Path("wellness_data/wellness.db")
    conn = sqlite3.connect(db_path)

    # Add missing tables
    migrations = [
        """CREATE TABLE IF NOT EXISTS moods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mood_score INTEGER NOT NULL,
            note TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",

        """CREATE TABLE IF NOT EXISTS user_streaks (
            user_id INTEGER PRIMARY KEY,
            current_streak INTEGER DEFAULT 0,
            longest_streak INTEGER DEFAULT 0,
            last_activity_date DATE,
            total_active_days INTEGER DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",

        """CREATE TABLE IF NOT EXISTS psychological_profiles (
            user_id INTEGER PRIMARY KEY,
            profile_data TEXT,
            mental_health_indicators TEXT,
            big_five TEXT,
            cognitive_metrics TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",

        """CREATE TABLE IF NOT EXISTS meditation_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            duration_minutes INTEGER,
            completed BOOLEAN DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",

        """CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            goal_text TEXT NOT NULL,
            category TEXT,
            target_date DATE,
            progress INTEGER DEFAULT 0,
            completed BOOLEAN DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )"""
    ]

    for migration in migrations:
        try:
            conn.execute(migration)
            print(f"✅ Migration successful: {migration[:50]}...")
        except sqlite3.OperationalError as e:
            print(f"⚠️ Migration skipped (may already exist): {e}")

    conn.commit()
    conn.close()
    print("Database migration complete!")

if __name__ == "__main__":
    migrate_database()
