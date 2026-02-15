from flask import Flask, render_template, request, jsonify
import sqlite3
import json
from datetime import datetime, timedelta
from database import get_db, sync_profiles
import sys
import os
import threading
import time
import subprocess
import signal

app = Flask(__name__)
# Configure standard port or 5010 as per previous context
PORT = 6060

# In-memory cache for symbol suggestions (profile_id -> {ts, symbols})
SYMBOL_SUGGESTIONS_CACHE = {}
SYMBOL_SUGGESTIONS_TTL_SEC = 60

# Global variable to track last restart to prevent loops
LAST_AUTO_RESTART = None

@app.template_filter('to_datetime')
def to_datetime_filter(value):
    # Timestamps in DB are now consistently Naive IST (from scraper datetime.now())
    # So we do NOT add any offset. We just ensure it's a datetime object.
    if isinstance(value, datetime):
        return value
    
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
             # Handle ISO format
             dt = datetime.fromisoformat(value)
             return dt.replace(tzinfo=None) # Ensure naive
        except:
             return value

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

def restart_scraper_internal():
    print("Restarting scraper process...")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    scraper_path = os.path.join(base_dir, 'scraper.py')
    log_file = os.path.join(base_dir, 'scraper.log')
    
    # Kill existing
    os.system("pkill -f 'vibhu/sensibull/scraper.py'")
    
    # Start new
    cmd = f"nohup python3 {scraper_path} >> {log_file} 2>&1 &"
    os.system(cmd)
    
def monitor_scraper():
    """Background thread to monitor scraper and restart if stuck"""
    global LAST_AUTO_RESTART
    print("Scraper monitor thread started.")
    
    while True:
        try:
            # Check every minute
            time.sleep(60)
            
            if not is_market_open():
                continue
                
            # Check DB for last update
            conn = get_db()
            c = conn.cursor()
            last_updated_row = c.execute("SELECT MAX(timestamp) FROM latest_snapshots").fetchone()
            conn.close()
            
            last_updated = last_updated_row[0] if last_updated_row else None
            
            should_restart = False
            
            if last_updated:
                last_dt = to_datetime_filter(last_updated)
                time_diff = (datetime.now() - last_dt).total_seconds()
                
                # If no update for > 5 minutes (300 seconds)
                if time_diff > 300:
                    print(f"Monitor: Scraper stuck! Last update {time_diff}s ago.")
                    should_restart = True
            else:
                 # If no data at all and market is open, maybe assume stuck? 
                 # Or maybe database is empty. Let's be conservative and only restart if data exists but is stale,
                 # or perhaps if empty database persists for long. 
                 # For now, only restart if stale.
                 pass
                 
            if should_restart:
                # Check cooldown (don't restart if we just restarted < 1 min ago)
                if LAST_AUTO_RESTART and (datetime.now() - LAST_AUTO_RESTART).total_seconds() < 60:
                    print("Monitor: Skipping restart due to cooldown.")
                else:
                    print("Monitor: Triggering auto-restart.")
                    restart_scraper_internal()
                    LAST_AUTO_RESTART = datetime.now()
                    
        except Exception as e:
            print(f"Monitor error: {e}")
            # Sleep a bit to avoid rapid loops on error
            time.sleep(10)

@app.route('/historical_data')
def historical_data():
    return render_template('historical_data.html')

@app.route('/')
def index():
    # Sync profiles from file on every refresh
    sync_profiles()
    
    conn = get_db()
    c = conn.cursor()
    
    # Get all profiles
    profiles_db = c.execute("SELECT * FROM profiles").fetchall()
    
    # Get order from urls.txt
    ordered_slugs = []
    try:
        urls_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'urls.txt')
        if os.path.exists(urls_path):
            with open(urls_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        slug = line.split('/')[-1] if 'sensibull.com' in line else line
                        ordered_slugs.append(slug)
    except Exception as e:
        print(f"Error reading urls.txt for sorting: {e}")

    # Create a map for sorting
    # profile_key -> index
    # We use a large number if not found so they go to the bottom
    sort_map = {slug: i for i, slug in enumerate(ordered_slugs)}
    
    # Sort the DB profiles: those in urls.txt first (in order), others after
    profiles = sorted(profiles_db, key=lambda p: sort_map.get(p['slug'], 99999))

    # Calculate dates (last 30 days)
    
    # We have profiles now. Now get dates.
    # Get unique dates from changes
    dates_rows = c.execute("SELECT DISTINCT date(timestamp) as day FROM position_changes ORDER BY day DESC LIMIT 30").fetchall()
    dates = [row['day'] for row in dates_rows]
    
    # Build matrix
    matrix = {} 
    for p in profiles:
        for d in dates:
            # Check if any changes on this day
            count = c.execute("""
                SELECT COUNT(*) FROM position_changes 
                WHERE profile_id = ? AND date(timestamp) = ?
            """, (p['id'], d)).fetchone()[0]
            
            pnl = 0
            if count > 0:
                 metrics = get_daily_pnl_metrics(c, p['id'], d)
                 pnl = metrics['todays_pnl']
                 
            matrix[(p['id'], d)] = {'count': count, 'pnl': pnl}
            
    # Get global last updated time
    last_updated_row = c.execute("SELECT MAX(timestamp) FROM latest_snapshots").fetchone()
    last_updated = last_updated_row[0] if last_updated_row else None
    
    scraper_error = None
    if not is_market_open():
        scraper_error = "Scraper is paused (Market Closed)"
    else:
        # Check if stuck (no update in last 3 minutes)
        if last_updated:
            last_dt = to_datetime_filter(last_updated)
            if (datetime.now() - last_dt).total_seconds() > 180:
                scraper_error = "Scraper is stuck or not running! (Last update > 3 mins ago)"
        else:
             scraper_error = "Scraper has no data yet!"

    conn.close()
    return render_template('index.html', profiles=profiles, dates=dates, matrix=matrix, last_updated=last_updated, scraper_error=scraper_error)

def calculate_snapshot_pnl(c, snapshot_id):
    snap = c.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if not snap: return 0, 0
    raw = json.loads(snap['raw_data'])
    data = raw.get('data', [])
    
    # Calculate manually to be safe
    total = 0
    booked = 0
    
    for item in data:
        for trade in item.get('trades', []):
            u_pnl = trade.get('unbooked_pnl', 0)
            b_pnl = trade.get('booked_profit_loss', 0)
            
            total += (u_pnl + b_pnl)
            booked += b_pnl
            
    return total, booked

def get_daily_pnl_metrics(c, profile_id, date):
    # 1. Start Day P&L
    start_day_pnl = 0
    
    # Try to get previous day's close
    prev_change = c.execute("""
        SELECT * FROM position_changes 
        WHERE profile_id = ? AND date(timestamp) < ? 
        ORDER BY timestamp DESC LIMIT 1
    """, (profile_id, date)).fetchone()
    
    if prev_change:
        prev_total, prev_booked = calculate_snapshot_pnl(c, prev_change['snapshot_id'])
        start_day_pnl = prev_total - prev_booked
    else:
        # Fallback to first change of the day
        first_change = c.execute("""
            SELECT * FROM position_changes 
            WHERE profile_id = ? AND date(timestamp) = ? 
            ORDER BY timestamp ASC LIMIT 1
        """, (profile_id, date)).fetchone()
        
        if first_change:
            total, booked = calculate_snapshot_pnl(c, first_change['snapshot_id'])
            # Only count unbooked if it's the very first record? 
            # Or use total? If we fallback, it's safer to assume 0 start or use total.
            # Let's stick to total.
            start_day_pnl = total
            
    # 2. Current P&L (Latest available snapshot for the day)
    # We query the `latest_snapshots` table which is updated on every scraper run
    # regardless of position changes. This gives us Realtime P&L.
    
    current_pnl = 0
    booked_pnl = 0
    
    # First try latest_snapshots for realtime data
    latest_realtime = c.execute("SELECT * FROM latest_snapshots WHERE profile_id = ?", (profile_id,)).fetchone()
    
    # We only use realtime if it matches the requested date
    # (Or should we always use it if date is TODAY? Yes.)
    # If date is in the past, we must fall back to historical snapshots.
    
    today_str = datetime.now().strftime('%Y-%m-%d')
    use_realtime = (date == today_str) and (latest_realtime is not None)
    
    last_updated = None
    
    if use_realtime:
        # Parse raw_data manually since we don't have calculate_snapshot_pnl helper for raw JSON input
        raw = json.loads(latest_realtime['raw_data'])
        last_updated = latest_realtime['timestamp'] # Get timestamp from latest_snapshots
        data = raw.get('data', [])
        total = 0
        booked = 0
        for item in data:
            for trade in item.get('trades', []):
                total += (trade.get('unbooked_pnl', 0) + trade.get('booked_profit_loss', 0))
                booked += trade.get('booked_profit_loss', 0)
        current_pnl = total
        booked_pnl = booked
    else:
        # Fallback to history (Last recorded snapshot for that day)
        latest_snapshot = c.execute("""
            SELECT * FROM snapshots 
            WHERE profile_id = ? AND date(timestamp) = ? 
            ORDER BY timestamp DESC LIMIT 1
        """, (profile_id, date)).fetchone()
        
        if latest_snapshot:
            current_pnl, booked_pnl = calculate_snapshot_pnl(c, latest_snapshot['id'])
            last_updated = latest_snapshot['timestamp']
    
    todays_pnl = current_pnl - start_day_pnl
    
    return {
        'start_pnl': start_day_pnl,
        'current_pnl': current_pnl,
        'todays_pnl': todays_pnl,
        'booked_pnl': booked_pnl,
        'last_updated': last_updated
    }

@app.route('/profile/<slug>/<date>')
def daily_view(slug, date):
    conn = get_db()
    c = conn.cursor()
    
    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return "Profile not found", 404
    
    # Get changes for this date
    changes = c.execute("""
        SELECT * FROM position_changes 
        WHERE profile_id = ? AND date(timestamp) = ? 
        ORDER BY timestamp DESC
    """, (profile['id'], date)).fetchall()
    
    # Get Metrics
    metrics = get_daily_pnl_metrics(c, profile['id'], date)
        
    conn.close()
    return render_template('daily_view.html', 
                         slug=slug, 
                         date=date, 
                         changes=changes,
                         metrics=metrics)

@app.route('/api/diff/<int:change_id>')
def api_diff(change_id):
    conn = get_db()
    c = conn.cursor()
    
    change = c.execute("SELECT * FROM position_changes WHERE id = ?", (change_id,)).fetchone()
    if not change:
        conn.close()
        return jsonify({'error': 'Change not found'}), 404
        
    current_snapshot = c.execute("SELECT * FROM snapshots WHERE id = ?", (change['snapshot_id'],)).fetchone()
    current_raw = json.loads(current_snapshot['raw_data']) if current_snapshot else {}
    current_trades = normalize_trades_for_diff(current_raw.get('data', []))

    # Find PREVIOUS snapshot for this profile
    # We want the latest snapshot BEFORE this one
    prev_snapshot = c.execute("""
        SELECT * FROM snapshots 
        WHERE profile_id = ? AND id < ? 
        ORDER BY id DESC LIMIT 1
    """, (change['profile_id'], change['snapshot_id'])).fetchone()
    
    prev_raw = json.loads(prev_snapshot['raw_data']) if prev_snapshot else {}
    prev_trades = normalize_trades_for_diff(prev_raw.get('data', []))
    
    # Find ALL previous snapshots to look back through history
    # We'll keep looking back until we find valid quantity data
    all_prev_snapshots = c.execute("""
        SELECT * FROM snapshots 
        WHERE profile_id = ? AND id < ? 
        ORDER BY id DESC
    """, (change['profile_id'], change['snapshot_id'])).fetchall()
    
    # Build a map of historical data for each symbol
    historical_trades = {}
    for snap in all_prev_snapshots:
        snap_data = json.loads(snap['raw_data'])
        trades = normalize_trades_for_diff(snap_data.get('data', []))
        # Merge into historical_trades (keep first with both qty > 0 AND avg_price > 0)
        for key, trade in trades.items():
            if key not in historical_trades:
                # Need BOTH quantity AND average_price to be non-zero
                if trade.get('quantity', 0) != 0 and trade.get('average_price', 0) != 0:
                    historical_trades[key] = trade
            else:
                # If we already have it but with 0 qty or 0 avg_price, update if this one has both
                existing = historical_trades[key]
                if (existing.get('quantity', 0) == 0 or existing.get('average_price', 0) == 0):
                    if trade.get('quantity', 0) != 0 and trade.get('average_price', 0) != 0:
                        historical_trades[key] = trade
    
    # Calculate Diff
    diff_data = calculate_diff(prev_trades, current_trades, historical_trades)
    
    # Build Detailed List for Added
    detailed_added = []
    for item in diff_data['added']:
        detailed_added.append({
            'symbol': item['trading_symbol'],
            'product': item.get('product', ''),
            'quantity': item.get('quantity', 0),
            'average_price': item.get('average_price', 0),
            'last_price': item.get('last_price', 0),
            'change_type': 'ADDED'
        })
    
    # Build Detailed List for Removed
    detailed_removed = []
    for item in diff_data['removed']:
        exit_pnl = item.get('exit_pnl', 0)
        exit_price = item.get('exit_price', 0)
        original_qty = item.get('original_quantity', 0)
        # Use original_average_price if available, otherwise use average_price
        avg_price = item.get('original_average_price', item.get('average_price', 0))
        detailed_removed.append({
            'symbol': item['trading_symbol'],
            'product': item.get('product', ''),
            'quantity': original_qty,  # Original quantity before removal
            'original_quantity': original_qty,
            'average_price': avg_price,
            'last_price': item.get('last_price', 0),
            'exit_pnl': exit_pnl,
            'exit_price': exit_price,
            'change_type': 'REMOVED'
        })
    
    # Build Detailed List for Modified
    detailed_modified = []
    for item in diff_data['modified']:
        exit_pnl = item.get('exit_pnl', 0)
        exit_price = item.get('exit_price', 0)
        # Use original_average_price if available, otherwise use average_price
        avg_price = item.get('original_average_price', item.get('average_price', 0))
        detailed_modified.append({
            'symbol': item['trading_symbol'],
            'product': item.get('product', ''),
            'old_quantity': item.get('old_quantity', 0),
            'new_quantity': item.get('quantity', 0),
            'quantity_diff': item.get('quantity_diff', 0),
            'average_price': avg_price,
            'last_price': item.get('last_price', 0),
            'exit_pnl': exit_pnl,
            'exit_price': exit_price,
            'change_type': 'MODIFIED'
        })
    
    # Ensure UI can display pre-change avg price for MODIFIED entries.
    # The UI uses `diff.modified`, not `detailed_modified`, so we expose `original_average_price` there too.
    for item in diff_data.get('modified', []) or []:
        if 'original_average_price' not in item:
            # Fallback (should already exist from calculate_diff)
            item['original_average_price'] = item.get('average_price', 0)

    # Prepare the response
    result = {
        'diff_summary': change['diff_summary'],
        'positions': current_raw.get('data', []),
        'diff': diff_data,
        'detailed_added': detailed_added,
        'detailed_removed': detailed_removed,
        'detailed_modified': detailed_modified
    }
    
    conn.close()
    return jsonify(result)

@app.route('/api/daily_log/<slug>/<date>')
def daily_log(slug, date):
    conn = get_db()
    c = conn.cursor()
    
    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404
        
    # Get metrics for the day to find 'start_day_pnl'
    metrics = get_daily_pnl_metrics(c, profile['id'], date)
    start_day_pnl = metrics['start_pnl']
        
    # fetch all changes for the day in chronological order
    changes = c.execute("""
        SELECT * FROM position_changes 
        WHERE profile_id = ? AND date(timestamp) = ? 
        ORDER BY timestamp ASC
    """, (profile['id'], date)).fetchall()
    
    events = []
    
    for i, change in enumerate(changes):
        # Calculate P&L at this snapshot
        snap_total, snap_booked = calculate_snapshot_pnl(c, change['snapshot_id'])
        todays_pnl = snap_total - start_day_pnl
        
        # Calculate Detailed Diff (Restore "Change" column detail)
        curr_snap = c.execute("SELECT raw_data FROM snapshots WHERE id = ?", (change['snapshot_id'],)).fetchone()
        curr_raw = json.loads(curr_snap['raw_data']) if curr_snap else {}
        curr_trades = normalize_trades_for_diff(curr_raw.get('data', []))
        
        # Find previous snapshot (relative to this change)
        prev_snap = c.execute("""
            SELECT raw_data FROM snapshots 
            WHERE profile_id = ? AND id < ? 
            ORDER BY id DESC LIMIT 1
        """, (profile['id'], change['snapshot_id'])).fetchone()
        
        prev_raw = json.loads(prev_snap['raw_data']) if prev_snap else {}
        prev_trades = normalize_trades_for_diff(prev_raw.get('data', []))
        
        # Find ALL previous snapshots to look back through history
        all_prev_snapshots = c.execute("""
            SELECT raw_data FROM snapshots 
            WHERE profile_id = ? AND id < ? 
            ORDER BY id DESC
        """, (profile['id'], change['snapshot_id'])).fetchall()
        
        # Build a map of historical data for each symbol
        historical_trades = {}
        for snap in all_prev_snapshots:
            snap_data = json.loads(snap['raw_data'])
            trades = normalize_trades_for_diff(snap_data.get('data', []))
            # Merge into historical_trades (keep first non-zero qty we find)
            for key, trade in trades.items():
                if key not in historical_trades:
                    if trade.get('quantity', 0) != 0:
                        historical_trades[key] = trade
                else:
                    # If we already have it but with 0 qty, update if this one has qty
                    existing = historical_trades[key]
                    if (existing.get('quantity', 0) == 0) and (trade.get('quantity', 0) != 0):
                        historical_trades[key] = trade
        
        diff_data = calculate_diff(prev_trades, curr_trades, historical_trades)
        
        # Build Detailed List
        detailed_changes = []
        for item in diff_data['added']:
            detailed_changes.append({
                'symbol': item['trading_symbol'],
                'text': f"Qty: 0 → {item['quantity']} (+{item['quantity']})",
                'color': 'green'
            })
        for item in diff_data['removed']:
            exit_pnl = item.get('exit_pnl', 0)
            exit_price = item.get('exit_price', 0)
            original_qty = item.get('original_quantity', item.get('quantity', 0))
            if exit_pnl != 0:
                pnl_text = f" | Exit P&L: ₹{exit_pnl:,.2f} | Exit Price: ₹{exit_price:.2f}"
            else:
                pnl_text = ""
            detailed_changes.append({
                'symbol': item['trading_symbol'],
                'text': f"Qty: {original_qty} → 0{pnl_text}",
                'color': 'red'
            })
        for item in diff_data['modified']:
            sign = '+' if item['quantity_diff'] > 0 else ''
            color = 'green' if item['quantity_diff'] > 0 else 'red'
            exit_pnl = item.get('exit_pnl', 0)
            exit_price = item.get('exit_price', 0)
            if exit_pnl != 0:
                pnl_text = f" | Exit P&L: ₹{exit_pnl:,.2f} | Exit Price: ₹{exit_price:.2f}"
            else:
                pnl_text = ""
            detailed_changes.append({
                'symbol': item['trading_symbol'],
                'text': f"Qty: {item['old_quantity']} → {item['quantity']} ({sign}{item['quantity_diff']}){pnl_text}",
                'color': color
            })
            
        event = {
            'time': to_datetime_filter(change['timestamp']).strftime('%H:%M:%S'),
            'type': 'Change', 
            'changes': detailed_changes, 
            'change_id': change['id'],
            'todays_pnl': todays_pnl,
            'booked_pnl': snap_booked
        }
        
        events.append(event)
    
    conn.close()
    events.reverse() # Latest first
    return jsonify({'events': events})


def _best_effort_trade_before(trade, last_good_trade):
    """Backfill zero qty / avg from last known good trade for display and calculations."""
    if not trade:
        return None
    if not last_good_trade:
        return trade

    out = dict(trade)
    if (out.get('quantity', 0) in (None, 0)) and last_good_trade.get('quantity') not in (None, 0):
        out['quantity'] = last_good_trade.get('quantity', 0)
    if (out.get('average_price', 0) in (None, 0)) and last_good_trade.get('average_price') not in (None, 0):
        out['average_price'] = last_good_trade.get('average_price', 0)
    if (out.get('last_price', 0) in (None, 0)) and last_good_trade.get('last_price') not in (None, 0):
        out['last_price'] = last_good_trade.get('last_price', 0)
    return out


def _compute_exit_metrics(before_trade):
    """Compute best-effort exit price/pnl, aligned with calculate_diff() logic."""
    if not before_trade:
        return 0, 0

    qty = before_trade.get('quantity', 0) or 0
    avg_price = before_trade.get('average_price', 0) or 0
    last_price = before_trade.get('last_price', 0) or 0
    booked_pnl = before_trade.get('booked_profit_loss', 0) or 0

    exit_price = last_price or 0
    if booked_pnl != 0:
        exit_pnl = booked_pnl
    elif qty and avg_price and exit_price:
        exit_pnl = (exit_price - avg_price) * qty
    else:
        exit_pnl = 0

    return exit_pnl, exit_price


def _compute_implied_fill(prev_trade, curr_trade, original_avg_price=None):
    """Compute implied fill side/qty/price for quantity delta between snapshots."""
    q0 = (prev_trade or {}).get('quantity', 0) or 0
    q1 = (curr_trade or {}).get('quantity', 0) or 0
    dq = q1 - q0

    side = 'BUY' if dq > 0 else ('SELL' if dq < 0 else '')
    fill_qty = abs(dq)

    try:
        a0 = original_avg_price if original_avg_price not in (None, 0) else ((prev_trade or {}).get('average_price', 0) or 0)
        a1 = (curr_trade or {}).get('average_price', 0) or 0
        if dq and a0 and a1:
            v0 = a0 * q0
            v1 = a1 * q1
            implied = (v1 - v0) / dq
            price = abs(implied)
        else:
            price = 0
    except Exception:
        price = 0

    return side, fill_qty, price


@app.route('/api/profile_symbol_suggest/<slug>')
def api_profile_symbol_suggest(slug):
    q = (request.args.get('q') or '').strip()
    limit = request.args.get('limit')
    try:
        limit = int(limit) if limit is not None else 30
    except Exception:
        limit = 30
    limit = max(5, min(200, limit))

    conn = get_db()
    c = conn.cursor()

    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found', 'symbols': []}), 404

    profile_id = profile['id']

    # Cache read
    now_ts = time.time()
    cached = SYMBOL_SUGGESTIONS_CACHE.get(profile_id)
    symbols = None
    if cached and (now_ts - cached.get('ts', 0)) < SYMBOL_SUGGESTIONS_TTL_SEC:
        symbols = cached.get('symbols')

    # Cache miss: scan snapshots
    if symbols is None:
        symbols_set = set()
        snapshots = c.execute(
            "SELECT raw_data FROM snapshots WHERE profile_id = ? ORDER BY id DESC",
            (profile_id,),
        ).fetchall()

        for row in snapshots:
            try:
                raw = json.loads(row['raw_data'])
            except Exception:
                continue
            trades_map = normalize_trades_for_diff(raw.get('data', []))
            for t in trades_map.values():
                sym = (t.get('trading_symbol') or '').strip()
                if sym:
                    symbols_set.add(sym)

        symbols = sorted(symbols_set)
        SYMBOL_SUGGESTIONS_CACHE[profile_id] = {'ts': now_ts, 'symbols': symbols}

    conn.close()

    q_norm = q.upper()
    if not q_norm:
        # Return top N alphabetically if no query
        out = symbols[:limit]
    else:
        # Prefer prefix matches, then substring matches
        prefix = [s for s in symbols if s.upper().startswith(q_norm)]
        contains = [s for s in symbols if (q_norm in s.upper()) and (not s.upper().startswith(q_norm))]
        out = (prefix + contains)[:limit]

    return jsonify({
        'profile': {'id': profile_id, 'slug': profile['slug'], 'name': profile['name']},
        'q': q,
        'limit': limit,
        'symbols': out,
    })


@app.route('/api/profile_all_underlyings/<slug>')
def api_profile_all_underlyings(slug):
    """Get all position lifecycle events grouped by underlying symbol from all snapshots."""
    underlying_filter = (request.args.get('underlying') or '').strip().upper()
    
    conn = get_db()
    c = conn.cursor()
    
    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found', 'underlyings': {}}), 404
    
    # Get all snapshots for this profile in chronological order
    snapshots = c.execute(
        "SELECT id, timestamp, raw_data FROM snapshots WHERE profile_id = ? ORDER BY id ASC",
        (profile['id'],),
    ).fetchall()
    
    # Map snapshot -> change_id for linking to details
    changes_rows = c.execute(
        "SELECT id, snapshot_id FROM position_changes WHERE profile_id = ?",
        (profile['id'],),
    ).fetchall()
    snapshot_to_change_id = {row['snapshot_id']: row['id'] for row in changes_rows}
    
    # Helper function to extract underlying from trading symbol
    def get_underlying(trading_symbol):
        """Extract underlying name from trading symbol (e.g., NIFTY2621725600PE -> NIFTY)"""
        if not trading_symbol:
            return None
        import re
        match = re.match(r'^[a-zA-Z&-]+', trading_symbol)
        if match:
            return match.group(0).upper()
        return None
    
    # Track state per key (symbol|product) using same logic as api_profile_symbol_lifecycle
    state = {}
    underlying_events = {}
    underlyings_seen = set()
    
    for snap in snapshots:
        snap_id = snap['id']
        timestamp = snap['timestamp']
        change_id = snapshot_to_change_id.get(snap_id)
        
        try:
            raw = json.loads(snap['raw_data'])
        except Exception:
            continue
        
        trades_map = normalize_trades_for_diff(raw.get('data', []))
        
        # Process all keys from current snapshot AND previously tracked keys
        all_keys_to_check = set(trades_map.keys()) | set(state.keys())
        
        for key in all_keys_to_check:
            curr_trade = trades_map.get(key)
            st = state.get(key) or {
                'present': False,
                'last_trade': None,
                'last_good_trade': None,
            }
            
            # Get symbol and underlying
            if curr_trade:
                symbol = curr_trade.get('trading_symbol', '')
                product = curr_trade.get('product', '')
            else:
                # Use previous trade info
                prev_t = st.get('last_trade')
                if prev_t:
                    symbol = prev_t.get('trading_symbol', '')
                    product = prev_t.get('product', '')
                else:
                    continue
            
            underlying = get_underlying(symbol)
            if not underlying:
                # Clean up state if no underlying
                if key in state:
                    del state[key]
                continue
            
            underlyings_seen.add(underlying)
            
            # Skip if filtering and doesn't match
            if underlying_filter and underlying != underlying_filter:
                # Update state but don't add event
                prev_present = bool(st.get('present'))
                curr_present = curr_trade is not None and ((curr_trade.get('quantity', 0) or 0) != 0)
                
                if curr_trade and (curr_trade.get('quantity', 0) not in (None, 0)) and (curr_trade.get('average_price', 0) not in (None, 0)):
                    st['last_good_trade'] = curr_trade
                
                st['present'] = curr_present
                st['last_trade'] = curr_trade if curr_trade is not None else None
                state[key] = st
                continue
            
            if underlying not in underlying_events:
                underlying_events[underlying] = []
            
            prev_present = bool(st.get('present'))
            prev_trade_raw = st.get('last_trade')
            prev_trade = _best_effort_trade_before(prev_trade_raw, st.get('last_good_trade'))
            
            curr_present = curr_trade is not None and ((curr_trade.get('quantity', 0) or 0) != 0)
            
            # Update last_good_trade based on current (if good)
            if curr_trade:
                if (curr_trade.get('quantity', 0) not in (None, 0)) and (curr_trade.get('average_price', 0) not in (None, 0)):
                    st['last_good_trade'] = curr_trade
            
            event_type = None
            
            if (not prev_present) and curr_present:
                event_type = 'ENTERED'
            elif prev_present and (not curr_present):
                # Position disappeared or quantity became 0
                event_type = 'EXITED'
            elif prev_present and curr_present:
                prev_qty = (prev_trade or {}).get('quantity', 0) or 0
                curr_qty = (curr_trade or {}).get('quantity', 0) or 0
                if prev_qty != curr_qty:
                    event_type = 'MODIFIED'
            
            if event_type:
                before_qty = (prev_trade or {}).get('quantity', 0) or 0
                after_qty = (curr_trade or {}).get('quantity', 0) or 0
                
                before_avg = (prev_trade or {}).get('average_price', 0) or 0
                after_avg = (curr_trade or {}).get('average_price', 0) or 0
                
                after_ltp = (curr_trade or {}).get('last_price', 0) or 0
                after_unbooked = (curr_trade or {}).get('unbooked_pnl', 0) or 0
                
                # Exit metrics come from the last snapshot where trade existed
                exit_pnl = 0
                exit_price = 0
                if event_type == 'EXITED':
                    exit_pnl, exit_price = _compute_exit_metrics(prev_trade)
                
                implied_fill_side = ''
                implied_fill_qty = 0
                implied_fill_price = 0
                if event_type == 'MODIFIED':
                    implied_fill_side, implied_fill_qty, implied_fill_price = _compute_implied_fill(
                        prev_trade or {},
                        curr_trade or {},
                        original_avg_price=before_avg,
                    )
                    # Compute exit P&L if reducing position
                    if (before_qty > 0 and after_qty < before_qty) or (before_qty < 0 and after_qty > before_qty):
                        exit_pnl, exit_price = _compute_exit_metrics(prev_trade)
                
                underlying_events[underlying].append({
                    'timestamp': timestamp,
                    'snapshot_id': snap_id,
                    'change_id': change_id,
                    'type': event_type,
                    'symbol': symbol,
                    'product': product,
                    'before_quantity': before_qty,
                    'after_quantity': after_qty,
                    'quantity_diff': after_qty - before_qty,
                    'before_average_price': before_avg,
                    'after_average_price': after_avg,
                    'after_last_price': after_ltp,
                    'after_unbooked_pnl': after_unbooked,
                    'exit_pnl': exit_pnl,
                    'exit_price': exit_price,
                    'implied_fill_side': implied_fill_side,
                    'implied_fill_qty': implied_fill_qty,
                    'implied_fill_price': implied_fill_price,
                })
            
            # Persist updated state
            st['present'] = curr_present
            st['last_trade'] = curr_trade if curr_trade is not None else None
            state[key] = st
    
    # Sort events by timestamp (latest first) for each underlying
    for underlying in underlying_events:
        underlying_events[underlying].sort(key=lambda e: e['timestamp'], reverse=True)
    
    # Sort underlyings alphabetically
    sorted_underlyings = sorted(underlyings_seen)
    
    conn.close()
    
    return jsonify({
        'profile': {'id': profile['id'], 'slug': profile['slug'], 'name': profile['name']},
        'underlyings': sorted_underlyings,
        'events': underlying_events,
        'filter': underlying_filter or None,
    })


@app.route('/api/profile_symbol_lifecycle/<slug>')
def api_profile_symbol_lifecycle(slug):
    symbol = (request.args.get('symbol') or '').strip()
    product_filter = (request.args.get('product') or '').strip()  # optional

    if not symbol:
        return jsonify({'error': 'symbol is required', 'events': []}), 400

    symbol_norm = symbol.upper()

    conn = get_db()
    c = conn.cursor()

    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found', 'events': []}), 404

    snapshots = c.execute(
        "SELECT id, timestamp, raw_data FROM snapshots WHERE profile_id = ? ORDER BY id ASC",
        (profile['id'],),
    ).fetchall()

    # Map snapshot -> change_id so UI can open existing diff modal when applicable
    changes_rows = c.execute(
        "SELECT id, snapshot_id FROM position_changes WHERE profile_id = ?",
        (profile['id'],),
    ).fetchall()
    snapshot_to_change_id = {row['snapshot_id']: row['id'] for row in changes_rows}

    # Track state per key=SYMBOL|PRODUCT
    state = {}
    products_seen = set()

    events = []

    for snap in snapshots:
        ts = snap['timestamp']
        snap_id = snap['id']
        change_id = snapshot_to_change_id.get(snap_id)

        try:
            raw = json.loads(snap['raw_data'])
        except Exception:
            raw = {}

        trades_map = normalize_trades_for_diff(raw.get('data', []))

        matching_keys = []
        for key, t in trades_map.items():
            if (t.get('trading_symbol') or '').upper() != symbol_norm:
                continue
            prod = t.get('product') or ''
            products_seen.add(prod)
            if product_filter and prod != product_filter:
                continue
            matching_keys.append(key)

        # Also consider keys that were previously present but now disappeared in this snapshot
        for key in list(state.keys()):
            if not key.startswith(symbol_norm + '|'):
                continue
            prod = key.split('|', 1)[1] if '|' in key else ''
            products_seen.add(prod)
            if product_filter and prod != product_filter:
                continue
            if key not in trades_map and key not in matching_keys:
                matching_keys.append(key)

        for key in matching_keys:
            curr_trade = trades_map.get(key)
            st = state.get(key) or {
                'present': False,
                'last_trade': None,
                'last_good_trade': None,
            }

            prev_present = bool(st.get('present'))
            prev_trade_raw = st.get('last_trade')
            prev_trade = _best_effort_trade_before(prev_trade_raw, st.get('last_good_trade'))

            curr_present = curr_trade is not None and ((curr_trade.get('quantity', 0) or 0) != 0)

            # Update last_good_trade based on current (if good)
            if curr_trade:
                if (curr_trade.get('quantity', 0) not in (None, 0)) and (curr_trade.get('average_price', 0) not in (None, 0)):
                    st['last_good_trade'] = curr_trade

            event_type = None

            if (not prev_present) and curr_present:
                event_type = 'ENTERED'
            elif prev_present and (not curr_present):
                # Position disappeared or quantity became 0
                event_type = 'EXITED'
            elif prev_present and curr_present:
                prev_qty = (prev_trade or {}).get('quantity', 0) or 0
                curr_qty = (curr_trade or {}).get('quantity', 0) or 0
                if prev_qty != curr_qty:
                    event_type = 'MODIFIED'

            if event_type:
                before_qty = (prev_trade or {}).get('quantity', 0) or 0
                after_qty = (curr_trade or {}).get('quantity', 0) or 0

                before_avg = (prev_trade or {}).get('average_price', 0) or 0
                after_avg = (curr_trade or {}).get('average_price', 0) or 0

                after_ltp = (curr_trade or {}).get('last_price', 0) or 0
                after_unbooked = (curr_trade or {}).get('unbooked_pnl', 0) or 0

                # Exit metrics come from the last snapshot where trade existed
                exit_pnl = 0
                exit_price = 0
                if event_type == 'EXITED':
                    exit_pnl, exit_price = _compute_exit_metrics(prev_trade)

                implied_fill_side = ''
                implied_fill_qty = 0
                implied_fill_price = 0
                if event_type == 'MODIFIED':
                    implied_fill_side, implied_fill_qty, implied_fill_price = _compute_implied_fill(
                        prev_trade or {},
                        curr_trade or {},
                        original_avg_price=before_avg,
                    )

                # Product always comes from key
                product_val = key.split('|', 1)[1] if '|' in key else (curr_trade or {}).get('product', '')

                events.append({
                    'timestamp': ts,
                    'snapshot_id': snap_id,
                    'change_id': change_id,
                    'type': event_type,
                    'symbol': symbol_norm,
                    'product': product_val,
                    'before_quantity': before_qty,
                    'after_quantity': after_qty,
                    'quantity_diff': after_qty - before_qty,
                    'before_average_price': before_avg,
                    'after_average_price': after_avg,
                    'after_last_price': after_ltp,
                    'after_unbooked_pnl': after_unbooked,
                    'exit_pnl': exit_pnl,
                    'exit_price': exit_price,
                    'implied_fill_side': implied_fill_side,
                    'implied_fill_qty': implied_fill_qty,
                    'implied_fill_price': implied_fill_price,
                })

            # Persist updated state
            st['present'] = curr_present
            st['last_trade'] = curr_trade if curr_trade is not None else None
            state[key] = st

    conn.close()

    events.sort(key=lambda e: (e.get('timestamp') or ''), reverse=True)

    return jsonify({
        'profile': {'id': profile['id'], 'slug': profile['slug'], 'name': profile['name']},
        'query': {'symbol': symbol_norm, 'product': product_filter},
        'products_seen': sorted([p for p in products_seen if p is not None]),
        'events': events,
    })


def normalize_trades_for_diff(positions_data):
    """Normalize broker snapshot trades for diffing.

    Returns a dict keyed by `trading_symbol|product`.

    NOTE: This is intentionally lightweight and based only on fields we need for
    diffing + display. In particular, some brokers will emit `booked_profit_loss=0`
    even at the time a position disappears; the UI exit metrics are therefore
    computed as best-effort using available price fields.
    """
    trades_map = {}
    for p in positions_data:
        for t in p.get('trades', []):
            # Create a unique key for the instrument
            key = f"{t.get('trading_symbol')}|{t.get('product')}"
            
            # Get quantity - try multiple field names
            qty = t.get('quantity') or t.get('qty') or t.get('net_qty') or t.get('net_quantity') or 0
            # Get average price - try multiple field names
            avg_p = t.get('average_price') or t.get('avg_price') or t.get('averageprice') or t.get('avg') or 0
            # Get last price
            last_p = t.get('last_price') or t.get('lastprice') or t.get('ltp') or t.get('last') or 0
            # Get P&L fields
            unbooked = t.get('unbooked_pnl') or t.get('unbookedpnl') or t.get('unbooked') or 0
            booked = t.get('booked_profit_loss') or t.get('bookedprofitloss') or t.get('booked_pnl') or t.get('bookedpnl') or 0
            
            if key not in trades_map:
                trades_map[key] = {
                    'trading_symbol': t.get('trading_symbol'),
                    'product': t.get('product'),
                    'quantity': qty,
                    'average_price': avg_p,
                    'last_price': last_p,
                    'unbooked_pnl': unbooked,
                    'booked_profit_loss': booked
                }
            else:
                # Update with latest values if they exist
                if qty: trades_map[key]['quantity'] = qty
                if avg_p: trades_map[key]['average_price'] = avg_p
                if last_p: trades_map[key]['last_price'] = last_p
                trades_map[key]['unbooked_pnl'] = unbooked
                trades_map[key]['booked_profit_loss'] = booked
    
    return trades_map

def calculate_diff(prev_map, curr_map, historical_trades=None):
    """
    Calculate differences between previous and current trade maps.
    historical_trades contains the earliest known data for each symbol (with qty > 0).
    """
    if historical_trades is None:
        historical_trades = {}
        
    added = []
    removed = []
    modified = []
    
    all_keys = set(prev_map.keys()) | set(curr_map.keys())
    
    for key in all_keys:
        p = prev_map.get(key)
        c = curr_map.get(key)
        
        if not p:
            # Added - position exists in current but not in previous
            c['change_type'] = 'ADDED'
            # For added positions, there's no exit yet
            c['exit_pnl'] = 0
            c['exit_price'] = 0
            c['original_quantity'] = 0
            added.append(c)
        elif not c:
            # Removed - position no longer exists (was in previous, not in current)
            p['change_type'] = 'REMOVED'

            # --- Identify the position before it disappeared ---
            # We prefer to display a non-zero quantity + average price; if the immediately
            # previous snapshot has zeros (common with some brokers), fall back to the first
            # earlier snapshot that has both quantity and avg_price.
            original_qty = p.get('quantity', 0)
            original_avg_price = p.get('average_price', 0)
            original_last_price = p.get('last_price', 0)

            if (original_qty == 0 or original_avg_price == 0) and historical_trades:
                hist = historical_trades.get(key)
                if hist:
                    if original_qty == 0:
                        original_qty = hist.get('quantity', 0)
                    if original_avg_price == 0:
                        original_avg_price = hist.get('average_price', 0)
                    if original_last_price == 0:
                        original_last_price = hist.get('last_price', 0)

            p['original_quantity'] = original_qty
            p['original_average_price'] = original_avg_price  # Store for display

            # --- Exit calculation ---
            # NOTE: In many snapshots, `booked_profit_loss` may remain 0 even though a leg was
            # closed (especially in mixed changes). In such cases, we still want to show a
            # meaningful Exit Price and Exit P&L in the UI.
            #
            # Best-effort exit_price:
            #   - Use the last traded price from the last snapshot where the position existed.
            # Best-effort exit_pnl:
            #   - Prefer booked_profit_loss if non-zero
            #   - Otherwise compute from prices: (exit_price - avg_price) * quantity
            #     (works for both long qty>0 and short qty<0)

            # Best effort exit price from the last available snapshot.
            exit_price = original_last_price or p.get('last_price', 0) or 0

            # Prefer booked profit/loss if broker provides it.
            booked_pnl = p.get('booked_profit_loss', 0) or 0

            # Determine avg price to use.
            avg_price = original_avg_price or p.get('average_price', 0) or 0

            if booked_pnl != 0:
                exit_pnl = booked_pnl
            elif original_qty and avg_price and exit_price:
                exit_pnl = (exit_price - avg_price) * original_qty
            else:
                exit_pnl = 0

            p['exit_price'] = exit_price
            p['exit_pnl'] = exit_pnl

            # Update average_price for display if we found it from history
            if original_avg_price and not p.get('average_price'):
                p['average_price'] = original_avg_price

            removed.append(p)
        else:
            # Check for modification (quantity change)
            if p['quantity'] != c['quantity']:
                c['change_type'] = 'MODIFIED'
                
                # Get original quantity from prev_map, or fall back to historical_trades if qty is 0
                original_qty = p.get('quantity', 0)
                original_avg_price = p.get('average_price', 0)
                original_last_price = p.get('last_price', 0)
                
                # If prev_map has qty=0 or avg_price=0, try to get data from historical_trades
                if (original_qty == 0 or original_avg_price == 0) and historical_trades:
                    hist = historical_trades.get(key)
                    if hist:
                        if original_qty == 0:
                            original_qty = hist.get('quantity', 0)
                        if original_avg_price == 0:
                            original_avg_price = hist.get('average_price', 0)
                        if original_last_price == 0:
                            original_last_price = hist.get('last_price', 0)
                
                c['old_quantity'] = p['quantity']
                c['quantity_diff'] = c['quantity'] - p['quantity']
                
                # Store original quantity before change
                c['original_quantity'] = original_qty
                c['original_average_price'] = original_avg_price
                
                # --- Implied modification fill price (best-effort) ---
                # We cannot know exact broker fills from snapshots, but we can derive the *implied*
                # average price for the net quantity change between snapshots using weighted-average math.
                #
                # Define snapshot "value" as V = avg_price * quantity.
                # Delta trade average price ~= (V1 - V0) / (Q1 - Q0)
                #
                # We expose this as:
                #   implied_fill_side: BUY/SELL (based on sign of quantity_diff)
                #   implied_fill_qty: abs(quantity_diff)
                #   implied_fill_price: abs(derived price)
                #
                # Note: We use original_average_price as "Avg (Before)" (already history-backed).
                try:
                    q0 = p.get('quantity', 0) or 0
                    q1 = c.get('quantity', 0) or 0
                    dq = q1 - q0
                    a0 = original_avg_price if original_avg_price not in (None, 0) else (p.get('average_price', 0) or 0)
                    a1 = c.get('average_price', 0) or 0

                    c['implied_fill_side'] = 'BUY' if dq > 0 else ('SELL' if dq < 0 else '')
                    c['implied_fill_qty'] = abs(dq)

                    if dq and a0 and a1 and q0 is not None and q1 is not None:
                        v0 = a0 * q0
                        v1 = a1 * q1
                        implied = (v1 - v0) / dq
                        c['implied_fill_price'] = abs(implied)
                    else:
                        c['implied_fill_price'] = 0
                except Exception:
                    c['implied_fill_side'] = ''
                    c['implied_fill_qty'] = abs(c.get('quantity_diff', 0) or 0)
                    c['implied_fill_price'] = 0

                # Calculate P&L from the portion that was closed
                closed_qty = p['quantity'] - c['quantity']
                if closed_qty > 0:
                    # P&L from the closed portion = difference in booked P&L
                    prev_booked = p.get('booked_profit_loss', 0)
                    curr_booked = c.get('booked_profit_loss', 0)
                    c['exit_pnl'] = prev_booked - curr_booked
                    
                    # Calculate exit price for the closed portion
                    avg_price = original_avg_price if original_avg_price else p.get('average_price', 0)
                    last_price = original_last_price if original_last_price else p.get('last_price', 0)
                    # Use avg_price if available, otherwise use last_price
                    price_for_calc = avg_price if avg_price else last_price
                    
                    if price_for_calc and closed_qty:
                        if c['exit_pnl'] >= 0:
                            c['exit_price'] = price_for_calc + (c['exit_pnl'] / closed_qty)
                        else:
                            c['exit_price'] = price_for_calc - (abs(c['exit_pnl']) / closed_qty)
                    else:
                        c['exit_price'] = 0
                else:
                    # Position increased (added more), no exit P&L
                    c['exit_pnl'] = 0
                    c['exit_price'] = 0
                
                # Update average_price for display if we found it from history
                if original_avg_price and not c.get('average_price'):
                    c['average_price'] = original_avg_price
                
                modified.append(c)
                
    return {
        'added': added,
        'removed': removed,
        'modified': modified
    }

@app.route('/api/search_instruments')
def search_instruments():
    """Search instruments from master_contract table for autocomplete"""
    query = request.args.get('q', '').strip().upper()
    limit = request.args.get('limit', 20)
    
    try:
        limit = int(limit)
        limit = max(5, min(100, limit))
    except:
        limit = 20
    
    if not query:
        return jsonify({'results': []})
    
    conn = get_db()
    c = conn.cursor()
    
    # Search by trading_symbol or name
    results = c.execute("""
        SELECT instrument_token, trading_symbol, name, exchange, instrument_type, expiry, strike, lot_size
        FROM master_contract
        WHERE UPPER(trading_symbol) LIKE ? OR UPPER(name) LIKE ?
        ORDER BY 
            CASE WHEN UPPER(trading_symbol) LIKE ? THEN 1 ELSE 2 END,
            trading_symbol
        LIMIT ?
    """, (f'{query}%', f'%{query}%', f'{query}%', limit)).fetchall()
    
    instruments = []
    for row in results:
        instruments.append({
            'instrument_token': row['instrument_token'],
            'trading_symbol': row['trading_symbol'],
            'name': row['name'],
            'exchange': row['exchange'],
            'instrument_type': row['instrument_type'],
            'expiry': row['expiry'],
            'strike': row['strike'],
            'lot_size': row['lot_size'],
            'display': f"{row['trading_symbol']} ({row['exchange']}) - {row['name']}"
        })
    
    conn.close()
    return jsonify({'results': instruments})

@app.route('/api/get_index_instrument')
def get_index_instrument():
    """Get instrument token for an underlying index (e.g., NIFTY -> NIFTY 50)"""
    underlying = request.args.get('underlying', '').strip().upper()
    
    if not underlying:
        return jsonify({'success': False, 'error': 'underlying parameter is required'}), 400
    
    conn = get_db()
    c = conn.cursor()
    
    # Map underlying names to their index names in master_contract
    index_mapping = {
        'NIFTY': 'NIFTY 50',
        'BANKNIFTY': 'NIFTY BANK',
        'FINNIFTY': 'NIFTY FIN SERVICE',
        'MIDCPNIFTY': 'NIFTY MID SELECT',
        'SENSEX': 'SENSEX',
        'BANKEX': 'BANKEX',
    }
    
    # Get the index name to search for
    index_name = index_mapping.get(underlying, underlying)
    
    # Search for the index instrument
    # Prioritize NSE exchange, instrument_type = 'INDEX' or 'EQ'
    result = c.execute("""
        SELECT instrument_token, trading_symbol, name, exchange, instrument_type
        FROM master_contract
        WHERE (UPPER(name) = ? OR UPPER(trading_symbol) = ?)
        AND exchange IN ('NSE', 'BSE', 'INDICES')
        ORDER BY 
            CASE 
                WHEN instrument_type = 'INDEX' THEN 1
                WHEN exchange = 'NSE' AND instrument_type = 'EQ' THEN 2
                WHEN exchange = 'BSE' THEN 3
                ELSE 4
            END
        LIMIT 1
    """, (index_name, underlying)).fetchone()
    
    conn.close()
    
    if result:
        return jsonify({
            'success': True,
            'instrument_token': result['instrument_token'],
            'trading_symbol': result['trading_symbol'],
            'name': result['name'],
            'exchange': result['exchange'],
            'instrument_type': result['instrument_type']
        })
    else:
        return jsonify({
            'success': False,
            'error': f'No instrument found for underlying: {underlying}',
            'suggestion': 'Please ensure master contract is downloaded'
        }), 404

@app.route('/api/fetch_historical_data', methods=['POST'])
def fetch_historical_data():
    """Fetch historical data from Zerodha using enctoken"""
    try:
        import requests
        from datetime import datetime as dt
        
        data = request.get_json()
        enctoken = data.get('enctoken', '').strip() if isinstance(data.get('enctoken'), str) else str(data.get('enctoken', ''))
        instrument_token = str(data.get('instrument_token', ''))  # Convert to string (can be int or string)
        from_date = data.get('from_date', '').strip() if isinstance(data.get('from_date'), str) else str(data.get('from_date', ''))
        to_date = data.get('to_date', '').strip() if isinstance(data.get('to_date'), str) else str(data.get('to_date', ''))
        interval = data.get('interval', 'day').strip() if isinstance(data.get('interval', 'day'), str) else str(data.get('interval', 'day'))
        
        # Validation
        if not enctoken:
            return jsonify({'success': False, 'error': 'Enctoken is required'}), 400
        if not instrument_token:
            return jsonify({'success': False, 'error': 'Instrument token is required'}), 400
        if not from_date or not to_date:
            return jsonify({'success': False, 'error': 'Date range is required'}), 400
        
        # Validate date format (YYYY-MM-DD)
        try:
            dt.strptime(from_date, '%Y-%m-%d')
            dt.strptime(to_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
        
        # Construct URL (based on kite_trade.py)
        url = f"https://kite.zerodha.com/oms/instruments/historical/{instrument_token}/{interval}"
        params = {
            'from': from_date,
            'to': to_date
        }
        
        headers = {
            'Authorization': f'enctoken {enctoken}',
            'User-Agent': 'Mozilla/5.0'
        }
        
        print(f"Fetching historical data: {url} with params {params}")
        
        response = requests.get(url, params=params, headers=headers, timeout=30)
        
        if response.status_code == 403:
            return jsonify({'success': False, 'error': 'Invalid enctoken or unauthorized access'}), 403
        
        response.raise_for_status()
        
        result = response.json()
        
        # Extract candles data
        if 'data' in result and 'candles' in result['data']:
            candles = result['data']['candles']
            
            # Format candles for display
            formatted_data = []
            for candle in candles:
                # candle format: [timestamp, open, high, low, close, volume, oi]
                formatted_data.append({
                    'date': candle[0],  # ISO timestamp
                    'open': candle[1],
                    'high': candle[2],
                    'low': candle[3],
                    'close': candle[4],
                    'volume': candle[5] if len(candle) > 5 else 0
                })
            
            return jsonify({
                'success': True,
                'data': formatted_data,
                'count': len(formatted_data)
            })
        else:
            return jsonify({'success': False, 'error': 'No data found in response'}), 404
            
    except requests.exceptions.RequestException as e:
        print(f"Network error fetching historical data: {e}")
        return jsonify({'success': False, 'error': f'Network error: {str(e)}'}), 500
    except Exception as e:
        print(f"Error fetching historical data: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/download_master_contract', methods=['POST'])
def download_master_contract():
    """Download Zerodha master contract and save to database"""
    try:
        import requests
        import csv
        import io
        
        # Download instruments CSV from Zerodha
        url = 'https://api.kite.trade/instruments'
        print(f"Downloading master contract from {url}...")
        
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        # Parse CSV
        csv_content = response.text
        csv_reader = csv.DictReader(io.StringIO(csv_content))
        
        conn = get_db()
        c = conn.cursor()
        
        # Clear existing data
        c.execute("DELETE FROM master_contract")
        
        instruments_count = 0
        for row in csv_reader:
            try:
                # Extract fields with safe defaults
                instrument_token = int(row.get('instrument_token', 0))
                if instrument_token == 0:
                    continue
                    
                trading_symbol = row.get('tradingsymbol', '') or row.get('trading_symbol', '')
                exchange = row.get('exchange', '')
                name = row.get('name', '')
                expiry = row.get('expiry', '')
                strike = float(row.get('strike', 0) or 0)
                lot_size = int(row.get('lot_size', 0) or 0)
                instrument_type = row.get('instrument_type', '')
                
                # Insert into database
                c.execute('''
                    INSERT OR REPLACE INTO master_contract 
                    (instrument_token, trading_symbol, exchange, name, expiry, strike, lot_size, instrument_type, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (instrument_token, trading_symbol, exchange, name, expiry, strike, lot_size, instrument_type, datetime.now()))
                
                instruments_count += 1
            except Exception as e:
                print(f"Error processing row: {e}, row: {row}")
                continue
        
        conn.commit()
        conn.close()
        
        print(f"Successfully downloaded and saved {instruments_count} instruments")
        return jsonify({
            'success': True,
            'message': f'Successfully downloaded {instruments_count} instruments',
            'count': instruments_count
        })
        
    except requests.exceptions.RequestException as e:
        print(f"Network error downloading master contract: {e}")
        return jsonify({'success': False, 'error': f'Network error: {str(e)}'}), 500
    except Exception as e:
        print(f"Error downloading master contract: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/scraper-status')
def scraper_status():
    """Get current scraper status - running, stuck, or stopped"""
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Get last update timestamp
        last_updated_row = c.execute("SELECT MAX(timestamp) FROM latest_snapshots").fetchone()
        last_updated = last_updated_row[0] if last_updated_row else None
        
        conn.close()
        
        # Determine status
        is_market_hours = is_market_open()
        
        status = {
            'running': False,
            'market_open': is_market_hours,
            'last_updated': last_updated,
            'status_text': '',
            'time_since_update': None
        }
        
        if not is_market_hours:
            status['status_text'] = 'Market Closed - Scraper Paused'
            status['running'] = False
        elif last_updated:
            last_dt = to_datetime_filter(last_updated)
            time_diff = (datetime.now() - last_dt).total_seconds()
            status['time_since_update'] = time_diff
            
            if time_diff <= 180:  # Less than 3 minutes
                status['running'] = True
                status['status_text'] = 'Running'
            else:
                status['running'] = False
                status['status_text'] = f'Stuck or Stopped (Last update {int(time_diff/60)} mins ago)'
        else:
            status['running'] = False
            status['status_text'] = 'No data yet - Scraper may be starting'
        
        return jsonify(status)
        
    except Exception as e:
        return jsonify({
            'error': str(e),
            'running': False,
            'status_text': 'Error checking status'
        }), 500

@app.route('/restart', methods=['POST'])
def restart_scraper_endpoint():
    threading.Thread(target=restart_scraper_internal).start()
    return "Restarting scraper process... It should resume in a few seconds.", 200

@app.route('/delete_date/<date>', methods=['DELETE', 'POST'])
def delete_date(date):
    try:
        conn = get_db()
        c = conn.cursor()
        
        # 1. Delete position_changes for this date
        c.execute("DELETE FROM position_changes WHERE date(timestamp) = ?", (date,))
        changes_deleted = c.rowcount
        
        # 2. Delete snapshots for this date
        # Note: Be careful if snapshots are shared (unlikely in this design) or used by latest_snapshots
        # latest_snapshots is separate, so current state is preserved.
        c.execute("DELETE FROM snapshots WHERE date(timestamp) = ?", (date,))
        snaps_deleted = c.rowcount
        
        conn.commit()
        conn.close()
        
        print(f"Deleted data for {date}: {changes_deleted} changes, {snaps_deleted} snapshots.")
        return jsonify({'success': True, 'message': f"Deleted {changes_deleted} changes and {snaps_deleted} snapshots."})
        
    except Exception as e:
        print(f"Error deleting data for {date}: {e}")
        return jsonify({'error': str(e)}), 500

def signal_handler(sig, frame):
    print('\nShutting down gracefully...')
    sys.exit(0)

if __name__ == '__main__':
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start monitor thread
    threading.Thread(target=monitor_scraper, daemon=True).start()
    app.run(debug=False, host='0.0.0.0', port=PORT)
