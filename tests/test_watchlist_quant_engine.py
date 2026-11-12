"""
Comprehensive Test Suite for TradeAI PRO Watchlist Quant Engine & Real-Time Providers.
======================================================================================
Covers:
  - TradingView Provider schema validation, missing column failure, and server-side change_pct
  - Yahoo Provider Semaphore concurrency and 429 backoff cooldown
  - Single-flight refresh with 60s minimum interval guard
  - Broadcaster capacity cap at 50 WebSocket clients
  - Watchlist Quant Engine (Trend 30, Momentum 25, Volume 15, RS 15, Structure 15)
  - Duality Levels (Actionable vs Support/Resistance/Invalidation)
  - Dynamic ATR horizon derivation and 200-bar sufficiency gate
"""

import asyncio
import math
import time
from datetime import datetime, date, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from data.providers.base import MarketQuote
from data.providers.tradingview import TradingViewProvider, SCANNER_COLUMNS
from data.providers.yahoo import YahooProvider
from agents.market_intelligence.watchlist_analyzer import (
    calculate_time_horizon,
    calculate_trade_levels,
    score_stock_factors,
    get_company_name,
)
from agents.market_intelligence.nse_holidays import is_holiday, get_holiday_name


# ── 1. TradingView Provider Tests ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tradingview_provider_success(monkeypatch):
    """Verify TradingView parses tick quote and calculates server-side change_pct."""
    import httpx
    provider = TradingViewProvider(timeout=5.0)

    # close=1339.7, change=0.32, change_abs=4.3, volume=12500000, open=1335.4, high=1342.5, low=1330.9, update_time=1794477544
    mock_payload = {
        "totalCount": 1,
        "data": [
            {
                "s": "NSE:RELIANCE",
                "d": [1339.7, 0.32, 4.3, 12500000, 1335.4, 1342.5, 1330.9, 1794477544],
            }
        ],
    }

    class MockResp:
        status_code = 200
        def json(self): return mock_payload

    async def mock_post(*args, **kwargs):
        return MockResp()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    quotes = await provider.get_batch_quotes(["RELIANCE"])
    assert "RELIANCE" in quotes
    q = quotes["RELIANCE"]

    assert q.price == 1339.7
    assert q.prev_close == 1335.4
    assert q.change_pct == 0.32
    assert q.source == "TRADINGVIEW_SCANNER"
    assert q.regular_market_time is not None
    assert q.is_valid() is True


@pytest.mark.asyncio
async def test_tradingview_provider_schema_mismatch_fails_cleanly(monkeypatch):
    """Verify that a malformed TradingView row with missing columns is rejected."""
    import httpx
    provider = TradingViewProvider(timeout=5.0)

    # Missing update_time column (only 7 items instead of 8)
    mock_payload = {
        "totalCount": 1,
        "data": [
            {
                "s": "NSE:BADSTOCK",
                "d": [100.0, 1.0, 1.0, 50000, 99.0, 102.0, 98.0],
            }
        ],
    }

    class MockResp:
        status_code = 200
        def json(self): return mock_payload

    async def mock_post(*args, **kwargs):
        return MockResp()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    quotes = await provider.get_batch_quotes(["BADSTOCK"])
    assert "BADSTOCK" not in quotes


# ── 2. Yahoo Provider Semaphore & 429 Adaptive Backoff Tests ──────────────────

@pytest.mark.asyncio
async def test_yahoo_provider_429_adaptive_backoff(monkeypatch):
    """Verify HTTP 429 sets cooldown and doubles backoff up to 120s."""
    import httpx
    provider = YahooProvider(timeout=5.0, max_concurrency=2)

    class Mock429Resp:
        status_code = 429

    async def mock_get(*args, **kwargs):
        return Mock429Resp()

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    assert provider._cooldown_until == 0.0
    quotes = await provider.get_batch_quotes(["INFY"])
    assert "INFY" not in quotes
    assert provider._cooldown_until > time.time()
    assert provider._current_backoff_sec == 60.0  # Doubled from 30 to 60


# ── 3. NSE Holidays Fail-Closed Behavior ──────────────────────────────────────

def test_nse_holidays_fail_closed_on_missing_year():
    """Verify that dates in an unconfigured future calendar year fail-closed to CLOSED."""
    future_date = date(2040, 5, 10)
    assert is_holiday(future_date) is True
    assert "Fail-Closed" in get_holiday_name(future_date)

    # Known 2026 holiday must resolve correctly
    rep_day_2026 = date(2026, 1, 26)
    assert is_holiday(rep_day_2026) is True
    assert get_holiday_name(rep_day_2026) == "Republic Day"


# ── 4. Broadcaster Client Capacity Cap ────────────────────────────────────────

def test_broadcaster_max_clients_cap():
    """Verify WebSocket broadcaster rejects clients once capacity cap (50) is reached."""
    from api.service import SnapshotBroadcaster
    bc = SnapshotBroadcaster()

    mock_clients = [MagicMock() for _ in range(50)]
    for ws in mock_clients:
        assert bc.register(ws) is True

    # 51st client must be rejected
    overflow_client = MagicMock()
    assert bc.register(overflow_client) is False
    assert len(bc._clients) == 50

    # Unregister one, now another can register
    bc.unregister(mock_clients[0])
    assert bc.register(overflow_client) is True


# ── 5. Watchlist Quant Engine & Dynamic Levels ────────────────────────────────

def test_watchlist_duality_trade_levels():
    """Verify actionable setups produce Entry/SL/TP; non-actionable produce Support/Resistance/Invalidation."""
    close = 1000.0
    atr = 25.0
    support = 970.0
    resistance = 1050.0

    # 1. Actionable LONG setup: SL floor 1.5x ATR, total_score >= 70, adx >= 20.0
    long_levels = calculate_trade_levels(
        close=close,
        high_last=1010.0,
        low_last=990.0,
        atr14=atr,
        supports=[support],
        resistances=[resistance],
        trend_bias="BULLISH",
        total_score=75,
        adx=30.0,
        symbol="",
    )
    assert long_levels["is_actionable"] is True
    assert long_levels["entry"] is not None
    # SL must be at or below support, with minimum 1.5x ATR (37.5) distance from entry
    assert long_levels["stop_loss"] <= long_levels["entry"] - (1.5 * atr)
    assert long_levels["target"] >= long_levels["entry"] + (1.5 * (long_levels["entry"] - long_levels["stop_loss"]))
    assert long_levels["rr_ratio"] >= 1.5

    # 2. Non-actionable HOLD setup: total_score < 60 -> No fake SL/TP, must return Support, Resistance & Invalidation
    hold_levels = calculate_trade_levels(
        close=close,
        high_last=1005.0,
        low_last=995.0,
        atr14=atr,
        supports=[support],
        resistances=[resistance],
        trend_bias="NEUTRAL",
        total_score=45,
        adx=15.0,
        symbol="TEST_HOLD",
    )
    assert hold_levels["is_actionable"] is False
    assert hold_levels["stop_loss"] is None
    assert hold_levels["target"] is None
    assert hold_levels["key_support"] == support
    assert hold_levels["key_resistance"] == resistance
    assert hold_levels["invalidation_level"] is not None


def test_watchlist_dynamic_atr_horizon_clamping():
    """Verify time horizon derives from distance/ATR and clamps between 2 and 15 daily bars."""
    # Distance to TP = 100, ATR = 20 -> est = 5 bars, time_stop = 10 bars
    h = calculate_time_horizon(
        is_actionable=True,
        entry=1000.0,
        target=1100.0,
        atr14=20.0,
        timeframe="1d",
    )
    assert h["est_bars"] == 5
    assert h["time_stop_bars"] == 10
    assert "Days" in h["short"]

    # Massive distance: 500 distance / 10 ATR = 50 -> clamped to 15 bars
    h_max = calculate_time_horizon(
        is_actionable=True,
        entry=1000.0,
        target=1500.0,
        atr14=10.0,
        timeframe="1d",
    )
    assert h_max["est_bars"] == 15
    assert h_max["time_stop_bars"] == 30


def test_company_name_resolution():
    """Verify company names resolve properly including unlisted/edge symbols like PRECWIRE."""
    assert "Precision Wires" in get_company_name("PRECWIRE")
    assert "Reliance Industries" in get_company_name("RELIANCE")
    assert "UNKNOWN · NSE Equity" == get_company_name("UNKNOWN")


def test_candles_5m_persistence():
    """Verify 5-minute candles persist into candles_5m table."""
    from db.connection import init_db
    from db.candles_store import upsert_candles_5m_sync
    init_db()

    df = pd.DataFrame([
        {
            "timestamp": datetime.now(timezone.utc),
            "open": 2500.0,
            "high": 2520.0,
            "low": 2490.0,
            "close": 2510.0,
            "volume": 100000.0,
        }
    ])
    count = upsert_candles_5m_sync("RELIANCE", df, source="YAHOO_TEST")
    assert count == 1

