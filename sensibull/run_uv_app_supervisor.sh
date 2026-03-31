#!/bin/bash

set -u

PROJECT_DIR="$HOME/Projects/trading-algo/sensibull"
RESTART_DELAY_SEC=5
CHECK_INTERVAL_SEC=15
TERM_GRACE_SEC=15
MARKET_TZ="Asia/Kolkata"
export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME}/.local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
app_pid=""

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

is_weekday() {
  local day
  day="$(TZ="$MARKET_TZ" date +%u)"
  [[ "$day" -ge 1 && "$day" -le 5 ]]
}

is_market_window() {
  local hhmm
  hhmm="$(TZ="$MARKET_TZ" date +%H%M)"
  is_weekday && [[ $((10#$hhmm)) -ge 915 && $((10#$hhmm)) -le 1530 ]]
}

stop_app_process() {
  local app_pid="$1"
  local second=0

  if ! kill -0 "$app_pid" 2>/dev/null; then
    return 0
  fi

  log "Stopping app process pid=$app_pid"
  kill "$app_pid" 2>/dev/null

  while [[ "$second" -lt "$TERM_GRACE_SEC" ]]; do
    if ! kill -0 "$app_pid" 2>/dev/null; then
      return 0
    fi
    second=$((second + 1))
    sleep 1
  done

  if kill -0 "$app_pid" 2>/dev/null; then
    log "Force killing app process pid=$app_pid"
    kill -9 "$app_pid" 2>/dev/null
  fi
}

main() {
  if ! command -v uv >/dev/null 2>&1; then
    log "ERROR: uv command not found in PATH."
    exit 1
  fi

  if ! is_market_window; then
    log "Outside weekday market window (09:15-15:30). Exiting."
    exit 0
  fi

  if ! cd "$PROJECT_DIR"; then
    log "ERROR: could not cd to $PROJECT_DIR"
    exit 1
  fi

  while is_market_window; do
    log "Starting: uv run app.py"
    uv run app.py &
    app_pid=$!

    while kill -0 "$app_pid" 2>/dev/null; do
      if ! is_market_window; then
        log "Market window closed. Stopping app."
        stop_app_process "$app_pid"
        wait "$app_pid" 2>/dev/null
        log "Supervisor exiting after market close."
        exit 0
      fi
      sleep "$CHECK_INTERVAL_SEC"
    done

    wait "$app_pid"
    exit_code=$?

    if ! is_market_window; then
      log "App exited with code $exit_code after market close. Exiting."
      exit 0
    fi

    log "App exited with code $exit_code during market window. Restarting in ${RESTART_DELAY_SEC}s."
    sleep "$RESTART_DELAY_SEC"
  done

  log "Supervisor reached end of market window."
}

cleanup() {
  if [[ -n "$app_pid" ]]; then
    stop_app_process "$app_pid"
    wait "$app_pid" 2>/dev/null || true
  fi
}

trap cleanup TERM INT EXIT

main
