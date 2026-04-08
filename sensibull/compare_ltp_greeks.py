from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from database import get_db

try:
    import requests as _requests
except ImportError:  # pragma: no cover - depends on user env
    _requests = None


SENSIBULL_GREEKS_URL_TEMPLATE = "https://oxide.sensibull.com/v1/compute/cache/live_derivative_prices/{underlying_token}"
SENSIBULL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://web.sensibull.com/",
    "Origin": "https://web.sensibull.com",
}

INDEX_UNDERLYING_NAME_MAP = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MID SELECT",
    "SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
}

MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
MON3_TO_NUM = {m: i for i, m in enumerate(MONTHS.split("|"), 1)}
MON_CODE_TO_NUM = {str(i): i for i in range(1, 10)} | {"O": 10, "N": 11, "D": 12}
NUM_TO_MON3 = {v: k for k, v in MON3_TO_NUM.items()}

OPENALGO_HOLIDAYS_PATH = os.environ.get("OPENALGO_HOLIDAYS_PATH", "/api/v1/holidays").strip() or "/api/v1/holidays"
_BSE_OPTION_UNDERLYINGS = frozenset({"SENSEX", "BANKEX"})
_EXPIRY_WEEKDAY = {
    "SENSEX": 3,  # Thursday
    "MIDCPNIFTY": 0,  # Monday
}
_DEFAULT_EXPIRY_WEEKDAY = 1  # Tuesday

LOGGER = logging.getLogger("compare_ltp_greeks")

# Define broker symbols here (required).
# Example:
BROKER_SYMBOLS = [
    "SENSEX2640272000CE",
    "SENSEX2640271000PE",
]
# BROKER_SYMBOLS: List[str] = []


def _require_requests():
    if _requests is None:
        raise RuntimeError("requests package is required. Install with: pip install requests")
    return _requests


@dataclass(frozen=True)
class SymbolContext:
    broker_symbol: str
    openalgo_symbol: str
    instrument_token: int
    underlying: str
    underlying_token: int
    http_exchange: str
    ws_exchange: str


class OpenAlgoRequestError(RuntimeError):
    def __init__(self, endpoint: str, status_code: int, response_body: str, latency_ms: float):
        super().__init__(f"OpenAlgo request failed ({status_code}) at {endpoint}")
        self.endpoint = endpoint
        self.status_code = status_code
        self.response_body = response_body
        self.latency_ms = latency_ms


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def normalize_openalgo_host(host: str) -> str:
    value = (host or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    return value.rstrip("/")


def normalize_broker_symbol(symbol: str) -> str:
    raw = (symbol or "").strip().upper()
    if ":" in raw:
        _, raw = raw.split(":", 1)
    raw = raw.strip()
    if raw.endswith(".0"):
        raw = raw[:-2]
    return raw


def extract_underlying_from_broker_symbol(broker_symbol: str) -> str:
    m = re.match(r"^([A-Z&-]+)", broker_symbol.upper())
    if not m:
        raise ValueError(f"Could not extract underlying from symbol: {broker_symbol}")
    return m.group(1)


def _build_profile_holiday_api_url(host: str) -> str:
    normalized_host = normalize_openalgo_host(host)
    if not normalized_host:
        return ""
    if OPENALGO_HOLIDAYS_PATH.startswith(("http://", "https://")):
        return OPENALGO_HOLIDAYS_PATH
    path = OPENALGO_HOLIDAYS_PATH if OPENALGO_HOLIDAYS_PATH.startswith("/") else f"/{OPENALGO_HOLIDAYS_PATH}"
    return f"{normalized_host}{path}"


def _get_holiday_api_url(year: int, holiday_api_url: str) -> str:
    base_url = (holiday_api_url or "").strip()
    if not base_url:
        return ""
    if "{year}" in base_url:
        return base_url.format(year=year)
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}year={year}"


def _get_nfo_trading_holidays(year: int, holiday_api_url: str, holiday_api_key: Optional[str], timeout: int) -> set[date]:
    req = _require_requests()
    holiday_url = _get_holiday_api_url(year, holiday_api_url)
    if not holiday_url:
        return set()

    payload: Dict[str, Any] = {"year": year}
    if holiday_api_key:
        payload["apikey"] = holiday_api_key

    try:
        response = req.post(holiday_url, json=payload, timeout=timeout)
        response.raise_for_status()
    except req.RequestException:
        params: Dict[str, Any] = {"year": year}
        if holiday_api_key:
            params["apikey"] = holiday_api_key
        response = req.get(holiday_url, params=params, timeout=timeout)
        response.raise_for_status()

    parsed = response.json()
    holidays: set[date] = set()
    for item in parsed.get("data", []):
        if item.get("holiday_type") != "TRADING_HOLIDAY":
            continue
        closed_exchanges = item.get("closed_exchanges") or []
        if "NFO" not in closed_exchanges:
            continue
        day_str = item.get("date")
        if not day_str:
            continue
        holidays.add(date.fromisoformat(day_str))
    return holidays


def _resolve_last_trading_day_of_month(
    base: str,
    year: int,
    month: int,
    holiday_api_url: str,
    holiday_api_key: Optional[str],
    timeout: int,
) -> int:
    target_weekday = _EXPIRY_WEEKDAY.get((base or "").upper(), _DEFAULT_EXPIRY_WEEKDAY)
    nfo_holidays = _get_nfo_trading_holidays(year, holiday_api_url, holiday_api_key, timeout=timeout)
    last_day = calendar.monthrange(year, month)[1]
    candidate = date(year, month, last_day)
    while candidate.weekday() != target_weekday:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in nfo_holidays:
        candidate -= timedelta(days=1)
    return candidate.day


def _lookup_expiry_day_from_master_contract(conn: Any, broker_symbol: str) -> Optional[int]:
    c = conn.cursor()
    row = c.execute(
        "SELECT expiry FROM master_contract WHERE trading_symbol = ?",
        (broker_symbol.upper(),),
    ).fetchone()
    if row and row["expiry"]:
        return date.fromisoformat(row["expiry"]).day
    return None


def convert_broker_symbol_to_openalgo_symbol(
    conn: Any,
    symbol: str,
    holiday_api_url: str,
    holiday_api_key: Optional[str],
    holiday_timeout: int,
) -> str:
    s = normalize_broker_symbol(symbol)

    m = re.fullmatch(rf"([A-Z0-9]+?)(\d{{2}})({MONTHS})(\d+(?:\.\d+)?)(CE|PE)", s)
    if m:
        base, yy, mon3, strike, opt = m.groups()
        day = _lookup_expiry_day_from_master_contract(conn, s)
        if day is None:
            year = 2000 + int(yy)
            month = MON3_TO_NUM[mon3]
            day = _resolve_last_trading_day_of_month(
                base,
                year,
                month,
                holiday_api_url=holiday_api_url,
                holiday_api_key=holiday_api_key,
                timeout=holiday_timeout,
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

    raise ValueError(f"Unsupported broker option symbol format: {symbol}")


def _extract_underlying_from_openalgo_symbol(symbol: str) -> str:
    m = re.match(r"^([A-Z]+?)\d{2}[A-Z]{3}\d{2}", (symbol or "").upper())
    return m.group(1) if m else ""


def _get_option_exchange_for_http(openalgo_symbol: str) -> str:
    underlying = _extract_underlying_from_openalgo_symbol(openalgo_symbol)
    return "BFO" if underlying in _BSE_OPTION_UNDERLYINGS else "NFO"


def _get_option_exchange_for_ws(openalgo_symbol: str) -> str:
    underlying = _extract_underlying_from_openalgo_symbol(openalgo_symbol)
    return "BFO" if underlying in _BSE_OPTION_UNDERLYINGS else "NFO"


def _resolve_underlying_token(conn: Any, underlying: str) -> Optional[int]:
    c = conn.cursor()
    index_name = INDEX_UNDERLYING_NAME_MAP.get(underlying, underlying)
    row = c.execute(
        """
        SELECT instrument_token
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
        """,
        (index_name, underlying),
    ).fetchone()
    if row and row["instrument_token"]:
        return int(row["instrument_token"])
    return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_ltp(option_data: Dict[str, Any]) -> Optional[float]:
    for key in ("ltp", "last_price", "last_traded_price", "option_price", "price"):
        val = _as_float(option_data.get(key))
        if val is not None:
            return val
    market_feed = option_data.get("market_feed") or {}
    return _as_float(market_feed.get("ltp"))


def fetch_sensibull_option_data_by_underlying(
    underlying_token: int,
    timeout: int,
) -> Tuple[float, Dict[int, Dict[str, Optional[float]]]]:
    req = _require_requests()
    url = SENSIBULL_GREEKS_URL_TEMPLATE.format(underlying_token=underlying_token)
    start = time.perf_counter()
    resp = req.get(
        url,
        params={"get_nse_feed": "true"},
        headers=SENSIBULL_HEADERS,
        timeout=timeout,
    )
    latency_ms = (time.perf_counter() - start) * 1000
    resp.raise_for_status()
    payload = resp.json() if resp.content else {}

    status_ok = payload.get("status") is True or str(payload.get("status", "")).lower() == "success"
    if not status_ok:
        raise RuntimeError(f"Sensibull request failed for token {underlying_token}: {payload.get('errors') or payload.get('error')}")

    per_expiry = (payload.get("data") or {}).get("per_expiry_data") or {}
    expiry_iter: Iterable[Any]
    if isinstance(per_expiry, dict):
        expiry_iter = per_expiry.values()
    else:
        expiry_iter = per_expiry

    token_map: Dict[int, Dict[str, Optional[float]]] = {}
    for expiry_data in expiry_iter:
        for option_data in (expiry_data or {}).get("options", []):
            tok = option_data.get("token")
            if tok is None:
                continue
            try:
                token = int(tok)
            except (TypeError, ValueError):
                continue
            greeks = option_data.get("greeks_with_iv") or {}
            token_map[token] = {
                "ltp": _pick_ltp(option_data),
                "delta": _as_float(greeks.get("delta")),
                "gamma": _as_float(greeks.get("gamma")),
                "theta": _as_float(greeks.get("theta")),
                "vega": _as_float(greeks.get("vega")),
                "iv": _as_float(greeks.get("iv")),
            }

    return latency_ms, token_map


def fetch_openalgo_multiquotes(
    host: str,
    apikey: str,
    symbols: List[Dict[str, str]],
    timeout: int,
) -> Tuple[float, Dict[str, Optional[float]]]:
    url = f"{normalize_openalgo_host(host)}/api/v1/multiquotes"
    try:
        latency_ms, body = _post_openalgo_json(url, {"apikey": apikey, "symbols": symbols}, timeout)
        if str(body.get("status", "")).lower() != "success":
            raise RuntimeError(f"OpenAlgo multiquotes non-success response: {body}")
        return latency_ms, _parse_openalgo_quotes_body(body)
    except OpenAlgoRequestError as exc:
        if exc.status_code == 400 and len(symbols) > 1:
            LOGGER.warning("multiquotes batch 400; falling back to per-symbol calls. Body: %s", exc.response_body[:400])
            return _fallback_multiquotes_per_symbol(host, apikey, symbols, timeout, initial_latency_ms=exc.latency_ms)
        raise


def fetch_openalgo_multioptiongreeks(
    host: str,
    apikey: str,
    symbols: List[Dict[str, str]],
    timeout: int,
) -> Tuple[float, Dict[str, Dict[str, Optional[float]]]]:
    url = f"{normalize_openalgo_host(host)}/api/v1/multioptiongreeks"
    try:
        latency_ms, body = _post_openalgo_json(url, {"apikey": apikey, "symbols": symbols}, timeout)
        if str(body.get("status", "")).lower() != "success":
            raise RuntimeError(f"OpenAlgo multioptiongreeks non-success response: {body}")
        return latency_ms, _parse_openalgo_greeks_body(body)
    except OpenAlgoRequestError as exc:
        if exc.status_code == 400 and len(symbols) > 1:
            LOGGER.warning("multioptiongreeks batch 400; falling back to per-symbol calls. Body: %s", exc.response_body[:400])
            return _fallback_multioptiongreeks_per_symbol(host, apikey, symbols, timeout, initial_latency_ms=exc.latency_ms)
        raise


def _post_openalgo_json(url: str, payload: Dict[str, Any], timeout: int) -> Tuple[float, Dict[str, Any]]:
    req = _require_requests()
    start = time.perf_counter()
    resp = req.post(
        url,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    latency_ms = (time.perf_counter() - start) * 1000

    body: Dict[str, Any]
    try:
        body = resp.json()
    except ValueError:
        body = {}

    if not resp.ok:
        body_text = resp.text if resp.text else json.dumps(body)
        raise OpenAlgoRequestError(url, resp.status_code, body_text, latency_ms)
    return latency_ms, body


def _parse_openalgo_quotes_body(body: Dict[str, Any]) -> Dict[str, Optional[float]]:
    result: Dict[str, Optional[float]] = {}
    for item in body.get("results", []):
        symbol = str(item.get("symbol", "")).upper()
        data = item.get("data") or {}
        if symbol:
            result[symbol] = _as_float(data.get("ltp"))
    return result


def _parse_openalgo_greeks_body(body: Dict[str, Any]) -> Dict[str, Dict[str, Optional[float]]]:
    result: Dict[str, Dict[str, Optional[float]]] = {}
    for item in body.get("data", []):
        symbol = str(item.get("symbol", "")).upper()
        greeks = item.get("greeks") or {}
        if symbol:
            result[symbol] = {
                "delta": _as_float(greeks.get("delta")),
                "gamma": _as_float(greeks.get("gamma")),
                "theta": _as_float(greeks.get("theta")),
                "vega": _as_float(greeks.get("vega")),
                "iv": _as_float(item.get("implied_volatility")),
            }
    return result


def _exchange_candidates(symbol: str, default_exchange: str) -> List[str]:
    symbol_u = (symbol or "").upper()
    default_u = (default_exchange or "").upper()
    candidates: List[str] = [default_u] if default_u else []

    is_option = bool(re.fullmatch(r"[A-Z]+?\d{2}[A-Z]{3}\d{2}\d+(?:\.\d+)?(CE|PE)", symbol_u))
    if is_option:
        underlying = _extract_underlying_from_openalgo_symbol(symbol_u)
        if underlying in _BSE_OPTION_UNDERLYINGS:
            candidates.extend(["BFO", "NFO"])
        else:
            candidates.extend(["NFO", "BFO"])
    else:
        candidates.extend(["NSE", "BSE"])

    # de-duplicate preserving order
    seen = set()
    deduped = []
    for ex in candidates:
        if ex and ex not in seen:
            deduped.append(ex)
            seen.add(ex)
    return deduped


def _fallback_multiquotes_per_symbol(
    host: str,
    apikey: str,
    symbols: List[Dict[str, str]],
    timeout: int,
    initial_latency_ms: float,
) -> Tuple[float, Dict[str, Optional[float]]]:
    total_latency_ms = initial_latency_ms
    merged: Dict[str, Optional[float]] = {}

    for item in symbols:
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        last_error: Optional[str] = None
        for exchange in _exchange_candidates(symbol, str(item.get("exchange", ""))):
            try:
                lat, body = _post_openalgo_json(
                    f"{normalize_openalgo_host(host)}/api/v1/multiquotes",
                    {"apikey": apikey, "symbols": [{"symbol": symbol, "exchange": exchange}]},
                    timeout,
                )
                total_latency_ms += lat
                if str(body.get("status", "")).lower() != "success":
                    last_error = f"non-success status: {body}"
                    continue
                parsed = _parse_openalgo_quotes_body(body)
                if symbol in parsed:
                    merged[symbol] = parsed[symbol]
                    break
            except OpenAlgoRequestError as exc:
                total_latency_ms += exc.latency_ms
                last_error = f"{exc.status_code}: {exc.response_body[:200]}"
                if exc.status_code not in (400, 422):
                    LOGGER.warning("multiquotes per-symbol hard failure for %s on %s: %s", symbol, exchange, last_error)
                    break
        if symbol not in merged:
            LOGGER.warning("multiquotes unresolved symbol=%s; marking missing. last_error=%s", symbol, last_error)
            merged[symbol] = None
    return total_latency_ms, merged


def _fallback_multioptiongreeks_per_symbol(
    host: str,
    apikey: str,
    symbols: List[Dict[str, str]],
    timeout: int,
    initial_latency_ms: float,
) -> Tuple[float, Dict[str, Dict[str, Optional[float]]]]:
    total_latency_ms = initial_latency_ms
    merged: Dict[str, Dict[str, Optional[float]]] = {}

    for item in symbols:
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        last_error: Optional[str] = None
        for exchange in _exchange_candidates(symbol, str(item.get("exchange", ""))):
            try:
                lat, body = _post_openalgo_json(
                    f"{normalize_openalgo_host(host)}/api/v1/multioptiongreeks",
                    {"apikey": apikey, "symbols": [{"symbol": symbol, "exchange": exchange}]},
                    timeout,
                )
                total_latency_ms += lat
                if str(body.get("status", "")).lower() != "success":
                    last_error = f"non-success status: {body}"
                    continue
                parsed = _parse_openalgo_greeks_body(body)
                if symbol in parsed:
                    merged[symbol] = parsed[symbol]
                    break
            except OpenAlgoRequestError as exc:
                total_latency_ms += exc.latency_ms
                last_error = f"{exc.status_code}: {exc.response_body[:200]}"
                if exc.status_code not in (400, 422):
                    LOGGER.warning("multioptiongreeks per-symbol hard failure for %s on %s: %s", symbol, exchange, last_error)
                    break
        if symbol not in merged:
            LOGGER.warning("multioptiongreeks unresolved symbol=%s; marking missing. last_error=%s", symbol, last_error)
            merged[symbol] = {}
    return total_latency_ms, merged


class OpenAlgoTruthWebSocket:
    def __init__(self, ws_url: str, api_key: str) -> None:
        self.ws_url = ws_url
        self.api_key = api_key
        self._conn = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._authenticated = threading.Event()
        self._failed = threading.Event()
        self._last_error: Optional[str] = None
        self._ltp: Dict[str, float] = {}

    def connect(self, timeout_sec: float) -> None:
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - depends on user env
            raise RuntimeError("websocket-client package is required. Install with: pip install websocket-client") from exc

        def on_open(ws_app) -> None:
            ws_app.send(json.dumps({"action": "authenticate", "api_key": self.api_key}))

        def on_message(ws_app, raw: str) -> None:
            if raw == "ping" or raw == '{"type":"ping"}':
                ws_app.send("pong")
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                return

            if not self._authenticated.is_set():
                if msg.get("status") == "error":
                    self._last_error = str(msg.get("message") or msg)
                    self._failed.set()
                    ws_app.close()
                    return
                self._authenticated.set()
                return

            symbol = str(msg.get("symbol", "")).upper()
            ltp_raw = ((msg.get("data") or {}).get("ltp")) if isinstance(msg.get("data"), dict) else None
            if ltp_raw is None:
                ltp_raw = msg.get("ltp")
            ltp = _as_float(ltp_raw)
            if symbol and ltp is not None:
                with self._lock:
                    self._ltp[symbol] = ltp

        def on_error(_ws_app, error: Any) -> None:
            self._last_error = str(error)

        def on_close(_ws_app, _status_code: int, _msg: str) -> None:
            if not self._authenticated.is_set() and not self._failed.is_set():
                self._failed.set()
                if self._last_error is None:
                    self._last_error = "WebSocket closed before authentication"

        self._conn = websocket.WebSocketApp(
            self.ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._thread = threading.Thread(
            target=lambda: self._conn.run_forever(ping_interval=20, ping_timeout=10),
            daemon=True,
        )
        self._thread.start()

        if not self._authenticated.wait(timeout=timeout_sec):
            self.close()
            detail = self._last_error or "timeout waiting for websocket authentication"
            raise RuntimeError(f"OpenAlgo truth websocket authentication failed: {detail}")

    def subscribe(self, symbols: List[Dict[str, str]]) -> None:
        if not self._conn:
            raise RuntimeError("WebSocket not connected")
        self._conn.send(json.dumps({"action": "subscribe", "symbols": symbols, "mode": 1}))

    def snapshot(self, symbols: Iterable[str]) -> Dict[str, Optional[float]]:
        keys = [s.upper() for s in symbols]
        with self._lock:
            return {key: self._ltp.get(key) for key in keys}

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    idx = (len(ordered) - 1) * pct
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    frac = idx - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def summarize_series(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"count": 0, "min": None, "max": None, "avg": None, "p50": None, "p95": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "avg": _mean(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def parse_symbols_from_config() -> List[str]:
    raw = list(BROKER_SYMBOLS)
    if not raw:
        raise ValueError("BROKER_SYMBOLS is empty. Define symbols in compare_ltp_greeks.py.")
    normalized: List[str] = []
    for part in raw:
        for item in part.split(","):
            symbol = normalize_broker_symbol(item)
            if symbol:
                normalized.append(symbol)
    if not normalized:
        raise ValueError("No valid symbols found in BROKER_SYMBOLS.")
    return list(dict.fromkeys(normalized))


def load_openalgo_profile(conn: Any, profile_id: Optional[int]) -> Optional[Dict[str, Any]]:
    c = conn.cursor()
    if profile_id is not None:
        row = c.execute(
            """
            SELECT id, profile_name, host, api_key, ws_url
            FROM openalgo_profiles
            WHERE id = ? AND is_active = 1
            """,
            (profile_id,),
        ).fetchone()
        return dict(row) if row else None

    row = c.execute(
        """
        SELECT id, profile_name, host, api_key, ws_url
        FROM openalgo_profiles
        WHERE is_active = 1
        ORDER BY is_ws_default DESC, id ASC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def build_symbol_contexts(
    conn: Any,
    broker_symbols: List[str],
    openalgo_host: str,
    openalgo_api_key: str,
    holiday_timeout: int,
) -> List[SymbolContext]:
    c = conn.cursor()
    contexts: List[SymbolContext] = []
    holiday_api_url = _build_profile_holiday_api_url(openalgo_host)

    for broker_symbol in broker_symbols:
        row = c.execute(
            """
            SELECT instrument_token, trading_symbol
            FROM master_contract
            WHERE trading_symbol = ?
               OR trading_symbol = ?
            LIMIT 1
            """,
            (broker_symbol, f"{broker_symbol}.0"),
        ).fetchone()
        if not row:
            raise ValueError(f"Symbol not found in master_contract: {broker_symbol}")

        instrument_token = int(row["instrument_token"])
        underlying = extract_underlying_from_broker_symbol(broker_symbol)
        underlying_token = _resolve_underlying_token(conn, underlying)
        if underlying_token is None:
            raise ValueError(f"Could not resolve underlying token for symbol: {broker_symbol} (underlying={underlying})")

        openalgo_symbol = convert_broker_symbol_to_openalgo_symbol(
            conn=conn,
            symbol=broker_symbol,
            holiday_api_url=holiday_api_url,
            holiday_api_key=openalgo_api_key,
            holiday_timeout=holiday_timeout,
        )
        contexts.append(
            SymbolContext(
                broker_symbol=broker_symbol,
                openalgo_symbol=openalgo_symbol,
                instrument_token=instrument_token,
                underlying=underlying,
                underlying_token=underlying_token,
                http_exchange=_get_option_exchange_for_http(openalgo_symbol),
                ws_exchange=_get_option_exchange_for_ws(openalgo_symbol),
            )
        )
    return contexts


def run_comparison(args: argparse.Namespace) -> Dict[str, Any]:
    broker_symbols = parse_symbols_from_config()
    conn = get_db()
    ws_client: Optional[OpenAlgoTruthWebSocket] = None
    try:
        profile = load_openalgo_profile(conn, args.openalgo_profile_id)
        host = normalize_openalgo_host(args.openalgo_host or (profile.get("host") if profile else ""))
        api_key = args.openalgo_api_key or (profile.get("api_key") if profile else "")
        ws_url = args.openalgo_ws_url or (profile.get("ws_url") if profile else "")

        if not host:
            raise ValueError("OpenAlgo host is required. Pass --openalgo-host or configure an active profile in openalgo_profiles.")
        if not api_key:
            raise ValueError("OpenAlgo API key is required. Pass --openalgo-api-key or configure an active profile in openalgo_profiles.")
        if not ws_url:
            raise ValueError("OpenAlgo WebSocket URL is required for truth baseline. Pass --openalgo-ws-url or configure ws_url in openalgo_profiles.")

        contexts = build_symbol_contexts(
            conn=conn,
            broker_symbols=broker_symbols,
            openalgo_host=host,
            openalgo_api_key=api_key,
            holiday_timeout=args.request_timeout,
        )
        LOGGER.info("Prepared %d symbols for comparison.", len(contexts))

        ws_client = OpenAlgoTruthWebSocket(ws_url=ws_url, api_key=api_key)
        ws_client.connect(timeout_sec=args.ws_connect_timeout)
        ws_client.subscribe([{"symbol": c.openalgo_symbol, "exchange": c.ws_exchange} for c in contexts])
        LOGGER.info("OpenAlgo truth WebSocket connected and subscribed.")
        time.sleep(args.ws_warmup)

        latency_openalgo_quotes: List[float] = []
        latency_openalgo_greeks: List[float] = []
        latency_sensibull_total: List[float] = []

        ltp_error_openalgo_http: List[float] = []
        ltp_error_sensibull: List[float] = []
        greek_diff: Dict[str, List[float]] = {k: [] for k in ("delta", "gamma", "theta", "vega", "iv")}
        missing: Dict[str, int] = {
            "truth_ltp_missing": 0,
            "openalgo_http_ltp_missing": 0,
            "sensibull_ltp_missing": 0,
            "openalgo_greeks_missing": 0,
            "sensibull_greeks_missing": 0,
        }
        per_iteration: List[Dict[str, Any]] = []

        for idx in range(args.samples):
            if idx > 0:
                time.sleep(args.interval)

            truth_ltp = ws_client.snapshot([c.openalgo_symbol for c in contexts])
            quotes_symbols = [{"symbol": c.openalgo_symbol, "exchange": c.http_exchange} for c in contexts]
            greeks_symbols = [{"symbol": c.openalgo_symbol, "exchange": c.http_exchange} for c in contexts]

            q_latency, openalgo_quotes = fetch_openalgo_multiquotes(
                host=host,
                apikey=api_key,
                symbols=quotes_symbols,
                timeout=args.request_timeout,
            )
            g_latency, openalgo_greeks = fetch_openalgo_multioptiongreeks(
                host=host,
                apikey=api_key,
                symbols=greeks_symbols,
                timeout=args.request_timeout,
            )
            latency_openalgo_quotes.append(q_latency)
            latency_openalgo_greeks.append(g_latency)

            sensibull_start = time.perf_counter()
            sensibull_by_underlying: Dict[int, Dict[int, Dict[str, Optional[float]]]] = {}
            for underlying_token in sorted({c.underlying_token for c in contexts}):
                _, token_map = fetch_sensibull_option_data_by_underlying(
                    underlying_token=underlying_token,
                    timeout=args.request_timeout,
                )
                sensibull_by_underlying[underlying_token] = token_map
            sensibull_latency = (time.perf_counter() - sensibull_start) * 1000
            latency_sensibull_total.append(sensibull_latency)

            iter_rows: List[Dict[str, Any]] = []
            for sym_ctx in contexts:
                oa_sym = sym_ctx.openalgo_symbol
                truth = truth_ltp.get(oa_sym)
                oa_ltp = openalgo_quotes.get(oa_sym)
                oa_g = openalgo_greeks.get(oa_sym) or {}
                sb_data = (sensibull_by_underlying.get(sym_ctx.underlying_token) or {}).get(sym_ctx.instrument_token) or {}
                sb_ltp = sb_data.get("ltp")

                if truth is None:
                    missing["truth_ltp_missing"] += 1
                else:
                    if oa_ltp is None:
                        missing["openalgo_http_ltp_missing"] += 1
                    else:
                        ltp_error_openalgo_http.append(abs(oa_ltp - truth))
                    if sb_ltp is None:
                        missing["sensibull_ltp_missing"] += 1
                    else:
                        ltp_error_sensibull.append(abs(sb_ltp - truth))

                oa_missing_greek = all(oa_g.get(k) is None for k in ("delta", "gamma", "theta", "vega", "iv"))
                sb_missing_greek = all(sb_data.get(k) is None for k in ("delta", "gamma", "theta", "vega", "iv"))
                if oa_missing_greek:
                    missing["openalgo_greeks_missing"] += 1
                if sb_missing_greek:
                    missing["sensibull_greeks_missing"] += 1

                for greek_key in ("delta", "gamma", "theta", "vega", "iv"):
                    oa_val = oa_g.get(greek_key)
                    sb_val = sb_data.get(greek_key)
                    if oa_val is not None and sb_val is not None:
                        greek_diff[greek_key].append(abs(oa_val - sb_val))

                iter_rows.append(
                    {
                        "broker_symbol": sym_ctx.broker_symbol,
                        "openalgo_symbol": oa_sym,
                        "truth_ltp_ws": truth,
                        "openalgo_ltp_http": oa_ltp,
                        "sensibull_ltp": sb_ltp,
                        "openalgo_greeks": oa_g,
                        "sensibull_greeks": {k: sb_data.get(k) for k in ("delta", "gamma", "theta", "vega", "iv")},
                    }
                )

            per_iteration.append(
                {
                    "iteration": idx + 1,
                    "timestamp_ist": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "latency_ms": {
                        "openalgo_multiquotes": q_latency,
                        "openalgo_multioptiongreeks": g_latency,
                        "sensibull_total": sensibull_latency,
                    },
                    "rows": iter_rows,
                }
            )

        report = {
            "status": "success",
            "config": {
                "samples": args.samples,
                "interval_sec": args.interval,
                "request_timeout_sec": args.request_timeout,
                "ws_warmup_sec": args.ws_warmup,
                "symbol_count": len(contexts),
                "symbols": [asdict(c) for c in contexts],
                "openalgo_host": host,
                "openalgo_ws_url": ws_url,
                "profile_name": profile.get("profile_name") if profile else None,
            },
            "latency_summary_ms": {
                "openalgo_multiquotes": summarize_series(latency_openalgo_quotes),
                "openalgo_multioptiongreeks": summarize_series(latency_openalgo_greeks),
                "sensibull_total": summarize_series(latency_sensibull_total),
            },
            "accuracy_vs_truth_ltp_abs_diff": {
                "openalgo_http": summarize_series(ltp_error_openalgo_http),
                "sensibull": summarize_series(ltp_error_sensibull),
            },
            "greeks_cross_provider_abs_diff": {k: summarize_series(v) for k, v in greek_diff.items()},
            "missing_counts": missing,
            "per_iteration": per_iteration,
        }
        return report
    finally:
        if ws_client is not None:
            ws_client.close()
        conn.close()


def print_table_report(report: Dict[str, Any]) -> None:
    lat = report["latency_summary_ms"]
    acc = report["accuracy_vs_truth_ltp_abs_diff"]
    miss = report["missing_counts"]
    print("\n=== Latency Summary (ms) ===")
    for key in ("openalgo_multiquotes", "openalgo_multioptiongreeks", "sensibull_total"):
        s = lat[key]
        print(
            f"{key:28} count={s['count']:<4} avg={_fmt(s['avg'])} "
            f"p50={_fmt(s['p50'])} p95={_fmt(s['p95'])} min={_fmt(s['min'])} max={_fmt(s['max'])}"
        )

    print("\n=== LTP Absolute Error vs OpenAlgo WS Truth ===")
    for key in ("openalgo_http", "sensibull"):
        s = acc[key]
        print(
            f"{key:28} count={s['count']:<4} avg={_fmt(s['avg'])} "
            f"p50={_fmt(s['p50'])} p95={_fmt(s['p95'])} min={_fmt(s['min'])} max={_fmt(s['max'])}"
        )

    print("\n=== Greeks Cross-Provider Absolute Difference ===")
    for greek_key, s in report["greeks_cross_provider_abs_diff"].items():
        print(
            f"{greek_key:28} count={s['count']:<4} avg={_fmt(s['avg'])} "
            f"p50={_fmt(s['p50'])} p95={_fmt(s['p95'])}"
        )

    print("\n=== Missing Data Counts ===")
    for k, v in miss.items():
        print(f"{k:28} {v}")

    print("\n=== Detailed Per-Sample Symbol Table ===")
    for sample in report.get("per_iteration", []):
        ts = sample.get("timestamp_ist", "NA")
        lat_s = sample.get("latency_ms", {})
        print(
            f"\n[Iteration {sample.get('iteration')} | {ts}] "
            f"latency(ms): mq={_fmt(lat_s.get('openalgo_multiquotes'))}, "
            f"mog={_fmt(lat_s.get('openalgo_multioptiongreeks'))}, "
            f"sb={_fmt(lat_s.get('sensibull_total'))}"
        )
        header = (
            f"{'Broker Symbol':<24} {'OpenAlgo Symbol':<24} "
            f"{'Truth LTP':>10} {'OA HTTP':>10} {'Sensibull':>10} "
            f"{'|OA-Truth|':>12} {'|SB-Truth|':>12} "
            f"{'OA Delta':>10} {'SB Delta':>10} {'|Δ|':>8}"
        )
        print(header)
        print("-" * len(header))
        for row in sample.get("rows", []):
            truth = row.get("truth_ltp_ws")
            oa_ltp = row.get("openalgo_ltp_http")
            sb_ltp = row.get("sensibull_ltp")
            oa_g = row.get("openalgo_greeks") or {}
            sb_g = row.get("sensibull_greeks") or {}
            ltp_diff_oa = abs(oa_ltp - truth) if (oa_ltp is not None and truth is not None) else None
            ltp_diff_sb = abs(sb_ltp - truth) if (sb_ltp is not None and truth is not None) else None
            oa_delta = oa_g.get("delta")
            sb_delta = sb_g.get("delta")
            delta_diff = abs(oa_delta - sb_delta) if (oa_delta is not None and sb_delta is not None) else None
            print(
                f"{str(row.get('broker_symbol', ''))[:24]:<24} "
                f"{str(row.get('openalgo_symbol', ''))[:24]:<24} "
                f"{_fmt(truth):>10} {_fmt(oa_ltp):>10} {_fmt(sb_ltp):>10} "
                f"{_fmt(ltp_diff_oa):>12} {_fmt(ltp_diff_sb):>12} "
                f"{_fmt(oa_delta):>10} {_fmt(sb_delta):>10} {_fmt(delta_diff):>8}"
            )


def _fmt(value: Optional[float]) -> str:
    return "NA" if value is None else f"{value:.4f}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare LTP + option greeks from Sensibull and OpenAlgo HTTP APIs, "
            "using OpenAlgo WebSocket LTP as truth baseline."
        )
    )
    parser.add_argument("--openalgo-profile-id", type=int, help="openalgo_profiles.id to load host/api/ws config")
    parser.add_argument("--openalgo-host", help="OpenAlgo host, e.g. http://127.0.0.1:3300")
    parser.add_argument("--openalgo-api-key", help="OpenAlgo API key")
    parser.add_argument("--openalgo-ws-url", help="OpenAlgo WebSocket URL for truth LTP baseline")

    parser.add_argument("--samples", type=int, default=5, help="Number of sampling iterations")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between samples")
    parser.add_argument("--request-timeout", type=int, default=8, help="HTTP request timeout (seconds)")
    parser.add_argument("--ws-connect-timeout", type=float, default=10.0, help="WebSocket auth timeout (seconds)")
    parser.add_argument("--ws-warmup", type=float, default=2.0, help="Warmup time for WS ticks before sampling")

    parser.add_argument("--output", choices=("table", "json"), default="table")
    parser.add_argument("--output-file", help="Optional output file path for full JSON report")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.verbose)

    try:
        report = run_comparison(args)
    except Exception as exc:
        LOGGER.error("Comparison failed: %s", exc)
        return 1

    if args.output == "json":
        print(json.dumps(report, indent=2))
    else:
        print_table_report(report)

    if args.output_file:
        Path(args.output_file).write_text(json.dumps(report, indent=2), encoding="utf-8")
        LOGGER.info("Report written to %s", args.output_file)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
