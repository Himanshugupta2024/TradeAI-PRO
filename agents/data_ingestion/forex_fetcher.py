"""
Forex Data Fetcher - OHLCV + Live Rates via Twelve Data API (free plan).

Free plan limits: 800 API calls/day, 8 API credits/minute.
All calls are tracked in the DAILY_API_USAGE_FILE (api_usage.json by default).
Cached responses are returned on failure - this module NEVER raises exceptions
to callers.

Rate-limit strategy: a minimum inter-call gap (_MIN_CALL_GAP_SECONDS) is
enforced between successive HTTP requests so that the scanner never fires
more than ~8 calls per minute even when multiple endpoints are called in
rapid succession.

Twelve Data API uses slash format natively (EUR/USD, not EUR_USD).
The forex_symbol() helper converts slash to underscore for URL path segments only.
"""
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from config import settings
from config.logger import get_logger

log = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# ── In-memory caches ─────────────────────────────────────────────────────────
# OHLCV cache: key = (symbol, interval) → {"df": DataFrame, "fetched_at": float}
_ohlcv_cache: dict[tuple[str, str], dict] = {}

# Rate cache: key = symbol → {"rate": float, "fetched_at": float}
_rate_cache: dict[str, dict] = {}

# Backoff when Twelve Data minute credits are exhausted.
_minute_backoff_until: float = 0.0

# Per-minute rate-limit: 8 credits/min → minimum gap between HTTP calls.
_MIN_CALL_GAP_SECONDS: float = 8.0
_last_api_call_ts: float = 0.0


def _wait_for_rate_limit() -> None:
    """Sleep if needed to respect the per-minute credit cap."""
    global _last_api_call_ts
    now = time.time()
    elapsed = now - _last_api_call_ts
    if elapsed < _MIN_CALL_GAP_SECONDS:
        wait = _MIN_CALL_GAP_SECONDS - elapsed
        log.debug("[FOREX] Rate-limit pause: %.1fs", wait)
        time.sleep(wait)
    _last_api_call_ts = time.time()

# ── Helpers ───────────────────────────────────────────────────────────────────


def forex_symbol(symbol: str) -> str:
    """Normalize pair to Twelve Data symbol format (slash notation)."""
    s = str(symbol or "").strip().upper()
    return s.replace("_", "/")


def is_twelve_data_backed_off() -> bool:
    """Return True while minute-credit backoff is active."""
    return time.time() < _minute_backoff_until


def _set_minute_backoff(seconds: int = 65) -> None:
    """Activate temporary backoff after minute-limit responses."""
    global _minute_backoff_until
    until = time.time() + max(1, int(seconds))
    if until > _minute_backoff_until:
        _minute_backoff_until = until


def get_pip_value(symbol: str) -> float:
    """
    Return pip size for the given forex pair.

    JPY pairs: 0.01
    All others: 0.0001
    """
    if "JPY" in symbol.upper():
        return 0.01
    return 0.0001


def is_session_active(symbol: str) -> tuple[bool, str]:
    """
    Check whether the relevant trading session for a symbol is currently active.

    Returns:
        (is_active, session_name)

    INR pairs are only active during NSE_INR hours (9:00–17:00 IST).
    Major pairs are active if ANY relevant session is currently open.
    """
    now_ist = datetime.now(_IST)
    now_time = now_ist.strftime("%H:%M")

    inr_pairs = {"USD/INR", "EUR/INR", "GBP/INR", "JPY/INR"}
    is_inr = symbol in inr_pairs

    sessions = settings.FOREX_SESSIONS

    def _in_session(start: str, end: str, current: str) -> bool:
        """Handle sessions that cross midnight (e.g. NEWYORK 18:30→03:30)."""
        if start <= end:
            return start <= current < end
        # Crosses midnight
        return current >= start or current < end

    if is_inr:
        sess = sessions.get("NSE_INR", {})
        active = _in_session(sess.get("start", "09:00"), sess.get("end", "17:00"), now_time)
        return active, "NSE_INR"

    # For major pairs: check all relevant sessions
    relevant_sessions: list[str] = []
    for sess_name, sess_data in sessions.items():
        if sess_name == "NSE_INR":
            continue
        if symbol in sess_data.get("best_pairs", []):
            relevant_sessions.append(sess_name)

    if not relevant_sessions:
        # Default: check all non-INR sessions
        relevant_sessions = [s for s in sessions if s != "NSE_INR"]

    for sess_name in relevant_sessions:
        sess = sessions[sess_name]
        if _in_session(sess["start"], sess["end"], now_time):
            return True, sess_name

    # Return the primary session from profile even if inactive
    profile = settings.FOREX_PROFILES.get(symbol, {})
    primary = profile.get("session", "LONDON")
    return False, primary


# ── API Usage Tracking ────────────────────────────────────────────────────────


def _load_api_usage() -> dict:
    """Load daily API usage from file. Returns a default dict if missing/corrupt."""
    path = settings.DAILY_API_USAGE_FILE
    try:
        with open(path, "r") as f:
            data = json.load(f)
        today = datetime.now(_IST).strftime("%Y-%m-%d")
        # Reset counter if it's a new day
        if data.get("date") != today:
            data = {
                "date": today,
                "twelve_data_calls": 0,
                "last_reset": datetime.now(_IST).isoformat(),
            }
            _save_api_usage(data)
        return data
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        today = datetime.now(_IST).strftime("%Y-%m-%d")
        return {
            "date": today,
            "twelve_data_calls": 0,
            "last_reset": datetime.now(_IST).isoformat(),
        }


def _save_api_usage(data: dict) -> None:
    """Persist API usage data to file."""
    path = settings.DAILY_API_USAGE_FILE
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        log.warning("[FOREX] Failed to save API usage: %s", e)


def _increment_api_calls(n: int = 1) -> bool:
    """
    Increment Twelve Data call counter. Returns True if within daily limit.
    Returns False (and logs a warning) if limit would be exceeded.
    """
    data = _load_api_usage()
    used = data.get("twelve_data_calls", 0)
    if used + n > settings.TWELVE_DATA_DAILY_LIMIT:
        log.warning(
            "[FOREX] Twelve Data daily limit reached (%d/%d). Using cache only.",
            used, settings.TWELVE_DATA_DAILY_LIMIT,
        )
        return False
    data["twelve_data_calls"] = used + n
    _save_api_usage(data)
    return True


# ── OHLCV Fetcher ─────────────────────────────────────────────────────────────


def get_forex_ohlcv(
    symbol: str,
    interval: str,
    outputsize: int = 200,
) -> pd.DataFrame:
    """
    Fetch historical OHLCV candles for a forex pair from Twelve Data.

    volume is always 0 (forex has no volume data).
    timestamp column is UTC datetime.

    On API failure: returns cached data if available, otherwise empty DataFrame.
    Never raises exceptions.

    Args:
        symbol:     Forex pair e.g. "EUR/USD"
        interval:   Candle interval e.g. "4h", "1day"
        outputsize: Number of candles to fetch (default 200)

    Returns:
        DataFrame with columns: [timestamp, open, high, low, close, volume]
    """
    cache_key = (symbol, interval)
    now_ts = time.time()

    # Check in-memory cache first
    cached = _ohlcv_cache.get(cache_key)
    if cached:
        age_minutes = (now_ts - cached["fetched_at"]) / 60
        if age_minutes < settings.FOREX_CACHE_MINUTES:
            log.debug("[FOREX] OHLCV cache hit: %s %s (age=%.1fm)", symbol, interval, age_minutes)
            return cached["df"].copy()

    # Avoid hammering the API after minute-credit exhaustion.
    if is_twelve_data_backed_off():
        if cached:
            return cached["df"].copy()
        return pd.DataFrame()

    # Check daily limit before making a call
    if not _increment_api_calls(1):
        if cached:
            log.warning("[FOREX] Limit hit — returning stale cache for %s %s", symbol, interval)
            return cached["df"].copy()
        return pd.DataFrame()

    base_url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": settings.TWELVE_DATA_API_KEY,
        "format": "JSON",
        "timezone": "UTC",
    }

    df = _fetch_ohlcv_with_retry(symbol, base_url, params, cache_key, cached)
    return df


def _fetch_ohlcv_with_retry(
    symbol: str,
    url: str,
    params: dict,
    cache_key: tuple,
    cached: Optional[dict],
) -> pd.DataFrame:
    """Attempt the HTTP call once, retry after 3s on failure, fall back to cache."""
    for attempt in range(2):
        try:
            _wait_for_rate_limit()
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == "error" or "values" not in data:
                msg = str(data.get("message", "unknown"))
                if "run out of api credits for the current minute" in msg.lower():
                    _set_minute_backoff(65)
                log.warning(
                    "[FOREX] %s OHLCV API error: %s",
                    symbol, msg,
                )
                break

            values = data["values"]
            if not values:
                log.warning("[FOREX] %s OHLCV: empty values list", symbol)
                break

            df = pd.DataFrame(values)
            df = df.rename(columns={"datetime": "timestamp"})
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            for col in ["open", "high", "low", "close"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["volume"] = 0.0
            df = df[["timestamp", "open", "high", "low", "close", "volume"]]
            df = df.sort_values("timestamp").reset_index(drop=True)

            _ohlcv_cache[cache_key] = {"df": df.copy(), "fetched_at": time.time()}
            log.info("[FOREX] Fetched %d candles for %s %s", len(df), symbol, cache_key[1])
            return df

        except requests.RequestException as e:
            log.warning("[FOREX] %s fetch failed (attempt %d): %s. Using cached data.", symbol, attempt + 1, e)
            if attempt == 0:
                time.sleep(3)

    # Return cached data if available
    if cached:
        return cached["df"].copy()
    return pd.DataFrame()


# ── Live Rate Fetcher ─────────────────────────────────────────────────────────


def get_live_rate(symbol: str) -> float:
    """
    Fetch the current price (midpoint) for a forex pair.

    Cached for FOREX_RATE_REFRESH_MIN minutes.
    On failure: returns last known cached rate, or 0.0 if no cache exists.
    """
    now_ts = time.time()
    cached = _rate_cache.get(symbol)
    if cached:
        age_minutes = (now_ts - cached["fetched_at"]) / 60
        if age_minutes < settings.FOREX_RATE_REFRESH_MIN:
            return cached["rate"]

    if is_twelve_data_backed_off():
        if cached:
            return cached["rate"]
        return 0.0

    if not _increment_api_calls(1):
        if cached:
            return cached["rate"]
        return 0.0

    url = "https://api.twelvedata.com/price"
    params = {
        "symbol": symbol,
        "apikey": settings.TWELVE_DATA_API_KEY,
    }

    for attempt in range(2):
        try:
            _wait_for_rate_limit()
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if "price" not in data:
                msg = str(data.get("message", "no price field"))
                if "run out of api credits for the current minute" in msg.lower():
                    _set_minute_backoff(65)
                log.warning("[FOREX] %s live rate error: %s", symbol, msg)
                break

            rate = float(data["price"])
            _rate_cache[symbol] = {"rate": rate, "fetched_at": time.time()}
            log.debug("[FOREX] Live rate %s = %.5f", symbol, rate)
            return rate

        except (requests.RequestException, ValueError) as e:
            log.warning("[FOREX] %s live rate fetch failed (attempt %d): %s. Using cached data.", symbol, attempt + 1, e)
            if attempt == 0:
                time.sleep(3)

    if cached:
        return cached["rate"]
    return 0.0


def _fetch_rate_batch(symbols: list[str], rates: dict[str, float]) -> None:
    """Fetch a batch of up to 7 symbols in one API call (≤ 7 credits)."""
    if not symbols:
        return

    if is_twelve_data_backed_off():
        return

    n = len(symbols)
    if not _increment_api_calls(n):
        return

    batch_str = ",".join(symbols)
    url = "https://api.twelvedata.com/price"
    params = {
        "symbol": batch_str,
        "apikey": settings.TWELVE_DATA_API_KEY,
    }

    try:
        _wait_for_rate_limit()
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict) and data.get("code") == 429:
            msg = str(data.get("message", ""))
            if "run out of api credits" in msg.lower():
                _set_minute_backoff(65)
            log.warning("[FOREX] Batch rate error: %s", msg)
            return

        if n == 1:
            sym = symbols[0]
            price_str = data.get("price")
            if price_str:
                rate = float(price_str)
                _rate_cache[sym] = {"rate": rate, "fetched_at": time.time()}
                rates[sym] = rate
        else:
            for sym in symbols:
                entry = data.get(sym, {})
                price_str = entry.get("price") if isinstance(entry, dict) else None
                if price_str:
                    rate = float(price_str)
                    _rate_cache[sym] = {"rate": rate, "fetched_at": time.time()}
                    rates[sym] = rate
                    log.debug("[FOREX] Batch rate %s = %.5f", sym, rate)

    except (requests.RequestException, ValueError) as e:
        log.warning("[FOREX] Batch rate fetch failed: %s", e)


def get_all_forex_rates() -> dict[str, float]:
    """
    Fetch current live rates for all FOREX_SYMBOLS.

    Uses Twelve Data batch /price endpoint. Free plan allows 8 credits/min,
    so we split into chunks of ≤7 symbols per batch (each symbol = 1 credit),
    with a rate-limit pause between batches.

    Returns:
        Dict mapping symbol to rate (e.g. {"EUR/USD": 1.0842, ...})
    """
    symbols = settings.FOREX_SYMBOLS
    rates: dict[str, float] = {}

    for sym in symbols:
        cached = _rate_cache.get(sym)
        rates[sym] = cached["rate"] if cached else 0.0

    if is_twelve_data_backed_off():
        log.debug("[FOREX] Batch rates: backed off, returning cached values")
        return rates

    now_ts = time.time()
    stale = [
        sym for sym in symbols
        if sym not in _rate_cache
        or (now_ts - _rate_cache[sym]["fetched_at"]) / 60 >= settings.FOREX_RATE_REFRESH_MIN
    ]
    if not stale:
        return rates

    _BATCH_SIZE = 8
    for i in range(0, len(stale), _BATCH_SIZE):
        chunk = stale[i : i + _BATCH_SIZE]
        _fetch_rate_batch(chunk, rates)

    return rates