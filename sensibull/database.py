import sqlite3
import json
from datetime import datetime
from zoneinfo import ZoneInfo
import os

# IST Timezone constant
IST = ZoneInfo("Asia/Kolkata")

def now_ist():
    """Get current datetime in IST timezone"""
    return datetime.now(IST)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sensibull.db')

def get_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    
    # Table to store unique profiles
    c.execute('''
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT,
            url TEXT,
            source_url TEXT,
            is_active INTEGER DEFAULT 1,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Table to store every raw snapshot (optional, can be large, maybe we only store changes?)
    # For now, let's store only if something changed, but we need the "latest" state to compare.
    # Actually, let's store every scheduled fetch's status or just the changes.
    # User wants: "Every cell has the number of times the person have taken a trade in that day... clickable link... table which has time and trade data... change with respect to previous time"
    # To support this, we need to store the full snapshot whenever it changes, so we can diff it against the previous one on demand, 
    # OR store the diffs directly. Storing full snapshots is safer and easier to rebuild.
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            raw_data JSON NOT NULL,
            created_at_source TEXT,
            FOREIGN KEY (profile_id) REFERENCES profiles (id)
        )
    ''')
    
    # Table to record that a change was detected (for easy indexing)
    c.execute('''
        CREATE TABLE IF NOT EXISTS position_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            profile_id INTEGER NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            diff_summary TEXT, -- JSON summary of what changed (e.g. "Added 1 trade, Removed 1 trade")
            FOREIGN KEY (snapshot_id) REFERENCES snapshots (id),
            FOREIGN KEY (profile_id) REFERENCES profiles (id)
        )
    ''')
    
    # Table to store strict latest state for Realtime P&L (updates on every fetch)
    c.execute('''
        CREATE TABLE IF NOT EXISTS latest_snapshots (
            profile_id INTEGER PRIMARY KEY,
            raw_data JSON NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Table to store Zerodha master contract data
    c.execute('''
        CREATE TABLE IF NOT EXISTS master_contract (
            instrument_token INTEGER PRIMARY KEY,
            trading_symbol TEXT NOT NULL,
            exchange TEXT,
            name TEXT,
            expiry TEXT,
            strike REAL,
            lot_size INTEGER,
            instrument_type TEXT,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Table to store user subscriptions per profile
    c.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            subscription_type TEXT NOT NULL, -- 'underlying', 'expiry', or 'position'
            underlying TEXT,
            expiry TEXT,
            position_identifier TEXT, -- JSON with symbol, product, strike, option_type
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles (id),
            UNIQUE(profile_id, subscription_type, underlying, expiry, position_identifier)
        )
    ''')
    
    # Table to store notifications
    c.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            subscription_id INTEGER,
            message TEXT NOT NULL,
            notification_type TEXT NOT NULL, -- 'new_position', 'modified_position', 'exited_position', 'quantity_change'
            notification_data TEXT, -- JSON with detailed change information
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_read INTEGER DEFAULT 0, -- 0 = unread, 1 = read
            FOREIGN KEY (profile_id) REFERENCES profiles (id),
            FOREIGN KEY (subscription_id) REFERENCES subscriptions (id)
        )
    ''')
    
    # Table to store user preferences
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            notification_sound TEXT DEFAULT 'default', -- Sound file name: 'default', 'chime', 'beep', 'bell', 'none'
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles (id),
            UNIQUE(profile_id)
        )
    ''')
    
    # Table to store admin users for authentication
    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            email TEXT,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_login DATETIME
        )
    ''')
    
    conn.commit()
    conn.close()
    print("Database initialized.")
    
    # Run migrations to add missing columns to existing tables
    try:
        from migrate_db import migrate_database
        migrate_database()
    except ImportError:
        # migrate_db.py might not exist yet in some deployments
        print("Warning: migrate_db.py not found, skipping migrations.")

def upsert_latest_snapshot(conn, profile_id, data, timestamp=None):
    c = conn.cursor()
    # SQLite upsert syntax
    # timestamps are stored as ISO format strings with timezone (e.g., "2026-02-16T10:30:00+05:30")
    # This explicitly shows that all times are in IST
    ts_val = timestamp.isoformat() if timestamp else now_ist().isoformat()
    
    c.execute("""
        INSERT INTO latest_snapshots (profile_id, raw_data, timestamp) 
        VALUES (?, ?, ?)
        ON CONFLICT(profile_id) DO UPDATE SET
            raw_data=excluded.raw_data,
            timestamp=excluded.timestamp
    """, (profile_id, json.dumps(data), ts_val))
    conn.commit()

def sync_profiles():
    # Helper to load profiles from file and ensure they exist in DB
    URLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'urls.txt')
    if not os.path.exists(URLS_FILE):
        return

    with open(URLS_FILE, 'r') as f:
        # Filter lines that are empty or start with #
        slugs = [line.strip().split('/')[-1] for line in f if line.strip() and not line.startswith('#')]
        # If full URL is given, extract slug, else assume slug
        clean_slugs = []
        for s in slugs:
            if 'sensibull.com' in s:
                 clean_slugs.append(s.split('/')[-1])
            else:
                 clean_slugs.append(s)
    
    conn = get_db()
    c = conn.cursor()
    
    # Add new profiles
    for slug in clean_slugs:
        c.execute("INSERT OR IGNORE INTO profiles (slug, name) VALUES (?, ?)", (slug, slug))
    
    # Remove profiles that are no longer in urls.txt
    if clean_slugs:
        placeholders = ','.join('?' * len(clean_slugs))
        c.execute(f"DELETE FROM profiles WHERE slug NOT IN ({placeholders})", clean_slugs)
        deleted = c.rowcount
        if deleted > 0:
            print(f"Removed {deleted} profiles no longer in urls.txt")
    
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()
