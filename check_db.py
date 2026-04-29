import sqlite3

conn = sqlite3.connect('wellness_data/wellness.db')
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables in wellness.db:')
for t in tables:
    print(f'  {t[0]}')
