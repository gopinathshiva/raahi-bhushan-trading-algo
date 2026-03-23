#!/usr/bin/env python3
"""
migrate_to_supabase.py — One-time migration: SQLite → Supabase (PostgreSQL).

Usage:
    DATABASE_URL=postgresql://... python3 migrate_to_supabase.py
    DATABASE_URL=postgresql://... python3 migrate_to_supabase.py --dry-run

Requirements:
    - sensibull.db must exist in the same directory as this script.
    - DATABASE_URL must point to the Supabase transaction pooler (port 6543).
    - supabase_schema.sql must already have been run in the Supabase SQL Editor.

Notes:
    - raw_data columns (snapshots, latest_snapshots) are JSONB in Supabase.
      This script deserializes the TEXT JSON from SQLite and re-inserts as JSONB.
    - After each table, SERIAL sequences are reset to max(id)+1 to prevent
      collision with future auto-generated IDs.
    - Run with --dry-run first to verify row counts before any writes.
"""

import sqlite3
import json
import os
import sys
import time

from dotenv import load_dotenv
load_dotenv()

# Tables in FK-safe insertion order (parents before children)
# master_contract is excluded — it will be populated automatically when the app runs
TABLES_IN_ORDER = [
    'profiles',
    'admin_users',
    'openalgo_profiles',
    'subscriptions',
    'user_preferences',
    'snapshots',
    'latest_snapshots',
    'position_changes',
    'notifications',
    'ai_chat_history',
]

# Tables that use JSONB for raw_data (psycopg2.extras.Json wrapper needed)
JSONB_TABLE_COLUMNS = {
    'snapshots': ['raw_data'],
    'latest_snapshots': ['raw_data'],
}

# Tables whose PK is a SERIAL sequence (need setval after bulk insert)
# latest_snapshots uses profile_id as PK (not a sequence), master_contract uses
# instrument_token (not a sequence) — exclude both.
SERIAL_TABLES = {
    'profiles', 'admin_users', 'openalgo_profiles', 'subscriptions',
    'user_preferences', 'snapshots', 'position_changes', 'notifications',
    'ai_chat_history',
}

BATCH_SIZE = 100


def get_sqlite_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sensibull.db')


def get_pg_conn():
    import psycopg2
    import psycopg2.extras
    url = os.environ.get('DATABASE_URL')
    if not url:
        print("ERROR: DATABASE_URL environment variable is not set.")
        sys.exit(1)
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def migrate(dry_run=False):
    import psycopg2.extras

    sqlite_path = get_sqlite_path()
    if not os.path.exists(sqlite_path):
        print(f"ERROR: SQLite database not found at {sqlite_path}")
        sys.exit(1)

    print(f"Source: {sqlite_path}")
    print(f"Target: Supabase (DATABASE_URL)")
    if dry_run:
        print("Mode: DRY RUN (no writes)")
    else:
        print("Mode: LIVE (writing to Supabase)")
    print()

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    pg_conn = None if dry_run else get_pg_conn()

    start = time.time()
    total_inserted = 0

    for table in TABLES_IN_ORDER:
        rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
        sqlite_count = len(rows)

        if dry_run:
            print(f"  {table}: {sqlite_count} rows (dry run — skipping insert)")
            continue

        if sqlite_count == 0:
            print(f"  {table}: 0 rows — skipping")
            continue

        cols = list(rows[0].keys())
        placeholders = ', '.join(['%s'] * len(cols))
        col_names = ', '.join(cols)

        # Two SQL variants:
        # batch_sql  — used with execute_values (single %s for entire VALUES block)
        # insert_sql — used for per-row fallback (one %s per column)
        batch_sql = (
            f"INSERT INTO {table} ({col_names}) VALUES %s "
            f"ON CONFLICT DO NOTHING"
        )
        insert_sql = (
            f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
            f"ON CONFLICT DO NOTHING"
        )

        jsonb_cols = JSONB_TABLE_COLUMNS.get(table, [])

        def to_values(row):
            row_dict = dict(row)
            for col in jsonb_cols:
                if col in row_dict and row_dict[col] is not None:
                    try:
                        data = json.loads(row_dict[col]) if isinstance(row_dict[col], str) else row_dict[col]
                        row_dict[col] = psycopg2.extras.Json(data)
                    except (json.JSONDecodeError, TypeError):
                        pass
            return [row_dict[c] for c in cols]

        all_values = [to_values(r) for r in rows]

        inserted = 0
        skipped_fk = 0
        pg_c = pg_conn.cursor()

        # Fast path: batch insert BATCH_SIZE rows at a time
        # On FK violation, fall back to row-by-row for that batch only
        for i in range(0, len(all_values), BATCH_SIZE):
            batch = all_values[i:i + BATCH_SIZE]
            try:
                psycopg2.extras.execute_values(pg_c, batch_sql, batch, page_size=BATCH_SIZE)
                inserted += pg_c.rowcount
            except Exception as e:
                pg_conn.rollback()
                err = str(e).lower()
                if 'foreign key' not in err and 'fkey' not in err:
                    raise
                # FK violation in batch — retry row by row to isolate bad rows
                for values in batch:
                    try:
                        pg_c.execute(insert_sql, values)
                        if pg_c.rowcount == 1:
                            inserted += 1
                    except Exception as e2:
                        pg_conn.rollback()
                        err2 = str(e2).lower()
                        if 'foreign key' in err2 or 'fkey' in err2:
                            skipped_fk += 1
                        else:
                            raise

        pg_conn.commit()

        # Reset SERIAL sequence to avoid PK collisions on future inserts
        if table in SERIAL_TABLES:
            pg_c.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
            )
            pg_conn.commit()

        # Verify: count rows in Supabase
        pg_c.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
        pg_count = pg_c.fetchone()['cnt']

        fk_note = f", {skipped_fk} skipped (orphaned FK)" if skipped_fk else ""
        print(f"  {table}: {sqlite_count} SQLite rows → {inserted} inserted{fk_note} → {pg_count} in Supabase")
        total_inserted += inserted

    sqlite_conn.close()
    if pg_conn:
        pg_conn.close()

    elapsed = time.time() - start
    print()
    if dry_run:
        print(f"Dry run complete in {elapsed:.1f}s — no data written.")
    else:
        print(f"Migration complete in {elapsed:.1f}s — {total_inserted} rows inserted total.")


if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv
    migrate(dry_run=dry_run)
