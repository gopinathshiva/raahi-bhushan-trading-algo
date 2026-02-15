import requests
import sqlite3
import json
import time
import os
import traceback
from datetime import datetime, timedelta
from database import get_db, init_db

# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
URLS_FILE = os.path.join(BASE_DIR, 'urls.txt')
API_TEMPLATE = "https://oxide.sensibull.com/v1/compute/verified_by_sensibull/live_positions/snapshot/{slug}"

def load_profiles():
    if not os.path.exists(URLS_FILE):
        print(f"Error: {URLS_FILE} not found.")
        return []

    with open(URLS_FILE, 'r') as f:
        # Filter lines that are empty or start with #
        slugs = [line.strip().split('/')[-1] for line in f if line.strip() and not line.startswith('#')]
        # If full URL is given, extract slug, else assume slug
        clean_slugs = []
        for s in slugs:
            if 'sensibull.com' in s:
                 clean_slugs.append(s.split('/')[-1]) # rudimentary extraction, assumes standard URL ending in slug
            else:
                 clean_slugs.append(s)
        return clean_slugs

def fetch_data(slug):
    url = API_TEMPLATE.format(slug=slug)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get('success'):
            return data.get('payload', {}).get('position_snapshot_data', {})
        return None
    except Exception as e:
        print(f"Error fetching {slug}: {e}")
        return None

def save_snapshot(conn, profile_id, data, timestamp=None):
    c = conn.cursor()
    # Use provided timestamp (IST) or default to CURRENT_TIMESTAMP (UTC, avoided now)
    if timestamp:
         c.execute("INSERT INTO snapshots (profile_id, raw_data, created_at_source, timestamp) VALUES (?, ?, ?, ?)", 
                  (profile_id, json.dumps(data), data.get('created_at'), timestamp))
    else:
         c.execute("INSERT INTO snapshots (profile_id, raw_data, created_at_source) VALUES (?, ?, ?)", 
                  (profile_id, json.dumps(data), data.get('created_at')))
    return c.lastrowid

def normalize_trades(trades):
    # Sort trades to ensure consistent order for comparison (e.g. by symbol + product)
    if not trades:
        return []
    # Create a unique key for each trade and sort
    return sorted(trades, key=lambda x: (x.get('trading_symbol', ''), x.get('product', ''), x.get('quantity', 0)))

def get_normalized_trades(data):
    """Return a stable, comparable representation of the position structure.

    Important: This is used to decide whether we record a new `position_changes` row.
    It should only change when the *structure/quantity* changes (symbol/product/strike/type/qty),
    not when prices or P&L fluctuate.
    """
    positions = data.get('data', [])
    all_trades = []

    for p in positions:
        for t in p.get('trades', []):
            instrument_info = t.get('instrument_info') or {}
            trade_key = {
                'symbol': t.get('trading_symbol'),
                'product': t.get('product'),
                'strike': instrument_info.get('strike'),
                'option_type': instrument_info.get('instrument_type'),
                'quantity': t.get('quantity'),
            }
            all_trades.append(trade_key)

    # Sort by identity and qty to ensure list order doesn't affect comparison
    all_trades.sort(key=lambda x: (x['symbol'] or '', x['product'] or '', x['strike'] or 0, x['option_type'] or '', x['quantity'] or 0))
    return all_trades

DAYS_TO_KEEP_DATA = 30

def cleanup_old_data(conn):
    """Delete snapshots and changes older than DAYS_TO_KEEP_DATA"""
    cutoff_date = datetime.now() - timedelta(days=DAYS_TO_KEEP_DATA)
    c = conn.cursor()

    # SQLite datetime comparison works with strings if format is consistent (YYYY-MM-DD...)
    # We store timestamps as strings in SQLite usually
    print(f"Cleaning up data older than {cutoff_date.strftime('%Y-%m-%d %H:%M:%S')}...")

    # Delete from position_changes first
    c.execute("DELETE FROM position_changes WHERE timestamp < ?", (cutoff_date.isoformat(),))
    deleted_changes = c.rowcount

    # Delete from snapshots. Assuming created_at_source is stored as ISO format string.
    # If created_at_source is from API, it might be a different format.
    # For robustness, it's better to have a 'created_at' column in snapshots for our own tracking.
    # For now, let's assume created_at_source is comparable or we're using a different column.
    # If 'created_at_source' is the API's timestamp, it might not be directly comparable as a string.
    # Let's assume it's stored in a format that allows string comparison (e.g., ISO 8601).
    c.execute("DELETE FROM snapshots WHERE created_at_source < ?", (cutoff_date.isoformat(),))
    deleted_snapshots = c.rowcount

    conn.commit()
    if deleted_changes > 0 or deleted_snapshots > 0:
        print(f"Deleted {deleted_changes} old position changes and {deleted_snapshots} old snapshots.")
    else:
        print("No old data to clean up.")


def run_scraper():
    conn = get_db()
    c = conn.cursor()

    slugs = load_profiles()
    print(f"Tracking {len(slugs)} profiles: {slugs}")

    # Ensure profiles exist in DB
    for slug in slugs:
        c.execute("INSERT OR IGNORE INTO profiles (slug, name) VALUES (?, ?)", (slug, slug))
    conn.commit()

    # Run cleanup occasionally (simple way: check if hour is 09:15 approx, or just every run is fine given low volume?
    # Let's run it once per loop, it's cheap for SQLite.)
    cleanup_old_data(conn)

    for slug in slugs:
        print(f"Checking {slug}...")
        
        # Get Profile ID
        profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (slug,)).fetchone()
        if not profile:
            continue
        profile_id = profile[0] # Fetchone returns a tuple, not a dict
        
        # Check if we have any data (Initial Snapshot Check)
        last_snapshot = c.execute("SELECT raw_data FROM snapshots WHERE profile_id = ? ORDER BY created_at_source DESC LIMIT 1", (profile_id,)).fetchone()
        
        # DECISION: Should we run?
        # Run IF: (No data exists) OR (Market is Open)
        should_run = (last_snapshot is None) or is_market_open()
        
        if not should_run:
            print(f"Skipping {slug} (market closed and data exists)")
            continue

        print(f"Processing {slug}...")
        
        # Fetch current data
        try:
            current_data = fetch_data(slug)
            if not current_data:
                print(f"Skipping {slug}, no data returned from API.")
                continue
            
            # ALWAYS update the latest snapshot for Realtime P&L
            from database import upsert_latest_snapshot
            now_ist = datetime.now()
            upsert_latest_snapshot(conn, profile_id, current_data, timestamp=now_ist)
            
        except Exception as e:
            print(f"Error fetching {slug}: {e}")
            continue
            
        # If no last snapshot, save as initial
        if not last_snapshot:
            print(f"-> Initial snapshot for {slug}")
            snapshot_id = save_snapshot(conn, profile_id, current_data, timestamp=datetime.now())
            # Record explicit 'Initial' change so it shows up in UI
            c.execute("INSERT INTO position_changes (profile_id, snapshot_id, timestamp, diff_summary) VALUES (?, ?, ?, ?)",
                      (profile_id, snapshot_id, datetime.now(), "Initial Snapshot"))
            conn.commit()
            continue
            
        # Compare with last snapshot
        last_data = json.loads(last_snapshot[0]) # Fetchone returns a tuple
        diff = get_normalized_trades(current_data) != get_normalized_trades(last_data)
        
        if diff:
            print(f"-> CHANGE DETECTED for {slug}")
            snapshot_id = save_snapshot(conn, profile_id, current_data, timestamp=datetime.now())
            
            # Simple diff summary (placeholder, ideally we list added/removed symbols)
            summary = generate_diff_summary(last_data, current_data)
            
            c.execute("INSERT INTO position_changes (profile_id, snapshot_id, timestamp, diff_summary) VALUES (?, ?, ?, ?)",
                      (profile_id, snapshot_id, datetime.now(), summary))
            conn.commit()
        else:
            print(f"-> No change for {slug}")

    conn.close()

def _positions_key(trade):
    """Identity of a position leg for summary purposes."""
    return (
        trade.get('symbol'),
        trade.get('product'),
        trade.get('strike'),
        trade.get('option_type'),
    )


def generate_diff_summary(old_data, new_data):
    """Generate a user-facing summary for the profile timeline.

    IMPORTANT: This must match the conceptual model used by the UI "Recent Changes"
    popup (Added / Removed / Modified). The old implementation inferred "Added" and
    "Reduced" purely from list length, which breaks when one leg is removed and a
    different leg is added in the same snapshot (net length unchanged).

    We define a "position leg" identity by `_positions_key()` (symbol+product+strike+type)
    and detect:
      - Added: present in new, not in old
      - Removed: present in old, not in new
      - Modified: present in both but quantity changed

    The summary string is intentionally compact for single-type events, and more
    explicit when multiple types occur together.
    """
    old_trades = get_normalized_trades(old_data)
    new_trades = get_normalized_trades(new_data)

    old_map = {_positions_key(t): (t.get('quantity') or 0) for t in old_trades}
    new_map = {_positions_key(t): (t.get('quantity') or 0) for t in new_trades}

    old_keys = set(old_map.keys())
    new_keys = set(new_map.keys())

    added_count = len(new_keys - old_keys)
    removed_count = len(old_keys - new_keys)

    modified_count = 0
    for k in (old_keys & new_keys):
        if old_map.get(k, 0) != new_map.get(k, 0):
            modified_count += 1

    # Prefer the legacy compact labels when only one type is present
    if added_count and not removed_count and not modified_count:
        return f"Positions Added ({added_count})"
    if removed_count and not added_count and not modified_count:
        return f"Positions Reduced ({removed_count})"
    if modified_count and not added_count and not removed_count:
        return f"Positions Modified ({modified_count})"

    # Otherwise be explicit to avoid misleading titles.
    return (
        f"Positions Changed (Added: {added_count}, Removed: {removed_count}, Modified: {modified_count})"
    )

def is_market_open():
    now = datetime.now()
    # Weekday check: 0=Monday, 4=Friday, 5=Saturday, 6=Sunday
    if now.weekday() > 4:
        return False
    
    # Time check: 09:15 to 15:30
    current_time = now.time()
    start_time = datetime.strptime("09:15", "%H:%M").time()
    end_time = datetime.strptime("15:30", "%H:%M").time()
    
    return start_time <= current_time <= end_time

if __name__ == '__main__':
    # Initialize DB if not exists
    init_db()
    print("Starting scraper service (Ctrl+C to stop)...")
    print("Market Hours: Mon-Fri, 09:15 - 15:30 (updates only)")
    print("New profiles will be fetched immediately.")
    
    while True:
        try:
            print(f"\n--- Run at {datetime.now()} ---")
            run_scraper()
        except Exception as e:
            print(f"Fatal error: {e}")
            traceback.print_exc()
        
        # If market is closed, we sleep longer? Or still 60s to catch new profiles added to text file?
        # Let's stick to 60s for responsiveness.
        time.sleep(60)
