// ============================================================
// Sensibull Tracker — Google Apps Script
// Paste into Apps Script editor (script.google.com)
// Configure the four constants below, then set up triggers.
// ============================================================

const RENDER_URL        = 'https://your-app.onrender.com';     // your Render service URL
const SCRAPE_TOKEN      = 'your-scrape-token-here';            // matches SCRAPE_TOKEN env var
const SUPABASE_URL      = 'https://abcdefghijkl.supabase.co';  // your Supabase project URL
const SUPABASE_ANON_KEY = 'eyJhbGciOiJI...';                   // Supabase anon/public key

// ─────────────────────────────────────────────────────────────
// FUNCTION 1: Keep Render awake (prevents free-tier sleep)
// Trigger: Every 10 minutes
//
// Render free tier sleeps after 15 minutes of inactivity.
// This function hits /health which also queries latest_snapshots
// in Supabase, so it keeps BOTH Render and Supabase active.
// ─────────────────────────────────────────────────────────────
function keepAlive() {
  try {
    const response = UrlFetchApp.fetch(RENDER_URL + '/health', {
      muteHttpExceptions: true,
      followRedirects: true,
      deadline: 30
    });
    Logger.log('[keepAlive] ' + response.getResponseCode()
      + ' — ' + response.getContentText().substring(0, 120));
  } catch (e) {
    Logger.log('[keepAlive] FAILED: ' + e.toString());
  }
}

// ─────────────────────────────────────────────────────────────
// FUNCTION 2: Keep Supabase alive (prevents free-tier pause)
// Trigger: Every day (or every 5 days — 7-day window is wide)
//
// Supabase free tier pauses projects after 7 consecutive days
// of zero database activity. This function queries the profiles
// table directly via Supabase's REST API, bypassing Render
// entirely. This ensures Supabase stays active even if Render
// is down or the keepAlive() trigger fails.
//
// The anon key is a read-only public key. It can read tables
// with RLS disabled (default for SQL-created tables on free tier).
// Do NOT use the service_role key here.
// ─────────────────────────────────────────────────────────────
function keepSupabaseAlive() {
  try {
    const response = UrlFetchApp.fetch(
      SUPABASE_URL + '/rest/v1/profiles?select=id&limit=1',
      {
        method: 'get',
        headers: {
          'apikey':        SUPABASE_ANON_KEY,
          'Authorization': 'Bearer ' + SUPABASE_ANON_KEY
        },
        muteHttpExceptions: true,
        deadline: 20
      }
    );
    Logger.log('[keepSupabaseAlive] ' + response.getResponseCode()
      + ' — ' + response.getContentText().substring(0, 80));
  } catch (e) {
    Logger.log('[keepSupabaseAlive] FAILED: ' + e.toString());
  }
}

// ─────────────────────────────────────────────────────────────
// FUNCTION 3: Trigger an immediate scrape cycle (optional)
// Trigger: Every 1–5 minutes during Indian market hours
//          OR rely on the background thread (usually sufficient)
//
// Use only if the background thread model is insufficient.
// Requires SCRAPE_TOKEN to be set in Render env vars.
// ─────────────────────────────────────────────────────────────
function triggerScrape() {
  try {
    const response = UrlFetchApp.fetch(RENDER_URL + '/api/trigger-scrape', {
      method: 'post',
      headers: { 'Authorization': 'Bearer ' + SCRAPE_TOKEN },
      muteHttpExceptions: true,
      deadline: 30
    });
    Logger.log('[triggerScrape] ' + response.getResponseCode());
  } catch (e) {
    Logger.log('[triggerScrape] FAILED: ' + e.toString());
  }
}

// ─────────────────────────────────────────────────────────────
// HOW TO SET UP TRIGGERS:
// Apps Script → Triggers (clock icon) → Add Trigger
//
//  Function           | Type             | Interval
//  ───────────────────|─────────────────|──────────────────────
//  keepAlive          | Time-driven      | Every 10 minutes
//  keepSupabaseAlive  | Time-driven      | Every day
//  triggerScrape      | Time-driven      | Every minute (optional)
// ─────────────────────────────────────────────────────────────
