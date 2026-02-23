import requests
import sqlite3
import json
import time
import os
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database import get_db, init_db

# IST Timezone constant
IST = ZoneInfo("Asia/Kolkata")

def now_ist():
    """Get current datetime in IST timezone"""
    return datetime.now(IST)

# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
URLS_FILE = os.path.join(BASE_DIR, 'urls.txt')
API_TEMPLATE = "https://oxide.sensibull.com/v1/compute/verified_by_sensibull/live_positions/snapshot/{slug}"

def load_profiles():
    """Load active profiles from database instead of urls.txt"""
    conn = get_db()
    c = conn.cursor()
    
    # Get all active profiles (is_active = 1)
    profiles = c.execute("""
        SELECT slug FROM profiles 
        WHERE is_active = 1
        ORDER BY slug
    """).fetchall()
    
    conn.close()
    
    slugs = [p['slug'] for p in profiles]
    return slugs

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
    # Store timestamps in ISO format with timezone (e.g., "2026-02-16T10:30:00+05:30")
    # This makes it explicit that all times are in IST
    if timestamp:
         # Convert datetime to ISO format string with timezone
         ts_str = timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp)
         c.execute("INSERT INTO snapshots (profile_id, raw_data, created_at_source, timestamp) VALUES (?, ?, ?, ?)", 
                  (profile_id, json.dumps(data), data.get('created_at'), ts_str))
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
    cutoff_date = now_ist() - timedelta(days=DAYS_TO_KEEP_DATA)
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


def cleanup_old_notifications(conn):
    """Delete notifications older than 30 days"""
    cutoff_date = now_ist() - timedelta(days=30)
    c = conn.cursor()
    
    c.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff_date.isoformat(),))
    deleted_notifications = c.rowcount
    
    conn.commit()
    if deleted_notifications > 0:
        print(f"Deleted {deleted_notifications} old notifications (>30 days).")

def extract_underlying_from_symbol(trading_symbol):
    """Extract underlying symbol from trading symbol (e.g., 'NIFTY26FEB25000PE' -> 'NIFTY')"""
    if not trading_symbol:
        return None
    
    # Common patterns:
    # NIFTY26FEB25000PE, RELIANCE26FEB1200PE, TCS26MAR2440PE, IEX26FEB114PE
    # The underlying is the alphabetic prefix before the date/strike portion
    
    # Find where the first digit appears
    for i, char in enumerate(trading_symbol):
        if char.isdigit():
            return trading_symbol[:i]
    
    # If no digits found, return the whole symbol
    return trading_symbol

def _compute_implied_fill(old_qty, new_qty, old_avg_price, new_avg_price):
    """Compute implied fill side, quantity, and price from a quantity change."""
    dq = new_qty - old_qty
    if dq == 0:
        return '', 0, 0
    side = 'BUY' if dq > 0 else 'SELL'
    fill_qty = abs(dq)
    try:
        if old_avg_price and new_avg_price:
            v0 = old_avg_price * old_qty
            v1 = new_avg_price * new_qty
            price = abs((v1 - v0) / dq)
        else:
            price = 0
    except Exception:
        price = 0
    return side, fill_qty, price

def generate_notifications_for_changes(conn, profile_id, old_data, new_data):
    """Generate notifications based on subscriptions and detected changes"""
    c = conn.cursor()
    
    # Get all subscriptions for this profile
    subscriptions = c.execute("""
        SELECT id, subscription_type, underlying, expiry, position_identifier
        FROM subscriptions
        WHERE profile_id = ?
    """, (profile_id,)).fetchall()
    
    if not subscriptions:
        return  # No subscriptions, nothing to do
    
    # Parse old and new positions into comparable structures
    old_positions = {}  # key: (underlying, expiry, symbol, product, strike, option_type) -> trade data
    new_positions = {}
    
    # Build old positions map
    for pos in old_data.get('data', []):
        # Use trading_symbol from position level (underlying symbol like "NIFTY", "TCS")
        # Or extract it from the trade's trading_symbol
        position_symbol = pos.get('trading_symbol')
        underlying = pos.get('underlying') or position_symbol
        for trade in pos.get('trades', []):
            inst = trade.get('instrument_info') or {}
            # If underlying is still None, extract from trading_symbol
            trade_underlying = underlying or extract_underlying_from_symbol(trade.get('trading_symbol'))
            # Use expiry from instrument_info (not from position level which is None)
            trade_expiry = inst.get('expiry')
            key = (
                trade_underlying,
                trade_expiry,
                trade.get('trading_symbol'),
                trade.get('product'),
                inst.get('strike'),
                inst.get('instrument_type')
            )
            old_positions[key] = trade
    
    # Build new positions map
    for pos in new_data.get('data', []):
        # Use trading_symbol from position level (underlying symbol like "NIFTY", "TCS")
        # Or extract it from the trade's trading_symbol
        position_symbol = pos.get('trading_symbol')
        underlying = pos.get('underlying') or position_symbol
        for trade in pos.get('trades', []):
            inst = trade.get('instrument_info') or {}
            # If underlying is still None, extract from trading_symbol
            trade_underlying = underlying or extract_underlying_from_symbol(trade.get('trading_symbol'))
            # Use expiry from instrument_info (not from position level which is None)
            trade_expiry = inst.get('expiry')
            key = (
                trade_underlying,
                trade_expiry,
                trade.get('trading_symbol'),
                trade.get('product'),
                inst.get('strike'),
                inst.get('instrument_type')
            )
            new_positions[key] = trade
    
    # Detect changes
    old_keys = set(old_positions.keys())
    new_keys = set(new_positions.keys())
    
    added_positions = new_keys - old_keys
    removed_positions = old_keys - new_keys
    existing_positions = old_keys & new_keys
    
    # Process each subscription
    for sub in subscriptions:
        sub_id = sub['id']
        sub_type = sub['subscription_type']
        sub_underlying = sub['underlying']
        sub_expiry = sub['expiry']
        sub_pos_id = json.loads(sub['position_identifier']) if sub['position_identifier'] else None
        
        # Check for relevant changes based on subscription type
        if sub_type == 'underlying':
            # Notify for any position changes in this underlying
            for key in added_positions:
                if key[0] == sub_underlying:  # key[0] is underlying
                    trade = new_positions[key]
                    notification_data = {
                        'underlying': key[0],
                        'expiry': key[1],
                        'symbol': key[2],
                        'product': key[3],
                        'quantity': trade.get('quantity', 0),
                        'average_price': trade.get('average_price', 0),
                        'last_price': trade.get('last_price', 0),
                        'unbooked_pnl': trade.get('unbooked_pnl', 0)
                    }
                    create_notification(conn, profile_id, sub_id, 'new_position', 
                                      f"New position added in {sub_underlying}", 
                                      notification_data)
            
            for key in removed_positions:
                if key[0] == sub_underlying:
                    trade = old_positions[key]
                    notification_data = {
                        'underlying': key[0],
                        'expiry': key[1],
                        'symbol': key[2],
                        'product': key[3],
                        'quantity': trade.get('quantity', 0),
                        'average_price': trade.get('average_price', 0),
                        'exit_pnl': trade.get('booked_pnl', 0)
                    }
                    create_notification(conn, profile_id, sub_id, 'exited_position',
                                      f"Position exited in {sub_underlying}",
                                      notification_data)
            
            for key in existing_positions:
                if key[0] == sub_underlying:
                    old_trade = old_positions[key]
                    new_trade = new_positions[key]
                    old_qty = old_trade.get('quantity', 0)
                    new_qty = new_trade.get('quantity', 0)
                    if old_qty != new_qty:
                        qty_diff = new_qty - old_qty
                        _if_side, _if_qty, _if_price = _compute_implied_fill(
                            old_qty, new_qty,
                            old_trade.get('average_price', 0),
                            new_trade.get('average_price', 0)
                        )
                        notification_data = {
                            'underlying': key[0],
                            'expiry': key[1],
                            'symbol': key[2],
                            'product': key[3],
                            'old_quantity': old_qty,
                            'quantity': new_qty,
                            'quantity_diff': qty_diff,
                            'old_average_price': old_trade.get('average_price', 0),
                            'average_price': new_trade.get('average_price', 0),
                            'last_price': new_trade.get('last_price', 0),
                            'unbooked_pnl': new_trade.get('unbooked_pnl', 0),
                            'booked_pnl': new_trade.get('booked_pnl', 0),
                            'implied_fill_side': _if_side,
                            'implied_fill_qty': _if_qty,
                            'implied_fill_price': _if_price
                        }
                        create_notification(conn, profile_id, sub_id, 'modified_position',
                                          f"Position modified in {sub_underlying}",
                                          notification_data)
        
        elif sub_type == 'expiry':
            # Notify for any position changes in this underlying + expiry combination
            for key in added_positions:
                if key[0] == sub_underlying and key[1] == sub_expiry:
                    trade = new_positions[key]
                    notification_data = {
                        'underlying': key[0],
                        'expiry': key[1],
                        'symbol': key[2],
                        'product': key[3],
                        'quantity': trade.get('quantity', 0),
                        'average_price': trade.get('average_price', 0),
                        'last_price': trade.get('last_price', 0),
                        'unbooked_pnl': trade.get('unbooked_pnl', 0)
                    }
                    create_notification(conn, profile_id, sub_id, 'new_position',
                                      f"New position added in {sub_underlying} {sub_expiry}",
                                      notification_data)
            
            for key in removed_positions:
                if key[0] == sub_underlying and key[1] == sub_expiry:
                    trade = old_positions[key]
                    notification_data = {
                        'underlying': key[0],
                        'expiry': key[1],
                        'symbol': key[2],
                        'product': key[3],
                        'quantity': trade.get('quantity', 0),
                        'average_price': trade.get('average_price', 0),
                        'exit_pnl': trade.get('booked_pnl', 0)
                    }
                    create_notification(conn, profile_id, sub_id, 'exited_position',
                                      f"Position exited in {sub_underlying} {sub_expiry}",
                                      notification_data)
            
            for key in existing_positions:
                if key[0] == sub_underlying and key[1] == sub_expiry:
                    old_trade = old_positions[key]
                    new_trade = new_positions[key]
                    old_qty = old_trade.get('quantity', 0)
                    new_qty = new_trade.get('quantity', 0)
                    if old_qty != new_qty:
                        qty_diff = new_qty - old_qty
                        _if_side, _if_qty, _if_price = _compute_implied_fill(
                            old_qty, new_qty,
                            old_trade.get('average_price', 0),
                            new_trade.get('average_price', 0)
                        )
                        notification_data = {
                            'underlying': key[0],
                            'expiry': key[1],
                            'symbol': key[2],
                            'product': key[3],
                            'old_quantity': old_qty,
                            'quantity': new_qty,
                            'quantity_diff': qty_diff,
                            'old_average_price': old_trade.get('average_price', 0),
                            'average_price': new_trade.get('average_price', 0),
                            'last_price': new_trade.get('last_price', 0),
                            'unbooked_pnl': new_trade.get('unbooked_pnl', 0),
                            'booked_pnl': new_trade.get('booked_pnl', 0),
                            'implied_fill_side': _if_side,
                            'implied_fill_qty': _if_qty,
                            'implied_fill_price': _if_price
                        }
                        create_notification(conn, profile_id, sub_id, 'modified_position',
                                          f"Position modified in {sub_underlying} {sub_expiry}",
                                          notification_data)
        
        elif sub_type == 'position' and sub_pos_id:
            # Notify for changes to this specific position
            # Match based on symbol, product, strike, option_type
            matching_key = None
            for key in old_keys | new_keys:
                if (key[2] == sub_pos_id.get('symbol') and 
                    key[3] == sub_pos_id.get('product')):
                    matching_key = key
                    break
            
            if matching_key:
                if matching_key in removed_positions:
                    trade = old_positions[matching_key]
                    notification_data = {
                        'symbol': matching_key[2],
                        'product': matching_key[3],
                        'quantity': trade.get('quantity', 0),
                        'average_price': trade.get('average_price', 0),
                        'exit_pnl': trade.get('booked_pnl', 0)
                    }
                    create_notification(conn, profile_id, sub_id, 'exited_position',
                                      f"Position exited",
                                      notification_data)
                elif matching_key in existing_positions:
                    old_trade = old_positions[matching_key]
                    new_trade = new_positions[matching_key]
                    old_qty = old_trade.get('quantity', 0)
                    new_qty = new_trade.get('quantity', 0)
                    if old_qty != new_qty:
                        qty_diff = new_qty - old_qty
                        _if_side, _if_qty, _if_price = _compute_implied_fill(
                            old_qty, new_qty,
                            old_trade.get('average_price', 0),
                            new_trade.get('average_price', 0)
                        )
                        notification_data = {
                            'symbol': matching_key[2],
                            'product': matching_key[3],
                            'old_quantity': old_qty,
                            'quantity': new_qty,
                            'quantity_diff': qty_diff,
                            'old_average_price': old_trade.get('average_price', 0),
                            'average_price': new_trade.get('average_price', 0),
                            'last_price': new_trade.get('last_price', 0),
                            'unbooked_pnl': new_trade.get('unbooked_pnl', 0),
                            'booked_pnl': new_trade.get('booked_pnl', 0),
                            'implied_fill_side': _if_side,
                            'implied_fill_qty': _if_qty,
                            'implied_fill_price': _if_price
                        }
                        create_notification(conn, profile_id, sub_id, 'quantity_change',
                                          f"Quantity changed",
                                          notification_data)

def create_notification(conn, profile_id, subscription_id, notification_type, message, notification_data):
    """Helper to create a notification"""
    c = conn.cursor()
    c.execute("""
        INSERT INTO notifications (profile_id, subscription_id, notification_type, message, notification_data, created_at, is_read)
        VALUES (?, ?, ?, ?, ?, ?, 0)
    """, (profile_id, subscription_id, notification_type, message, json.dumps(notification_data), now_ist().isoformat()))
    conn.commit()
    print(f"  → Created notification: {message}")

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
    cleanup_old_notifications(conn)

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
            current_time_ist = now_ist()
            upsert_latest_snapshot(conn, profile_id, current_data, timestamp=current_time_ist)
            
        except Exception as e:
            print(f"Error fetching {slug}: {e}")
            continue
            
        # If no last snapshot, save as initial
        if not last_snapshot:
            print(f"-> Initial snapshot for {slug}")
            snapshot_id = save_snapshot(conn, profile_id, current_data, timestamp=now_ist())
            # Record explicit 'Initial' change so it shows up in UI
            c.execute("INSERT INTO position_changes (profile_id, snapshot_id, timestamp, diff_summary) VALUES (?, ?, ?, ?)",
                      (profile_id, snapshot_id, now_ist(), "Initial Snapshot"))
            conn.commit()
            continue
            
        # Compare with last snapshot
        last_data = json.loads(last_snapshot[0]) # Fetchone returns a tuple
        diff = get_normalized_trades(current_data) != get_normalized_trades(last_data)
        
        if diff:
            print(f"-> CHANGE DETECTED for {slug}")
            snapshot_id = save_snapshot(conn, profile_id, current_data, timestamp=now_ist())
            
            # Simple diff summary (placeholder, ideally we list added/removed symbols)
            summary = generate_diff_summary(last_data, current_data)
            
            c.execute("INSERT INTO position_changes (profile_id, snapshot_id, timestamp, diff_summary) VALUES (?, ?, ?, ?)",
                      (profile_id, snapshot_id, now_ist(), summary))
            conn.commit()
            
            # Generate notifications for subscribed items
            generate_notifications_for_changes(conn, profile_id, last_data, current_data)
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
    """Check if Indian stock market is open (in IST timezone)"""
    now = now_ist()
    # Weekday check: 0=Monday, 4=Friday, 5=Saturday, 6=Sunday
    if now.weekday() > 4:
        return False
    
    # Time check: 09:15 to 15:30 IST
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
            print(f"\n--- Run at {now_ist()} ---")
            run_scraper()
        except Exception as e:
            print(f"Fatal error: {e}")
            traceback.print_exc()
        
        # If market is closed, we sleep longer? Or still 60s to catch new profiles added to text file?
        # Let's stick to 60s for responsiveness.
        time.sleep(60)
