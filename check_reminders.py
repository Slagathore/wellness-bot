#!/usr/bin/env python3
"""Check current reminders in database to understand structure for batching feature."""

import sqlite3
from pathlib import Path

db_path = Path(__file__).parent / "wellness.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== Current Reminders ===\n")
rows = conn.execute("""
    SELECT id, user_id, kind, next_run_at, cadence_cron, last_delivered_at, payload
    FROM reminders
    WHERE enabled=1
    LIMIT 10
""").fetchall()

if not rows:
    print("No enabled reminders found.")
else:
    for r in rows:
        print(f"ID: {r['id']}")
        print(f"  User: {r['user_id']}")
        print(f"  Kind: {r['kind']}")
        print(f"  Next Run: {r['next_run_at']}")
        print(f"  Cron: {r['cadence_cron']}")
        print(f"  Last Delivered: {r['last_delivered_at']}")
        print(f"  Payload: {r['payload']}")
        print()

conn.close()
