#!/usr/bin/env python3
"""
Database migration script to add missing columns to existing tables.
This ensures the database schema is up-to-date without losing existing data.
Supports both SQLite (default) and PostgreSQL/Supabase backends.
"""

import sqlite3
import os
from database import DB_PATH, get_db
from db_adapter import BACKEND


def column_exists(cursor, table_name, column_name):
    """Check if a column exists in a table (portable: SQLite and PostgreSQL)."""
    if BACKEND == 'supabase':
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
        """, (table_name, column_name))
        return cursor.fetchone() is not None
    else:
        cursor.execute(f"PRAGMA table_info({table_name})")
        return any(row[1] == column_name for row in cursor.fetchall())


def table_exists(cursor, table_name):
    """Check if a table exists (portable: SQLite and PostgreSQL)."""
    if BACKEND == 'supabase':
        cursor.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
        """, (table_name,))
        return cursor.fetchone() is not None
    else:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        return cursor.fetchone() is not None


def migrate_database():
    """Run all database migrations"""
    print("Running database migrations...")

    conn = get_db()
    c = conn.cursor()

    migrations_applied = 0

    # Migration 1: Add is_active column to profiles table
    if not column_exists(c, 'profiles', 'is_active'):
        print("  - Adding 'is_active' column to 'profiles' table...")
        c.execute("ALTER TABLE profiles ADD COLUMN is_active INTEGER DEFAULT 1")
        migrations_applied += 1
        print("    ✓ Added 'is_active' column")

    # Migration 2: Add source_url column to profiles table if missing
    if not column_exists(c, 'profiles', 'source_url'):
        print("  - Adding 'source_url' column to 'profiles' table...")
        c.execute("ALTER TABLE profiles ADD COLUMN source_url TEXT")
        # Populate source_url from url column if it exists
        c.execute("UPDATE profiles SET source_url = url WHERE source_url IS NULL")
        migrations_applied += 1
        print("    ✓ Added 'source_url' column")

    # Migration 3: Add added_at column to profiles table if missing
    if not column_exists(c, 'profiles', 'added_at'):
        print("  - Adding 'added_at' column to 'profiles' table...")
        c.execute("ALTER TABLE profiles ADD COLUMN added_at TEXT")
        migrations_applied += 1
        print("    ✓ Added 'added_at' column")

    # Migration 4: Create ai_chat_history table if missing
    if not table_exists(c, 'ai_chat_history'):
        print("  - Creating 'ai_chat_history' table...")
        if BACKEND == 'supabase':
            c.execute('''
                CREATE TABLE IF NOT EXISTS ai_chat_history (
                    id SERIAL PRIMARY KEY,
                    profile_id INTEGER NOT NULL,
                    scope_type TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    expiry_key TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    model TEXT,
                    created_at TEXT DEFAULT NULL,
                    FOREIGN KEY (profile_id) REFERENCES profiles (id)
                )
            ''')
        else:
            c.execute('''
                CREATE TABLE IF NOT EXISTS ai_chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    scope_type TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    expiry_key TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    model TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (profile_id) REFERENCES profiles (id)
                )
            ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_ai_chat_scope ON ai_chat_history (profile_id, scope_type, underlying, expiry_key)')
        migrations_applied += 1
        print("    ✓ Created 'ai_chat_history' table")

    # Migration 7: Add ws_url column to openalgo_profiles
    if table_exists(c, 'openalgo_profiles') and not column_exists(c, 'openalgo_profiles', 'ws_url'):
        print("  - Adding 'ws_url' column to 'openalgo_profiles' table...")
        c.execute("ALTER TABLE openalgo_profiles ADD COLUMN ws_url TEXT DEFAULT ''")
        migrations_applied += 1
        print("    ✓ Added 'ws_url' column")

    # Migration 8: Add is_ws_default column to openalgo_profiles
    if table_exists(c, 'openalgo_profiles') and not column_exists(c, 'openalgo_profiles', 'is_ws_default'):
        print("  - Adding 'is_ws_default' column to 'openalgo_profiles' table...")
        c.execute("ALTER TABLE openalgo_profiles ADD COLUMN is_ws_default INTEGER DEFAULT 0")
        migrations_applied += 1
        print("    ✓ Added 'is_ws_default' column")

    # Migration 5: Create openalgo_profiles table if missing
    if not table_exists(c, 'openalgo_profiles'):
        print("  - Creating 'openalgo_profiles' table...")
        if BACKEND == 'supabase':
            c.execute('''
                CREATE TABLE IF NOT EXISTS openalgo_profiles (
                    id SERIAL PRIMARY KEY,
                    profile_name TEXT UNIQUE NOT NULL,
                    host TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            ''')
        else:
            c.execute('''
                CREATE TABLE IF NOT EXISTS openalgo_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_name TEXT UNIQUE NOT NULL,
                    host TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        migrations_applied += 1
        print("    ✓ Created 'openalgo_profiles' table")

    # Migration 6: Fix position_changes timestamps stored as UTC (+00) → convert to IST (+05:30)
    # Root cause: during the Supabase migration window, psycopg2 normalized now_ist() datetime
    # objects to UTC instead of preserving IST. This caused ORDER BY timestamp to sort those
    # entries as earlier than IST entries from the same day, hiding them in the daily timeline.
    if BACKEND != 'supabase':
        from datetime import timezone, timedelta
        from zoneinfo import ZoneInfo

        IST_ZONE = ZoneInfo("Asia/Kolkata")
        UTC_ZONE = timezone.utc

        # Fetch rows where timestamp was stored in UTC (contains '+00' offset)
        utc_rows = c.execute(
            "SELECT id, timestamp FROM position_changes WHERE timestamp LIKE '%+00%' OR timestamp LIKE '%+00:00%'"
        ).fetchall()

        if utc_rows:
            print(f"  - Migration 6: Converting {len(utc_rows)} UTC timestamp(s) in position_changes to IST...")
            import re as _re
            _TZ_RE = _re.compile(r'([+-])(\d{2}):(\d{2})$')

            for row in utc_rows:
                raw_ts = row[1] if isinstance(row, (list, tuple)) else row['timestamp']
                row_id = row[0] if isinstance(row, (list, tuple)) else row['id']
                try:
                    # Normalize format
                    s = raw_ts.replace(' ', 'T')
                    m = _TZ_RE.search(s)
                    if m:
                        sign = 1 if m.group(1) == '+' else -1
                        offset = timedelta(hours=sign * int(m.group(2)), minutes=sign * int(m.group(3)))
                        s_naive = s[:m.start()]
                        dt_naive = __import__('datetime').datetime.fromisoformat(s_naive)
                        dt_aware = dt_naive.replace(tzinfo=timezone(offset))
                    else:
                        dt_aware = __import__('datetime').datetime.fromisoformat(s).replace(tzinfo=UTC_ZONE)

                    dt_ist = dt_aware.astimezone(IST_ZONE)
                    c.execute("UPDATE position_changes SET timestamp = ? WHERE id = ?",
                              (dt_ist.isoformat(), row_id))
                except Exception as e:
                    print(f"    Warning: Could not convert timestamp '{raw_ts}' for id={row_id}: {e}")

            migrations_applied += 1
            print(f"    ✓ Converted {len(utc_rows)} UTC timestamp(s) to IST")
        else:
            print("  - Migration 6: No UTC timestamps found in position_changes (already clean)")

    # Commit all changes
    conn.commit()
    conn.close()

    if migrations_applied > 0:
        print(f"\n✓ Applied {migrations_applied} migration(s) successfully!")
    else:
        print("✓ Database schema is up-to-date. No migrations needed.")

    return migrations_applied


if __name__ == '__main__':
    migrate_database()
