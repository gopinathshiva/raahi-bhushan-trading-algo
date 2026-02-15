# Sensibull Position Tracker - Project Memory

## Project Overview

A real-time position tracking system for monitoring Sensibull trading profiles. The application scrapes position data from multiple Sensibull profiles and displays changes in a web dashboard with P&L tracking.

### Core Components

1. **scraper.py** - Background service that fetches position data from Sensibull API
2. **app.py** - Flask web application serving the dashboard (runs on port 6060)
3. **database.py** - SQLite database management and profile synchronization
4. **urls.txt** - List of Sensibull profile slugs to monitor

### Key Features

- **Real-time Position Tracking**: Monitors position changes during market hours (Mon-Fri, 09:15-15:30)
- **Historical Data**: Stores snapshots and tracks changes over time (30-day retention)
- **P&L Calculation**: Displays daily P&L with real-time updates
- **Change Detection**: Only records snapshots when positions actually change
- **Auto-sync Profiles**: Automatically adds/removes profiles based on urls.txt

### Database Schema

- **profiles** - Unique trader profiles (synced from urls.txt)
- **snapshots** - Full position snapshots when changes are detected
- **position_changes** - Index of when changes occurred for quick lookup
- **latest_snapshots** - Current state for real-time P&L (updated every scraper run)

### Architecture Flow

```
urls.txt → scraper.py → Database (SQLite) → app.py → Web UI (port 6060)
              ↓                                          ↑
        Sensibull API                              User Browser
```

### Configuration

- **Scraper Interval**: 60 seconds
- **Market Hours**: Mon-Fri, 09:15-15:30 IST
- **Data Retention**: 30 days
- **Web Port**: 6060
- **Database**: sensibull.db (SQLite)

---

## Recent Fixes (Feb 11, 2026)

### Issue Summary
User reported that only 4 out of 7 profile IDs were showing data in the web interface, and deleted IDs from urls.txt were still appearing.

### Root Causes Identified

1. **Stale Database Profiles**: Database contained 13 profiles, but urls.txt only had 7 (old profiles never removed)
2. **Scraper Logic Bug**: Duplicate data fetching and incorrect indentation causing profiles to be skipped
3. **Port Not Releasing**: Flask app with background threads wasn't shutting down cleanly (port 6060 stayed occupied)
4. **None Handling Bug**: `instrument_info` could be None, causing AttributeError crashes

### Fixes Applied

#### 1. Fixed Scraper Logic (scraper.py)
**Problem**: Code was fetching data twice and had incorrect indentation
```python
# BEFORE (lines 138-143)
for slug in slugs:
    print(f"Checking {slug}...")
    current_data = fetch_data(slug)  # First fetch - Result ignored!
    if not current_data:
        print(f"Skipping {slug}, no data.")
    conn.commit()  # Wrong indentation!
```

**Solution**: Removed duplicate fetch, fixed control flow
```python
# AFTER
for slug in slugs:
    print(f"Checking {slug}...")
    # ... profile lookup ...
    if not should_run:
        print(f"Skipping {slug} (market closed and data exists)")
        continue
    print(f"Processing {slug}...")
    current_data = fetch_data(slug)  # Single fetch at correct time
    if not current_data:
        print(f"Skipping {slug}, no data returned from API.")
        continue
```

#### 2. Added Profile Cleanup (database.py)
**Problem**: Profiles deleted from urls.txt remained in database forever

**Solution**: Enhanced `sync_profiles()` to remove old profiles
```python
# Added to sync_profiles() function
# Remove profiles that are no longer in urls.txt
if clean_slugs:
    placeholders = ','.join('?' * len(clean_slugs))
    c.execute(f"DELETE FROM profiles WHERE slug NOT IN ({placeholders})", clean_slugs)
    deleted = c.rowcount
    if deleted > 0:
        print(f"Removed {deleted} profiles no longer in urls.txt")
```

**Result**: Cleaned up 6 stale profiles (descriptive-armadillo, gaited-blenny, jolly-budgerigar, looking-laver, oculated-toy, stale-star)

#### 3. Added Graceful Shutdown (app.py)
**Problem**: Background monitor thread prevented clean exit, port 6060 stayed occupied

**Solution**: Added signal handlers
```python
import signal

def signal_handler(sig, frame):
    print('\nShutting down gracefully...')
    sys.exit(0)

if __name__ == '__main__':
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    threading.Thread(target=monitor_scraper, daemon=True).start()
    app.run(debug=False, host='0.0.0.0', port=PORT)
```

**Result**: App now exits cleanly on Ctrl+C, port releases immediately

#### 4. Fixed None Handling (scraper.py)
**Problem**: `instrument_info` field could be None, causing crashes
```python
# BEFORE
'strike': t.get('instrument_info', {}).get('strike'),  # Crashes if None!
```

**Solution**: Safe None handling
```python
# AFTER
instrument_info = t.get('instrument_info') or {}
'strike': instrument_info.get('strike'),
'option_type': instrument_info.get('instrument_type'),
```

Also updated sort key to handle None values:
```python
all_trades.sort(key=lambda x: (x['symbol'] or '', x['product'] or '', x['quantity'] or 0))
```

### Test Results

After fixes, all 7 profiles from urls.txt are now properly tracked:

| Profile | Status | Latest Data | Position Changes |
|---------|--------|-------------|------------------|
| latered-garage | ✅ Working | Feb 11, 2026 | 1 |
| ravishing-goose | ✅ Working | Feb 11, 2026 | 1 |
| valuable-woodpecker | ✅ Working | Feb 11, 2026 | 3 |
| popular-playground | ✅ Working | Feb 11, 2026 | 1 |
| skillful-bedbug | ✅ Working | Feb 11, 2026 | 1 |
| happening-earphones | ✅ Working | Feb 11, 2026 | 1 |
| easygoing-bird | ⚠️ No API data | Dec 23, 2025 | 0 |

**Note on easygoing-bird**: This profile shows in the UI but has no clickable data because the Sensibull API returns no position data for this ID. This could indicate an inactive/invalid profile.

### Files Modified

1. `scraper.py` - Fixed logic bugs, None handling
2. `database.py` - Added profile cleanup logic
3. `app.py` - Added signal handlers for graceful shutdown

### Testing Commands

```bash
# Run scraper once to test
uv run python scraper.py

# Check database state
sqlite3 sensibull.db "SELECT slug FROM profiles;"

# Start web app
uv run app.py

# Clean shutdown
# Press Ctrl+C (now works correctly!)

# Kill orphaned processes on port 6060
lsof -ti:6060 | xargs kill -9
```

---

## Development Notes

### Running the Application

```bash
# Terminal 1: Run the scraper
uv run python scraper.py

# Terminal 2: Run the web app
uv run python app.py

# Access dashboard
http://localhost:6060
```

### Modifying Tracked Profiles

1. Edit `urls.txt` - Add/remove Sensibull profile slugs (one per line)
2. Restart scraper and app - They auto-sync on startup
3. Old profiles are automatically removed from database

### Understanding Data Flow

- Scraper runs every 60 seconds
- During market hours: Fetches all profiles
- Outside market hours: Only fetches new profiles (no existing data)
- Position changes trigger snapshot storage
- Latest data always updated for real-time P&L

### Troubleshooting

**Port 6060 still occupied after exit?**
```bash
lsof -ti:6060 | xargs kill -9
```

**Scraper not updating?**
- Check market hours (Mon-Fri, 09:15-15:30)
- Verify urls.txt is readable
- Check API rate limits

**Profile not showing data?**
- Verify the Sensibull profile slug is correct
- Check if profile has active positions
- Look for API errors in scraper logs

---

## Future Enhancements

- [ ] Add authentication for web dashboard
- [ ] Export data to CSV/Excel
- [ ] Email/Slack notifications on position changes
- [ ] Support for multiple exchanges/brokers
- [ ] Mobile-responsive UI improvements
- [ ] Real-time WebSocket updates instead of polling
