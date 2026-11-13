"""
Unit tests for TradingView quote tier and INTRADAY_SIGNAL freshness policy.
Verifies Fix 1a and Fix 1b.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock

from agents.data_ingestion.nse_live_quote import (
    NSELiveQuote,
    validate_quote_for_trade,
    get_live_quote_client,
)
from data.providers.base import MarketQuote

_IST = ZoneInfo("Asia/Kolkata")


def test_intraday_signal_policy_fresh_realtime():
    """Realtime quote must be eligible as ELIGIBLE_REALTIME."""
    now_ist = datetime.now(_IST).isoformat()
    quote = NSELiveQuote(
        symbol="RELIANCE",
        ltp=2500.0,
        change_pct=1.2,
        open=2480.0,
        high=2510.0,
        low=2475.0,
        previous_close=2470.0,
        vwap=2495.0,
        total_volume=1000000.0,
        delivery_pct=None,
        rvol=1.5,
        source="LIVE_TV",
        timestamp_ist=now_ist,
        data_timestamp=now_ist,
        freshness_seconds=12.0,
        is_realtime=True,
        trade_eligible=True,
    )
    eligible, reason = validate_quote_for_trade(quote, trade_type="INTRADAY_SIGNAL")
    assert eligible is True
    assert reason == "ELIGIBLE_REALTIME"


def test_intraday_signal_policy_delayed_under_20m():
    """15m delayed quote must be eligible for signal generation as ELIGIBLE_DELAYED_15M."""
    now_ist = datetime.now(_IST).isoformat()
    quote = NSELiveQuote(
        symbol="TCS",
        ltp=3500.0,
        change_pct=-0.5,
        open=3520.0,
        high=3530.0,
        low=3490.0,
        previous_close=3517.0,
        vwap=3510.0,
        total_volume=500000.0,
        delivery_pct=None,
        rvol=0.9,
        source="DELAYED_YFINANCE",
        timestamp_ist=now_ist,
        data_timestamp=now_ist,
        freshness_seconds=900.0,  # 15m old
        is_realtime=False,
        trade_eligible=False,
    )
    eligible, reason = validate_quote_for_trade(quote, trade_type="INTRADAY_SIGNAL")
    assert eligible is True
    assert reason == "ELIGIBLE_DELAYED_15M"


def test_intraday_signal_policy_stale_over_20m():
    """Quotes older than 20 minutes (1200s) must be rejected to prevent extreme staleness."""
    now_ist = datetime.now(_IST).isoformat()
    quote = NSELiveQuote(
        symbol="INFY",
        ltp=1500.0,
        change_pct=0.2,
        open=1495.0,
        high=1505.0,
        low=1490.0,
        previous_close=1497.0,
        vwap=1498.0,
        total_volume=300000.0,
        delivery_pct=None,
        rvol=0.7,
        source="DELAYED_YFINANCE",
        timestamp_ist=now_ist,
        data_timestamp=now_ist,
        freshness_seconds=1500.0,  # 25m old > 20m
        is_realtime=False,
        trade_eligible=False,
    )
    eligible, reason = validate_quote_for_trade(quote, trade_type="INTRADAY_SIGNAL")
    assert eligible is False
    assert reason == "DELAYED_FEED_TOO_STALE_20M"


def test_tradingview_quote_tier_returns_live_quote():
    """Verify _fetch_tradingview converts TradingView MarketQuote into LIVE_TV NSELiveQuote."""
    client = get_live_quote_client()
    now_utc = datetime.now(timezone.utc)

    mock_mq = MarketQuote(
        symbol="RELIANCE",
        price=1326.65,
        open=1320.0,
        high=1335.0,
        low=1318.0,
        close=1326.65,
        volume=5000000.0,
        prev_close=1310.0,
        change_pct=1.27,
        source="tradingview",
        regular_market_time=now_utc,
    )

    with patch("data.providers.tradingview.TradingViewProvider.get_batch_quotes") as mock_batch:
        async def mock_get_batch(symbols):
            return {"RELIANCE": mock_mq}
        mock_batch.side_effect = mock_get_batch

        quote = client._fetch_tradingview("RELIANCE", avg_volume=4000000.0)

        assert quote is not None
        assert quote.symbol == "RELIANCE"
        assert quote.ltp == 1326.65
        assert quote.source == "LIVE_TV"
        assert quote.is_realtime is True
        assert quote.trade_eligible is True
        assert quote.status_reason == "LIVE_TV_SCANNER"
        assert quote.rvol > 0
