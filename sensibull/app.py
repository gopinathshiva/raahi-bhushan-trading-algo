from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash
import sqlite3
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database import get_db, sync_profiles, now_ist, init_db
import sys
import os
import threading
import time
import subprocess
import signal

# IST Timezone constant
IST = ZoneInfo("Asia/Kolkata")

app = Flask(__name__)
# Configure secret key for sessions (generate a secure random key in production)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production-12345')
# Configure standard port or 5010 as per previous context
PORT = 6060

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'

# User class for Flask-Login
class AdminUser(UserMixin):
    def __init__(self, id, username, email, is_active):
        self.id = id
        self.username = username
        self.email = email
        self._is_active = is_active
    
    def get_id(self):
        return str(self.id)
    
    @property
    def is_active(self):
        """Override UserMixin is_active property"""
        return bool(self._is_active)

@login_manager.user_loader
def load_user(user_id):
    """Load user from database"""
    conn = get_db()
    c = conn.cursor()
    user = c.execute("""
        SELECT id, username, email, is_active 
        FROM admin_users 
        WHERE id = ? AND is_active = 1
    """, (user_id,)).fetchone()
    conn.close()
    
    if user:
        return AdminUser(user['id'], user['username'], user['email'], user['is_active'])
    return None

# In-memory cache for symbol suggestions (profile_id -> {ts, symbols})
SYMBOL_SUGGESTIONS_CACHE = {}
SYMBOL_SUGGESTIONS_TTL_SEC = 60

# Global variable to track last restart to prevent loops
LAST_AUTO_RESTART = None
# Global variable to track app start time for auto-start countdown
APP_START_TIME = None
SCRAPER_AUTO_START_DELAY = 30  # seconds

# Global flag to track if the daily master contract download failed
MASTER_CONTRACT_DOWNLOAD_FAILED = False

@app.template_filter('to_datetime')
def to_datetime_filter(value):
    # Timestamps in DB are stored in ISO format with IST timezone (e.g., "2026-02-16T10:30:00+05:30")
    # We parse them and return as timezone-aware datetime objects for accurate calculations
    if isinstance(value, datetime):
        # If datetime is naive (old data), assume it's IST
        if value.tzinfo is None:
            return value.replace(tzinfo=IST)
        return value
    
    try:
        # First try ISO format with timezone (new format)
        dt = datetime.fromisoformat(value)
        # Ensure it has timezone info
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt
    except (ValueError, AttributeError):
        try:
            # Fallback to old format without timezone (for backward compatibility)
            dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
            # Assume it's IST and add timezone info
            return dt.replace(tzinfo=IST)
        except:
            return value

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

def _do_download_master_contract():
    """Download Zerodha master contract and save to DB. Returns (success, message)."""
    global MASTER_CONTRACT_DOWNLOAD_FAILED
    try:
        import requests
        import csv
        import io

        url = 'https://api.kite.trade/instruments'
        print(f"[MasterContract] Downloading from {url}...")

        response = requests.get(url, timeout=30)
        response.raise_for_status()

        csv_content = response.text
        csv_reader = csv.DictReader(io.StringIO(csv_content))

        conn = get_db()
        c = conn.cursor()

        c.execute("DELETE FROM master_contract")

        instruments_count = 0
        for row in csv_reader:
            try:
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

                c.execute('''
                    INSERT OR REPLACE INTO master_contract
                    (instrument_token, trading_symbol, exchange, name, expiry, strike, lot_size, instrument_type, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (instrument_token, trading_symbol, exchange, name, expiry, strike, lot_size, instrument_type, now_ist()))

                instruments_count += 1
            except Exception as e:
                print(f"[MasterContract] Error processing row: {e}")
                continue

        conn.commit()
        conn.close()

        print(f"[MasterContract] Successfully downloaded {instruments_count} instruments.")
        MASTER_CONTRACT_DOWNLOAD_FAILED = False
        return True, f"Successfully downloaded {instruments_count} instruments"

    except Exception as e:
        print(f"[MasterContract] Download failed: {e}")
        MASTER_CONTRACT_DOWNLOAD_FAILED = True
        return False, str(e)


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

    # --- Daily master contract download (at startup, before scraper starts) ---
    try:
        conn = get_db()
        c = conn.cursor()
        row = c.execute("SELECT MAX(last_updated) as last_dl FROM master_contract").fetchone()
        conn.close()
        last_dl = row['last_dl'] if row else None

        today_str = now_ist().strftime('%Y-%m-%d')
        already_downloaded_today = last_dl and last_dl[:10] == today_str

        if already_downloaded_today:
            print(f"[MasterContract] Already downloaded today ({today_str}), skipping.")
        else:
            print(f"[MasterContract] Not downloaded today, starting download...")
            _do_download_master_contract()
    except Exception as e:
        print(f"[MasterContract] Error during startup download check: {e}")
        MASTER_CONTRACT_DOWNLOAD_FAILED = True

    # Wait for auto-start delay before first check
    print(f"Waiting {SCRAPER_AUTO_START_DELAY} seconds before auto-starting scraper...")
    time.sleep(SCRAPER_AUTO_START_DELAY)

    # Auto-start scraper on first run
    print("Auto-starting scraper...")
    restart_scraper_internal()
    LAST_AUTO_RESTART = now_ist()
    
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
                # Ensure both datetimes are timezone-aware for comparison
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=IST)
                time_diff = (now_ist() - last_dt).total_seconds()
                
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
                if LAST_AUTO_RESTART and (now_ist() - LAST_AUTO_RESTART).total_seconds() < 60:
                    print("Monitor: Skipping restart due to cooldown.")
                else:
                    print("Monitor: Triggering auto-restart.")
                    restart_scraper_internal()
                    LAST_AUTO_RESTART = now_ist()
                    
        except Exception as e:
            print(f"Monitor error: {e}")
            # Sleep a bit to avoid rapid loops on error
            time.sleep(10)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and handler"""
    # Redirect to index if already logged in
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        action = request.form.get('action', 'login')
        
        # Handle Registration
        if action == 'register':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            verification_code = request.form.get('verification_code', '').strip()
            
            # Validate inputs
            if not username or not password or not verification_code:
                return render_template('login.html', error='All fields are required for registration')
            
            # Check verification code
            if verification_code != '1083':
                return render_template('login.html', error='Invalid verification code')
            
            # Validate password length
            if len(password) < 6:
                return render_template('login.html', error='Password must be at least 6 characters long')
            
            # Check if username already exists
            conn = get_db()
            c = conn.cursor()
            
            existing_user = c.execute("""
                SELECT id FROM admin_users WHERE username = ?
            """, (username,)).fetchone()
            
            if existing_user:
                conn.close()
                return render_template('login.html', error=f'Username "{username}" already exists')
            
            # Create new admin user
            try:
                from werkzeug.security import generate_password_hash
                password_hash = generate_password_hash(password, method='pbkdf2:sha256')
                
                c.execute("""
                    INSERT INTO admin_users (username, password_hash, email, created_at, is_active)
                    VALUES (?, ?, ?, ?, 1)
                """, (username, password_hash, None, now_ist().isoformat()))
                
                conn.commit()
                conn.close()
                
                return render_template('login.html', success=f'Admin user "{username}" created successfully! You can now login.')
            
            except Exception as e:
                conn.close()
                return render_template('login.html', error=f'Error creating admin user: {str(e)}')
        
        # Handle Login
        else:
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            remember = request.form.get('remember') == 'yes'
            
            if not username or not password:
                return render_template('login.html', error='Username and password are required')
            
            conn = get_db()
            c = conn.cursor()
            user = c.execute("""
                SELECT id, username, password_hash, email, is_active 
                FROM admin_users 
                WHERE username = ?
            """, (username,)).fetchone()
            
            if user and user['is_active'] and check_password_hash(user['password_hash'], password):
                # Update last login
                c.execute("""
                    UPDATE admin_users 
                    SET last_login = ? 
                    WHERE id = ?
                """, (now_ist().isoformat(), user['id']))
                conn.commit()
                conn.close()
                
                # Create user object and login
                user_obj = AdminUser(user['id'], user['username'], user['email'], user['is_active'])
                login_user(user_obj, remember=remember, duration=timedelta(days=30))
                
                # Redirect to next page or index
                next_page = request.args.get('next')
                if next_page and next_page.startswith('/'):
                    return redirect(next_page)
                return redirect(url_for('index'))
            else:
                conn.close()
                return render_template('login.html', error='Invalid username or password', username=username)
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    """Logout handler"""
    logout_user()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('login'))

@app.route('/historical_data')
@login_required
def historical_data():
    return render_template('historical_data.html')

@app.route('/manage-profiles')
@login_required
def manage_profiles():
    """Profile management page"""
    return render_template('manage_profiles.html')

@app.route('/notifications/<slug>')
@login_required
def notifications(slug):
    """Notifications page for a profile"""
    conn = get_db()
    c = conn.cursor()
    
    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return "Profile not found", 404
    
    # Get the last snapshot date for this profile
    last_snapshot = c.execute("""
        SELECT date(timestamp) as snapshot_date 
        FROM snapshots 
        WHERE profile_id = ? 
        ORDER BY timestamp DESC 
        LIMIT 1
    """, (profile['id'],)).fetchone()
    
    # Default to today if no snapshots exist
    last_date = last_snapshot['snapshot_date'] if last_snapshot else now_ist().strftime('%Y-%m-%d')
    
    conn.close()
    return render_template('notifications.html', slug=slug, profile=profile, last_date=last_date)

@app.route('/')
@login_required
def index():
    conn = get_db()
    c = conn.cursor()
    
    # Get all active profiles, ordered by slug
    profiles = c.execute("""
        SELECT * FROM profiles 
        WHERE is_active = 1 
        ORDER BY slug
    """).fetchall()

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
            # Ensure both datetimes are timezone-aware for comparison
            current_time = now_ist()
            if last_dt.tzinfo is None:
                # If old data without timezone, assume it's IST
                last_dt = last_dt.replace(tzinfo=IST)
            if (current_time - last_dt).total_seconds() > 180:
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
    
    today_str = now_ist().strftime('%Y-%m-%d')
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
                after_booked = (curr_trade or {}).get('booked_profit_loss', 0) or 0
                
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
                    'after_booked_pnl': after_booked,
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
@login_required
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
                after_booked = (curr_trade or {}).get('booked_profit_loss', 0) or 0

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
                    'after_booked_pnl': after_booked,
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
            # NOTE: Some brokers put booked_profit_loss in the current snapshot (when qty=0)
            # rather than the previous snapshot, so we check curr_map[key] as well
            booked_pnl = p.get('booked_profit_loss', 0) or 0
            if booked_pnl == 0 and key in curr_map:
                # Check if current snapshot has booked_profit_loss (common when qty goes to 0)
                booked_pnl = curr_map[key].get('booked_profit_loss', 0) or 0

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
            p['booked_pnl'] = exit_pnl  # Add booked_pnl field for consistency

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

                # --- Calculate Booked P&L for quantity reductions ---
                # When position is reduced, calculate the P&L on the closed portion.
                # This is the actual profit/loss booked by reducing the position size.
                #
                # For long positions (qty > 0): reducing means selling some quantity
                # For short positions (qty < 0): reducing means buying back some quantity
                #
                # We detect reduction as:
                # - Long position: old_qty > 0 and new_qty < old_qty (sold some)
                # - Short position: old_qty < 0 and new_qty > old_qty (bought back some, i.e., abs(new_qty) < abs(old_qty))
                
                q0 = p.get('quantity', 0) or 0
                q1 = c.get('quantity', 0) or 0
                is_reducing = (q0 > 0 and q1 < q0) or (q0 < 0 and q1 > q0)
                
                if is_reducing:
                    # First check if broker provided booked_profit_loss directly
                    # Some brokers provide this field when positions are reduced
                    broker_booked_pnl = c.get('booked_profit_loss', 0) or 0
                    
                    if broker_booked_pnl != 0:
                        # Use broker-provided booked P&L (most accurate)
                        c['booked_pnl'] = broker_booked_pnl
                        c['exit_pnl'] = broker_booked_pnl
                        # Calculate exit price from booked P&L if possible
                        closed_qty = abs(q0 - q1)
                        entry_price = original_avg_price if original_avg_price not in (None, 0) else (p.get('average_price', 0) or 0)
                        if closed_qty and entry_price:
                            if q0 > 0:
                                # Long: booked_pnl = (exit - entry) * qty
                                exit_price = (broker_booked_pnl / closed_qty) + entry_price
                            else:
                                # Short: booked_pnl = (entry - exit) * qty
                                exit_price = entry_price - (broker_booked_pnl / closed_qty)
                            c['exit_price'] = exit_price
                            c['implied_fill_price'] = exit_price  # Update for consistency
                        else:
                            c['exit_price'] = c.get('implied_fill_price', 0) or 0
                    else:
                        # Calculate booked P&L using the implied fill price
                        # Quantity that was closed (always positive for calculation)
                        closed_qty = abs(q0 - q1)
                        
                        # Original entry price (from history or previous snapshot)
                        entry_price = original_avg_price if original_avg_price not in (None, 0) else (p.get('average_price', 0) or 0)
                        
                        # Exit price is the implied fill price
                        # Special case: If position is fully exited (q1 == 0) and implied fill price is same as entry
                        # (broker didn't update avg), use LTP as best approximation
                        exit_price = c.get('implied_fill_price', 0) or 0
                        
                        # If fully exited and exit_price equals entry_price (broker didn't update), use LTP
                        if q1 == 0 and exit_price != 0 and abs(exit_price - entry_price) < 0.01:
                            # Use LTP as exit price instead
                            ltp = c.get('last_price', 0) or p.get('last_price', 0) or 0
                            if ltp > 0:
                                exit_price = ltp
                                c['implied_fill_price'] = ltp  # Update for display consistency
                        
                        if entry_price and exit_price and closed_qty:
                            # For long positions (q0 > 0): Booked P&L = (Exit - Entry) * Qty
                            # For short positions (q0 < 0): Booked P&L = (Entry - Exit) * Qty
                            # Note: Since q0 is signed, (exit_price - entry_price) * q0 handles both cases
                            # But we're closing abs(q0-q1) contracts, so we need to be explicit:
                            if q0 > 0:
                                # Long position being reduced (selling)
                                booked_pnl = (exit_price - entry_price) * closed_qty
                            else:
                                # Short position being reduced (buying back)
                                booked_pnl = (entry_price - exit_price) * closed_qty
                            
                            c['booked_pnl'] = booked_pnl
                            c['exit_pnl'] = booked_pnl  # Keep exit_pnl for backward compatibility
                            c['exit_price'] = exit_price
                        else:
                            c['booked_pnl'] = 0
                            c['exit_pnl'] = 0
                            c['exit_price'] = 0
                else:
                    # Position increased (added more), no booked P&L
                    c['booked_pnl'] = 0
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
@login_required
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
@login_required
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
@login_required
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
@login_required
def download_master_contract():
    """Download Zerodha master contract and save to database"""
    success, message = _do_download_master_contract()
    if success:
        # Re-query count for the response
        conn = get_db()
        c = conn.cursor()
        count = c.execute("SELECT COUNT(*) FROM master_contract").fetchone()[0]
        conn.close()
        return jsonify({'success': True, 'message': message, 'count': count})
    else:
        return jsonify({'success': False, 'error': message}), 500


@app.route('/api/master-contract-status')
@login_required
def master_contract_status():
    """Return whether the daily master contract download failed at startup"""
    return jsonify({'failed': MASTER_CONTRACT_DOWNLOAD_FAILED})

@app.route('/api/scraper-status')
@login_required
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
            'time_since_update': None,
            'auto_start_in': None  # New field for countdown
        }
        
        # Check if we're in the auto-start waiting period
        if APP_START_TIME:
            elapsed_since_start = (now_ist() - APP_START_TIME).total_seconds()
            if elapsed_since_start < SCRAPER_AUTO_START_DELAY:
                # Still waiting for auto-start
                remaining = int(SCRAPER_AUTO_START_DELAY - elapsed_since_start)
                status['auto_start_in'] = remaining
                status['status_text'] = f'Auto-starting in {remaining} seconds...'
                status['running'] = False
                return jsonify(status)
        
        if not is_market_hours:
            status['status_text'] = 'Market Closed - Scraper Paused'
            status['running'] = False
        elif last_updated:
            last_dt = to_datetime_filter(last_updated)
            # Ensure both datetimes are timezone-aware for comparison
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=IST)
            time_diff = (now_ist() - last_dt).total_seconds()
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
@login_required
def restart_scraper_endpoint():
    threading.Thread(target=restart_scraper_internal).start()
    return "Restarting scraper process... It should resume in a few seconds.", 200

@app.route('/delete_date/<date>', methods=['DELETE', 'POST'])
@login_required
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

# ==================== SUBSCRIPTION API ROUTES ====================

@app.route('/api/subscriptions', methods=['GET'])
@login_required
def get_subscriptions():
    """Get all subscriptions for a profile"""
    try:
        profile_slug = request.args.get('profile_slug')
        if not profile_slug:
            return jsonify({'error': 'profile_slug is required'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Get profile_id
        profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (profile_slug,)).fetchone()
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        profile_id = profile['id']
        
        # Get all subscriptions
        subscriptions = c.execute("""
            SELECT id, subscription_type, underlying, expiry, position_identifier, created_at
            FROM subscriptions
            WHERE profile_id = ?
            ORDER BY created_at DESC
        """, (profile_id,)).fetchall()
        
        conn.close()
        
        result = []
        for sub in subscriptions:
            result.append({
                'id': sub['id'],
                'subscription_type': sub['subscription_type'],
                'underlying': sub['underlying'],
                'expiry': sub['expiry'],
                'position_identifier': json.loads(sub['position_identifier']) if sub['position_identifier'] else None,
                'created_at': sub['created_at']
            })
        
        return jsonify({'success': True, 'subscriptions': result})
        
    except Exception as e:
        print(f"Error getting subscriptions: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/subscriptions/subscribe', methods=['POST'])
@login_required
def subscribe():
    """Create a new subscription"""
    try:
        data = request.get_json()
        profile_slug = data.get('profile_slug')
        subscription_type = data.get('subscription_type')  # 'underlying', 'expiry', or 'position'
        underlying = data.get('underlying')
        expiry = data.get('expiry')
        position_identifier = data.get('position_identifier')  # dict with symbol, product, strike, option_type
        
        if not profile_slug or not subscription_type:
            return jsonify({'error': 'profile_slug and subscription_type are required'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Get profile_id
        profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (profile_slug,)).fetchone()
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        profile_id = profile['id']
        
        # Validate subscription type
        if subscription_type == 'underlying' and not underlying:
            return jsonify({'error': 'underlying is required for underlying subscription'}), 400
        elif subscription_type == 'expiry' and (not underlying or not expiry):
            return jsonify({'error': 'underlying and expiry are required for expiry subscription'}), 400
        elif subscription_type == 'position' and not position_identifier:
            return jsonify({'error': 'position_identifier is required for position subscription'}), 400
        
        # Convert position_identifier to JSON string
        position_id_str = json.dumps(position_identifier) if position_identifier else None
        
        # Insert subscription (UNIQUE constraint will prevent duplicates)
        try:
            c.execute("""
                INSERT INTO subscriptions (profile_id, subscription_type, underlying, expiry, position_identifier, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (profile_id, subscription_type, underlying, expiry, position_id_str, now_ist().isoformat()))
            conn.commit()
            subscription_id = c.lastrowid
            conn.close()
            
            return jsonify({'success': True, 'message': 'Subscription created successfully', 'subscription_id': subscription_id})
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'success': False, 'message': 'Already subscribed to this item'})
        
    except Exception as e:
        print(f"Error creating subscription: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/subscriptions/unsubscribe', methods=['POST'])
@login_required
def unsubscribe():
    """Delete a subscription"""
    try:
        data = request.get_json()
        subscription_id = data.get('subscription_id')
        
        if not subscription_id:
            return jsonify({'error': 'subscription_id is required'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Delete subscription
        c.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        
        if deleted > 0:
            return jsonify({'success': True, 'message': 'Unsubscribed successfully'})
        else:
            return jsonify({'success': False, 'message': 'Subscription not found'})
        
    except Exception as e:
        print(f"Error unsubscribing: {e}")
        return jsonify({'error': str(e)}), 500

# ==================== NOTIFICATION API ROUTES ====================

@app.route('/api/notifications', methods=['GET'])
@login_required
def get_notifications():
    """Get all notifications for a profile"""
    try:
        profile_slug = request.args.get('profile_slug')
        if not profile_slug:
            return jsonify({'error': 'profile_slug is required'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Get profile_id
        profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (profile_slug,)).fetchone()
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        profile_id = profile['id']
        
        # Get all notifications
        notifications = c.execute("""
            SELECT id, subscription_id, message, notification_type, notification_data, created_at, is_read
            FROM notifications
            WHERE profile_id = ?
            ORDER BY created_at DESC
        """, (profile_id,)).fetchall()
        
        conn.close()
        
        result = []
        for notif in notifications:
            result.append({
                'id': notif['id'],
                'subscription_id': notif['subscription_id'],
                'message': notif['message'],
                'notification_type': notif['notification_type'],
                'notification_data': json.loads(notif['notification_data']) if notif['notification_data'] else None,
                'created_at': notif['created_at'],
                'is_read': notif['is_read'] == 1
            })
        
        return jsonify({'success': True, 'notifications': result})
        
    except Exception as e:
        print(f"Error getting notifications: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/notifications/unread_count', methods=['GET'])
@login_required
def get_unread_count():
    """Get count of unread notifications for a profile"""
    try:
        profile_slug = request.args.get('profile_slug')
        if not profile_slug:
            return jsonify({'error': 'profile_slug is required'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Get profile_id
        profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (profile_slug,)).fetchone()
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        profile_id = profile['id']
        
        # Get unread count
        count = c.execute("""
            SELECT COUNT(*) as count
            FROM notifications
            WHERE profile_id = ? AND is_read = 0
        """, (profile_id,)).fetchone()
        
        conn.close()
        
        return jsonify({'success': True, 'unread_count': count['count']})
        
    except Exception as e:
        print(f"Error getting unread count: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/notifications/mark_read', methods=['POST'])
@login_required
def mark_notification_read():
    """Mark notification(s) as read"""
    try:
        data = request.get_json()
        notification_id = data.get('notification_id')
        mark_all = data.get('mark_all', False)
        profile_slug = data.get('profile_slug')
        
        conn = get_db()
        c = conn.cursor()
        
        if mark_all:
            if not profile_slug:
                return jsonify({'error': 'profile_slug is required for mark_all'}), 400
            
            # Get profile_id
            profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (profile_slug,)).fetchone()
            if not profile:
                return jsonify({'error': 'Profile not found'}), 404
            profile_id = profile['id']
            
            c.execute("UPDATE notifications SET is_read = 1 WHERE profile_id = ?", (profile_id,))
        else:
            if not notification_id:
                return jsonify({'error': 'notification_id is required'}), 400
            
            c.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (notification_id,))
        
        updated = c.rowcount
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'updated': updated})
        
    except Exception as e:
        print(f"Error marking notification as read: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/notifications/delete', methods=['POST'])
@login_required
def delete_notification():
    """Delete notification(s)"""
    try:
        data = request.get_json()
        notification_id = data.get('notification_id')
        
        if not notification_id:
            return jsonify({'error': 'notification_id is required'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        c.execute("DELETE FROM notifications WHERE id = ?", (notification_id,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        
        if deleted > 0:
            return jsonify({'success': True, 'message': 'Notification deleted successfully'})
        else:
            return jsonify({'success': False, 'message': 'Notification not found'})
        
    except Exception as e:
        print(f"Error deleting notification: {e}")
        return jsonify({'error': str(e)}), 500

# ==================== USER PREFERENCES API ROUTES ====================

@app.route('/api/preferences', methods=['GET'])
@login_required
def get_preferences():
    """Get user preferences for a profile"""
    try:
        profile_slug = request.args.get('profile_slug')
        if not profile_slug:
            return jsonify({'error': 'profile_slug is required'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Get profile_id
        profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (profile_slug,)).fetchone()
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        profile_id = profile['id']
        
        # Get preferences
        prefs = c.execute("""
            SELECT notification_sound
            FROM user_preferences
            WHERE profile_id = ?
        """, (profile_id,)).fetchone()
        
        conn.close()
        
        if prefs:
            return jsonify({
                'success': True,
                'preferences': {
                    'notification_sound': prefs['notification_sound']
                }
            })
        else:
            # Return default preferences
            return jsonify({
                'success': True,
                'preferences': {
                    'notification_sound': 'default'
                }
            })
        
    except Exception as e:
        print(f"Error getting preferences: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/preferences/update', methods=['POST'])
@login_required
def update_preferences():
    """Update user preferences for a profile"""
    try:
        data = request.get_json()
        profile_slug = data.get('profile_slug')
        notification_sound = data.get('notification_sound')
        
        if not profile_slug:
            return jsonify({'error': 'profile_slug is required'}), 400
        
        conn = get_db()
        c = conn.cursor()
        
        # Get profile_id
        profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (profile_slug,)).fetchone()
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        profile_id = profile['id']
        
        # Upsert preferences
        c.execute("""
            INSERT INTO user_preferences (profile_id, notification_sound, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                notification_sound = excluded.notification_sound,
                updated_at = excluded.updated_at
        """, (profile_id, notification_sound, now_ist().isoformat(), now_ist().isoformat()))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Preferences updated successfully'})
        
    except Exception as e:
        print(f"Error updating preferences: {e}")
        return jsonify({'error': str(e)}), 500

def cleanup_port():
    """Kill any process using port 6060"""
    try:
        print(f"Cleaning up port {PORT}...")
        # Find and kill process using the port
        result = subprocess.run(
            ['lsof', '-ti', f':{PORT}'],
            capture_output=True,
            text=True
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    print(f"Killed process {pid} using port {PORT}")
                except:
                    pass
    except Exception as e:
        print(f"Error cleaning up port: {e}")

def signal_handler(sig, frame):
    print('\nShutting down gracefully...')
    cleanup_port()
    sys.exit(0)

@app.route('/api/profiles')
@login_required
def api_get_profiles():
    """Get all profiles with stats"""
    conn = get_db()
    c = conn.cursor()
    
    profiles = c.execute("""
        SELECT 
            p.id,
            p.slug,
            p.name,
            p.source_url,
            p.is_active,
            p.added_at,
            (SELECT MAX(timestamp) FROM snapshots WHERE profile_id = p.id) as last_scraped,
            (SELECT COUNT(*) FROM snapshots WHERE profile_id = p.id) as snapshot_count,
            (SELECT COUNT(*) FROM notifications WHERE profile_id = p.id AND is_read = 0) as unread_notifications
        FROM profiles p
        ORDER BY p.slug
    """).fetchall()
    
    conn.close()
    
    return jsonify({
        'success': True,
        'profiles': [dict(p) for p in profiles]
    })


@app.route('/api/profiles/validate', methods=['POST'])
@login_required
def api_validate_profile():
    """Validate if a profile exists on Sensibull"""
    data = request.json
    slug = data.get('slug', '').strip()
    
    if not slug:
        return jsonify({'success': False, 'error': 'Username is required'}), 400
    
    # Check if profile already exists in database
    conn = get_db()
    c = conn.cursor()
    existing = c.execute("SELECT id FROM profiles WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    
    if existing:
        return jsonify({'success': False, 'error': 'Profile already exists'}), 400
    
    # Validate on Sensibull
    import requests
    url = f"https://web.sensibull.com/portfolio/positions?username={slug}"
    
    try:
        response = requests.head(url, timeout=10, allow_redirects=True)
        if response.status_code == 200:
            return jsonify({'success': True, 'message': 'Profile exists', 'url': url})
        else:
            return jsonify({'success': False, 'error': 'Profile not found on Sensibull'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': f'Validation failed: {str(e)}'}), 500


@app.route('/api/profiles/add', methods=['POST'])
@login_required
def api_add_profile():
    """Add a new profile"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON data'}), 400
    
    slug = data.get('slug', '').strip()
    
    if not slug:
        return jsonify({'success': False, 'error': 'Username is required'}), 400
    
    # Validate slug format (alphanumeric and hyphens only)
    import re
    if not re.match(r'^[a-z0-9-]+$', slug):
        return jsonify({'success': False, 'error': 'Invalid username format'}), 400
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        # Construct URL
        url = f"https://web.sensibull.com/portfolio/positions?username={slug}"
        name = slug.replace('-', ' ').title()
        
        c.execute("""
            INSERT INTO profiles (slug, name, url, source_url, is_active, added_at)
            VALUES (?, ?, ?, ?, 1, datetime('now'))
        """, (slug, name, url, url))
        
        conn.commit()
        profile_id = c.lastrowid
        
        profile = c.execute("""
            SELECT id, slug, name, source_url, is_active, added_at
            FROM profiles WHERE id = ?
        """, (profile_id,)).fetchone()
        
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Profile added successfully',
            'profile': dict(profile)
        })
        
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'error': 'Profile already exists'}), 400
    except Exception as e:
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/profiles/<int:profile_id>/toggle', methods=['POST'])
@login_required
def api_toggle_profile(profile_id):
    """Toggle profile active status"""
    conn = get_db()
    c = conn.cursor()
    
    profile = c.execute("SELECT is_active FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'success': False, 'error': 'Profile not found'}), 404
    
    new_status = 0 if profile['is_active'] else 1
    c.execute("UPDATE profiles SET is_active = ? WHERE id = ?", (new_status, profile_id))
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'message': 'Profile ' + ('enabled' if new_status else 'disabled'),
        'is_active': new_status
    })


@app.route('/api/profiles/<int:profile_id>', methods=['DELETE'])
@login_required
def api_delete_profile(profile_id):
    """Delete profile (soft or hard delete)"""
    soft = request.args.get('soft', 'false').lower() == 'true'
    
    conn = get_db()
    c = conn.cursor()
    
    profile = c.execute("SELECT slug FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'success': False, 'error': 'Profile not found'}), 404
    
    if soft:
        # Soft delete - just disable
        c.execute("UPDATE profiles SET is_active = 0 WHERE id = ?", (profile_id,))
        message = 'Profile disabled'
    else:
        # Hard delete - remove completely
        c.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        message = 'Profile deleted permanently'
    
    conn.commit()
    conn.close()
    
    return jsonify({
        'success': True,
        'message': message
    })


# ============================================================================
# EXPORT / IMPORT ROUTES
# ============================================================================

@app.route('/export')
@login_required
def export_page():
    """Export page - shows UI for database export"""
    return render_template('export.html')

@app.route('/export/download')
@login_required
def export_download():
    """Download the database file"""
    from flask import send_file
    from database import DB_PATH
    
    if not os.path.exists(DB_PATH):
        flash('Database file not found!', 'error')
        return redirect(url_for('export_page'))
    
    return send_file(
        DB_PATH,
        as_attachment=True,
        download_name='sensibull.db',
        mimetype='application/x-sqlite3'
    )

@app.route('/import', methods=['GET'])
@login_required
def import_page():
    """Import page - shows UI for database import"""
    return render_template('import.html')

@app.route('/import/upload', methods=['POST'])
@login_required
def import_upload():
    """Handle database file upload and replacement"""
    from werkzeug.utils import secure_filename
    from database import DB_PATH
    import shutil
    
    if 'database_file' not in request.files:
        return render_template('import.html', error='No file uploaded')
    
    file = request.files['database_file']
    
    if file.filename == '':
        return render_template('import.html', error='No file selected')
    
    if not file.filename.endswith('.db'):
        return render_template('import.html', error='Only .db files are allowed')
    
    try:
        # Save uploaded file to a temporary location
        temp_path = os.path.join(os.path.dirname(DB_PATH), 'tmp_upload.db')
        file.save(temp_path)
        
        # Verify it's a valid SQLite database
        try:
            conn = sqlite3.connect(temp_path)
            conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
            conn.close()
        except Exception as e:
            os.remove(temp_path)
            return render_template('import.html', error=f'Invalid database file: {str(e)}')
        
        # Backup existing database (if it exists)
        if os.path.exists(DB_PATH):
            backup_path = DB_PATH + '.backup'
            shutil.copy2(DB_PATH, backup_path)
        
        # Replace the database
        shutil.move(temp_path, DB_PATH)
        
        return render_template('import.html', success=True)
        
    except Exception as e:
        return render_template('import.html', error=f'Import failed: {str(e)}')

# ============================================================================
# END EXPORT / IMPORT ROUTES
# ============================================================================

if __name__ == '__main__':
    # Initialize database (create tables if they don't exist)
    init_db()
    
    # Clean up port before starting (in case of unclean shutdown)
    cleanup_port()
    time.sleep(0.5)  # Give OS time to release the port
    
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Record app start time for auto-start countdown
    APP_START_TIME = now_ist()
    
    # Start monitor thread
    threading.Thread(target=monitor_scraper, daemon=True).start()
    
    try:
        app.run(debug=False, host='0.0.0.0', port=PORT)
    finally:
        # Ensure cleanup on exit
        cleanup_port()
