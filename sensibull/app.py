from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from brokerage import calculate_option_brokerage, get_exchange_for_underlying
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash
import sqlite3
import json
import requests as http_requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from database import get_db, sync_profiles, now_ist, init_db
import sys
import os
import threading
import re
import calendar
from urllib.parse import urlparse
from typing import Callable, Optional
from dotenv import load_dotenv
load_dotenv()
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

# Holiday API config (use {year} placeholder, e.g. https://example.com/holidays/{year})
MARKET_HOLIDAYS_API_URL = os.environ.get('MARKET_HOLIDAYS_API_URL', '').strip()
MARKET_HOLIDAYS_API_KEY = os.environ.get('MARKET_HOLIDAYS_API_KEY', '').strip()
OPENALGO_HOLIDAYS_PATH = os.environ.get('OPENALGO_HOLIDAYS_PATH', '/api/v1/holidays').strip() or '/api/v1/holidays'
MARKET_HOLIDAY_CACHE = {}
MARKET_HOLIDAY_CACHE_LOCK = threading.Lock()

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

@app.template_filter('fmt_inr')
def fmt_inr_filter(value, decimals=1):
    """Format a number in the Indian numbering system (e.g. 12,34,567.0)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = '-' if value < 0 else ''
    abs_val = abs(value)
    int_part = int(abs_val)
    dec_val = round(abs_val - int_part, decimals)
    dec_str = (f'.{int(round(dec_val * (10 ** decimals))):0{decimals}d}') if decimals > 0 else ''
    s = str(int_part)
    if len(s) <= 3:
        formatted = s
    else:
        formatted = s[-3:]
        s = s[:-3]
        while s:
            formatted = s[-2:] + ',' + formatted
            s = s[:-2]
    return sign + formatted + dec_str


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

@app.route('/openalgo')
@login_required
def openalgo_profiles_page():
    """OpenAlgo profile management page"""
    return render_template('openalgo.html')

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


def _compute_exit_metrics(before_trade, closed_qty=None, exit_price_override=None):
    """Compute best-effort exit price/pnl, aligned with calculate_diff() logic.

    closed_qty: quantity actually closed (for partial exits). If None, uses the
                full before_qty (appropriate for full exits).
    exit_price_override: use this as the exit price (e.g. implied_fill_price)
                         instead of falling back to the previous LTP.
    """
    if not before_trade:
        return 0, 0

    qty = before_trade.get('quantity', 0) or 0
    avg_price = before_trade.get('average_price', 0) or 0
    last_price = before_trade.get('last_price', 0) or 0

    exit_price = exit_price_override if exit_price_override else (last_price or 0)

    # For full exits only: prefer broker-provided booked P&L if available.
    # We intentionally skip this for partial exits (closed_qty is not None)
    # because broker's booked_profit_loss is cumulative and would be wrong here.
    if closed_qty is None:
        booked_pnl = before_trade.get('booked_profit_loss', 0) or 0
        if booked_pnl != 0:
            return booked_pnl, exit_price

    qty_for_pnl = abs(closed_qty if closed_qty is not None else qty)

    if avg_price and exit_price and qty_for_pnl:
        if qty > 0:
            exit_pnl = (exit_price - avg_price) * qty_for_pnl   # Long
        else:
            exit_pnl = (avg_price - exit_price) * qty_for_pnl   # Short
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


def _calculate_event_brokerage(evt, underlying):
    """Calculate brokerage for a single trade event (ENTERED/MODIFIED/EXITED)."""
    event_type = evt.get('type', '')
    exchange = get_exchange_for_underlying(underlying)

    if event_type == 'ENTERED':
        after_qty = evt.get('after_quantity') or 0
        qty = abs(after_qty)
        premium = evt.get('after_average_price') or 0
        tx_type = 'BUY' if after_qty > 0 else 'SELL'
    elif event_type == 'MODIFIED':
        qty = abs(evt.get('implied_fill_qty') or 0)
        premium = evt.get('implied_fill_price') or 0
        tx_type = (evt.get('implied_fill_side') or 'BUY').upper()
        if tx_type not in ('BUY', 'SELL'):
            tx_type = 'BUY'
    elif event_type == 'EXITED':
        before_qty = evt.get('before_quantity') or 0
        qty = abs(before_qty)
        premium = evt.get('exit_price') or 0
        tx_type = 'SELL' if before_qty > 0 else 'BUY'
    else:
        return 0.0

    if qty == 0 or premium == 0:
        return 0.0

    return calculate_option_brokerage(
        premium=premium,
        lot_size=qty,
        transaction_type=tx_type,
        exchange=exchange,
    )


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
    date_filter = (request.args.get('date') or '').strip()  # e.g. '2026-03-13'

    conn = get_db()
    c = conn.cursor()

    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found', 'underlyings': {}}), 404

    # Get snapshots for this profile in chronological order, optionally filtered by date
    if date_filter:
        snapshots = c.execute(
            "SELECT id, timestamp, raw_data FROM snapshots WHERE profile_id = ? AND date(timestamp) = ? ORDER BY id ASC",
            (profile['id'], date_filter),
        ).fetchall()
    else:
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
    latest_underlying_price = {}  # underlying -> latest ltp seen in snapshots
    actual_expiry_map = {}        # trading_symbol -> actual expiry (YYYY-MM-DD) from instrument_info

    for snap in snapshots:
        snap_id = snap['id']
        timestamp = snap['timestamp']
        change_id = snapshot_to_change_id.get(snap_id)

        try:
            raw = json.loads(snap['raw_data'])
        except Exception:
            continue

        # Track latest underlying_price and actual instrument_info expiry per symbol
        for item in raw.get('data', []):
            sym = (item.get('trading_symbol') or '').upper()
            price = item.get('underlying_price')
            if sym and price is not None:
                latest_underlying_price[sym] = price
            for trade in item.get('trades', []):
                tsym = trade.get('trading_symbol', '')
                actual_expiry = (trade.get('instrument_info') or {}).get('expiry', '')
                if tsym and actual_expiry:
                    actual_expiry_map[tsym] = actual_expiry

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
                    # For positions that expired (broker removes them without providing booked P&L),
                    # fall back to computed exit metrics so display and group stats are correct.
                    if after_booked == 0 and exit_pnl != 0:
                        after_booked = exit_pnl
                    if after_ltp == 0 and exit_price != 0:
                        after_ltp = exit_price

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
                        closed_qty = abs(before_qty - after_qty)
                        exit_pnl, exit_price = _compute_exit_metrics(
                            prev_trade,
                            closed_qty=closed_qty,
                            exit_price_override=implied_fill_price if implied_fill_price else None,
                        )

                underlying_events[underlying].append({
                    'timestamp': timestamp,
                    'snapshot_id': snap_id,
                    'change_id': change_id,
                    'type': event_type,
                    'symbol': symbol,
                    'product': product,
                    'expiry_date': actual_expiry_map.get(symbol) or _parse_expiry_from_symbol(symbol),
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

    # Bulk-lookup lot sizes from master_contract and inject into each event
    all_symbols = set(
        evt['symbol']
        for evts in underlying_events.values()
        for evt in evts
    )
    lot_size_map = {}
    if all_symbols:
        placeholders = ','.join('?' * len(all_symbols))
        rows = c.execute(
            f"SELECT trading_symbol, lot_size FROM master_contract WHERE trading_symbol IN ({placeholders})",
            list(all_symbols),
        ).fetchall()
        for row in rows:
            lot_size_map[row['trading_symbol']] = row['lot_size']

    # Fallback: for old symbols not found in master_contract, look up any current
    # contract for the same underlying to get its lot size.
    missing_underlyings = set(
        underlying
        for underlying, evts in underlying_events.items()
        for evt in evts
        if not lot_size_map.get(evt['symbol'])
    )
    fallback_lot_size_map = {}
    for underlying in missing_underlyings:
        row = c.execute(
            "SELECT lot_size FROM master_contract WHERE trading_symbol LIKE ? AND lot_size > 0 ORDER BY expiry DESC LIMIT 1",
            (underlying + '%',),
        ).fetchone()
        if row:
            fallback_lot_size_map[underlying] = row['lot_size']

    for underlying, evts in underlying_events.items():
        for evt in evts:
            evt['lot_size'] = lot_size_map.get(evt['symbol']) or fallback_lot_size_map.get(underlying, 0)
            evt['brokerage'] = _calculate_event_brokerage(evt, underlying)

    # Compute snapshot-accurate Net P&L from the final state (last_trade from latest snapshot).
    # This matches the Daily Timeline's Current P&L calculation which sums unbooked_pnl +
    # booked_profit_loss directly from the latest snapshot, not from change events.
    snapshot_booked = 0.0
    snapshot_unbooked = 0.0
    for st in state.values():
        if st.get('present') and st.get('last_trade'):
            t = st['last_trade']
            snapshot_booked += t.get('booked_profit_loss', 0) or 0
            snapshot_unbooked += t.get('unbooked_pnl', 0) or 0

    # Determine prev-day close prices from the last snapshot of the previous trading day.
    # "Previous day" relative to the date being viewed (date_filter) or today if no filter.
    prev_underlying_price = {}
    try:
        if date_filter:
            view_date = date.fromisoformat(date_filter)
        else:
            view_date = now_ist().date()
        # Walk back up to 7 days to skip weekends/holidays
        prev_date = view_date - timedelta(days=1)
        for _ in range(7):
            prev_snap_row = c.execute(
                "SELECT raw_data FROM snapshots WHERE profile_id = ? AND date(timestamp) = ? ORDER BY id DESC LIMIT 1",
                (profile['id'], prev_date.isoformat()),
            ).fetchone()
            if prev_snap_row:
                try:
                    prev_raw = json.loads(prev_snap_row['raw_data'])
                    for item in prev_raw.get('data', []):
                        sym = (item.get('trading_symbol') or '').upper()
                        price = item.get('underlying_price')
                        if sym and price is not None:
                            prev_underlying_price[sym] = price
                except Exception:
                    pass
                break
            prev_date -= timedelta(days=1)
    except Exception:
        pass

    # Build underlying_prices: {symbol -> {ltp, prev_close, pct_change}}
    underlying_prices = {}
    for sym, ltp in latest_underlying_price.items():
        prev_close = prev_underlying_price.get(sym)
        pct_change = None
        if prev_close and prev_close != 0:
            pct_change = round((ltp - prev_close) / prev_close * 100, 2)
        underlying_prices[sym] = {
            'ltp': ltp,
            'prev_close': prev_close,
            'pct_change': pct_change,
        }

    conn.close()

    return jsonify({
        'profile': {'id': profile['id'], 'slug': profile['slug'], 'name': profile['name']},
        'underlyings': sorted_underlyings,
        'events': underlying_events,
        'filter': underlying_filter or None,
        'snapshot_booked_pnl': snapshot_booked,
        'snapshot_unbooked_pnl': snapshot_unbooked,
        'snapshot_net_pnl': snapshot_booked + snapshot_unbooked,
        'underlying_prices': underlying_prices,
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
                    # For positions that expired (broker removes them without providing booked P&L),
                    # fall back to computed exit metrics so display and group stats are correct.
                    if after_booked == 0 and exit_pnl != 0:
                        after_booked = exit_pnl
                    if after_ltp == 0 and exit_price != 0:
                        after_ltp = exit_price

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
                        closed_qty = abs(before_qty - after_qty)
                        exit_pnl, exit_price = _compute_exit_metrics(
                            prev_trade,
                            closed_qty=closed_qty,
                            exit_price_override=implied_fill_price if implied_fill_price else None,
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
                    'expiry_date': _parse_expiry_from_symbol(symbol_norm),
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

def _fetch_underlying_events(c, profile_id, underlying_filter):
    """Fetch all trade lifecycle events for a specific underlying. Returns {underlying: [events...]}."""
    import re

    def _get_underlying(trading_symbol):
        if not trading_symbol:
            return None
        m = re.match(r'^[a-zA-Z&-]+', trading_symbol)
        return m.group(0).upper() if m else None

    snapshots = c.execute(
        "SELECT id, timestamp, raw_data FROM snapshots WHERE profile_id = ? ORDER BY id ASC",
        (profile_id,),
    ).fetchall()

    changes_rows = c.execute(
        "SELECT id, snapshot_id FROM position_changes WHERE profile_id = ?",
        (profile_id,),
    ).fetchall()
    snapshot_to_change_id = {row['snapshot_id']: row['id'] for row in changes_rows}

    state = {}
    underlying_events = {}

    for snap in snapshots:
        snap_id = snap['id']
        timestamp = snap['timestamp']
        change_id = snapshot_to_change_id.get(snap_id)

        try:
            raw = json.loads(snap['raw_data'])
        except Exception:
            continue

        trades_map = normalize_trades_for_diff(raw.get('data', []))
        all_keys = set(trades_map.keys()) | set(state.keys())

        for key in all_keys:
            curr_trade = trades_map.get(key)
            st = state.get(key) or {'present': False, 'last_trade': None, 'last_good_trade': None}

            if curr_trade:
                symbol = curr_trade.get('trading_symbol', '')
                product = curr_trade.get('product', '')
            else:
                prev_t = st.get('last_trade')
                if prev_t:
                    symbol = prev_t.get('trading_symbol', '')
                    product = prev_t.get('product', '')
                else:
                    continue

            underlying = _get_underlying(symbol)
            if not underlying:
                if key in state:
                    del state[key]
                continue

            # Track state for non-matching underlyings too (needed for correct state tracking)
            if underlying != underlying_filter:
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

            if curr_trade and (curr_trade.get('quantity', 0) not in (None, 0)) and (curr_trade.get('average_price', 0) not in (None, 0)):
                st['last_good_trade'] = curr_trade

            event_type = None
            if (not prev_present) and curr_present:
                event_type = 'ENTERED'
            elif prev_present and (not curr_present):
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

                exit_pnl = 0
                exit_price = 0
                if event_type == 'EXITED':
                    exit_pnl, exit_price = _compute_exit_metrics(prev_trade)
                    if after_booked == 0 and exit_pnl != 0:
                        after_booked = exit_pnl
                    if after_ltp == 0 and exit_price != 0:
                        after_ltp = exit_price

                implied_fill_side = ''
                implied_fill_qty = 0
                implied_fill_price = 0
                if event_type == 'MODIFIED':
                    implied_fill_side, implied_fill_qty, implied_fill_price = _compute_implied_fill(
                        prev_trade or {}, curr_trade or {}, original_avg_price=before_avg,
                    )
                    if (before_qty > 0 and after_qty < before_qty) or (before_qty < 0 and after_qty > before_qty):
                        closed_qty = abs(before_qty - after_qty)
                        exit_pnl, exit_price = _compute_exit_metrics(
                            prev_trade,
                            closed_qty=closed_qty,
                            exit_price_override=implied_fill_price if implied_fill_price else None,
                        )

                underlying_events[underlying].append({
                    'timestamp': timestamp,
                    'change_id': change_id,
                    'type': event_type,
                    'symbol': symbol,
                    'product': product,
                    'expiry_date': _parse_expiry_from_symbol(symbol),
                    'before_quantity': before_qty,
                    'after_quantity': after_qty,
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

            st['present'] = curr_present
            st['last_trade'] = curr_trade if curr_trade is not None else None
            state[key] = st

    # Sort each underlying's events chronologically (earliest first for AI narrative)
    for u in underlying_events:
        underlying_events[u].sort(key=lambda e: e['timestamp'])

    return underlying_events


def _parse_expiry_from_symbol(trading_symbol):
    """Parse expiry date (YYYY-MM-DD) from an NSE trading symbol.

    Mirrors the frontend getExpiryDateFromSymbolEnhanced logic.
    Returns a YYYY-MM-DD string or None if unparseable.

    NSE monthly stock options (last Tuesday of month, dayOfWeek=2):
        ETERNAL26MAR10700PE  → 26=year, MAR=month → last Tuesday of Mar 2026
    NSE weekly single-letter months (O=Oct, N=Nov, D=Dec):
        NIFTY25O0319000CE    → 25=year, O=Oct, 03=day
    NSE weekly numeric:
        NIFTY2531310700CE    → year extracted, then MMDD or similar
    """
    import re as _re
    import calendar

    def last_day_of_week_in_month(year, month_0indexed, day_of_week):
        """Return the date of the last occurrence of day_of_week (Mon=0..Sun=6) in that month."""
        # calendar.monthrange returns (weekday of first day, number of days)
        _, num_days = calendar.monthrange(year, month_0indexed + 1)
        # Start from last day and go back
        d = num_days
        while True:
            dt = datetime(year, month_0indexed + 1, d)
            if dt.weekday() == day_of_week:  # Python: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4
                return dt.strftime('%Y-%m-%d')
            d -= 1

    MONTH_MAP = {'JAN':0,'FEB':1,'MAR':2,'APR':3,'MAY':4,'JUN':5,
                 'JUL':6,'AUG':7,'SEP':8,'OCT':9,'NOV':10,'DEC':11}
    SINGLE_LETTER_MAP = {'O':9,'N':10,'D':11}  # Oct, Nov, Dec
    # For stock options on NSE: last Tuesday (weekday=1). SENSEX: Friday (weekday=4).
    is_sensex = 'SENSEX' in trading_symbol.upper()
    default_dow = 4 if is_sensex else 1  # Tue=1, Fri=4

    # Pattern 1: standard month abbreviation YY + MON (e.g. 26MAR)
    m = _re.search(r'(\d{2})([A-Z]{3})', trading_symbol)
    if m:
        yr = 2000 + int(m.group(1))
        mon = MONTH_MAP.get(m.group(2).upper())
        if mon is not None:
            return last_day_of_week_in_month(yr, mon, default_dow)

    # Pattern 2: single-letter months O/N/D (e.g. NIFTY25O0319000CE)
    m2 = _re.match(r'^[A-Z]+(\d{2})([OND])(\d{2})', trading_symbol)
    if m2:
        yr = 2000 + int(m2.group(1))
        mon = SINGLE_LETTER_MAP.get(m2.group(2))
        day = int(m2.group(3))
        if mon is not None and 1 <= day <= 31:
            try:
                return datetime(yr, mon + 1, day).strftime('%Y-%m-%d')
            except ValueError:
                pass

    # Pattern 3: numeric encoding (e.g. NIFTY2631023700PE → 26 + 310 → month=3, day=10)
    # Mirrors JS logic: match YY + 3-digit code, zero-pad code to 4 chars → MMDD
    m3 = _re.search(r'(\d{2})(\d{3})', trading_symbol)
    if m3:
        yr_match = _re.match(r'^[A-Z]+(\d{2})', trading_symbol)
        yr = 2000 + int(yr_match.group(1)) if yr_match else 2000 + int(m3.group(1))
        code_str = m3.group(2).zfill(4)  # e.g. "310" -> "0310"
        p1, p2 = int(code_str[:2]), int(code_str[2:4])
        if 1 <= p1 <= 12 and 1 <= p2 <= 31:
            try:
                return datetime(yr, p1, p2).strftime('%Y-%m-%d')
            except ValueError:
                pass

    return None


def _build_prompt_from_consolidated_trades(profile_name, underlying, scope_type, expiry_key, consolidated_trades):
    """Build a system prompt using pre-consolidated trade data from the frontend."""

    if scope_type == 'expiry' and expiry_key:
        scope_label = f"Expiry: {expiry_key}"
    else:
        scope_label = "All Expiries"

    lines = [
        f"TRADER PROFILE: {profile_name}",
        f"UNDERLYING: {underlying}",
        f"SCOPE: {scope_label}",
        "",
        "=== CONSOLIDATED TRADE DATA ===",
        "(This is the exact consolidated view the user is currently seeing in their browser)",
        "",
    ]

    if not consolidated_trades or len(consolidated_trades) == 0:
        lines.append("No consolidated trade data provided.")
    else:
        for trade in consolidated_trades:
            symbol = trade.get('symbol', '')
            product = trade.get('product', '')
            is_open = trade.get('isOpen', False)
            net_qty = trade.get('netQty', 0)
            entry_date = trade.get('entryDate', '')
            exit_date = trade.get('exitDate')
            avg_entry = trade.get('avgEntryPrice', 0)
            avg_exit = trade.get('avgExitPrice', 0)
            ltp = trade.get('ltp', 0)
            total_pnl = trade.get('totalExitPnl', 0)
            unbooked_pnl = trade.get('unbookedPnl', 0)

            status = "OPEN" if is_open else "CLOSED"
            lines.append(f"[{symbol} | {product}] - {status}")
            lines.append(f"  Entry Date: {entry_date}")
            if exit_date:
                lines.append(f"  Exit Date: {exit_date}")
            lines.append(f"  Net Quantity: {net_qty}")
            lines.append(f"  Avg Entry Price: ₹{avg_entry:.2f}")

            if is_open:
                lines.append(f"  LTP: ₹{ltp:.2f}")
                lines.append(f"  Total Exit P&L: ₹{total_pnl:+,.2f}")
                lines.append(f"  Unbooked P&L: ₹{unbooked_pnl:+,.2f}")
            else:
                lines.append(f"  Avg Exit Price: ₹{avg_exit:.2f}")
                lines.append(f"  Total Exit P&L: ₹{total_pnl:+,.2f}")

            lines.append("")

    # Summary statistics
    total_booked_pnl = sum(t.get('totalExitPnl', 0) for t in consolidated_trades if not t.get('isOpen', False))
    total_unbooked_pnl = sum(t.get('unbookedPnl', 0) for t in consolidated_trades if t.get('isOpen', False))
    open_count = sum(1 for t in consolidated_trades if t.get('isOpen', False))
    closed_count = sum(1 for t in consolidated_trades if not t.get('isOpen', False))

    lines.append("=== SUMMARY ===")
    lines.append(f"Total Symbols: {len(consolidated_trades)}")
    lines.append(f"Open Positions: {open_count}")
    lines.append(f"Closed Positions: {closed_count}")
    lines.append(f"Total Realized P&L: ₹{total_booked_pnl:+,.2f}")
    lines.append(f"Total Unrealized P&L: ₹{total_unbooked_pnl:+,.2f}")
    lines.append("")
    lines.append("You are an expert trading analyst. Help the user understand their trades and performance.")

    return "\n".join(lines)


def _build_ai_system_prompt(c, profile_id, profile_name, underlying, scope_type, expiry_key, consolidated_trades=None):
    """Build a structured system prompt with full trade context for Claude.

    If consolidated_trades is provided (from frontend), use that directly instead of
    fetching and computing from database. This ensures the AI sees exactly what the user sees.
    """

    # Use pre-consolidated trades from frontend if provided
    if consolidated_trades:
        return _build_prompt_from_consolidated_trades(profile_name, underlying, scope_type, expiry_key, consolidated_trades)

    # Fallback to original behavior: fetch from database
    events_by_underlying = _fetch_underlying_events(c, profile_id, underlying)
    all_events = events_by_underlying.get(underlying, [])

    # Filter to a specific expiry if needed
    if scope_type == 'expiry' and expiry_key:
        # Build expiry_map using symbol-name parsing first (mirrors frontend getExpiryDateFromSymbolEnhanced),
        # so expiry_key from the frontend always matches. Fall back to master_contract only when
        # symbol-name parsing fails (e.g. non-standard symbols).
        symbols = set(e['symbol'] for e in all_events)
        expiry_map = {}

        for sym in symbols:
            parsed = _parse_expiry_from_symbol(sym)
            if parsed:
                expiry_map[sym] = parsed

        # Fallback: for symbols where name-parsing returned nothing, try master_contract
        missing = [sym for sym in symbols if sym not in expiry_map]
        if missing:
            placeholders = ','.join('?' * len(missing))
            rows = c.execute(
                f"SELECT trading_symbol, expiry FROM master_contract WHERE trading_symbol IN ({placeholders})",
                missing,
            ).fetchall()
            for row in rows:
                if row['expiry']:
                    expiry_map[row['trading_symbol']] = row['expiry'][:10]  # YYYY-MM-DD

        all_events = [e for e in all_events if expiry_map.get(e['symbol']) == expiry_key]
        scope_label = f"Expiry: {expiry_key}"
    else:
        scope_label = "All Expiries"

    def fmt_ts(ts):
        try:
            dt = datetime.fromisoformat(ts) if 'T' in ts else datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
            return dt.strftime('%d %b %Y %H:%M')
        except Exception:
            return ts

    # Group by symbol|product
    events_by_symbol = {}
    for e in all_events:
        sym_key = f"{e['symbol']}|{e['product']}"
        events_by_symbol.setdefault(sym_key, []).append(e)

    lines = [
        f"TRADER PROFILE: {profile_name}",
        f"UNDERLYING: {underlying}",
        f"SCOPE: {scope_label}",
        "",
        "=== TRADE LIFECYCLE DATA ===",
        "(Sorted earliest to latest. All prices in INR. Times in IST.)",
        "",
    ]

    def _symbol_realized_pnl(sym_events):
        """Sum exit_pnl across all MODIFIED (partial exits) and EXITED events for a symbol.

        This is more accurate than using only the last event's after_booked_pnl, which
        reflects only the final-exit P&L when the broker clears booked_profit_loss on close.
        """
        return sum(float(e.get('exit_pnl') or 0) for e in sym_events)

    if not events_by_symbol:
        lines.append("No trade data found for this scope.")
    else:
        for sym_key, sym_events in sorted(events_by_symbol.items()):
            symbol, product = sym_key.split('|', 1)
            lines.append(f"[{symbol} | {product}]")
            for e in sym_events:
                ts = fmt_ts(e['timestamp'])
                etype = e['type']
                bq = e['before_quantity']
                aq = e['after_quantity']
                ba = float(e['before_average_price'] or 0)
                aa = float(e['after_average_price'] or 0)

                if etype == 'ENTERED':
                    direction = "SHORT" if aq < 0 else "LONG"
                    lines.append(f"  ENTERED  {ts} | Qty: {bq}->{aq} ({direction}) | Avg: {ba:.2f}->{aa:.2f}")
                elif etype == 'EXITED':
                    ep = float(e.get('exit_pnl') or 0)
                    eprice = float(e.get('exit_price') or 0)
                    pnl_str = f"+{ep:,.2f}" if ep >= 0 else f"{ep:,.2f}"
                    lines.append(f"  EXITED   {ts} | Qty: {bq}->{aq} | Exit Price: {eprice:.2f} | Exit P&L: {pnl_str}")
                elif etype == 'MODIFIED':
                    fill_side = e.get('implied_fill_side', '')
                    fill_qty = e.get('implied_fill_qty', 0)
                    fill_price = float(e.get('implied_fill_price') or 0)
                    fill_str = f" | Fill: {fill_side} {fill_qty} @ {fill_price:.2f}" if (fill_side and fill_qty and fill_price) else ""
                    ep = float(e.get('exit_pnl') or 0)
                    exit_str = f" | Partial Exit P&L: {ep:+,.2f}" if ep else ""
                    lines.append(f"  MODIFIED {ts} | Qty: {bq}->{aq} | Avg: {ba:.2f}->{aa:.2f}{fill_str}{exit_str}")

            # Show consolidated status line for this symbol
            last_e = sym_events[-1]
            is_closed = last_e['type'] == 'EXITED' or (last_e.get('after_quantity') or 0) == 0
            if is_closed:
                realized = _symbol_realized_pnl(sym_events)
                lines.append(f"  → TOTAL REALIZED P&L: {realized:+,.2f}")
            else:
                booked = float(last_e.get('after_booked_pnl') or 0)
                unbooked = float(last_e.get('after_unbooked_pnl') or 0)
                ltp = float(last_e.get('after_last_price') or 0)
                lines.append(f"  STATUS: OPEN | Booked P&L: {booked:+,.2f} | Unbooked P&L: {unbooked:+,.2f} | LTP: {ltp:.2f}")
            lines.append("")

    # Summary statistics
    total_booked = 0.0
    total_unbooked = 0.0
    active_count = 0
    closed_count = 0
    for sym_key, sym_events in events_by_symbol.items():
        last_e = sym_events[-1]
        is_closed = last_e['type'] == 'EXITED' or (last_e.get('after_quantity') or 0) == 0
        if is_closed:
            total_booked += _symbol_realized_pnl(sym_events)
            closed_count += 1
        else:
            total_unbooked += float(last_e.get('after_unbooked_pnl') or 0)
            active_count += 1

    lines += [
        "=== SUMMARY ===",
        f"Total Symbols Traded: {len(events_by_symbol)}",
        f"Active (Open) Positions: {active_count}",
        f"Closed Positions: {closed_count}",
        f"Total Booked P&L: {total_booked:+,.2f}",
    ]
    if active_count > 0:
        lines.append(f"Total Unbooked P&L (open positions): {total_unbooked:+,.2f}")

    lines += [
        "",
        "=== YOUR ROLE ===",
        "You are an expert options trading coach and analyst. Your job is to help the trader understand their trades, identify patterns, and improve.",
        "IMPORTANT — P&L RULES:",
        "- Each [SYMBOL | PRODUCT] block above is ONE position lifecycle (ENTERED → optional MODIFIEDs → EXITED).",
        "- Use ONLY the '→ TOTAL REALIZED P&L' line for each closed position's booked P&L. Do NOT recompute P&L from prices.",
        "- Use ONLY the SUMMARY section totals for aggregate P&L. Do NOT sum individual exit events yourself.",
        "When answering questions:",
        "- Be specific with timestamps, prices, and P&L numbers from the data above",
        "- Identify trading patterns (scalping, spreads, straddles, hedges, directional bets, etc.)",
        "- Assess entry/exit timing quality (NSE normal session: 09:15-15:30 IST)",
        "- Highlight what worked well and what could be improved",
        "- Calculate risk/reward ratios when relevant",
        "- If something is unclear from the data, say so honestly",
        "- Keep responses concise and actionable",
    ]

    return '\n'.join(lines)


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

@app.route('/api/subscriptions/test_notify', methods=['POST'])
@login_required
def test_subscription_notify():
    """Fire a synthetic new_position event through the subscription to verify it works"""
    try:
        from scraper import generate_notifications_for_changes

        data = request.get_json()
        subscription_id = data.get('subscription_id')
        if not subscription_id:
            return jsonify({'error': 'subscription_id is required'}), 400

        conn = get_db()
        c = conn.cursor()

        sub = c.execute("""
            SELECT s.id, s.profile_id, s.subscription_type, s.underlying, s.expiry
            FROM subscriptions s WHERE s.id = ?
        """, (subscription_id,)).fetchone()

        if not sub:
            conn.close()
            return jsonify({'error': 'Subscription not found'}), 404

        if sub['subscription_type'] != 'expiry':
            conn.close()
            return jsonify({'error': 'Test notification is only supported for expiry subscriptions'}), 400

        underlying = sub['underlying']
        expiry = sub['expiry']
        profile_id = sub['profile_id']

        # Fabricate before (empty) and after (synthetic new position) snapshots
        before_data = {'data': []}
        after_data = {
            'data': [{
                'trading_symbol': underlying,
                'trades': [{
                    'trading_symbol': f'{underlying}TEST0000CE',
                    'quantity': 50,
                    'average_price': 100.0,
                    'last_price': 100.0,
                    'unbooked_pnl': 0.0,
                    'booked_profit_loss': 0.0,
                    'product': 'MIS',
                    'instrument_info': {
                        'expiry': expiry,
                        'strike': 0.0,
                        'instrument_type': 'CE'
                    }
                }]
            }]
        }

        max_id_row = c.execute(
            "SELECT COALESCE(MAX(id), 0) FROM notifications WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        max_id_before = max_id_row[0]

        generate_notifications_for_changes(conn, profile_id, before_data, after_data)

        # Prefix message with [TEST] so the user can tell it apart
        new_notifs = c.execute(
            "SELECT id, notification_data FROM notifications WHERE profile_id = ? AND id > ?",
            (profile_id, max_id_before)
        ).fetchall()

        for notif in new_notifs:
            c.execute("UPDATE notifications SET message = '[TEST] ' || message WHERE id = ?", (notif['id'],))
            try:
                nd = json.loads(notif['notification_data']) if notif['notification_data'] else {}
                nd['is_test'] = True
                c.execute("UPDATE notifications SET notification_data = ? WHERE id = ?",
                          (json.dumps(nd), notif['id']))
            except Exception:
                pass

        conn.commit()
        conn.close()

        if new_notifs:
            return jsonify({'success': True,
                            'message': f'Test notification created — subscription for {underlying} {expiry} is working correctly.',
                            'count': len(new_notifs)})
        else:
            return jsonify({'success': False,
                            'message': 'No notification generated. The subscription did not match the test event — check your subscription details.'})

    except Exception as e:
        import traceback
        print(f"Error creating test notification: {traceback.format_exc()}")
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

# ==================== OPENALGO HELPERS ====================

def _to_float(value):
    """Best-effort number parsing for notification payload values."""
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def normalize_openalgo_host(host):
    host = (host or '').strip()
    if not host:
        return ''
    if not host.startswith(('http://', 'https://')):
        host = f"http://{host}"
    return host.rstrip('/')

def is_valid_host_url(host):
    parsed = urlparse(host)
    return bool(parsed.scheme in ('http', 'https') and parsed.netloc)

def mask_api_key(api_key):
    key = api_key or ''
    if len(key) <= 8:
        return '*' * len(key)
    return f"{key[:6]}...{key[-4:]}"

MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
MON3_TO_NUM = {m: i for i, m in enumerate(MONTHS.split("|"), 1)}
MON_CODE_TO_NUM = {str(i): i for i in range(1, 10)} | {"O": 10, "N": 11, "D": 12}
NUM_TO_MON3 = {v: k for k, v in MON3_TO_NUM.items()}

def _build_profile_holiday_api_url(host):
    normalized_host = normalize_openalgo_host(host)
    if not normalized_host:
        return ''
    if OPENALGO_HOLIDAYS_PATH.startswith(('http://', 'https://')):
        return OPENALGO_HOLIDAYS_PATH
    path = OPENALGO_HOLIDAYS_PATH if OPENALGO_HOLIDAYS_PATH.startswith('/') else f"/{OPENALGO_HOLIDAYS_PATH}"
    return f"{normalized_host}{path}"

def _get_holiday_api_url(year, holiday_api_url=None):
    base_url = (holiday_api_url or MARKET_HOLIDAYS_API_URL).strip()
    if not base_url:
        return ''
    if '{year}' in base_url:
        return base_url.format(year=year)
    separator = '&' if '?' in base_url else '?'
    return f"{base_url}{separator}year={year}"

def _get_nfo_trading_holidays(year, holiday_api_url=None, holiday_api_key=None):
    holiday_url = _get_holiday_api_url(year, holiday_api_url=holiday_api_url)
    cache_key = (year, holiday_url)
    with MARKET_HOLIDAY_CACHE_LOCK:
        if cache_key in MARKET_HOLIDAY_CACHE:
            return MARKET_HOLIDAY_CACHE[cache_key]

    holidays = set()
    if not holiday_url:
        with MARKET_HOLIDAY_CACHE_LOCK:
            MARKET_HOLIDAY_CACHE[cache_key] = holidays
        return holidays

    effective_api_key = holiday_api_key if holiday_api_key is not None else MARKET_HOLIDAYS_API_KEY
    payload = {'year': year}
    if effective_api_key:
        payload['apikey'] = effective_api_key

    try:
        response = http_requests.post(holiday_url, json=payload, timeout=6)
        response.raise_for_status()
    except http_requests.RequestException as exc:
        # Fallback to GET for providers that do not accept POST for holidays endpoint.
        params = {'year': year}
        if effective_api_key:
            params['apikey'] = effective_api_key
        try:
            response = http_requests.get(holiday_url, params=params, timeout=6)
            response.raise_for_status()
        except http_requests.RequestException as get_exc:
            print(f"[OpenAlgo] Failed to fetch holidays for {year}: {exc} | GET fallback failed: {get_exc}")
            with MARKET_HOLIDAY_CACHE_LOCK:
                MARKET_HOLIDAY_CACHE[cache_key] = holidays
            return holidays

    try:
        payload = response.json()
    except ValueError:
        print(f"[OpenAlgo] Invalid holiday JSON for {year} from {holiday_url}")
        with MARKET_HOLIDAY_CACHE_LOCK:
            MARKET_HOLIDAY_CACHE[cache_key] = holidays
        return holidays

    for item in payload.get('data', []):
        if item.get('holiday_type') != 'TRADING_HOLIDAY':
            continue
        closed_exchanges = item.get('closed_exchanges') or []
        if 'NFO' not in closed_exchanges:
            continue
        day_str = item.get('date')
        if not day_str:
            continue
        try:
            holidays.add(date.fromisoformat(day_str))
        except ValueError:
            continue

    with MARKET_HOLIDAY_CACHE_LOCK:
        MARKET_HOLIDAY_CACHE[cache_key] = holidays
    return holidays

# Expiry weekday per underlying (0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri).
# Default is Tuesday for all instruments (NIFTY, BANKNIFTY, FINNIFTY, stocks, etc.).
_EXPIRY_WEEKDAY = {
    'SENSEX':     3,  # Thursday
    'MIDCPNIFTY': 0,  # Monday
}
_DEFAULT_EXPIRY_WEEKDAY = 1  # Tuesday


def _resolve_last_trading_day_of_month(base, year, month, holiday_api_url=None, holiday_api_key=None):
    target_weekday = _EXPIRY_WEEKDAY.get((base or '').upper(), _DEFAULT_EXPIRY_WEEKDAY)
    nfo_holidays = _get_nfo_trading_holidays(
        year,
        holiday_api_url=holiday_api_url,
        holiday_api_key=holiday_api_key
    )
    last_day = calendar.monthrange(year, month)[1]
    # Find the last occurrence of target_weekday in the month
    candidate = date(year, month, last_day)
    while candidate.weekday() != target_weekday:
        candidate -= timedelta(days=1)
    # If that day is a holiday, walk back to the previous trading day
    while candidate.weekday() >= 5 or candidate in nfo_holidays:
        candidate -= timedelta(days=1)
    return candidate.day

def convert_broker_symbol_to_openalgo_symbol(
    symbol: str,
    holiday_api_url: Optional[str] = None,
    holiday_api_key: Optional[str] = None
) -> str:
    s = symbol.strip().upper()
    if ":" in s:
        _, s = s.split(":", 1)

    m = re.fullmatch(rf"([A-Z0-9]+?)(\d{{2}})({MONTHS})(\d+(?:\.\d+)?)(CE|PE)", s)
    if m:
        base, yy, mon3, strike, opt = m.groups()
        year = 2000 + int(yy)
        month = MON3_TO_NUM[mon3]
        day = _resolve_last_trading_day_of_month(
            base,
            year,
            month,
            holiday_api_url=holiday_api_url,
            holiday_api_key=holiday_api_key
        )
        if strike.endswith(".0"):
            strike = strike[:-2]
        return f"{base}{day:02d}{mon3}{yy}{strike}{opt}"

    m = re.fullmatch(r"([A-Z0-9]+?)(\d{2})([1-9OND])(\d{2})(\d+(?:\.\d+)?)(CE|PE)", s)
    if m:
        base, yy, mcode, dd, strike, opt = m.groups()
        year = 2000 + int(yy)
        month = MON_CODE_TO_NUM[mcode]
        mon3 = NUM_TO_MON3[month]
        day = int(dd)
        if not 1 <= day <= calendar.monthrange(year, month)[1]:
            raise ValueError(f"Invalid day in symbol: {symbol}")
        if strike.endswith(".0"):
            strike = strike[:-2]
        return f"{base}{day:02d}{mon3}{yy}{strike}{opt}"

    raise ValueError(f"Unsupported Zerodha option symbol format: {symbol}")

def infer_openalgo_action(notification_type, notification_data, message):
    implied_side = (notification_data.get('implied_fill_side') or '').strip().upper()
    if implied_side in ('BUY', 'SELL'):
        return implied_side

    qty_diff = _to_float(notification_data.get('quantity_diff'))
    if qty_diff is not None and qty_diff != 0:
        return 'BUY' if qty_diff > 0 else 'SELL'

    qty = _to_float(notification_data.get('quantity'))
    if qty is not None and qty != 0:
        if notification_type == 'exited_position':
            return 'SELL' if qty > 0 else 'BUY'
        return 'BUY' if qty > 0 else 'SELL'

    upper_msg = (message or '').upper()
    if ' BUY ' in f" {upper_msg} ":
        return 'BUY'
    if ' SELL ' in f" {upper_msg} ":
        return 'SELL'

    if notification_type == 'exited_position':
        return 'SELL'
    return 'BUY'

def infer_openalgo_quantity(notification_type, notification_data):
    implied_qty = _to_float(notification_data.get('implied_fill_qty'))
    if implied_qty is not None and implied_qty != 0:
        return max(1, int(abs(implied_qty)))

    qty_diff = _to_float(notification_data.get('quantity_diff'))
    if qty_diff is not None and qty_diff != 0:
        return max(1, int(abs(qty_diff)))

    quantity = _to_float(notification_data.get('quantity'))
    if quantity is not None and quantity != 0:
        return max(1, int(abs(quantity)))

    old_quantity = _to_float(notification_data.get('old_quantity'))
    if notification_type in ('modified_position', 'quantity_change') and old_quantity is not None and quantity is not None:
        calculated_diff = quantity - old_quantity
        if calculated_diff != 0:
            return max(1, int(abs(calculated_diff)))

    return 1

def extract_notification_symbol(notification_data, message):
    symbol = (notification_data.get('symbol') or '').strip().upper()
    if symbol:
        return symbol

    upper_message = (message or '').upper()
    symbol_match = re.search(r'\b[A-Z]{2,}\d[A-Z0-9]*(?:CE|PE)\b', upper_message)
    return symbol_match.group(0) if symbol_match else ''

def build_openalgo_order_hint(notification_type, notification_data, message):
    broker_symbol = extract_notification_symbol(notification_data, message)
    openalgo_symbol = ''
    if broker_symbol:
        try:
            openalgo_symbol = convert_broker_symbol_to_openalgo_symbol(broker_symbol)
        except ValueError:
            openalgo_symbol = broker_symbol
    action = infer_openalgo_action(notification_type, notification_data, message)
    quantity = infer_openalgo_quantity(notification_type, notification_data)
    return {
        'broker_symbol': broker_symbol,
        'openalgo_symbol': openalgo_symbol,
        'action': action,
        'quantity': quantity,
        'exchange': 'NFO',
        'product': 'NRML',
        'pricetype': 'MARKET',
        'strategy': 'from sensibull tracker',
        'price': '0'
    }

def check_openalgo_host(host):
    normalized_host = normalize_openalgo_host(host)
    health_url = f"{normalized_host}/api/v1/placeorder"
    try:
        response = http_requests.get(health_url, timeout=3)
        return {
            'alive': True,
            'checked_url': health_url,
            'status_code': response.status_code
        }
    except http_requests.RequestException as exc:
        return {
            'alive': False,
            'checked_url': health_url,
            'error': str(exc)
        }

# ==================== OPENALGO API ROUTES ====================

@app.route('/api/openalgo/profiles', methods=['GET'])
@login_required
def api_get_openalgo_profiles():
    """Get all active OpenAlgo profiles."""
    conn = get_db()
    c = conn.cursor()

    profiles = c.execute("""
        SELECT id, profile_name, host, api_key, is_active, created_at, updated_at
        FROM openalgo_profiles
        WHERE is_active = 1
        ORDER BY profile_name
    """).fetchall()
    conn.close()

    return jsonify({
        'success': True,
        'profiles': [{
            'id': row['id'],
            'profile_name': row['profile_name'],
            'host': row['host'],
            'api_key_masked': mask_api_key(row['api_key']),
            'is_active': bool(row['is_active']),
            'created_at': row['created_at'],
            'updated_at': row['updated_at']
        } for row in profiles]
    })

@app.route('/api/openalgo/profiles', methods=['POST'])
@login_required
def api_create_openalgo_profile():
    """Create an OpenAlgo profile."""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON data'}), 400

    profile_name = (data.get('profile_name') or '').strip()
    host = normalize_openalgo_host(data.get('host'))
    api_key = (data.get('api_key') or '').strip()

    if not profile_name:
        return jsonify({'success': False, 'error': 'Profile name is required'}), 400
    if not host:
        return jsonify({'success': False, 'error': 'Host is required'}), 400
    if not is_valid_host_url(host):
        return jsonify({'success': False, 'error': 'Host must be a valid http/https URL'}), 400
    if not api_key:
        return jsonify({'success': False, 'error': 'API key is required'}), 400

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO openalgo_profiles (profile_name, host, api_key, is_active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
        """, (profile_name, host, api_key, now_ist().isoformat(), now_ist().isoformat()))
        conn.commit()
        profile_id = c.lastrowid
        created = c.execute("""
            SELECT id, profile_name, host, api_key, is_active, created_at, updated_at
            FROM openalgo_profiles
            WHERE id = ?
        """, (profile_id,)).fetchone()
        conn.close()
        return jsonify({
            'success': True,
            'message': 'OpenAlgo profile created successfully',
            'profile': {
                'id': created['id'],
                'profile_name': created['profile_name'],
                'host': created['host'],
                'api_key_masked': mask_api_key(created['api_key']),
                'is_active': bool(created['is_active']),
                'created_at': created['created_at'],
                'updated_at': created['updated_at']
            }
        })
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'error': 'Profile name already exists'}), 400
    except sqlite3.Error as exc:
        conn.close()
        return jsonify({'success': False, 'error': f'Database error: {str(exc)}'}), 500

@app.route('/api/openalgo/host_status', methods=['GET'])
@login_required
def api_openalgo_host_status():
    """Check if OpenAlgo host is reachable for the selected profile."""
    profile_id = request.args.get('profile_id', type=int)
    if not profile_id:
        return jsonify({'success': False, 'error': 'profile_id is required'}), 400

    conn = get_db()
    c = conn.cursor()
    profile = c.execute("""
        SELECT id, profile_name, host
        FROM openalgo_profiles
        WHERE id = ? AND is_active = 1
    """, (profile_id,)).fetchone()
    conn.close()

    if not profile:
        return jsonify({'success': False, 'error': 'OpenAlgo profile not found'}), 404

    health = check_openalgo_host(profile['host'])
    return jsonify({
        'success': True,
        'profile': {
            'id': profile['id'],
            'profile_name': profile['profile_name'],
            'host': profile['host']
        },
        'health': health
    })

@app.route('/api/openalgo/place_order', methods=['POST'])
@login_required
def api_openalgo_place_order():
    """Place an OpenAlgo order using selected OpenAlgo profile and notification."""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON data'}), 400

    openalgo_profile_id = data.get('openalgo_profile_id')
    notification_id = data.get('notification_id')

    if not openalgo_profile_id:
        return jsonify({'success': False, 'error': 'openalgo_profile_id is required'}), 400
    if not notification_id:
        return jsonify({'success': False, 'error': 'notification_id is required'}), 400

    conn = get_db()
    c = conn.cursor()
    profile = c.execute("""
        SELECT id, profile_name, host, api_key
        FROM openalgo_profiles
        WHERE id = ? AND is_active = 1
    """, (openalgo_profile_id,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'success': False, 'error': 'OpenAlgo profile not found'}), 404

    notif = c.execute("""
        SELECT id, message, notification_type, notification_data
        FROM notifications
        WHERE id = ?
    """, (notification_id,)).fetchone()
    conn.close()
    if not notif:
        return jsonify({'success': False, 'error': 'Notification not found'}), 404

    notification_data = json.loads(notif['notification_data']) if notif['notification_data'] else {}
    hint = build_openalgo_order_hint(notif['notification_type'], notification_data, notif['message'])

    broker_symbol = hint['broker_symbol']
    profile_holiday_api_url = _build_profile_holiday_api_url(profile['host'])
    inferred_symbol_for_profile = ''
    if broker_symbol:
        try:
            inferred_symbol_for_profile = convert_broker_symbol_to_openalgo_symbol(
                broker_symbol,
                holiday_api_url=profile_holiday_api_url,
                holiday_api_key=profile['api_key']
            )
        except ValueError:
            inferred_symbol_for_profile = ''

    requested_openalgo_symbol = (data.get('openalgo_symbol') or '').strip().upper()
    openalgo_symbol = (
        requested_openalgo_symbol
        or inferred_symbol_for_profile
        or (hint['openalgo_symbol'] or '').strip().upper()
    )
    action = (data.get('action') or hint['action']).strip().upper()
    try:
        quantity = int(data.get('quantity') or hint['quantity'])
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'quantity must be an integer'}), 400

    if not openalgo_symbol:
        return jsonify({'success': False, 'error': 'Could not infer symbol from notification'}), 400
    if action not in ('BUY', 'SELL'):
        return jsonify({'success': False, 'error': 'action must be BUY or SELL'}), 400
    if quantity <= 0:
        return jsonify({'success': False, 'error': 'quantity must be greater than 0'}), 400

    host = normalize_openalgo_host(profile['host'])
    order_url = f"{host}/api/v1/placeorder"
    order_payload = {
        'apikey': profile['api_key'],
        'symbol': openalgo_symbol,
        'exchange': 'NFO',
        'action': action,
        'product': 'NRML',
        'pricetype': 'MARKET',
        'strategy': 'from sensibull tracker',
        'quantity': str(quantity),
        'price': '0'
    }

    try:
        response = http_requests.post(
            order_url,
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
            json=order_payload,
            timeout=12
        )
        try:
            response_data = response.json()
        except ValueError:
            response_data = {'raw_response': response.text}

        success = response.ok
        return jsonify({
            'success': success,
            'message': 'Order placed successfully' if success else 'OpenAlgo rejected order',
            'request': {
                'notification_id': notif['id'],
                'broker_symbol': broker_symbol,
                'openalgo_symbol': openalgo_symbol,
                'action': action,
                'quantity': quantity,
                'openalgo_profile_id': profile['id'],
                'openalgo_profile_name': profile['profile_name'],
                'host': host,
                'holiday_api_url': profile_holiday_api_url or MARKET_HOLIDAYS_API_URL
            },
            'order_payload': order_payload,
            'response_status_code': response.status_code,
            'response_data': response_data
        }), (200 if success else 502)
    except http_requests.RequestException as exc:
        return jsonify({
            'success': False,
            'message': 'Failed to reach OpenAlgo host',
            'error': str(exc),
            'request': {
                'notification_id': notif['id'],
                'broker_symbol': broker_symbol,
                'openalgo_symbol': openalgo_symbol,
                'action': action,
                'quantity': quantity,
                'openalgo_profile_id': profile['id'],
                'openalgo_profile_name': profile['profile_name'],
                'host': host,
                'holiday_api_url': profile_holiday_api_url or MARKET_HOLIDAYS_API_URL
            },
            'order_payload': order_payload
        }), 502

# ==================== OPENALGO MARGIN ====================

@app.route('/api/openalgo/margin', methods=['POST'])
@login_required
def api_openalgo_margin():
    """Calculate margin for positions by trying active OpenAlgo profiles in order."""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Invalid JSON data'}), 400

    positions = data.get('positions', [])
    if not positions:
        return jsonify({'success': False, 'error': 'positions array is required'}), 400

    conn = get_db()
    c = conn.cursor()
    profiles = c.execute("""
        SELECT id, profile_name, host, api_key
        FROM openalgo_profiles
        WHERE is_active = 1
        ORDER BY id
    """).fetchall()
    conn.close()

    if not profiles:
        return jsonify({'success': False, 'error': 'No active OpenAlgo profile configured. Please add one in OpenAlgo settings.'}), 404

    # Convert broker symbols to OpenAlgo symbols
    converted_positions = []
    for pos in positions:
        converted = dict(pos)
        broker_sym = (pos.get('symbol') or '').strip().upper()
        if broker_sym:
            try:
                converted['symbol'] = convert_broker_symbol_to_openalgo_symbol(broker_sym)
            except Exception as e:
                print(f"[OpenAlgo margin] Symbol conversion failed for {broker_sym}: {e}")
        converted_positions.append(converted)

    BATCH_SIZE = 50
    # Fields that represent required/used margin and should be summed across batches.
    # Account-level fields (available_margin, net, etc.) are kept from the first batch.
    ADDITIVE_KEYS = {
        'required_margin', 'total_margin_required',
        'span_margin', 'span', 'exposure_margin', 'exposure',
        'additional_margin', 'bo_margin', 'cash_margin',
    }
    last_error = None
    for profile in profiles:
        host = normalize_openalgo_host(profile['host'])
        margin_url = f"{host}/api/v1/margin"
        try:
            merged_data = None
            batches = [converted_positions[i:i+BATCH_SIZE] for i in range(0, len(converted_positions), BATCH_SIZE)]
            failed = False
            for batch in batches:
                payload = {
                    'apikey': profile['api_key'],
                    'positions': batch
                }
                response = http_requests.post(margin_url, json=payload, timeout=10)
                result = response.json()
                if result.get('status') != 'success':
                    last_error = result.get('message') or result.get('error') or 'OpenAlgo returned error'
                    failed = True
                    break
                batch_data = result.get('data', {})
                if not isinstance(batch_data, dict):
                    continue
                if merged_data is None:
                    # First batch: take all fields as the base
                    merged_data = dict(batch_data)
                else:
                    # Subsequent batches: sum position-level margin fields; keep account-level from first batch
                    for k, v in batch_data.items():
                        if k in ADDITIVE_KEYS and isinstance(v, (int, float)):
                            merged_data[k] = (merged_data.get(k) or 0) + v
            if not failed and merged_data is not None:
                return jsonify({
                    'success': True,
                    'data': merged_data,
                    'profile_name': profile['profile_name']
                })
        except http_requests.RequestException as exc:
            last_error = f"Failed to reach {profile['profile_name']}: {str(exc)}"
        except Exception as exc:
            last_error = str(exc)

    converted_syms = [p.get('symbol') for p in converted_positions]
    print(f"[OpenAlgo margin] Converted symbols sent: {converted_syms}. Last error: {last_error}")
    return jsonify({'success': False, 'error': last_error or 'All OpenAlgo profiles failed'}), 502


# ==================== MASTER CONTRACT STRIKE INFO ====================

@app.route('/api/master_contract/strike_info', methods=['GET'])
@login_required
def api_master_contract_strike_info():
    """Return strike size and available strikes for a broker symbol from master contract."""
    broker_symbol = (request.args.get('broker_symbol') or '').strip().upper()
    if not broker_symbol:
        return jsonify({'success': False, 'error': 'broker_symbol is required'}), 400

    conn = get_db()
    c = conn.cursor()
    instrument = c.execute("""
        SELECT trading_symbol, name, expiry, strike, lot_size, instrument_type
        FROM master_contract
        WHERE trading_symbol = ?
    """, (broker_symbol,)).fetchone()

    if not instrument:
        conn.close()
        return jsonify({'success': False, 'error': f'Symbol {broker_symbol} not found in master contract'}), 404

    strikes_rows = c.execute("""
        SELECT DISTINCT strike
        FROM master_contract
        WHERE name = ? AND expiry = ? AND instrument_type = ?
        ORDER BY strike ASC
    """, (instrument['name'], instrument['expiry'], instrument['instrument_type'])).fetchall()
    conn.close()

    strike_list = [row['strike'] for row in strikes_rows]
    strike_size = None
    if len(strike_list) >= 2:
        gaps = [strike_list[i + 1] - strike_list[i] for i in range(len(strike_list) - 1)]
        strike_size = int(min(gaps))

    return jsonify({
        'success': True,
        'broker_symbol': broker_symbol,
        'current_strike': instrument['strike'],
        'strike_size': strike_size,
        'lot_size': instrument['lot_size'],
        'available_strikes': strike_list,
        'name': instrument['name'],
        'expiry': instrument['expiry'],
        'instrument_type': instrument['instrument_type']
    })

@app.route('/api/master_contract/lot_sizes', methods=['POST'])
@login_required
def api_master_contract_lot_sizes():
    """Return lot sizes for a batch of trading symbols.
    POST body: {"symbols": ["NIFTY25APR25000CE", ...]}
    Falls back to underlying-prefix lookup for symbols not found (e.g. expired contracts).
    """
    data = request.get_json(force=True, silent=True) or {}
    symbols = data.get('symbols', [])
    if not symbols:
        return jsonify({'lot_sizes': {}})

    conn = get_db()
    c = conn.cursor()

    placeholders = ','.join('?' * len(symbols))
    rows = c.execute(
        f"SELECT trading_symbol, lot_size FROM master_contract WHERE trading_symbol IN ({placeholders})",
        symbols
    ).fetchall()
    lot_sizes = {row['trading_symbol']: row['lot_size'] for row in rows}

    # Fallback for symbols not found: look up any contract for the same underlying
    missing = [s for s in symbols if s not in lot_sizes]
    for sym in missing:
        # Extract underlying prefix (letters at start of symbol)
        import re
        m = re.match(r'^([A-Z]+)', sym)
        if not m:
            continue
        underlying = m.group(1)
        row = c.execute(
            "SELECT lot_size FROM master_contract WHERE trading_symbol LIKE ? AND lot_size > 0 ORDER BY expiry DESC LIMIT 1",
            (underlying + '%',)
        ).fetchone()
        if row:
            lot_sizes[sym] = row['lot_size']

    conn.close()
    return jsonify({'lot_sizes': lot_sizes})


@app.route('/api/master_contract/watch_underlyings')
@login_required
def api_watch_underlyings():
    """Get distinct underlying symbols available in master_contract for NFO futures"""
    import re
    try:
        conn = get_db()
        c = conn.cursor()
        rows = c.execute("""
            SELECT DISTINCT trading_symbol FROM master_contract
            WHERE exchange = 'NFO' AND instrument_type = 'FUT'
            ORDER BY trading_symbol
        """).fetchall()
        conn.close()
        underlyings = set()
        for row in rows:
            m = re.match(r'^([A-Z0-9&-]+)\d{2}[A-Z]{3}FUT$', row['trading_symbol'])
            if m:
                underlyings.add(m.group(1))
        return jsonify({'success': True, 'underlyings': sorted(underlyings)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/master_contract/watch_expiries')
@login_required
def api_watch_expiries():
    """Get distinct future expiry dates for a given underlying from master_contract"""
    underlying = (request.args.get('underlying') or '').strip().upper()
    if not underlying:
        return jsonify({'error': 'underlying is required'}), 400
    try:
        conn = get_db()
        c = conn.cursor()
        # Resolve the master_contract `name` for this underlying via its FUT symbol
        fut_row = c.execute("""
            SELECT name FROM master_contract
            WHERE exchange = 'NFO' AND instrument_type = 'FUT'
            AND trading_symbol LIKE ?
            ORDER BY expiry ASC
            LIMIT 1
        """, (underlying + '%',)).fetchone()
        if fut_row:
            rows = c.execute("""
                SELECT DISTINCT expiry FROM master_contract
                WHERE name = ? AND exchange = 'NFO'
                AND instrument_type IN ('CE', 'PE')
                AND expiry > date('now')
                ORDER BY expiry ASC
            """, (fut_row['name'],)).fetchall()
        else:
            rows = c.execute("""
                SELECT DISTINCT expiry FROM master_contract
                WHERE trading_symbol LIKE ? AND exchange = 'NFO'
                AND instrument_type IN ('CE', 'PE')
                AND expiry > date('now')
                ORDER BY expiry ASC
            """, (underlying + '%',)).fetchall()
        conn.close()
        expiries = [row['expiry'] for row in rows if row['expiry']]
        return jsonify({'success': True, 'expiries': expiries})
    except Exception as e:
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
            notification_data = json.loads(notif['notification_data']) if notif['notification_data'] else {}
            openalgo_hint = build_openalgo_order_hint(
                notif['notification_type'],
                notification_data,
                notif['message']
            )
            result.append({
                'id': notif['id'],
                'subscription_id': notif['subscription_id'],
                'message': notif['message'],
                'notification_type': notif['notification_type'],
                'notification_data': notification_data,
                'openalgo_hint': openalgo_hint,
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

@app.route('/api/notifications/simulate_events', methods=['GET'])
@login_required
def get_simulate_events():
    """Return recent position_changes available for notification simulation"""
    try:
        profile_slug = request.args.get('profile_slug')
        if not profile_slug:
            return jsonify({'error': 'profile_slug is required'}), 400

        conn = get_db()
        c = conn.cursor()

        profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (profile_slug,)).fetchone()
        if not profile:
            conn.close()
            return jsonify({'error': 'Profile not found'}), 404
        profile_id = profile['id']

        changes = c.execute("""
            SELECT pc.id, pc.snapshot_id, pc.timestamp, pc.diff_summary
            FROM position_changes pc
            WHERE pc.profile_id = ?
              AND pc.diff_summary != 'Initial Snapshot'
              AND EXISTS (
                  SELECT 1 FROM snapshots s
                  WHERE s.profile_id = pc.profile_id AND s.id < pc.snapshot_id
              )
            ORDER BY pc.timestamp DESC
            LIMIT 50
        """, (profile_id,)).fetchall()
        conn.close()

        return jsonify({'success': True, 'events': [dict(c) for c in changes]})

    except Exception as e:
        print(f"Error getting simulate events: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/notifications/simulate', methods=['POST'])
@login_required
def simulate_notifications():
    """Replay a historical position change through generate_notifications_for_changes"""
    try:
        from scraper import generate_notifications_for_changes

        data = request.get_json()
        profile_slug = data.get('profile_slug')
        change_id = data.get('change_id')

        if not profile_slug or not change_id:
            return jsonify({'error': 'profile_slug and change_id are required'}), 400

        conn = get_db()
        c = conn.cursor()

        profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (profile_slug,)).fetchone()
        if not profile:
            conn.close()
            return jsonify({'error': 'Profile not found'}), 404
        profile_id = profile['id']

        change = c.execute("""
            SELECT snapshot_id FROM position_changes WHERE id = ? AND profile_id = ?
        """, (change_id, profile_id)).fetchone()
        if not change:
            conn.close()
            return jsonify({'error': 'Change event not found'}), 404

        after_snapshot_id = change['snapshot_id']

        after_snap = c.execute("SELECT raw_data FROM snapshots WHERE id = ?", (after_snapshot_id,)).fetchone()
        before_snap = c.execute("""
            SELECT raw_data FROM snapshots
            WHERE profile_id = ? AND id < ?
            ORDER BY id DESC LIMIT 1
        """, (profile_id, after_snapshot_id)).fetchone()

        if not after_snap or not before_snap:
            conn.close()
            return jsonify({'error': 'Could not find before/after snapshots for this event'}), 404

        before_data = json.loads(before_snap['raw_data'])
        after_data = json.loads(after_snap['raw_data'])

        # Record the max notification id before simulation
        max_id_row = c.execute("SELECT COALESCE(MAX(id), 0) FROM notifications WHERE profile_id = ?", (profile_id,)).fetchone()
        max_id_before = max_id_row[0]

        generate_notifications_for_changes(conn, profile_id, before_data, after_data)

        new_notifs = c.execute("""
            SELECT id, message, notification_type, notification_data, created_at
            FROM notifications
            WHERE profile_id = ? AND id > ?
            ORDER BY id ASC
        """, (profile_id, max_id_before)).fetchall()

        conn.close()

        created = [dict(n) for n in new_notifs]
        return jsonify({'success': True, 'created': len(created), 'notifications': created})

    except Exception as e:
        import traceback
        print(f"Error simulating notifications: {traceback.format_exc()}")
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


@app.route('/api/profile/<slug>/pnl_history')
@login_required
def api_profile_pnl_history(slug):
    """Return daily PnL for a profile over a date range."""
    from_date = request.args.get('from')
    to_date   = request.args.get('to')

    today_str = now_ist().strftime('%Y-%m-%d')

    if not to_date:
        to_date = today_str
    if not from_date:
        # default last 30 days
        from_dt = now_ist() - timedelta(days=29)
        from_date = from_dt.strftime('%Y-%m-%d')

    try:
        from_dt = datetime.strptime(from_date, '%Y-%m-%d').date()
        to_dt   = datetime.strptime(to_date,   '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date format, use YYYY-MM-DD'}), 400

    if from_dt > to_dt:
        return jsonify({'error': 'from date must be <= to date'}), 400

    conn = get_db()
    c = conn.cursor()

    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404

    # Build list of all dates in range
    days = []
    cur = from_dt
    while cur <= to_dt:
        days.append(cur.strftime('%Y-%m-%d'))
        cur += timedelta(days=1)

    result = []
    cumulative = 0.0
    for d in days:
        metrics      = get_daily_pnl_metrics(c, profile['id'], d)
        booked_pnl   = round(metrics['booked_pnl'], 2)
        unbooked_pnl = round(metrics['current_pnl'] - metrics['booked_pnl'], 2)
        net_pnl      = round(metrics['current_pnl'], 2)
        cumulative   = round(cumulative + booked_pnl, 2)
        result.append({
            'date':         d,
            'booked_pnl':   booked_pnl,
            'unbooked_pnl': unbooked_pnl,
            'net_pnl':      net_pnl,
            'cumulative':   cumulative,
        })

    conn.close()
    return jsonify({'success': True, 'data': result, 'slug': slug})


@app.route('/api/profile/<slug>/pnl_snapshots')
@login_required
def api_profile_pnl_snapshots(slug):
    """Return PnL at each snapshot, optionally resampled by interval.

    Query params:
        from  - YYYY-MM-DD (default: today)
        to    - YYYY-MM-DD (default: today)
        interval - raw | 5m | 15m | 30m | 1h | 4h | 1d (default: raw)
    """
    from_date = request.args.get('from', now_ist().strftime('%Y-%m-%d'))
    to_date   = request.args.get('to',   now_ist().strftime('%Y-%m-%d'))
    interval  = request.args.get('interval', 'raw')

    interval_minutes = {
        'raw': None, '5m': 5, '15m': 15, '30m': 30,
        '1h': 60,    '4h': 240, '1d': 1440
    }
    if interval not in interval_minutes:
        return jsonify({'error': f'Invalid interval. Choose from: {", ".join(interval_minutes)}'}), 400

    try:
        from_dt = datetime.strptime(from_date, '%Y-%m-%d')
        to_dt   = datetime.strptime(to_date,   '%Y-%m-%d')
        to_dt   = to_dt.replace(hour=23, minute=59, second=59)
    except ValueError:
        return jsonify({'error': 'Invalid date format, use YYYY-MM-DD'}), 400

    if from_dt > to_dt:
        return jsonify({'error': 'from date must be <= to date'}), 400

    conn = get_db()
    c = conn.cursor()

    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404

    # Fetch all snapshots in range using date() to handle ISO tz strings correctly
    snapshots = c.execute("""
        SELECT id, timestamp FROM snapshots
        WHERE profile_id = ? AND date(timestamp) >= ? AND date(timestamp) <= ?
        ORDER BY timestamp ASC
    """, (profile['id'], from_date, to_date)).fetchall()

    def parse_snapshot_ts(ts_str):
        """Parse ISO timestamp stored with or without timezone, return naive datetime."""
        ts = ts_str[:19].replace('T', ' ')   # "2024-01-15T09:30:00+05:30" → "2024-01-15 09:30:00"
        return datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')

    # Compute PnL for each snapshot
    raw_points = []
    for snap in snapshots:
        total_pnl, _ = calculate_snapshot_pnl(c, snap['id'])
        raw_points.append({
            'ts':  snap['timestamp'],
            'pnl': round(total_pnl, 2)
        })

    conn.close()

    if not raw_points:
        return jsonify({'success': True, 'data': [], 'slug': slug, 'interval': interval})

    # Resample if requested
    bucket_mins = interval_minutes[interval]
    if bucket_mins is None:
        # Return raw points with normalised timestamp display
        result = []
        for p in raw_points:
            try:
                dt = parse_snapshot_ts(p['ts'])
                result.append({'timestamp': dt.strftime('%Y-%m-%d %H:%M'), 'pnl': p['pnl']})
            except Exception:
                result.append({'timestamp': p['ts'][:16], 'pnl': p['pnl']})
    else:
        # Group into time buckets; take last value in each bucket
        from collections import OrderedDict
        buckets = OrderedDict()
        for p in raw_points:
            try:
                dt = parse_snapshot_ts(p['ts'])
            except Exception:
                continue
            # Floor to bucket boundary
            total_mins = dt.hour * 60 + dt.minute
            bucket_start_mins = (total_mins // bucket_mins) * bucket_mins
            bucket_dt = dt.replace(
                hour=bucket_start_mins // 60,
                minute=bucket_start_mins % 60,
                second=0
            )
            bucket_key = bucket_dt.strftime('%Y-%m-%d %H:%M')
            buckets[bucket_key] = p['pnl']  # last value wins

        result = [{'timestamp': k, 'pnl': v} for k, v in buckets.items()]

    return jsonify({'success': True, 'data': result, 'slug': slug, 'interval': interval,
                    'count': len(result)})


@app.route('/api/profile/<slug>/pnl_snapshots_by_expiry/<date>')
@login_required
def api_pnl_snapshots_by_expiry(slug, date):
    """Return PnL time series filtered to a specific underlying + set of symbols.

    Query params:
        underlying  - e.g. "HDFCBANK"
        symbols     - comma-separated trading symbols
        from_date   - YYYY-MM-DD start date (defaults to <date>)
        to_date     - YYYY-MM-DD end date   (defaults to <date>)
    """
    underlying_filter = request.args.get('underlying', '')
    symbols_param     = request.args.get('symbols', '')
    symbol_set        = set(s.strip() for s in symbols_param.split(',') if s.strip())
    from_date         = request.args.get('from_date', date)
    to_date           = request.args.get('to_date',   date)

    conn = get_db()
    c    = conn.cursor()

    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404

    snapshots = c.execute("""
        SELECT id, timestamp, raw_data FROM snapshots
        WHERE profile_id = ? AND date(timestamp) BETWEEN ? AND ?
        ORDER BY timestamp ASC
    """, (profile['id'], from_date, to_date)).fetchall()

    conn.close()

    multi_day = from_date != to_date

    result = []
    for snap in snapshots:
        raw = json.loads(snap['raw_data'])
        pnl = 0.0
        for item in raw.get('data', []):
            item_underlying = item.get('trading_symbol') or item.get('underlying', '')
            if underlying_filter and item_underlying != underlying_filter:
                continue
            for trade in item.get('trades', []):
                if symbol_set and trade.get('trading_symbol', '') not in symbol_set:
                    continue
                pnl += (trade.get('unbooked_pnl', 0) or 0) + (trade.get('booked_profit_loss', 0) or 0)

        ts = snap['timestamp'][:19].replace('T', ' ')
        try:
            dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
            ts_display = dt.strftime('%d-%b %H:%M') if multi_day else dt.strftime('%H:%M')
        except Exception:
            ts_display = ts[:16]

        result.append({'timestamp': ts_display, 'pnl': round(pnl, 2)})

    return jsonify({'success': True, 'data': result, 'underlying': underlying_filter,
                    'count': len(result), 'from_date': from_date, 'to_date': to_date})


@app.route('/api/profile/<slug>/pnl_breakdown')
@login_required
def api_profile_pnl_breakdown(slug):
    """Return PnL broken down by underlying or expiry.

    Query params:
        from      - YYYY-MM-DD
        to        - YYYY-MM-DD
        group_by  - underlying | expiry  (default: underlying)
    Returns one entry per day with a dict of group → pnl.
    """
    from_date = request.args.get('from', now_ist().strftime('%Y-%m-%d'))
    to_date   = request.args.get('to',   now_ist().strftime('%Y-%m-%d'))
    group_by  = request.args.get('group_by', 'underlying')

    if group_by not in ('underlying', 'expiry'):
        return jsonify({'error': 'group_by must be "underlying" or "expiry"'}), 400

    try:
        from_dt = datetime.strptime(from_date, '%Y-%m-%d').date()
        to_dt   = datetime.strptime(to_date,   '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date format, use YYYY-MM-DD'}), 400

    if from_dt > to_dt:
        return jsonify({'error': 'from date must be <= to date'}), 400

    conn = get_db()
    c = conn.cursor()

    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404

    # Collect all unique group keys across the whole range (for consistent chart columns)
    all_keys = set()
    result = []

    cur = from_dt
    while cur <= to_dt:
        date_str = cur.strftime('%Y-%m-%d')

        # Get the last snapshot for this day
        snap = c.execute("""
            SELECT id, raw_data FROM snapshots
            WHERE profile_id = ? AND date(timestamp) = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (profile['id'], date_str)).fetchone()

        groups = {}
        groups_booked   = {}
        groups_unbooked = {}
        if snap:
            raw = json.loads(snap['raw_data'])
            for item in raw.get('data', []):
                underlying = item.get('trading_symbol') or item.get('underlying', 'UNKNOWN')
                for trade in item.get('trades', []):
                    booked   = trade.get('booked_profit_loss', 0)
                    unbooked = trade.get('unbooked_pnl', 0)
                    pnl      = booked + unbooked
                    if group_by == 'underlying':
                        key = underlying
                    else:
                        key = (trade.get('instrument_info') or {}).get('expiry', 'N/A')
                    groups[key]          = round(groups.get(key, 0)          + pnl,      2)
                    groups_booked[key]   = round(groups_booked.get(key, 0)   + booked,   2)
                    groups_unbooked[key] = round(groups_unbooked.get(key, 0) + unbooked, 2)

        all_keys.update(groups.keys())
        result.append({'date': date_str, 'groups': groups,
                        'groups_booked': groups_booked, 'groups_unbooked': groups_unbooked})
        cur += timedelta(days=1)

    conn.close()
    return jsonify({
        'success':  True,
        'data':     result,
        'keys':     sorted(all_keys),
        'group_by': group_by,
        'slug':     slug
    })


@app.route('/api/profile/<slug>/symbol_expiry_map/<date>')
@login_required
def api_symbol_expiry_map(slug, date):
    """Return {trading_symbol: expiry_date} from the last snapshot of the given date.
    Used by the frontend to display correct expiry dates instead of computed ones.
    """
    conn = get_db()
    c    = conn.cursor()

    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404

    snap = c.execute("""
        SELECT raw_data FROM snapshots
        WHERE profile_id = ? AND date(timestamp) = ?
        ORDER BY timestamp DESC LIMIT 1
    """, (profile['id'], date)).fetchone()

    conn.close()

    if not snap:
        return jsonify({'success': True, 'map': {}})

    result = {}
    for item in json.loads(snap['raw_data']).get('data', []):
        for trade in item.get('trades', []):
            sym    = trade.get('trading_symbol', '')
            expiry = (trade.get('instrument_info') or {}).get('expiry', '')
            if sym and expiry:
                result[sym] = expiry

    return jsonify({'success': True, 'map': result})


@app.route('/api/profile/<slug>/pnl_by_expiry/<date>')
@login_required
def api_pnl_by_expiry(slug, date):
    """Return PnL broken down by expiry for each underlying on a given date.

    Uses the last snapshot of that date.
    Returns:
      {
        underlyings: {
          "NIFTY": {
            "2026-03-27": { pnl, unbooked_pnl, booked_pnl, trades: [{symbol, pnl, qty, opt_type, strike}] },
            ...
          }
        },
        snapshot_time: "HH:MM"
      }
    """
    conn = get_db()
    c = conn.cursor()

    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404

    snap = c.execute("""
        SELECT id, timestamp, raw_data FROM snapshots
        WHERE profile_id = ? AND date(timestamp) = ?
        ORDER BY timestamp DESC LIMIT 1
    """, (profile['id'], date)).fetchone()

    conn.close()

    if not snap:
        return jsonify({'error': 'No snapshot found for this date'}), 404

    raw = json.loads(snap['raw_data'])
    result = {}

    for item in raw.get('data', []):
        underlying = item.get('trading_symbol') or item.get('underlying', 'UNKNOWN')
        result.setdefault(underlying, {})

        for trade in item.get('trades', []):
            u_pnl  = trade.get('unbooked_pnl', 0) or 0
            b_pnl  = trade.get('booked_profit_loss', 0) or 0
            pnl    = u_pnl + b_pnl
            info   = trade.get('instrument_info') or {}
            expiry = info.get('expiry') or 'N/A'

            if expiry not in result[underlying]:
                result[underlying][expiry] = {
                    'pnl': 0, 'unbooked_pnl': 0, 'booked_pnl': 0, 'trades': []
                }

            result[underlying][expiry]['pnl']          = round(result[underlying][expiry]['pnl'] + pnl, 2)
            result[underlying][expiry]['unbooked_pnl']  = round(result[underlying][expiry]['unbooked_pnl'] + u_pnl, 2)
            result[underlying][expiry]['booked_pnl']    = round(result[underlying][expiry]['booked_pnl'] + b_pnl, 2)
            result[underlying][expiry]['trades'].append({
                'symbol':   trade.get('trading_symbol', ''),
                'qty':      trade.get('quantity', 0),
                'opt_type': info.get('instrument_type', ''),
                'strike':   info.get('strike', ''),
                'pnl':      round(pnl, 2),
                'unbooked': round(u_pnl, 2),
                'booked':   round(b_pnl, 2),
                'avg_price': trade.get('average_price', 0),
                'ltp':      trade.get('last_price', 0),
            })

    # Parse snapshot timestamp to display time
    ts_raw = snap['timestamp']
    try:
        ts_clean = ts_raw[:19].replace('T', ' ')
        snap_dt  = datetime.strptime(ts_clean, '%Y-%m-%d %H:%M:%S')
        snap_time = snap_dt.strftime('%H:%M:%S')
    except Exception:
        snap_time = ts_raw

    return jsonify({'success': True, 'underlyings': result, 'snapshot_time': snap_time, 'date': date})


@app.route('/profile/<slug>')
@login_required
def profile_overview(slug):
    """Profile overview page with PnL curve."""
    conn = get_db()
    c = conn.cursor()
    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return "Profile not found", 404
    conn.close()
    return render_template('profile_overview.html', slug=slug, profile=profile)


# ============================================================================
# EXPORT / IMPORT ROUTES
# ============================================================================

@app.route('/api/sensibull/snapshot/<slug>')
@login_required
def sensibull_snapshot_proxy(slug):
    """Proxy Sensibull live snapshot to avoid browser CORS restrictions."""
    url = f"https://oxide.sensibull.com/v1/compute/verified_by_sensibull/live_positions/snapshot/{slug}"
    try:
        resp = http_requests.get(url, timeout=10)
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


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

# ============================================================================
# AI CHAT ROUTES
# ============================================================================

# Qwen OAuth constants
_QWEN_OAUTH_CREDS_PATH = os.path.expanduser('~/.qwen/oauth_creds.json')
_QWEN_CLIENT_ID = 'f0304373b74a44d2b584a3fb70ca9e56'
_QWEN_DEVICE_CODE_URL = 'https://chat.qwen.ai/api/v1/oauth2/device/code'
_QWEN_TOKEN_URL = 'https://chat.qwen.ai/api/v1/oauth2/token'
_QWEN_DEFAULT_API_BASE = 'https://dashscope.aliyuncs.com/compatible-mode/v1'


def _get_qwen_oauth_token():
    """Return (access_token, api_base_url) from ~/.qwen/oauth_creds.json, refreshing if expired."""
    if not os.path.exists(_QWEN_OAUTH_CREDS_PATH):
        raise FileNotFoundError('Qwen credentials not found — please connect your Qwen account.')
    with open(_QWEN_OAUTH_CREDS_PATH, 'r') as f:
        creds = json.load(f)
    access_token = creds.get('access_token', '')
    refresh_token = creds.get('refresh_token', '')
    expiry_date = creds.get('expiry_date', 0)  # epoch milliseconds
    resource_url = creds.get('resource_url') or _QWEN_DEFAULT_API_BASE
    # Refresh if within 5 minutes of expiry
    now_ms = int(time.time() * 1000)
    if expiry_date and now_ms >= (expiry_date - 5 * 60 * 1000):
        if not refresh_token:
            raise ValueError('Qwen token expired — please reconnect your Qwen account.')
        resp = http_requests.post(_QWEN_TOKEN_URL, data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': _QWEN_CLIENT_ID,
        }, headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}, timeout=15)
        resp.raise_for_status()
        new_creds = resp.json()
        expires_in = new_creds.get('expires_in')
        new_expiry = int(time.time() * 1000) + expires_in * 1000 if expires_in else new_creds.get('expiry_date', expiry_date)
        creds.update({
            'access_token': new_creds.get('access_token', access_token),
            'refresh_token': new_creds.get('refresh_token', refresh_token),
            'expiry_date': new_expiry,
        })
        os.makedirs(os.path.dirname(_QWEN_OAUTH_CREDS_PATH), exist_ok=True)
        with open(_QWEN_OAUTH_CREDS_PATH, 'w') as f:
            json.dump(creds, f, indent=2)
        access_token = creds['access_token']
        resource_url = creds.get('resource_url') or _QWEN_DEFAULT_API_BASE
    normalized = resource_url if resource_url.startswith('http') else f'https://{resource_url}'
    api_base = normalized.rstrip('/')
    if not api_base.endswith('/v1'):
        api_base = api_base + '/v1'
    return access_token, api_base

@app.route('/api/ai/providers')
@login_required
def api_ai_providers():
    """Return available AI providers and their models based on configured API keys."""
    providers = []
    # if os.environ.get('ANTHROPIC_API_KEY'):
    #     providers.append({
    #         'id': 'anthropic',
    #         'name': 'Claude (Anthropic)',
    #         'connected': True,
    #         'models': [
    #             {'id': 'claude-sonnet-4-6', 'name': 'Sonnet 4.6 (balanced)'},
    #             {'id': 'claude-haiku-4-5-20251001', 'name': 'Haiku 4.5 (fast)'},
    #             {'id': 'claude-opus-4-6', 'name': 'Opus 4.6 (deep)'},
    #         ],
    #     })
    if os.environ.get('GEMINI_API_KEY'):
        providers.append({
            'id': 'gemini',
            'name': 'Gemini (Google)',
            'connected': True,
            'models': [
                {'id': 'gemini-2.5-pro-preview-06-05', 'name': 'Gemini 2.5 Pro (best)'},
                {'id': 'gemini-2.5-flash-preview-05-20', 'name': 'Gemini 2.5 Flash (fast)'},
                {'id': 'gemini-2.0-pro-exp', 'name': 'Gemini 2.0 Pro Experimental'},
                {'id': 'gemini-2.0-flash', 'name': 'Gemini 2.0 Flash'},
                {'id': 'gemini-1.5-pro', 'name': 'Gemini 1.5 Pro'},
            ],
        })
    if os.environ.get('OPENROUTER_API_KEY'):
        providers.append({
            'id': 'openrouter',
            'name': 'OpenRouter',
            'connected': True,
            'models': [
                {'id': 'google/gemini-2.5-flash-preview', 'name': 'Gemini 2.5 Flash'},
                # {'id': 'anthropic/claude-3-5-sonnet', 'name': 'Claude 3.5 Sonnet'},
                {'id': 'meta-llama/llama-3.3-70b-instruct', 'name': 'Llama 3.3 70B'},
                {'id': 'deepseek/deepseek-chat', 'name': 'DeepSeek Chat'},
            ],
        })
    if os.environ.get('QWEN_API_KEY'):
        qwen_models = [
            {'id': 'qwen-max', 'name': 'Qwen Max (best)'},
            {'id': 'qwen-plus', 'name': 'Qwen Plus (balanced)'},
            {'id': 'qwen-turbo', 'name': 'Qwen Turbo (fast)'},
        ]
    else:
        qwen_models = [
            {'id': 'coder-model', 'name': 'Qwen Coder'},
        ]
    providers.append({
        'id': 'qwen',
        'name': 'Qwen (Alibaba)',
        'connected': True,
        'models': qwen_models,
    })
    return jsonify({'providers': providers})


@app.route('/api/ai/qwen/status')
@login_required
def api_ai_qwen_status():
    """Check whether Qwen credentials exist and are valid (API key or OAuth)."""
    if os.environ.get('QWEN_API_KEY'):
        return jsonify({'connected': True, 'auth_type': 'api_key'})
    try:
        _get_qwen_oauth_token()
        return jsonify({'connected': True, 'auth_type': 'oauth'})
    except Exception as e:
        return jsonify({'connected': False, 'reason': str(e)})


@app.route('/api/ai/qwen/auth/start', methods=['POST'])
@login_required
def api_ai_qwen_auth_start():
    """Initiate Qwen OAuth2 Device Authorization Flow with PKCE."""
    import hashlib, base64, secrets as _secrets
    code_verifier = _secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    try:
        resp = http_requests.post(_QWEN_DEVICE_CODE_URL, json={
            'client_id': _QWEN_CLIENT_ID,
            'scope': 'openid profile email model.completion',
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256',
        }, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/json',
            'Origin': 'https://chat.qwen.ai',
            'Referer': 'https://chat.qwen.ai/',
        })
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        detail = str(e)
        try:
            detail = f'HTTP {resp.status_code}: {resp.text[:500]}'
        except Exception:
            pass
        return jsonify({'error': detail}), 502
    session['qwen_code_verifier'] = code_verifier
    session['qwen_device_code'] = data.get('device_code')
    return jsonify({
        'user_code': data.get('user_code'),
        'verification_uri': data.get('verification_uri'),
        'verification_uri_complete': data.get('verification_uri_complete'),
        'expires_in': data.get('expires_in', 300),
        'interval': data.get('interval', 5),
    })


@app.route('/api/ai/qwen/auth/poll')
@login_required
def api_ai_qwen_auth_poll():
    """SSE endpoint that polls the Qwen token endpoint until authorized or timed out."""
    from flask import stream_with_context
    device_code = session.get('qwen_device_code')
    code_verifier = session.get('qwen_code_verifier')
    if not device_code or not code_verifier:
        return jsonify({'error': 'No pending Qwen auth session'}), 400

    def poll_generator():
        for _ in range(60):  # ~5 minutes at 5s interval
            time.sleep(5)
            try:
                resp = http_requests.post(_QWEN_TOKEN_URL, data={
                    'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                    'device_code': device_code,
                    'client_id': _QWEN_CLIENT_ID,
                    'code_verifier': code_verifier,
                }, headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}, timeout=15)
                if resp.status_code == 200:
                    token_data = resp.json()
                    os.makedirs(os.path.dirname(_QWEN_OAUTH_CREDS_PATH), exist_ok=True)
                    with open(_QWEN_OAUTH_CREDS_PATH, 'w') as f:
                        json.dump(token_data, f, indent=2)
                    yield f"data: {json.dumps({'type': 'success'})}\n\n"
                    return
                elif resp.status_code == 428:  # authorization_pending
                    yield f"data: {json.dumps({'type': 'pending'})}\n\n"
                elif resp.status_code == 429:  # slow_down
                    time.sleep(5)
                    yield f"data: {json.dumps({'type': 'pending'})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'message': resp.text})}\n\n"
                    return
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                return
        yield f"data: {json.dumps({'type': 'error', 'message': 'Authentication timed out'})}\n\n"

    return app.response_class(
        stream_with_context(poll_generator()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/ai/qwen/auth/logout', methods=['POST'])
@login_required
def api_ai_qwen_auth_logout():
    """Remove saved Qwen credentials."""
    if os.path.exists(_QWEN_OAUTH_CREDS_PATH):
        os.remove(_QWEN_OAUTH_CREDS_PATH)
    session.pop('qwen_device_code', None)
    session.pop('qwen_code_verifier', None)
    return jsonify({'ok': True})


@app.route('/api/ai/chat/history')
@login_required
def api_ai_chat_history():
    """Return persisted AI chat history for a given scope."""
    slug = (request.args.get('slug') or '').strip()
    scope_type = (request.args.get('scope_type') or '').strip()
    underlying = (request.args.get('underlying') or '').strip().upper()
    expiry_key = (request.args.get('expiry_key') or '').strip()

    if not slug or not scope_type or not underlying:
        return jsonify({'error': 'slug, scope_type, underlying required', 'messages': []}), 400

    conn = get_db()
    c = conn.cursor()

    profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found', 'messages': []}), 404

    profile_id = profile['id']

    if scope_type == 'expiry' and expiry_key:
        rows = c.execute(
            """SELECT role, content, model, created_at FROM ai_chat_history
               WHERE profile_id = ? AND scope_type = ? AND underlying = ? AND expiry_key = ?
               ORDER BY id ASC LIMIT 200""",
            (profile_id, scope_type, underlying, expiry_key),
        ).fetchall()
    else:
        rows = c.execute(
            """SELECT role, content, model, created_at FROM ai_chat_history
               WHERE profile_id = ? AND scope_type = ? AND underlying = ? AND (expiry_key IS NULL OR expiry_key = '')
               ORDER BY id ASC LIMIT 200""",
            (profile_id, scope_type, underlying),
        ).fetchall()

    conn.close()
    messages = [{'role': r['role'], 'content': r['content'], 'model': r['model'], 'created_at': r['created_at']} for r in rows]
    return jsonify({'messages': messages})


@app.route('/api/ai/chat/clear', methods=['DELETE'])
@login_required
def api_ai_chat_clear():
    """Clear AI chat history for a given scope."""
    body = request.get_json() or {}
    slug = (body.get('slug') or '').strip()
    scope_type = (body.get('scope_type') or '').strip()
    underlying = (body.get('underlying') or '').strip().upper()
    expiry_key = (body.get('expiry_key') or '').strip()

    if not slug or not scope_type or not underlying:
        return jsonify({'error': 'slug, scope_type, underlying required'}), 400

    conn = get_db()
    c = conn.cursor()

    profile = c.execute("SELECT id FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404

    profile_id = profile['id']

    if scope_type == 'expiry' and expiry_key:
        c.execute(
            "DELETE FROM ai_chat_history WHERE profile_id = ? AND scope_type = ? AND underlying = ? AND expiry_key = ?",
            (profile_id, scope_type, underlying, expiry_key),
        )
    else:
        c.execute(
            "DELETE FROM ai_chat_history WHERE profile_id = ? AND scope_type = ? AND underlying = ? AND (expiry_key IS NULL OR expiry_key = '')",
            (profile_id, scope_type, underlying),
        )

    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/ai/debug-prompt', methods=['POST'])
@login_required
def api_ai_debug_prompt():
    """Return the exact system prompt that would be sent to the AI for a given request."""
    body = request.get_json() or {}
    slug = (body.get('slug') or '').strip()
    scope_type = (body.get('scope_type') or 'underlying').strip()
    underlying = (body.get('underlying') or '').strip().upper()
    expiry_key = (body.get('expiry_key') or '').strip()

    if not slug or not underlying:
        return jsonify({'error': 'slug and underlying required'}), 400

    conn = get_db()
    c = conn.cursor()
    profile = c.execute("SELECT id, name FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404

    profile_name = profile['name'] or slug
    prompt = _build_ai_system_prompt(c, profile['id'], profile_name, underlying, scope_type, expiry_key)
    conn.close()
    return jsonify({'system_prompt': prompt})


@app.route('/api/ai/chat', methods=['POST'])
@login_required
def api_ai_chat():
    """Stream an AI chat response with full trade context.

    The 'model' field uses the format 'provider:model_id', e.g.:
      anthropic:claude-sonnet-4-6
      gemini:gemini-2.5-flash
      openrouter:google/gemini-2.5-flash-preview
      qwen:qwen-plus
    If no provider prefix is given, 'anthropic' is assumed.
    """
    from flask import stream_with_context

    body = request.get_json() or {}
    slug = (body.get('slug') or '').strip()
    scope_type = (body.get('scope_type') or 'underlying').strip()
    underlying = (body.get('underlying') or '').strip().upper()
    expiry_key = (body.get('expiry_key') or '').strip()
    user_message = (body.get('message') or '').strip()
    raw_model = (body.get('model') or 'anthropic:claude-sonnet-4-6').strip()
    consolidated_trades = body.get('consolidated_trades')  # Optional: pre-consolidated trade data from frontend

    # Parse provider:model format
    if ':' in raw_model:
        provider, model_id = raw_model.split(':', 1)
    else:
        provider, model_id = 'anthropic', raw_model

    provider = provider.lower()

    if not slug or not underlying or not user_message:
        return jsonify({'error': 'slug, underlying, message required'}), 400

    # Validate provider and resolve credentials
    if provider == 'anthropic':
        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return jsonify({'error': 'ANTHROPIC_API_KEY is not configured in .env'}), 500
        _ANTHROPIC_MODELS = {'claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'claude-opus-4-6'}
        if model_id not in _ANTHROPIC_MODELS:
            model_id = 'claude-sonnet-4-6'
    elif provider == 'gemini':
        api_key = os.environ.get('GEMINI_API_KEY', '')
        if not api_key:
            return jsonify({'error': 'GEMINI_API_KEY is not configured in .env'}), 500
        api_base = 'https://generativelanguage.googleapis.com/v1beta/openai/'
    elif provider == 'openrouter':
        api_key = os.environ.get('OPENROUTER_API_KEY', '')
        if not api_key:
            return jsonify({'error': 'OPENROUTER_API_KEY is not configured in .env'}), 500
        api_base = 'https://openrouter.ai/api/v1'
    elif provider == 'qwen':
        qwen_env_key = os.environ.get('QWEN_API_KEY', '')
        if qwen_env_key:
            api_key = qwen_env_key
            api_base = _QWEN_DEFAULT_API_BASE
        else:
            try:
                api_key, api_base = _get_qwen_oauth_token()
                model_id = 'coder-model'  # portal.qwen.ai only supports this model
            except Exception as e:
                return jsonify({'error': str(e)}), 401
    else:
        return jsonify({'error': f'Unknown provider: {provider}'}), 400

    conn = get_db()
    c = conn.cursor()

    profile = c.execute("SELECT id, name FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404

    profile_id = profile['id']
    profile_name = profile['name'] or slug
    norm_expiry_key = expiry_key if (scope_type == 'expiry' and expiry_key) else None
    stored_model = raw_model  # store the full provider:model string

    # Save user message immediately
    now_ts = now_ist().isoformat()
    c.execute(
        """INSERT INTO ai_chat_history (profile_id, scope_type, underlying, expiry_key, role, content, model, created_at)
           VALUES (?, ?, ?, ?, 'user', ?, ?, ?)""",
        (profile_id, scope_type, underlying, norm_expiry_key, user_message, stored_model, now_ts),
    )
    conn.commit()

    # Fetch recent history (last 40 messages = 20 exchanges)
    if norm_expiry_key:
        history_rows = c.execute(
            """SELECT role, content FROM ai_chat_history
               WHERE profile_id = ? AND scope_type = ? AND underlying = ? AND expiry_key = ?
               ORDER BY id DESC LIMIT 41""",
            (profile_id, scope_type, underlying, norm_expiry_key),
        ).fetchall()
    else:
        history_rows = c.execute(
            """SELECT role, content FROM ai_chat_history
               WHERE profile_id = ? AND scope_type = ? AND underlying = ? AND (expiry_key IS NULL OR expiry_key = '')
               ORDER BY id DESC LIMIT 41""",
            (profile_id, scope_type, underlying),
        ).fetchall()

    history_rows = list(reversed(history_rows))
    messages_for_ai = [{'role': r['role'], 'content': r['content']} for r in history_rows[:-1]]
    messages_for_ai.append({'role': 'user', 'content': user_message})

    system_prompt = _build_ai_system_prompt(c, profile_id, profile_name, underlying, scope_type, expiry_key, consolidated_trades)
    conn.close()

    def _save_response(full_text):
        save_conn = get_db()
        save_c = save_conn.cursor()
        save_c.execute(
            """INSERT INTO ai_chat_history (profile_id, scope_type, underlying, expiry_key, role, content, model, created_at)
               VALUES (?, ?, ?, ?, 'assistant', ?, ?, ?)""",
            (profile_id, scope_type, underlying, norm_expiry_key, full_text, stored_model, now_ist().isoformat()),
        )
        save_conn.commit()
        save_conn.close()

    def generate_anthropic():
        import anthropic as _anthropic
        full_response = []
        try:
            ai_client = _anthropic.Anthropic(api_key=api_key)
            with ai_client.messages.stream(
                model=model_id,
                max_tokens=2048,
                system=system_prompt,
                messages=messages_for_ai,
            ) as stream:
                for text in stream.text_stream:
                    full_response.append(text)
                    yield f"data: {json.dumps({'type': 'chunk', 'text': text})}\n\n"
            _save_response(''.join(full_response))
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    def generate_openai_compatible(base_url, key, extra_headers=None):
        from openai import OpenAI
        full_response = []
        try:
            client_kwargs = {'api_key': key, 'base_url': base_url}
            oa_client = OpenAI(**client_kwargs)
            oa_messages = [{'role': 'system', 'content': system_prompt}] + messages_for_ai
            stream = oa_client.chat.completions.create(
                model=model_id,
                messages=oa_messages,
                max_tokens=2048,
                stream=True,
                extra_headers=extra_headers or {},
            )
            for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    full_response.append(delta.content)
                    yield f"data: {json.dumps({'type': 'chunk', 'text': delta.content})}\n\n"
            _save_response(''.join(full_response))
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    if provider == 'anthropic':
        generator = generate_anthropic()
    elif provider == 'openrouter':
        generator = generate_openai_compatible(api_base, api_key, extra_headers={
            'HTTP-Referer': 'https://sensibull-tracker',
            'X-Title': 'Sensibull Trade Tracker',
        })
    else:
        # gemini and qwen both use plain OpenAI-compatible calls
        generator = generate_openai_compatible(api_base, api_key)

    return app.response_class(
        stream_with_context(generator),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )

# ============================================================================
# END AI CHAT ROUTES
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
