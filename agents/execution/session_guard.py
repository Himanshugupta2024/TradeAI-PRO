"""
TradeAI-PRO — Market Session Guard & Data Freshness Gate.
=========================================================
Ensures:
  1. Equities (NSE) execute only during active market hours in Asia/Kolkata.
  2. Cryptocurrencies execute 24/7.
  3. Stale or EOD historical candles never trigger live intraday autonomous trades.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo
import pandas as pd

log = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# Standard NSE Cash Market Session Hours (IST)
_NSE_OPEN_HOUR = 9
_NSE_OPEN_MINUTE = 15
_NSE_CLOSE_HOUR = 15
_NSE_CLOSE_MINUTE = 30


def get_current_ist_time() -> datetime:
    """Return the current localized datetime in Asia/Kolkata."""
    return datetime.now(_IST)


def is_nse_market_open(dt: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Check if the National Stock Exchange (NSE) cash market is currently open.
    Ensures input datetime is strictly normalized to Asia/Kolkata timezone.
    Returns:
      (True, "SESSION_ACTIVE") or (False, reason)
    """
    now = dt or get_current_ist_time()
    if now.tzinfo is not None:
        now = now.astimezone(_IST)
    else:
        now = now.replace(tzinfo=_IST)

    # 1. Weekend check (Monday = 0, Sunday = 6)
    if now.weekday() >= 5:
        return False, "NSE_CLOSED_WEEKEND"

    # 2. Time window check: 09:15 to 15:30 IST
    market_open = now.replace(hour=_NSE_OPEN_HOUR, minute=_NSE_OPEN_MINUTE, second=0, microsecond=0)
    market_close = now.replace(hour=_NSE_CLOSE_HOUR, minute=_NSE_CLOSE_MINUTE, second=0, microsecond=0)

    if now < market_open:
        return False, "NSE_PRE_MARKET"
    if now > market_close:
        return False, "NSE_POST_MARKET"

    # 3. Holiday check
    try:
        from agents.market_intelligence.nse_holidays import is_holiday
        if is_holiday(now.date()):
            return False, "NSE_MARKET_HOLIDAY"
    except Exception:
        pass

    return True, "SESSION_ACTIVE"


def get_next_nse_session(dt: Optional[datetime] = None) -> Tuple[datetime, str]:
    """
    Calculate the next official NSE trading session open (09:15 IST).
    Skips weekends and exchange holidays.
    Returns:
      (next_session_datetime_ist, description_string)
      e.g. (datetime, "today (2026-11-05) at 09:15 IST") or ("tomorrow (2026-11-06) at 09:15 IST")
    """
    from datetime import timedelta
    try:
        from agents.market_intelligence.nse_holidays import is_holiday
    except Exception:
        def is_holiday(d): return False

    now = dt or get_current_ist_time()
    if now.tzinfo is not None:
        now = now.astimezone(_IST)
    else:
        now = now.replace(tzinfo=_IST)

    market_open_today = now.replace(hour=_NSE_OPEN_HOUR, minute=_NSE_OPEN_MINUTE, second=0, microsecond=0)
    candidate_date = now.date()

    # If today is a trading day and market has not yet opened
    if now < market_open_today and candidate_date.weekday() < 5 and not is_holiday(candidate_date):
        return market_open_today, f"today ({candidate_date.isoformat()}) at 09:15 IST"

    # Otherwise forward-scan day by day for the next valid trading business day
    candidate_date += timedelta(days=1)
    while candidate_date.weekday() >= 5 or is_holiday(candidate_date):
        candidate_date += timedelta(days=1)

    next_dt = datetime(
        candidate_date.year, candidate_date.month, candidate_date.day,
        _NSE_OPEN_HOUR, _NSE_OPEN_MINUTE, 0, tzinfo=_IST
    )
    days_ahead = (candidate_date - now.date()).days
    if days_ahead == 1:
        desc = f"tomorrow ({candidate_date.isoformat()}) at 09:15 IST"
    else:
        day_name = candidate_date.strftime("%A")
        desc = f"{day_name} ({candidate_date.isoformat()}) at 09:15 IST"

    return next_dt, desc


def check_session_gate(asset_class: str, dt: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Verify whether the target asset class is permitted to trade at the current time.
    """
    ac = asset_class.upper().strip()
    if ac in ("CRYPTO", "CRYPTOCURRENCY"):
        return True, "SESSION_ACTIVE"

    if ac in ("EQUITY", "NSE", "STOCK", "INDEX"):
        return is_nse_market_open(dt)

    if ac == "FOREX":
        # Forex operates 24/5 (Mon morning to Fri evening UTC)
        now_utc = datetime.now(timezone.utc)
        if now_utc.weekday() == 5 or (now_utc.weekday() == 6 and now_utc.hour < 21):
            return False, "FOREX_CLOSED_WEEKEND"
        return True, "SESSION_ACTIVE"

    # Default fallback
    return True, "SESSION_ACTIVE"


def evaluate_data_freshness(
    df: pd.DataFrame,
    symbol: str,
    max_live_age_seconds: int = 900,     # 15 minutes
    max_delayed_age_seconds: int = 3600, # 60 minutes
) -> Tuple[str, float]:
    """
    Inspect candle timestamp freshness.
    Returns (freshness_status, age_seconds).
    Statuses:
      - LIVE        : age <= 15 minutes
      - DELAYED     : 15 mins < age <= 60 mins
      - STALE       : age > 60 mins (Reject intraday entry)
      - EOD         : Bar is from previous trading day
      - UNAVAILABLE : No bars or invalid timestamp
    """
    if df is None or df.empty:
        return "UNAVAILABLE", 999999.0

    last_bar = df.iloc[-1]
    ts_val = last_bar.get("timestamp")
    if ts_val is None:
        if isinstance(df.index, pd.DatetimeIndex):
            ts_val = df.index[-1]
        else:
            return "UNAVAILABLE", 999999.0

    if not isinstance(ts_val, (datetime, pd.Timestamp)):
        try:
            ts_val = pd.to_datetime(ts_val)
        except Exception:
            return "UNAVAILABLE", 999999.0

    # Ensure UTC normalized comparison
    if ts_val.tzinfo is None:
        if "/" not in symbol:
            # NSE equities from nse_fetcher are in Asia/Kolkata
            ts_localized = ts_val.replace(tzinfo=_IST)
            ts_utc = ts_localized.astimezone(timezone.utc)
        else:
            ts_utc = ts_val.replace(tzinfo=timezone.utc)
    else:
        ts_utc = ts_val.astimezone(timezone.utc)

    now_utc = datetime.now(timezone.utc)
    age_seconds = max(0.0, (now_utc - ts_utc).total_seconds())

    # Date check and Session bounds check for NSE equities
    if "/" not in symbol:
        bar_ist = ts_utc.astimezone(_IST)
        today_ist = get_current_ist_time().date()

        # Check if bar is from prior day
        if bar_ist.date() < today_ist:
            return "EOD", age_seconds

        # Daily candle check: often 00:00:00
        if bar_ist.hour == 0 and bar_ist.minute == 0 and bar_ist.second == 0:
            return "EOD", age_seconds

        # Check for weekend candle
        if bar_ist.weekday() >= 5:
            return "IMPOSSIBLE_SESSION_TIME", age_seconds

        # Check market session window (09:15 to 15:30 IST)
        bar_time_minutes = bar_ist.hour * 60 + bar_ist.minute
        open_time_minutes = _NSE_OPEN_HOUR * 60 + _NSE_OPEN_MINUTE
        close_time_minutes = _NSE_CLOSE_HOUR * 60 + _NSE_CLOSE_MINUTE

        if bar_time_minutes < open_time_minutes or bar_time_minutes > close_time_minutes:
            return "IMPOSSIBLE_SESSION_TIME", age_seconds

    if age_seconds <= max_live_age_seconds:
        return "LIVE", age_seconds
    elif age_seconds <= max_delayed_age_seconds:
        return "DELAYED", age_seconds
    else:
        return "STALE", age_seconds
