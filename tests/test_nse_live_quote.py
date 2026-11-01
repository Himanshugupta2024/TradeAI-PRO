"""Unit tests for NSE Live Quote Client (multi-tier fallback, caching, RVOL)."""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from agents.data_ingestion.nse_live_quote import (
    NSELiveQuote,
    NSELiveQuoteClient,
    _calculate_rvol,
    get_live_quote_client,
    get_nse_live_quote,
)

_IST = ZoneInfo("Asia/Kolkata")


def test_calculate_rvol_market_closed():
    # Outside market hours: full-day volume ratio
    now_evening = datetime(2026, 11, 2, 18, 0, tzinfo=_IST)
    with patch("agents.data_ingestion.nse_live_quote.datetime") as mock_dt:
        mock_dt.now.return_value = now_evening
        rvol = _calculate_rvol(current_volume=1_500_000, avg_daily_volume=1_000_000)
        assert rvol == 1.5


def test_calculate_rvol_intraday_halfway():
    # 09:15 to 15:30 is 375 mins. Halfway is ~12:22 (187.5 mins).
    # Expected volume is 50% of avg_daily_volume.
    now_noon = datetime(2026, 11, 2, 12, 22, 30, tzinfo=_IST)
    with patch("agents.data_ingestion.nse_live_quote.datetime") as mock_dt:
        mock_dt.now.return_value = now_noon
        # If current volume is 1,000,000 and avg is 1,000,000, rvol should be ~2.0
        rvol = _calculate_rvol(current_volume=1_000_000, avg_daily_volume=1_000_000)
        assert 1.95 <= rvol <= 2.05


def test_quote_client_caching():
    client = NSELiveQuoteClient(cache_ttl_seconds=10)
    fake_quote = NSELiveQuote(
        symbol="RELIANCE",
        ltp=2500.0,
        change_pct=1.2,
        open=2480.0,
        high=2510.0,
        low=2475.0,
        previous_close=2470.0,
        vwap=2495.0,
        total_volume=500000.0,
        delivery_pct=45.5,
        rvol=1.8,
        source="direct_nse",
        timestamp_ist=datetime.now(_IST).isoformat(),
    )

    # First call calls _fetch_direct_nse
    with patch.object(client, "_fetch_direct_nse", return_value=fake_quote) as mock_direct:
        q1 = client.get_quote("RELIANCE")
        assert q1 == fake_quote
        assert mock_direct.call_count == 1

        # Second call immediately within TTL must return cached quote without refetching
        q2 = client.get_quote("RELIANCE")
        assert q2 == fake_quote
        assert mock_direct.call_count == 1  # Not called again


def test_quote_client_fallback_to_yfinance():
    client = NSELiveQuoteClient(cache_ttl_seconds=10)
    fake_yf_quote = NSELiveQuote(
        symbol="TCS",
        ltp=3800.0,
        change_pct=-0.5,
        open=3820.0,
        high=3830.0,
        low=3790.0,
        previous_close=3819.0,
        vwap=3805.0,
        total_volume=300000.0,
        delivery_pct=None,
        rvol=1.1,
        source="yfinance",
        timestamp_ist=datetime.now(_IST).isoformat(),
    )

    # Direct NSE fails, nsepython fails -> falls back to yfinance
    with patch.object(client, "_fetch_direct_nse", return_value=None), \
         patch.object(client, "_fetch_nsepython", return_value=None), \
         patch.object(client, "_fetch_yfinance", return_value=fake_yf_quote) as mock_yf:
        quote = client.get_quote("TCS")
        assert quote is not None
        assert quote.symbol == "TCS"
        assert quote.source == "yfinance"
        assert mock_yf.call_count == 1


def test_quote_symbol_cleaning():
    client = NSELiveQuoteClient()
    assert client._clean_symbol("RELIANCE.NS") == "RELIANCE"
    assert client._clean_symbol("^NSEI") == "NSEI"
    assert client._get_yf_ticker("NIFTY 50") == "^NSEI"
    assert client._get_yf_ticker("RELIANCE") == "RELIANCE.NS"
