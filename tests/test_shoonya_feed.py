"""
Unit & Contract Tests for Dedicated Market Data Feed Service (Box C) & Shoonya Integration.
Tests TokenMaster, BarBuilder tick accumulation, fail-closed auth, feed_server endpoints,
DataFrame reconstruction, and failover honesty in Box A.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.feed_client import convert_feed_bars_to_df, fetch_feed_snapshot, is_feed_configured
from data.providers.shoonya_feed import ShoonyaFeedProvider
from feed.bar_builder import BarBuilder, bucket_5m, is_nse_market_hours, IST
from feed.token_master import TokenMaster
from feed_server import app


# ── 1. Token Master Tests ──────────────────────────────────────────────────

def test_token_master_normalization():
    tm = TokenMaster(cache_file=None)
    assert tm.normalize_symbol("reliance.ns") == "RELIANCE"
    assert tm.normalize_symbol("TCS-EQ") == "TCS"
    assert tm.normalize_symbol(" Nifty 50 ") == "NIFTY 50"


def test_token_master_static_fallbacks():
    tm = TokenMaster(cache_file=None)
    assert tm.get_token("NIFTY 50") == "26000"
    assert tm.get_token("NIFTY BANK") == "26009"
    assert tm.get_token("RELIANCE") == "2885"
    assert tm.get_token("INFY") == "1594"

    # Reverse lookup
    assert tm.get_symbol_from_token("NSE", "2885") == "RELIANCE"
    assert tm.get_symbol_from_token("NSE", "26000") == "NIFTY 50"


def test_token_master_custom_registration():
    tm = TokenMaster(cache_file=None)
    tm.register_token("NEWSTOCK", "99999", exchange="NSE")
    assert tm.get_token("NEWSTOCK") == "99999"
    assert tm.get_symbol_from_token("NSE", "99999") == "NEWSTOCK"


# ── 2. Bar Builder & Tick Accumulation Tests ───────────────────────────────

def test_bucket_5m_alignment():
    # 09:17:34 -> 09:15:00
    t1 = datetime(2026, 11, 23, 9, 17, 34, tzinfo=IST)
    b1 = bucket_5m(t1)
    assert b1.minute == 15
    assert b1.second == 0
    assert b1.microsecond == 0

    # 09:20:00 -> 09:20:00
    t2 = datetime(2026, 11, 23, 9, 20, 0, tzinfo=IST)
    assert bucket_5m(t2).minute == 20


def test_market_hours_evaluation():
    # Monday 09:20 IST -> Open
    monday_open = datetime(2026, 11, 23, 9, 20, tzinfo=IST)
    assert is_nse_market_hours(monday_open) is True

    # Monday 09:10 IST -> Pre-market (False for bar building)
    monday_pre = datetime(2026, 11, 23, 9, 10, tzinfo=IST)
    assert is_nse_market_hours(monday_pre) is False

    # Monday 15:35 IST -> Post-market (False)
    monday_post = datetime(2026, 11, 23, 15, 35, tzinfo=IST)
    assert is_nse_market_hours(monday_post) is False

    # Sunday 10:00 IST -> Weekend (False)
    sunday = datetime(2026, 11, 22, 10, 0, tzinfo=IST)
    assert is_nse_market_hours(sunday) is False


def test_bar_builder_tick_aggregation():
    bb = BarBuilder(enforce_market_hours=False)
    base_t = datetime(2026, 11, 23, 9, 15, 10, tzinfo=IST)

    # Tick 1: open candle
    bb.on_tick("RELIANCE", price=2500.0, volume=1000.0, prev_close=2480.0, tick_time=base_t)
    # Tick 2: new high
    bb.on_tick("RELIANCE", price=2515.0, volume=1200.0, tick_time=base_t + timedelta(seconds=20))
    # Tick 3: new low
    bb.on_tick("RELIANCE", price=2495.0, volume=1500.0, tick_time=base_t + timedelta(seconds=40))
    # Tick 4: close
    bb.on_tick("RELIANCE", price=2505.0, volume=2000.0, tick_time=base_t + timedelta(seconds=60))

    bars = bb.get_bars("RELIANCE")
    assert len(bars) == 1
    bar = bars[0]
    assert bar["o"] == 2500.0
    assert bar["h"] == 2515.0
    assert bar["l"] == 2495.0
    assert bar["c"] == 2505.0
    # First tick seeded volume 1000. Delta = (1200-1000) + (1500-1200) + (2000-1500) = 1000.0
    assert bar["v"] == 1000.0

    # Tick in next 5m bucket (09:20:05) -> opens second bar
    next_t = datetime(2026, 11, 23, 9, 20, 5, tzinfo=IST)
    bb.on_tick("RELIANCE", price=2510.0, volume=2500.0, tick_time=next_t)
    bars2 = bb.get_bars("RELIANCE")
    assert len(bars2) == 2
    assert bars2[1]["o"] == 2510.0


def test_bar_builder_snapshot():
    bb = BarBuilder(enforce_market_hours=False)
    t = datetime(2026, 11, 23, 9, 16, tzinfo=IST)
    bb.on_tick("RELIANCE", price=2500.0, volume=5000.0, prev_close=2480.0, tick_time=t)

    snap = bb.get_snapshot(["RELIANCE", "MISSING_SYM"])
    assert snap["source"] == "SHOONYA_LIVE"
    assert snap["data_delay_minutes"] == 0
    assert "RELIANCE" in snap["quotes"]
    assert snap["quotes"]["RELIANCE"]["lp"] == 2500.0
    assert "MISSING_SYM" in snap["stale_symbols"]
    assert len(snap["bars"]["RELIANCE"]) == 1


# ── 3. Feed Server Endpoint & Auth Tests ───────────────────────────────────

def test_feed_server_cheap_liveness():
    client = TestClient(app)
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_feed_server_health_telemetry():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "trading-feed"
    assert "memory_rss_mb" in data
    assert "symbols_tracked" in data
    assert "total_ticks" in data


def test_feed_server_snapshot_fail_closed_missing_env(monkeypatch):
    monkeypatch.delenv("FEED_INTERNAL_TOKEN", raising=False)
    client = TestClient(app)
    resp = client.get("/snapshot?symbols=RELIANCE")
    assert resp.status_code == 503
    assert "FEED_INTERNAL_TOKEN" in resp.json()["detail"]


def test_feed_server_snapshot_auth_forbidden(monkeypatch):
    monkeypatch.setenv("FEED_INTERNAL_TOKEN", "valid-secret-token")
    client = TestClient(app)
    resp = client.get("/snapshot?symbols=RELIANCE", headers={"X-Internal-Token": "wrong-token"})
    assert resp.status_code == 403
    assert "Invalid internal" in resp.json()["detail"]


def test_feed_server_snapshot_authorized_success(monkeypatch):
    monkeypatch.setenv("FEED_INTERNAL_TOKEN", "valid-secret-token")
    client = TestClient(app)
    resp = client.get("/snapshot?symbols=RELIANCE", headers={"X-Internal-Token": "valid-secret-token"})
    assert resp.status_code == 200
    data = resp.json()
    assert "source" in data
    assert "quotes" in data
    assert "bars" in data


# ── 4. Feed Client & DataFrame Reconstruction Tests ────────────────────────

def test_convert_feed_bars_to_df():
    sample_bars = [
        {"t": "2026-11-23T09:15:00+05:30", "o": 100.0, "h": 105.0, "l": 98.0, "c": 103.0, "v": 5000},
        {"t": "2026-11-23T09:20:00+05:30", "o": 103.0, "h": 107.0, "l": 102.0, "c": 106.0, "v": 6000},
    ]
    df = convert_feed_bars_to_df(sample_bars)
    assert df is not None
    assert len(df) == 2
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["open"].iloc[0] == 100.0
    assert df["close"].iloc[1] == 106.0
    assert isinstance(df.index, pd.DatetimeIndex)


def test_feed_client_unconfigured_returns_none(monkeypatch):
    monkeypatch.delenv("FEED_SERVICE_URL", raising=False)
    assert is_feed_configured() is False


# ── 5. ShoonyaFeedProvider Tests ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_shoonya_feed_provider_quote(monkeypatch):
    mock_snapshot = {
        "source": "SHOONYA_LIVE",
        "data_delay_minutes": 0,
        "as_of": "2026-11-23T09:25:00+05:30",
        "quotes": {
            "RELIANCE": {"lp": 2520.0, "prev_close": 2500.0, "volume": 120000.0, "open": 2505.0, "high": 2525.0, "low": 2500.0}
        },
        "bars": {
            "RELIANCE": [
                {"t": "2026-11-23T09:15:00+05:30", "o": 2505.0, "h": 2525.0, "l": 2500.0, "c": 2520.0, "v": 120000.0}
            ]
        },
        "stale_symbols": []
    }

    with patch("data.providers.shoonya_feed.fetch_feed_snapshot", new=AsyncMock(return_value=mock_snapshot)):
        provider = ShoonyaFeedProvider(feed_url="http://mock-feed.local")
        quote = await provider.get_quote("RELIANCE")

        assert quote is not None
        assert quote.symbol == "RELIANCE"
        assert quote.price == 2520.0
        assert quote.change_pct == 0.8
        assert quote.source == "SHOONYA_LIVE"
        assert quote.is_valid() is True

        # Batch ohlcv
        dfs = await provider.get_batch_ohlcv(["RELIANCE"], interval="5m")
        assert "RELIANCE" in dfs
        assert len(dfs["RELIANCE"]) == 1
        assert dfs["RELIANCE"]["close"].iloc[0] == 2520.0


def test_feed_server_oauth_callback(monkeypatch):
    monkeypatch.setenv("FEED_INTERNAL_TOKEN", "test-feed-token-2026")
    with TestClient(app) as client:
        # Missing code parameter
        r_fail = client.get("/oauth/callback")
        assert r_fail.status_code == 400

        # Successful exchange mock
        with patch("feed_server.feed_manager.exchange_auth_code", return_value="mock_susertoken_12345678"):
            with patch("feed_server.feed_manager.login_with_token", return_value=True):
                with patch("feed_server.feed_manager._start_websocket"):
                    r_ok = client.get("/oauth/callback?code=test_code_123")
                    assert r_ok.status_code == 200
                    data = r_ok.json()
                    assert data["status"] == "ok"
                    assert "mock_sus" in data["susertoken_prefix"]


def test_fetch_ohlcv_shoonya_box_c_priority(monkeypatch):
    """Option A: Verify fetch_ohlcv retrieves 5m rolling bars from Box C when configured."""
    from agents.market_intelligence.nse_fetcher import fetch_ohlcv, _cache
    from datetime import datetime, timedelta

    _cache.clear()
    base_dt = datetime(2026, 11, 23, 9, 15)
    mock_bars = [
        {
            "t": (base_dt + timedelta(minutes=i * 5)).isoformat(),
            "o": 2500.0 + i,
            "h": 2505.0 + i,
            "l": 2498.0 + i,
            "c": 2502.0 + i,
            "v": 10000.0,
        }
        for i in range(15)
    ]
    mock_snap = {
        "source": "SHOONYA_LIVE",
        "quotes": {"RELIANCE": {"lp": 2516.0}},
        "bars": {"RELIANCE": mock_bars},
    }

    monkeypatch.setenv("FEED_SERVICE_URL", "http://mock-feed.local")
    with patch("api.feed_client.fetch_feed_snapshot_sync", return_value=mock_snap):
        df = fetch_ohlcv("RELIANCE", period="1d", interval="5m")
        assert df is not None
        assert len(df) == 15
        assert df["close"].iloc[-1] == 2516.0  # Calibrated to live tick


def test_fetch_current_price_option_a_and_c_tiers(monkeypatch):
    """Verify fetch_current_price prioritizes Box C (Option A) then TradingView (Option C)."""
    from agents.market_intelligence.nse_fetcher import fetch_current_price
    from data.providers.base import MarketQuote

    # 1. Option A (Shoonya Box C) hit
    monkeypatch.setenv("FEED_SERVICE_URL", "http://mock-feed.local")
    mock_snap = {"quotes": {"RELIANCE": {"lp": 2555.0}}}
    with patch("api.feed_client.fetch_feed_snapshot_sync", return_value=mock_snap):
        p = fetch_current_price("RELIANCE")
        assert p == 2555.0

    # 2. Option C (TradingView Scanner) fallback when Box C has no quote
    monkeypatch.delenv("FEED_SERVICE_URL", raising=False)
    mock_tv_quote = MarketQuote(
        symbol="VEDL",
        price=236.40,
        open=234.0,
        high=238.0,
        low=233.0,
        close=236.40,
        volume=100000.0,
        source="TRADINGVIEW_LIVE",
        quality_score=1.0,
    )
    with patch("data.providers.tradingview.TradingViewProvider.get_quote_sync", return_value=mock_tv_quote):
        p_vedl = fetch_current_price("VEDL")
        assert p_vedl == 236.40


def test_format_human_data_age_realtime_badges():
    """Verify health data age formatting reflects real-time tags for Shoonya and TradingView."""
    from api.health_v2 import format_human_data_age
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(_IST)
    snap = now_ist - timedelta(seconds=120)

    # Shoonya live feed
    shoonya_badge = format_human_data_age(snap, age_seconds=120, is_nse_open=True, now_ist=now_ist, source="SHOONYA_LIVE")
    assert "⚡ Real-Time Feed (Shoonya)" in shoonya_badge
    assert "2m ago" in shoonya_badge

    # TradingView live feed
    tv_badge = format_human_data_age(snap, age_seconds=120, is_nse_open=True, now_ist=now_ist, source="TRADINGVIEW_LIVE")
    assert "⚡ Real-Time Feed (TradingView)" in tv_badge

    # Delayed public feed
    delayed_badge = format_human_data_age(snap, age_seconds=120, is_nse_open=True, now_ist=now_ist, source="YAHOO_DELAYED")
    assert "15m Delayed · Updated 2m ago" in delayed_badge




def test_fetch_feed_snapshot_rejects_simulated_by_default(monkeypatch):
    """Box A feed client must reject Box C SIMULATED snapshots unless allow_simulated=True."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from api import feed_client

    monkeypatch.setenv("FEED_SERVICE_URL", "http://mock-box-c.local")
    sim_payload = {
        "source": "SHOONYA_SIMULATED",
        "feed_state": "SIMULATED",
        "quotes": {"HDFCBANK": {"lp": 1734.39}},
        "bars": {},
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = sim_payload
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("api.feed_client._get_async_client", return_value=mock_client):
        blocked = asyncio.run(feed_client.fetch_feed_snapshot(["HDFCBANK"]))
        assert blocked is None
        allowed = asyncio.run(feed_client.fetch_feed_snapshot(["HDFCBANK"], allow_simulated=True))
        assert allowed is not None
        assert allowed["quotes"]["HDFCBANK"]["lp"] == 1734.39


def test_validate_live_tick_rejects_gt_20_pct_outlier():
    """Tick sanity gate blocks >20% single-session outlier moves and invalid ticks."""
    from api.feed_client import validate_live_tick

    assert validate_live_tick("HDFCBANK", 1734.39, prev_close=817.81) is False
    assert validate_live_tick("RELIANCE", 2486.73, prev_close=1350.94) is False
    assert validate_live_tick("HDFCBANK", 895.0, prev_close=817.81) is True
    assert validate_live_tick("INFY", float("nan"), prev_close=1103.38) is False
    assert validate_live_tick("INFY", -10.0, prev_close=1103.38) is False
