"""
Forex Data Fetcher - OHLCV + Live Rates via Twelve Data API (free plan) with keyless yFinance fallback.

Free plan limits: 800 API calls/day, 8 API credits/minute.
When Twelve Data quota or minute-rate is reached or errors occur, this module seamlessly
falls back to keyless yFinance data so that trading intelligence NEVER stalls or produces missing bars.

Rate-limit strategy: a minimum inter-call gap (_MIN_CALL_GAP_SECONDS) is
enforced between successive HTTP requests so that the scanner never fires
more than ~8 calls per minute even when multiple endpoints are called in
rapid succession.
"""
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from config import settings
from config.logger import get_logger

log = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# ── In-memory caches ─────────────────────────────────────────────────────────
_ohlcv_cache: dict[tuple[str, str], dict] = {}
_rate_cache: dict[str, dict] = {}

# Backoff when Twelve Data minute credits are exhausted.
_minute_backoff_until: float = 0.0
_MIN_CALL_GAP_SECONDS: float = 8.0
_last_api_call_ts: float = 0.0

# ── Symbol to yfinance mapping ────────────────────────────────────────────────
_SYMBOL_YF_MAP = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "USD/CHF": "USDCHF=X",
    "AUD/USD": "AUDUSD=X",
    "NZD/USD": "NZDUSD=X",
    "USD/CAD": "USDCAD=X",
    "EUR/GBP": "EURGBP=X",
    "EUR/JPY": "EURJPY=X",
    "GBP/JPY": "GBPJPY=X",
    "AUD/JPY": "AUDJPY=X",
    "USD/INR": "USDINR=X",
    "EUR/INR": "EURINR=X",
    "GBP/INR": "GBPINR=X",
    "JPY/INR": "JPYINR=X",
}


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
    Returns: (is_active, session_name)
    """
    now_ist = datetime.now(_IST)
    if now_ist.weekday() >= 5:
        return False, "CLOSED_WEEKEND"
    now_time = now_ist.strftime("%H:%M")

    inr_pairs = {"USD/INR", "EUR/INR", "GBP/INR", "JPY/INR"}
    is_inr = symbol in inr_pairs
    sessions = settings.FOREX_SESSIONS

    def _in_session(start: str, end: str, current: str) -> bool:
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    if is_inr:
        sess = sessions.get("NSE_INR", {})
        active = _in_session(sess.get("start", "09:00"), sess.get("end", "17:00"), now_time)
        return active, "NSE_INR"

    relevant_sessions: list[str] = []
    for sess_name, sess_data in sessions.items():
        if sess_name == "NSE_INR":
            continue
        if symbol in sess_data.get("best_pairs", []):
            relevant_sessions.append(sess_name)

    if not relevant_sessions:
        relevant_sessions = [s for s in sessions if s != "NSE_INR"]

    for sess_name in relevant_sessions:
        sess = sessions[sess_name]
        if _in_session(sess["start"], sess["end"], now_time):
            return True, sess_name

    profile = settings.FOREX_PROFILES.get(symbol, {})
    primary = profile.get("session", "LONDON")
    return False, primary


# ── API Usage Tracking ────────────────────────────────────────────────────────

def _load_api_usage() -> dict:
    default_usage = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "calls_today": 0,
        "daily_limit": settings.TWELVE_DATA_DAILY_LIMIT,
    }
    try:
        with open(settings.DAILY_API_USAGE_FILE, "r") as f:
            data = json.load(f)
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if data.get("date") != today_str:
                data = default_usage
                _save_api_usage(data)
            if "calls_today" not in data and "twelve_data_calls" in data:
                data["calls_today"] = data["twelve_data_calls"]
            if "daily_limit" not in data:
                data["daily_limit"] = settings.TWELVE_DATA_DAILY_LIMIT
            return data
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        _save_api_usage(default_usage)
        return default_usage


def _save_api_usage(usage: dict) -> None:
    try:
        with open(settings.DAILY_API_USAGE_FILE, "w") as f:
            json.dump(usage, f, indent=2)
    except OSError as e:
        log.warning("[FOREX] Failed to save API usage file: %s", e)


def _increment_api_calls(count: int = 1) -> bool:
    usage = _load_api_usage()
    cur = usage.get("calls_today", usage.get("twelve_data_calls", 0))
    limit = usage.get("daily_limit", settings.TWELVE_DATA_DAILY_LIMIT)
    if cur + count > limit:
        log.warning(
            "[FOREX] Daily API limit reached: %d/%d calls used today",
            cur,
            limit,
        )
        return False
    usage["calls_today"] = cur + count
    usage["twelve_data_calls"] = cur + count
    _save_api_usage(usage)
    return True


# ── yFinance Keyless Fallbacks ────────────────────────────────────────────────

def _fetch_ohlcv_yfinance(symbol: str, interval: str, outputsize: int = 200) -> pd.DataFrame:
    """Fetch OHLCV candles via yfinance as a zero-cost, unlimited fallback."""
    yf_sym = _SYMBOL_YF_MAP.get(symbol)
    if not yf_sym:
        return pd.DataFrame()
    try:
        import yfinance as yf
        period = "60d" if interval in ("1h", "4h") else "1y"
        yf_interval = "1h" if interval in ("1h", "4h") else "1d"
        t = yf.Ticker(yf_sym)
        df = t.history(period=period, interval=yf_interval)
        if df.empty or "Close" not in df.columns:
            return pd.DataFrame()
        df = df.reset_index()
        col_map = {"Datetime": "timestamp", "Date": "timestamp", "Open": "open", "High": "high", "Low": "low", "Close": "close"}
        df = df.rename(columns=col_map)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        for c in ["open", "high", "low", "close"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["volume"] = 0.0
        df = df[["timestamp", "open", "high", "low", "close", "volume"]].dropna(subset=["close"])
        if interval == "4h":
            df = df.set_index("timestamp").resample("4h").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).dropna().reset_index()
        df = df.tail(outputsize).sort_values("timestamp").reset_index(drop=True)
        log.debug("[FOREX] Fetched %d candles for %s %s via yfinance fallback", len(df), symbol, interval)
        return df
    except Exception as e:
        log.debug("[FOREX] yfinance OHLCV fallback failed for %s %s: %s", symbol, interval, e)
        return pd.DataFrame()


def _fetch_rates_yfinance_batch(symbols: list[str]) -> dict[str, float]:
    """Fetch live rates for multiple pairs in a single fast yfinance batch download."""
    results: dict[str, float] = {}
    valid_map = {s: _SYMBOL_YF_MAP[s] for s in symbols if s in _SYMBOL_YF_MAP}
    if not valid_map:
        return results

    tickers = list(valid_map.values())
    try:
        import yfinance as yf
        now_ts = time.time()
        data = yf.download(tickers, period="1d", interval="5m", progress=False)
        if not data.empty and "Close" in data.columns:
            close_data = data["Close"]
            for sym, ticker in valid_map.items():
                try:
                    if len(tickers) == 1:
                        c_series = close_data.dropna()
                    else:
                        c_series = close_data[ticker].dropna() if ticker in close_data else pd.Series()
                    if not c_series.empty:
                        last_c = float(c_series.iloc[-1])
                        if last_c > 0:
                            results[sym] = last_c
                            _rate_cache[sym] = {"rate": last_c, "fetched_at": now_ts}
                except Exception:
                    pass
    except Exception as e:
        log.debug("[FOREX] Batch yfinance rate download failed: %s", e)
    return results


def _fetch_rate_yfinance(symbol: str) -> float:
    """Fetch live quote via yfinance as a zero-cost fallback."""
    yf_sym = _SYMBOL_YF_MAP.get(symbol)
    if not yf_sym:
        return 0.0
    try:
        import yfinance as yf
        t = yf.Ticker(yf_sym)
        df = t.history(period="1d", interval="1m")
        if not df.empty and "Close" in df.columns:
            rate = float(df["Close"].iloc[-1])
            if rate > 0:
                _rate_cache[symbol] = {"rate": rate, "fetched_at": time.time()}
                return rate
    except Exception as e:
        log.debug("[FOREX] yfinance live rate fallback failed for %s: %s", symbol, e)
    return 0.0


# ── OHLCV Fetcher ─────────────────────────────────────────────────────────────

def get_forex_ohlcv(
    symbol: str,
    interval: str,
    outputsize: int = 200,
) -> pd.DataFrame:
    """
    Fetch historical OHLCV candles for a forex pair.
    Uses fast keyless yfinance first to protect Twelve Data 8-credit/min limit,
    with Twelve Data fallback.
    Never raises exceptions.
    """
    cache_key = (symbol, interval)
    now_ts = time.time()

    # Check in-memory cache first
    cached = _ohlcv_cache.get(cache_key)
    if cached:
        age_minutes = (now_ts - cached["fetched_at"]) / 60
        if age_minutes < settings.FOREX_CACHE_MINUTES:
            return cached["df"].copy()

    # Fast keyless fetch (0.3s, 0 rate-limits, completely protects free quota)
    yf_df = _fetch_ohlcv_yfinance(symbol, interval, outputsize)
    if not yf_df.empty and len(yf_df) >= 20:
        _ohlcv_cache[cache_key] = {"df": yf_df.copy(), "fetched_at": time.time()}
        return yf_df

    # If Twelve Data is backed off or daily limit reached, return whatever cache we have
    if is_twelve_data_backed_off() or not _increment_api_calls(1):
        if cached:
            return cached["df"].copy()
        return yf_df if not yf_df.empty else pd.DataFrame()

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
    if not df.empty:
        return df

    return cached["df"].copy() if cached else pd.DataFrame()


def _fetch_ohlcv_with_retry(
    symbol: str,
    url: str,
    params: dict,
    cache_key: tuple,
    cached: Optional[dict],
) -> pd.DataFrame:
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
                log.warning("[FOREX] %s OHLCV API error: %s", symbol, msg)
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
            return df

        except requests.RequestException as e:
            log.warning("[FOREX] %s fetch failed (attempt %d): %s. Using cached data.", symbol, attempt + 1, e)
            if attempt == 0:
                time.sleep(2)

    if cached:
        return cached["df"].copy()
    return pd.DataFrame()


# ── Live Rate Fetcher ─────────────────────────────────────────────────────────

def get_forex_rate(symbol: str) -> float:
    """Fetch current live exchange rate for a forex pair."""
    now_ts = time.time()
    cached = _rate_cache.get(symbol)
    if cached:
        age_minutes = (now_ts - cached["fetched_at"]) / 60
        if age_minutes < settings.FOREX_RATE_REFRESH_MIN:
            return cached["rate"]

    if is_twelve_data_backed_off() or not _increment_api_calls(1):
        yf_rate = _fetch_rate_yfinance(symbol)
        if yf_rate > 0:
            return yf_rate
        if cached:
            return cached["rate"]
        return 0.0

    url = "https://api.twelvedata.com/price"
    params = {"symbol": symbol, "apikey": settings.TWELVE_DATA_API_KEY}

    try:
        _wait_for_rate_limit()
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if "price" in data:
            rate = float(data["price"])
            _rate_cache[symbol] = {"rate": rate, "fetched_at": time.time()}
            return rate
    except Exception as e:
        log.warning("[FOREX] %s live rate fetch error: %s", symbol, e)

    # Fallback
    yf_rate = _fetch_rate_yfinance(symbol)
    if yf_rate > 0:
        return yf_rate
    return cached["rate"] if cached else 0.0


# Backward compatibility alias
get_live_rate = get_forex_rate


def _fetch_rate_batch(symbols: list[str], rates: dict[str, float]) -> None:
    """Fetch a batch of symbols from Twelve Data with fallback."""
    if not symbols:
        return
    if is_twelve_data_backed_off() or not _increment_api_calls(len(symbols)):
        for s in symbols:
            yf_r = _fetch_rate_yfinance(s)
            if yf_r > 0:
                rates[s] = yf_r
        return

    batch_str = ",".join(symbols)
    url = "https://api.twelvedata.com/price"
    params = {"symbol": batch_str, "apikey": settings.TWELVE_DATA_API_KEY}

    try:
        _wait_for_rate_limit()
        resp = requests.get(url, params=params, timeout=12)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict) and data.get("code") == 429:
            _set_minute_backoff(65)
            for s in symbols:
                yf_r = _fetch_rate_yfinance(s)
                if yf_r > 0:
                    rates[s] = yf_r
            return

        if len(symbols) == 1:
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
                else:
                    yf_r = _fetch_rate_yfinance(sym)
                    if yf_r > 0:
                        rates[sym] = yf_r

    except Exception as e:
        log.warning("[FOREX] Batch rate fetch failed: %s, falling back to yfinance", e)
        for s in symbols:
            yf_r = _fetch_rate_yfinance(s)
            if yf_r > 0:
                rates[s] = yf_r


def get_all_forex_rates() -> dict[str, float]:
    """Fetch live rates for all FOREX_SYMBOLS with zero missing rates."""
    symbols = settings.FOREX_SYMBOLS
    rates: dict[str, float] = {}

    for sym in symbols:
        cached = _rate_cache.get(sym)
        rates[sym] = cached["rate"] if cached else 0.0

    now_ts = time.time()
    stale = [
        sym for sym in symbols
        if sym not in _rate_cache
        or rates[sym] <= 0
        or (now_ts - _rate_cache[sym]["fetched_at"]) / 60 >= settings.FOREX_RATE_REFRESH_MIN
    ]
    if not stale:
        return rates

    # Fast single batch fetch for all stale pairs
    batch_rates = _fetch_rates_yfinance_batch(stale)
    rates.update(batch_rates)

    # For any remaining unresolved pairs, try fallback batch
    remaining = [s for s in symbols if rates.get(s, 0.0) <= 0]
    if remaining and not is_twelve_data_backed_off():
        _BATCH_SIZE = 7
        for i in range(0, len(remaining), _BATCH_SIZE):
            chunk = remaining[i : i + _BATCH_SIZE]
            _fetch_rate_batch(chunk, rates)

    return rates


# ── Market & Session Intelligence ─────────────────────────────────────────────

def compute_session_intelligence() -> dict:
    """
    Evaluate global trading sessions, active overlap, and market liquidity.
    """
    now_ist = datetime.now(_IST)
    is_weekend = now_ist.weekday() == 5 or (now_ist.weekday() == 6 and now_ist.hour < 23)
    now_time = now_ist.strftime("%H:%M")
    sessions = settings.FOREX_SESSIONS

    def _in_session(start: str, end: str, current: str) -> bool:
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    status_map = {}
    active_sessions = []
    for name, data in sessions.items():
        is_open = False if is_weekend else _in_session(data["start"], data["end"], now_time)
        status_map[name] = {
            "open": is_open,
            "start": data["start"],
            "end": data["end"],
            "best_pairs": data.get("best_pairs", []),
        }
        if is_open:
            active_sessions.append(name)

    major_active = [s for s in active_sessions if s in ("ASIAN", "LONDON", "NEWYORK")]
    overlap_desc = " + ".join(major_active) if major_active else "OFF-HOURS"

    if "LONDON" in active_sessions and "NEWYORK" in active_sessions:
        liquidity = "HIGH"
        liquidity_score = 95
    elif "LONDON" in active_sessions or "NEWYORK" in active_sessions:
        liquidity = "MODERATE"
        liquidity_score = 75
    elif "ASIAN" in active_sessions:
        liquidity = "MODERATE"
        liquidity_score = 50
    else:
        liquidity = "LOW"
        liquidity_score = 25

    return {
        "sessions": status_map,
        "active_sessions": active_sessions,
        "overlap": overlap_desc,
        "liquidity": liquidity,
        "liquidity_score": liquidity_score,
        "current_ist": now_ist.strftime("%H:%M:%S IST"),
    }


def compute_currency_strengths() -> dict:
    """
    Compute relative strength index for 8 major/INR currencies and overall risk sentiment
    using fast parallel daily changes.
    """
    currencies = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "INR"]
    inv_map = {v: k for k, v in _SYMBOL_YF_MAP.items()}
    tickers = list(_SYMBOL_YF_MAP.values())
    changes: dict[str, float] = {}

    try:
        import yfinance as yf
        data = yf.download(tickers, period="5d", interval="1d", progress=False)
        if "Close" in data:
            for t in tickers:
                if t in data["Close"]:
                    s = data["Close"][t].dropna()
                    if len(s) >= 2:
                        c0 = float(s.iloc[-2])
                        c1 = float(s.iloc[-1])
                        if c0 > 0:
                            sym = inv_map.get(t, t)
                            changes[sym] = ((c1 - c0) / c0) * 100.0
    except Exception as e:
        log.warning("[FOREX] yfinance batch download for currency strength failed: %s", e)

    curr_scores = {c: [] for c in currencies}
    for sym, chg in changes.items():
        parts = sym.split("/")
        if len(parts) == 2:
            base, quote = parts[0], parts[1]
            if base in curr_scores:
                curr_scores[base].append(chg)
            if quote in curr_scores:
                curr_scores[quote].append(-chg)

    strengths: dict[str, float] = {}
    for c, vals in curr_scores.items():
        avg = float(np.mean(vals)) if vals else 0.0
        strengths[c] = round(avg, 2)

    # Risk Sentiment analysis: High yield / Cyclical vs Safe Haven
    growth_score = strengths.get("AUD", 0) + strengths.get("GBP", 0) + strengths.get("EUR", 0)
    safe_score = strengths.get("USD", 0) + strengths.get("JPY", 0) + strengths.get("CHF", 0)
    risk_sentiment = "RISK ON" if growth_score >= safe_score else "RISK OFF"

    return {
        "strengths": strengths,
        "risk_sentiment": risk_sentiment,
        "growth_score": round(growth_score, 2),
        "safe_haven_score": round(safe_score, 2),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
