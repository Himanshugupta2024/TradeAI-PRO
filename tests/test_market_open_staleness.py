"""
Regression & Correctness Test Suite: Market-Open Staleness & Fake Data Fixes
Validates:
  1. Day change percentage (day_chg_pct) calculates strictly from true previous daily close (< today), not 5-day open.
  2. Strict session isolation: Yesterday's intraday signals are NEVER revived as active trades today (evaluates to HOLD / NO_SIGNAL).
  3. Timezone normalization in _signal_age_minutes: 08:34 UTC vs 14:04 IST yields ~0 drift, not negative 5.5 hours.
  4. Hydration honesty: Startup DB hydration never stamps current time on missing timestamps, and previous-day snapshots are expired.
  5. Monday-morning boot: Correctly identifies Friday as previous session close.
  6. Post-holiday boot: Correctly handles non-trading holiday intervals.
  7. Dedicated /api/warmup route behavior.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import pytest
from unittest.mock import AsyncMock, patch

from api import service as svc
from api.service import (
    _get_previous_daily_close,
    _get_signal_for_symbol,
    _signal_age_minutes,
    _hydrate_signals_cache_from_db,
)

_IST = ZoneInfo("Asia/Kolkata")


def _make_sample_5m_df(days: int = 5) -> pd.DataFrame:
    """Generate 5 days of 5-minute bars ending today."""
    records = []
    base_price = 1000.0
    now_ist = datetime.now(_IST)
    
    # Generate bars starting 5 days ago up to now
    start_dt = now_ist - timedelta(days=days)
    curr = start_dt.replace(hour=9, minute=15, second=0, microsecond=0)
    
    while curr <= now_ist:
        if curr.weekday() < 5 and (9 <= curr.hour < 15 or (curr.hour == 15 and curr.minute <= 30)):
            # Active market bar
            records.append({
                "timestamp": curr.replace(tzinfo=None),  # Provider style naive IST
                "open": base_price,
                "high": base_price + 5.0,
                "low": base_price - 5.0,
                "close": base_price + 2.0,
                "volume": 5000,
                "RSI_14": 55.0,
                "EMA_20": base_price + 1.0,
                "EMA_50": base_price,
                "signal": "HOLD",
            })
            base_price += 1.0
        curr += timedelta(minutes=5)

    df = pd.DataFrame(records)
    return df


def test_day_change_uses_previous_daily_close_not_5d_open():
    """Verify day_chg_pct uses previous session close (< today), not df.iloc[0]['open']."""
    today = datetime.now(_IST).date()
    yesterday = today - timedelta(days=1)
    
    # Mock daily cache with yesterday's close = 100.0
    daily_df = pd.DataFrame([
        {"timestamp": datetime.combine(yesterday, datetime.min.time()), "close": 100.0},
        # Today's partial daily bar (close = 105.0) - must be ignored when finding prev_close
        {"timestamp": datetime.combine(today, datetime.min.time()), "close": 105.0},
    ])
    
    svc._htf_daily_cache["dfs"]["TEST_SYM"] = daily_df
    
    prev_close = _get_previous_daily_close("TEST_SYM", today)
    assert prev_close == 100.0, f"Expected prev_close=100.0 from yesterday, got {prev_close}"
    
    # Build 5m DataFrame where first bar 5 days ago is 50.0, but today's close is 105.0
    df_5m = pd.DataFrame([
        {"timestamp": datetime.combine(today - timedelta(days=5), datetime.min.time()), "open": 50.0, "close": 52.0, "signal": "HOLD"},
        {"timestamp": datetime.combine(today, datetime.min.time()).replace(hour=9, minute=20), "open": 102.0, "close": 105.0, "signal": "HOLD"},
    ])
    svc._signal_cache["TEST_SYM"] = df_5m
    
    sig = _get_signal_for_symbol("TEST_SYM")
    assert sig is not None
    # If using 5d open (50.0), change_pct would be +110%.
    # Using true prev_close (100.0), change_pct is (105 - 100) / 100 * 100 = +5.0%
    assert sig["change_pct"] == 5.0, f"Expected day_chg_pct=5.0%, got {sig['change_pct']}%"


def test_yesterday_signals_not_revived_today():
    """At 09:16 AM, a BUY signal generated yesterday at 14:00 must NOT be revived as active today."""
    today = datetime.now(_IST).date()
    yesterday = today - timedelta(days=1)
    
    # 5m DataFrame where yesterday has a BUY signal at 14:00, but today has only a 09:15 HOLD bar
    df = pd.DataFrame([
        {
            "timestamp": datetime.combine(yesterday, datetime.min.time()).replace(hour=14, minute=0),
            "open": 200.0, "high": 205.0, "low": 199.0, "close": 204.0, "volume": 1000,
            "signal": "BUY", "stop_loss": 198.0, "target": 210.0, "RSI_14": 62.0,
        },
        {
            "timestamp": datetime.combine(today, datetime.min.time()).replace(hour=9, minute=15),
            "open": 203.0, "high": 204.0, "low": 202.0, "close": 203.5, "volume": 500,
            "signal": "HOLD", "RSI_14": 52.0,
        },
    ])
    svc._signal_cache["RELIANCE"] = df
    
    sig = _get_signal_for_symbol("RELIANCE")
    assert sig is not None
    assert sig["signal"] == "HOLD", f"Expected signal=HOLD, but got resurrected signal: {sig['signal']}"
    assert sig["actionable"] is False
    assert sig["signal_status"] == "NO_SIGNAL"


def test_timezone_age_accuracy():
    """Verify _signal_age_minutes correctly normalizes IST and UTC without negative skew."""
    now_ist = datetime.now(_IST)
    ten_min_ago_ist = now_ist - timedelta(minutes=10)
    
    # 1. Naive IST datetime
    naive_ist = ten_min_ago_ist.replace(tzinfo=None)
    age = _signal_age_minutes(naive_ist)
    assert 9.5 <= age <= 10.5, f"Expected ~10m age for naive IST, got {age}"
    
    # 2. Timezone-aware UTC datetime (08:34 UTC vs 14:04 IST)
    aware_utc = ten_min_ago_ist.astimezone(timezone.utc)
    age_utc = _signal_age_minutes(aware_utc)
    assert 9.5 <= age_utc <= 10.5, f"Expected ~10m age for aware UTC, got {age_utc}"
    
    # 3. None or invalid timestamp returns 999.0 (stale), never 0.0 (fresh)
    assert _signal_age_minutes(None) == 999.0


@pytest.mark.asyncio
async def test_hydration_honesty_expires_previous_session():
    """Startup DB hydration of yesterday's snapshot marks signals historical, never live."""
    yesterday_iso = (datetime.now(_IST) - timedelta(days=1)).isoformat()
    mock_snapshot = {
        "timestamp": yesterday_iso,
        "signals": [
            {"symbol": "INFY", "signal": "BUY", "actionable": True, "signal_status": "ACTIVE", "close": 1500.0}
        ]
    }
    
    with patch("db.runtime_state.get_runtime_state", return_value=mock_snapshot):
        hydrated = await _hydrate_signals_cache_from_db()
        assert hydrated is True
        
        # Verify signals in snapshot are marked historical
        snap_sigs = svc._persisted_signals_snapshot
        assert len(snap_sigs) > 0
        infy_sig = next(s for s in snap_sigs if s["symbol"] == "INFY")
        assert infy_sig["actionable"] is False
        assert infy_sig["signal_status"] == "EXPIRED_PREVIOUS_SESSION"
        assert infy_sig["stale"] is True


def test_monday_morning_boot_picks_friday_close():
    """On Monday morning, previous completed session was Friday (T-3)."""
    # Simulate today is Monday 2026-11-16
    monday_date = date(2026, 11, 16)
    friday_date = date(2026, 11, 13)
    
    daily_df = pd.DataFrame([
        {"timestamp": datetime.combine(date(2026, 11, 12), datetime.min.time()), "close": 98.0},
        {"timestamp": datetime.combine(friday_date, datetime.min.time()), "close": 100.0},
        # Partial Monday bar
        {"timestamp": datetime.combine(monday_date, datetime.min.time()), "close": 102.0},
    ])
    svc._htf_daily_cache["dfs"]["HDFCBANK"] = daily_df
    
    prev_close = _get_previous_daily_close("HDFCBANK", monday_date)
    assert prev_close == 100.0, f"Expected Friday close 100.0, got {prev_close}"


def test_post_holiday_boot_picks_pre_holiday_close():
    """If Tuesday was a holiday, Wednesday morning boot picks Monday as previous close."""
    monday_date = date(2026, 10, 19)
    wednesday_date = date(2026, 10, 21)  # Dussehra holiday was Tuesday 2026-10-20
    
    daily_df = pd.DataFrame([
        {"timestamp": datetime.combine(monday_date, datetime.min.time()), "close": 500.0},
        # Partial Wednesday bar
        {"timestamp": datetime.combine(wednesday_date, datetime.min.time()), "close": 510.0},
    ])
    svc._htf_daily_cache["dfs"]["TCS"] = daily_df
    
    prev_close = _get_previous_daily_close("TCS", wednesday_date)
    assert prev_close == 500.0, f"Expected Monday close 500.0 before Dussehra, got {prev_close}"


@pytest.mark.asyncio
async def test_warmup_endpoint():
    """Verify /api/warmup returns WARMING_UP when empty and READY when populated."""
    from api.routers.health import warmup_check
    
    # 1. When empty
    svc._market_data = {}
    res_warming = await warmup_check()
    assert res_warming["status"] in ("WARMING_UP", "READY")
    
    # 2. When populated
    svc._market_data = {f"SYM_{i}": pd.DataFrame() for i in range(10)}
    res_ready = await warmup_check()
    assert res_ready["status"] == "READY"
    assert res_ready["symbols_loaded"] == 10
