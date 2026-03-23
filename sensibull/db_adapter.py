"""
db_adapter.py — Storage backend abstraction layer.

Controls whether the app talks to SQLite (default) or Supabase (PostgreSQL).
Set DB_BACKEND=supabase in the environment to switch to Supabase mode.

All SQL placeholders should use '?' (SQLite style); this module translates
them to '%s' at execute() time when running in Supabase mode. The
AdaptedConnection / AdaptedCursor wrappers do this transparently, so
existing c.execute(sql, params) calls require no changes.
"""

import os
import sqlite3

BACKEND = os.environ.get('DB_BACKEND', 'sqlite').lower()

# Validate early so errors surface at startup, not mid-request.
if BACKEND == 'supabase' and not os.environ.get('DATABASE_URL'):
    raise RuntimeError(
        "DB_BACKEND=supabase requires DATABASE_URL to be set. "
        "Get the transaction pooler URL from Supabase → Settings → Database."
    )

# ─── SQL dialect translation ──────────────────────────────────────────────────

def adapt_sql(sql: str) -> str:
    """
    Translate SQLite-style SQL to the active backend dialect.
    Only performs replacements when BACKEND == 'supabase'.

    Handles:
      - '?' parameter placeholders → '%s'
      - date(timestamp) → SUBSTR(timestamp, 1, 10)
      - date(created_at) → SUBSTR(created_at, 1, 10)
    Note: INSERT OR IGNORE / INSERT OR REPLACE are handled at the
    individual query level (they need ON CONFLICT column names).
    """
    if BACKEND != 'supabase':
        return sql
    sql = sql.replace('?', '%s')
    sql = sql.replace('date(timestamp)', "SUBSTR(timestamp, 1, 10)")
    sql = sql.replace('date(created_at)', "SUBSTR(created_at, 1, 10)")
    return sql


# ─── Cursor wrapper ───────────────────────────────────────────────────────────

class AdaptedCursor:
    """
    Thin cursor wrapper that applies adapt_sql() to every execute() call.
    This means existing code never needs to call adapt_sql() manually.
    Delegates all other methods/properties to the real underlying cursor.
    """

    def __init__(self, real_cursor):
        self._c = real_cursor

    def execute(self, sql, params=None):
        adapted = adapt_sql(sql)
        if params is not None:
            self._c.execute(adapted, params)
        else:
            self._c.execute(adapted)
        return self  # always return self so c.execute(...).fetchone() chains correctly

    def executemany(self, sql, params_list):
        adapted = adapt_sql(sql)
        self._c.executemany(adapted, params_list)
        return self

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    def fetchmany(self, size=None):
        return self._c.fetchmany(size) if size is not None else self._c.fetchmany()

    @property
    def lastrowid(self):
        return self._c.lastrowid

    @property
    def rowcount(self):
        return self._c.rowcount

    @property
    def description(self):
        return self._c.description

    def close(self):
        return self._c.close()

    def __iter__(self):
        return iter(self._c)

    def __next__(self):
        return next(self._c)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ─── Connection wrapper ───────────────────────────────────────────────────────

class AdaptedConnection:
    """
    Thin connection wrapper that returns AdaptedCursor from cursor().
    Also wraps conn.execute() for callers that execute directly on the
    connection object (used in a few places in app.py).
    """

    def __init__(self, real_conn):
        self._conn = real_conn

    def cursor(self):
        return AdaptedCursor(self._conn.cursor())

    def execute(self, sql, params=None):
        # psycopg2 connections have no .execute() — always go via cursor()
        c = AdaptedCursor(self._conn.cursor())
        c.execute(sql, params)  # adapt_sql applied inside AdaptedCursor.execute
        return c

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()
        return False


# ─── Connection factory ───────────────────────────────────────────────────────

def get_connection() -> AdaptedConnection:
    """
    Return a backend-appropriate database connection wrapped in AdaptedConnection.

    SQLite mode:  reads DB_PATH from database.py (avoiding circular imports by
                  importing inline).
    Supabase mode: connects via psycopg2 to DATABASE_URL using RealDictCursor
                   so row access by column name (row['col']) works identically
                   to sqlite3.Row.
    """
    if BACKEND == 'supabase':
        import psycopg2
        import psycopg2.extras
        real_conn = psycopg2.connect(
            os.environ['DATABASE_URL'],
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        real_conn.autocommit = False
        return AdaptedConnection(real_conn)
    else:
        # Inline import to avoid circular dependency (database imports db_adapter)
        from database import DB_PATH
        real_conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        real_conn.row_factory = sqlite3.Row
        return AdaptedConnection(real_conn)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def last_insert_id(cursor) -> int:
    """
    Return the ID of the last inserted row in a portable way.

    SQLite:    cursor.lastrowid works after a plain INSERT.
    Supabase:  requires RETURNING id in the INSERT statement;
               call cursor.fetchone()[0] (or ['id']) after the execute.
               This helper reads from fetchone() for the Supabase path.
    """
    if BACKEND == 'supabase':
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("last_insert_id: no row returned — did you add RETURNING id to the INSERT?")
        # RealDictCursor returns a dict-like; support both dict and tuple access
        try:
            return row['id']
        except (KeyError, TypeError):
            return row[0]
    return cursor.lastrowid


# Expose BACKEND for other modules that need to branch on it.
__all__ = ['BACKEND', 'adapt_sql', 'get_connection', 'last_insert_id',
           'AdaptedConnection', 'AdaptedCursor']
