"""
Session Quality Scoring.

Returns a 0.0–1.0 quality multiplier for a given timestamp and symbol.

NSE session quality:
  9:15–11:30  = 1.0   (high liquidity open)
  11:30–14:00 = 0.70  (midday drift, lower volume)
  14:00–15:00 = 0.85  (afternoon pickup)
  15:00–15:30 = 0.60  (last 30 min — institutional window dressing)
  outside NSE hours = 0.0  (no valid signal)

Crypto session quality (24x7 but volume-weighted by UTC hour):
  Weekday  = 0.85 default
  Weekend  = 0.65 default
  High-liquidity windows (UTC):
    00:00–04:00 = 0.70  (Asia overlap)
    08:00–12:00 = 1.00  (EU open)
    13:00–17:00 = 0.95  (US open)
    20:00–24:00 = 0.75  (US close / Asia pre-open)
"""

from __future__ import annotations

import pandas as pd


# ── NSE market sessions (IST) ─────────────────────────────────────────────────
# Represented as (open_min, close_min, quality) tuples in minutes-since-midnight
_NSE_SESSIONS: list[tuple[int, int, float]] = [
    (9 * 60 + 15,  11 * 60 + 30, 1.00),   # High-liquidity open
    (11 * 60 + 30, 14 * 60 + 0,  0.70),   # Midday drift
    (14 * 60 + 0,  15 * 60 + 0,  0.85),   # Afternoon pickup
    (15 * 60 + 0,  15 * 60 + 30, 0.60),   # Pre-close
]
_NSE_MARKET_OPEN_MIN  = 9 * 60 + 15
_NSE_MARKET_CLOSE_MIN = 15 * 60 + 30

# ── Crypto sessions (UTC hour ranges → quality) ───────────────────────────────
_CRYPTO_SESSIONS: list[tuple[int, int, float]] = [
    (0,  4,  0.70),   # Asia quiet
    (4,  8,  0.80),   # Asia active
    (8,  12, 1.00),   # EU open
    (12, 17, 0.95),   # US open
    (17, 20, 0.80),   # EU close
    (20, 24, 0.75),   # Late US
]
_CRYPTO_WEEKEND_MULTIPLIER = 0.80   # Slightly lower on weekends

# ── Symbols known to be NSE equities / indices ───────────────────────────────
_NSE_KEYWORDS = {
    "NIFTY", "BANKNIFTY", "SENSEX", "RELIANCE", "TCS", "INFY",
    "HDFCBANK", "ICICIBANK", "SBIN", "WIPRO", "TATAMOTORS", "BAJFINANCE",
    "AXISBANK", "KOTAKBANK", "LT", "ITC", "ADANIENT", "MARUTI", "TITAN",
    "HINDUNILVR", "SUNPHARMA", "BHARTIARTL",
}


# ─────────────────────────────────────────────────────────────────────────────

def get_session_quality(timestamp: pd.Timestamp, symbol: str) -> float:
    """
    Return a session quality score 0.0–1.0 for the given bar timestamp.

    Parameters
    ----------
    timestamp : pd.Timestamp
        The bar's timestamp. Timezone-aware is preferred; if naive it is assumed
        to be UTC for crypto and IST for NSE.
    symbol : str
        Trading symbol (e.g. "NIFTY 50", "BTC/USDT").

    Returns
    -------
    float in [0.0, 1.0]
    """
    if pd.isna(timestamp):
        return 0.5   # Unknown timestamp → neutral

    is_crypto = _is_crypto(symbol)

    if is_crypto:
        return _crypto_quality(timestamp)
    else:
        return _nse_quality(timestamp)


def get_session_quality_series(df: pd.DataFrame, symbol: str) -> pd.Series:
    """
    Vectorised version — adds a `session_quality` column to df.

    Parameters
    ----------
    df : pd.DataFrame
        Must have a `timestamp` column or a DatetimeIndex.
    symbol : str

    Returns
    -------
    pd.Series with dtype float64, index aligned to df.
    """
    ts_col = _extract_timestamps(df)
    return ts_col.apply(lambda t: get_session_quality(t, symbol))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_crypto(symbol: str) -> bool:
    """Return True for crypto symbols (contain '/' like 'BTC/USDT')."""
    return "/" in symbol


def _nse_quality(ts: pd.Timestamp) -> float:
    """Session quality for NSE equity symbols (in IST)."""
    try:
        # Convert to IST if tz-aware
        if ts.tzinfo is not None:
            ts = ts.tz_convert("Asia/Kolkata")
        else:
            # Naive timestamp — treat as IST
            ts = ts.tz_localize("Asia/Kolkata")
    except Exception:
        pass

    # NSE closed on weekends
    if ts.weekday() >= 5:
        return 0.0

    minutes = ts.hour * 60 + ts.minute

    if minutes < _NSE_MARKET_OPEN_MIN or minutes >= _NSE_MARKET_CLOSE_MIN:
        return 0.0   # Pre-market or after-market

    for open_min, close_min, quality in _NSE_SESSIONS:
        if open_min <= minutes < close_min:
            return quality

    return 0.0


def _crypto_quality(ts: pd.Timestamp) -> float:
    """Session quality for crypto symbols (in UTC)."""
    try:
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC")
        else:
            ts = ts.tz_localize("UTC")
    except Exception:
        pass

    hour = ts.hour
    base = 0.75

    for start_h, end_h, quality in _CRYPTO_SESSIONS:
        if start_h <= hour < end_h:
            base = quality
            break

    # Weekend discount
    if ts.weekday() >= 5:
        base *= _CRYPTO_WEEKEND_MULTIPLIER

    return round(min(base, 1.0), 3)


def _extract_timestamps(df: pd.DataFrame) -> pd.Series:
    """Return a Series of pd.Timestamp from df's timestamp column or index."""
    if "timestamp" in df.columns:
        return pd.to_datetime(df["timestamp"])
    elif isinstance(df.index, pd.DatetimeIndex):
        return pd.Series(df.index, index=df.index)
    else:
        # Fall back to a neutral series
        return pd.Series([pd.Timestamp("2000-01-01")] * len(df), index=df.index)
