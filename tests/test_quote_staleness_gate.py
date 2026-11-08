"""
Automated Regression Tests for TradeAI PRO Quote Schema, Staleness Gate, and EOD Protection.

Acceptance Criteria:
  1. Delayed yFinance quote -> Intraday trade blocked (trade_eligible=False)
  2. Missing exchange timestamp -> Intraday trade blocked (fail-closed)
  3. Verified fresh quote (<= 60s) -> Intraday eligible
  4. Stale quote (> 60s) -> Intraday trade blocked
  5. Stale cached quote -> New trade blocked
  6. Completed EOD candle -> Swing eligible
  7. Incomplete forming daily bar during market hours -> Dropped from Swing scan
  8. Backward compatibility -> Legacy fields like timestamp_ist intact
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from agents.data_ingestion.nse_live_quote import (
    NSELiveQuote,
    _parse_exchange_timestamp,
    is_nse_market_open,
    validate_quote_for_trade,
)
from agents.market_intelligence.swing_scanner import (
    get_completed_eod_df,
    _last_bar_timestamp,
)

_IST = ZoneInfo("Asia/Kolkata")


def test_yfinance_quote_blocks_intraday():
    """Delayed yFinance quote must be flagged not trade-eligible for intraday."""
    now_ist = datetime.now(_IST)
    quote = NSELiveQuote(
        symbol="INFY",
        ltp=1850.0,
        change_pct=-0.4,
        open=1860.0,
        high=1870.0,
        low=1845.0,
        previous_close=1858.0,
        vwap=1855.0,
        total_volume=1_200_000.0,
        delivery_pct=None,
        rvol=1.05,
        source="DELAYED_YFINANCE",
        timestamp_ist=now_ist.isoformat(),
        data_timestamp=None,
        fetched_at=now_ist.isoformat(),
        freshness_seconds=None,
        is_realtime=False,
        trade_eligible=False,
        status_reason="DELAYED_YFINANCE_UNSAFE_INTRADAY",
    )

    allowed, reason = validate_quote_for_trade(quote, trade_type="INTRADAY")
    assert allowed is False
    assert reason in ("TIMESTAMP_UNKNOWN_FAIL_CLOSED", "DELAYED_FEED_INTRADAY_BLOCKED")
    assert quote.trade_eligible is False
    assert quote.is_realtime is False


def test_missing_timestamp_fails_closed():
    """Missing exchange timestamp must fail closed even if source claims LIVE."""
    now_ist = datetime.now(_IST)
    quote = NSELiveQuote(
        symbol="TCS",
        ltp=3900.0,
        change_pct=1.1,
        open=3880.0,
        high=3920.0,
        low=3875.0,
        previous_close=3858.0,
        vwap=3895.0,
        total_volume=800_000.0,
        delivery_pct=48.0,
        rvol=1.3,
        source="LIVE_NSE",
        timestamp_ist=now_ist.isoformat(),
        data_timestamp=None,  # Missing!
        fetched_at=now_ist.isoformat(),
        freshness_seconds=None,
        is_realtime=False,
        trade_eligible=False,
        status_reason="TIMESTAMP_UNKNOWN_FAIL_CLOSED",
    )

    allowed, reason = validate_quote_for_trade(quote, trade_type="INTRADAY")
    assert allowed is False
    assert reason == "TIMESTAMP_UNKNOWN_FAIL_CLOSED"


def test_verified_fresh_quote_eligible():
    """Verified fresh quote with exchange timestamp <= 60s is eligible for intraday."""
    now_ist = datetime.now(_IST)
    bar_time = now_ist - timedelta(seconds=15)

    quote = NSELiveQuote(
        symbol="RELIANCE",
        ltp=2500.0,
        change_pct=0.8,
        open=2480.0,
        high=2510.0,
        low=2475.0,
        previous_close=2480.0,
        vwap=2495.0,
        total_volume=2_500_000.0,
        delivery_pct=52.0,
        rvol=1.8,
        source="LIVE_NSE",
        timestamp_ist=now_ist.isoformat(),
        data_timestamp=bar_time.isoformat(),
        fetched_at=now_ist.isoformat(),
        freshness_seconds=15.0,
        is_realtime=True,
        trade_eligible=True,
        status_reason="VERIFIED_REALTIME",
    )

    allowed, reason = validate_quote_for_trade(quote, trade_type="INTRADAY")
    assert allowed is True
    assert reason == "ELIGIBLE_REALTIME"


def test_stale_quote_exceeding_threshold_blocks_intraday():
    """Quote with exchange timestamp > 60s must be blocked from intraday trade."""
    now_ist = datetime.now(_IST)
    bar_time = now_ist - timedelta(seconds=180)  # 3 minutes old

    quote = NSELiveQuote(
        symbol="HDFCBANK",
        ltp=1650.0,
        change_pct=-0.2,
        open=1655.0,
        high=1660.0,
        low=1645.0,
        previous_close=1653.0,
        vwap=1652.0,
        total_volume=4_000_000.0,
        delivery_pct=60.0,
        rvol=0.9,
        source="LIVE_NSE",
        timestamp_ist=now_ist.isoformat(),
        data_timestamp=bar_time.isoformat(),
        fetched_at=now_ist.isoformat(),
        freshness_seconds=180.0,
        is_realtime=False,
        trade_eligible=False,
        status_reason="STALE_DATA_180S",
    )

    allowed, reason = validate_quote_for_trade(quote, trade_type="INTRADAY")
    assert allowed is False
    assert "STALE_QUOTE_EXCEEDS_60S" in reason or "DELAYED" in reason


def test_stale_cache_fallback_blocks_new_trades():
    """Expired cache fallback must block new trade entries."""
    now_ist = datetime.now(_IST)
    quote = NSELiveQuote(
        symbol="SBIN",
        ltp=780.0,
        change_pct=0.5,
        open=775.0,
        high=785.0,
        low=770.0,
        previous_close=776.0,
        vwap=778.0,
        total_volume=5_000_000.0,
        delivery_pct=35.0,
        rvol=1.0,
        source="STALE_CACHE",
        timestamp_ist=now_ist.isoformat(),
        data_timestamp=None,
        fetched_at=now_ist.isoformat(),
        freshness_seconds=350.0,
        is_realtime=False,
        trade_eligible=False,
        status_reason="STALE_CACHE_FALLBACK_350S",
    )

    allowed, reason = validate_quote_for_trade(quote, trade_type="INTRADAY")
    assert allowed is False
    assert reason == "STALE_CACHE_EXPIRED"


def test_swing_eod_allows_completed_candle_with_yfinance():
    """Swing setups operate on completed daily EOD candles and accept yfinance."""
    now_ist = datetime.now(_IST)
    quote = NSELiveQuote(
        symbol="TATAMOTORS",
        ltp=920.0,
        change_pct=1.5,
        open=910.0,
        high=925.0,
        low=905.0,
        previous_close=906.0,
        vwap=915.0,
        total_volume=8_000_000.0,
        delivery_pct=None,
        rvol=1.2,
        source="DELAYED_YFINANCE",
        timestamp_ist=now_ist.isoformat(),
        data_timestamp=None,
        fetched_at=now_ist.isoformat(),
        freshness_seconds=None,
        is_realtime=False,
        trade_eligible=False,
        status_reason="DELAYED_YFINANCE_UNSAFE_INTRADAY",
    )

    allowed, reason = validate_quote_for_trade(quote, trade_type="SWING")
    assert allowed is True
    assert reason == "ELIGIBLE_EOD"


def test_forming_daily_candle_protection():
    """During market hours, swing scanner drops incomplete forming today bar."""
    now_ist = datetime.now(_IST)
    today = now_ist.date()

    # Create 35 daily bars ending with today's date
    dates = [today - timedelta(days=35 - i) for i in range(35)]
    dates[-1] = today  # Today's forming bar

    df = pd.DataFrame({
        "timestamp": [pd.Timestamp(d) for d in dates],
        "open": [100.0] * 35,
        "high": [105.0] * 35,
        "low": [98.0] * 35,
        "close": [102.0] * 35,
        "volume": [500_000.0] * 35,
    })

    # Case A: Market is open (e.g. 11:30 AM IST) -> forming bar must be dropped
    mock_1130am = datetime(today.year, today.month, today.day, 11, 30, tzinfo=_IST)
    with patch("agents.market_intelligence.swing_scanner.datetime") as mock_dt:
        mock_dt.now.return_value = mock_1130am
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

        completed_df = get_completed_eod_df(df)
        assert len(completed_df) == 34
        last_bar_date = _last_bar_timestamp(completed_df).date()
        assert last_bar_date < today

    # Case B: Market is closed (e.g. 17:00 IST) -> today's bar is completed and kept
    mock_5pm = datetime(today.year, today.month, today.day, 17, 0, tzinfo=_IST)
    with patch("agents.market_intelligence.swing_scanner.datetime") as mock_dt:
        mock_dt.now.return_value = mock_5pm
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

        completed_df = get_completed_eod_df(df)
        assert len(completed_df) == 35
        last_bar_date = _last_bar_timestamp(completed_df).date()
        assert last_bar_date == today


def test_backward_compatibility():
    """Existing code expecting timestamp_ist and standard dict must not break."""
    now_ist = datetime.now(_IST)
    quote = NSELiveQuote(
        symbol="WIPRO",
        ltp=540.0,
        change_pct=-0.1,
        open=542.0,
        high=545.0,
        low=538.0,
        previous_close=541.0,
        vwap=541.5,
        total_volume=1_000_000.0,
        delivery_pct=40.0,
        rvol=1.0,
        source="LIVE_NSE",
        timestamp_ist=now_ist.isoformat(),
    )

    d = quote.to_dict()
    assert d["symbol"] == "WIPRO"
    assert d["ltp"] == 540.0
    assert d["timestamp_ist"] == now_ist.isoformat()
    assert d["fetched_at"] == now_ist.isoformat()
    assert "data_timestamp" in d
    assert "freshness_seconds" in d
    assert "is_realtime" in d
    assert "trade_eligible" in d
    assert "status_reason" in d
