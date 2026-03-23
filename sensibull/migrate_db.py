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
                    created_at TEXT DEFAULT '',
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
