#!/usr/bin/env python3
"""
Database migration script to add missing columns to existing tables.
This ensures the database schema is up-to-date without losing existing data.
"""

import sqlite3
import os
from database import DB_PATH, get_db

def column_exists(cursor, table_name, column_name):
    """Check if a column exists in a table"""
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    return column_name in columns

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
        # SQLite doesn't support CURRENT_TIMESTAMP in ALTER TABLE, use NULL default
        c.execute("ALTER TABLE profiles ADD COLUMN added_at DATETIME")
        migrations_applied += 1
        print("    ✓ Added 'added_at' column")
    
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
