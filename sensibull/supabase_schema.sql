-- supabase_schema.sql
-- Full PostgreSQL DDL for Sensibull Tracker.
-- Run this in the Supabase SQL Editor (Dashboard → SQL Editor → New query).
-- All timestamps are stored as TEXT (ISO 8601 with IST offset, e.g. "2026-02-16T10:30:00+05:30").
-- JSONB is used for raw_data columns (snapshots, latest_snapshots) for efficient storage.
-- Other JSON-like columns (notification_data, position_identifier, diff_summary) remain TEXT.

-- ─── profiles ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS profiles (
    id           SERIAL PRIMARY KEY,
    slug         TEXT UNIQUE NOT NULL,
    name         TEXT,
    url          TEXT,
    source_url   TEXT,
    is_active    INTEGER DEFAULT 1,
    added_at     TEXT DEFAULT NULL
);

-- ─── snapshots ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS snapshots (
    id                 SERIAL PRIMARY KEY,
    profile_id         INTEGER NOT NULL REFERENCES profiles(id),
    timestamp          TEXT DEFAULT NULL,
    raw_data           JSONB NOT NULL,
    created_at_source  TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_profile_ts
    ON snapshots (profile_id, timestamp);

-- ─── position_changes ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS position_changes (
    id           SERIAL PRIMARY KEY,
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
    profile_id   INTEGER NOT NULL REFERENCES profiles(id),
    timestamp    TEXT DEFAULT NULL,
    diff_summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_changes_profile_ts
    ON position_changes (profile_id, timestamp);

-- ─── latest_snapshots ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS latest_snapshots (
    profile_id  INTEGER PRIMARY KEY REFERENCES profiles(id),
    raw_data    JSONB NOT NULL,
    timestamp   TEXT DEFAULT NULL
);

-- ─── master_contract ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS master_contract (
    instrument_token  INTEGER PRIMARY KEY,
    trading_symbol    TEXT NOT NULL,
    exchange          TEXT,
    name              TEXT,
    expiry            TEXT,
    strike            REAL,
    lot_size          INTEGER,
    instrument_type   TEXT,
    last_updated      TEXT DEFAULT NULL
);

-- ─── subscriptions ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscriptions (
    id                   SERIAL PRIMARY KEY,
    profile_id           INTEGER NOT NULL REFERENCES profiles(id),
    subscription_type    TEXT NOT NULL,
    underlying           TEXT,
    expiry               TEXT,
    position_identifier  TEXT,
    created_at           TEXT DEFAULT NULL,
    UNIQUE(profile_id, subscription_type, underlying, expiry, position_identifier)
);

-- ─── notifications ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id                 SERIAL PRIMARY KEY,
    profile_id         INTEGER NOT NULL REFERENCES profiles(id),
    subscription_id    INTEGER REFERENCES subscriptions(id),
    message            TEXT NOT NULL,
    notification_type  TEXT NOT NULL,
    notification_data  TEXT,
    created_at         TEXT DEFAULT NULL,
    is_read            INTEGER DEFAULT 0
);

-- ─── user_preferences ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_preferences (
    id                  SERIAL PRIMARY KEY,
    profile_id          INTEGER NOT NULL REFERENCES profiles(id),
    notification_sound  TEXT DEFAULT 'default',
    created_at          TEXT DEFAULT NULL,
    updated_at          TEXT DEFAULT NULL,
    UNIQUE(profile_id)
);

-- ─── admin_users ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admin_users (
    id             SERIAL PRIMARY KEY,
    username       TEXT UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL,
    email          TEXT,
    is_active      INTEGER DEFAULT 1,
    created_at     TEXT DEFAULT NULL,
    last_login     TEXT
);

-- ─── openalgo_profiles ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS openalgo_profiles (
    id            SERIAL PRIMARY KEY,
    profile_name  TEXT UNIQUE NOT NULL,
    host          TEXT NOT NULL,
    api_key       TEXT NOT NULL,
    is_active     INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT NULL,
    updated_at    TEXT DEFAULT NULL
);

-- ─── ai_chat_history ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_chat_history (
    id          SERIAL PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles(id),
    scope_type  TEXT NOT NULL,
    underlying  TEXT NOT NULL,
    expiry_key  TEXT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    model       TEXT,
    created_at  TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_chat_scope
    ON ai_chat_history (profile_id, scope_type, underlying, expiry_key);
