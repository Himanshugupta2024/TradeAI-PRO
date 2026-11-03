"""Unit tests for Intraday Live Candle Overlay, RVOL filter, Index Confluence & API routes."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from agents.data_ingestion.nse_live_quote import NSELiveQuote
from agents.intraday_engine.fetcher import merge_live_candle_overlay
from agents.intraday_engine.risk_manager import (
    is_entry_cutoff_active,
    should_square_off_now,
)
from agents.intraday_engine.scanner import (
    get_nifty_market_context,
    scan_intraday_setups,
)
from agents.intraday_engine.strategies import strat_orb
from agents.intraday_engine.types import IntradaySignal
from config import settings

_IST = ZoneInfo("Asia/Kolkata")


def _sample_df() -> pd.DataFrame:
    base = datetime.now(_IST).replace(hour=10, minute=0, second=0, microsecond=0)
    rows = []
    price = 100.0
    for i in range(25):
        ts = base + timedelta(minutes=5 * i)
        vol = 100000.0 if i >= 20 else 50000.0
        c = price + (0.8 if i >= 20 else 0.2)
        rows.append(
            {
                "timestamp": pd.Timestamp(ts),
                "open": price,
                "high": c + 0.3,
                "low": price - 0.3,
                "close": c,
                "volume": vol,
            }
        )
        price = c
    return pd.DataFrame(rows)


def test_merge_live_candle_overlay_appends_or_updates():
    df = _sample_df()
    initial_len = len(df)
    now_ist = datetime.now(_IST)

    quote = NSELiveQuote(
        symbol="RELIANCE",
        ltp=108.5,
        change_pct=2.5,
        open=100.0,
        high=109.0,
        low=99.5,
        previous_close=100.0,
        vwap=106.0,
        total_volume=2_000_000.0,
        delivery_pct=40.0,
        rvol=2.1,
        source="test",
        timestamp_ist=now_ist.isoformat(),
    )

    merged = merge_live_candle_overlay(df, quote)
    assert merged is not None
    assert len(merged) >= initial_len
    # Verify latest candle has live LTP
    assert float(merged.iloc[-1]["close"]) == 108.5
    # Verify metadata preservation on DataFrame attrs
    assert merged.attrs["rvol"] == 2.1
    assert merged.attrs["live_vwap"] == 106.0
    assert merged.attrs["live_ltp"] == 108.5


def test_rvol_gate_blocks_low_volume_signals(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "INTRADAY_RVOL_GATE_ENABLED", True)
    monkeypatch.setattr(settings, "INTRADAY_MIN_RVOL", 1.5)

    df = _sample_df()
    df.attrs["rvol"] = 1.1  # Below 1.5 threshold

    sig = strat_orb("RELIANCE", df)
    # Should be blocked due to RVOL < 1.5
    assert sig is None

    # Now set RVOL above 1.5
    df.attrs["rvol"] = 2.2
    sig2 = strat_orb("RELIANCE", df)
    assert sig2 is not None
    assert sig2.rvol == 2.2
    assert "RVOL=2.20x" in sig2.rationale


def test_index_confluence_blocks_divergent_trades(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "INTRADAY_INDEX_CONFLUENCE_GATE", True)
    monkeypatch.setattr(settings, "INTRADAY_LIVE_FEED_ENABLED", False)

    # 1. Bearish parent index (NIFTY is dumping below VWAP)
    bearish_nifty = {
        "symbol": "NIFTY 50",
        "ltp": 24000.0,
        "vwap": 24100.0,
        "change_pct": -0.6,
        "trend": "BEARISH",
        "source": "mock",
    }

    # Dummy strategy that generates a LONG breakout
    def _long_strat(sym: str, df: pd.DataFrame) -> IntradaySignal:
        return IntradaySignal(
            symbol=sym, strategy="ORB", direction="LONG", score=85.0,
            entry=100.0, stop_loss=95.0, target=110.0, qty=10,
        )

    with patch("agents.intraday_engine.scanner.get_nifty_market_context", return_value=bearish_nifty), \
         patch("agents.intraday_engine.scanner.fetch_5m", return_value=_sample_df()), \
         patch("agents.intraday_engine.scanner.STRATS", (_long_strat,)), \
         patch("agents.intraday_engine.scanner.should_square_off_now", return_value=False):
        
        # When NIFTY is BEARISH, LONG signal should be blocked
        signals = scan_intraday_setups(["RELIANCE"])
        assert len(signals) == 0

    # 2. Bullish parent index (NIFTY is rallying above VWAP)
    bullish_nifty = {
        "symbol": "NIFTY 50",
        "ltp": 24200.0,
        "vwap": 24100.0,
        "change_pct": 0.8,
        "trend": "BULLISH",
        "source": "mock",
    }

    with patch("agents.intraday_engine.scanner.get_nifty_market_context", return_value=bullish_nifty), \
         patch("agents.intraday_engine.scanner.fetch_5m", return_value=_sample_df()), \
         patch("agents.intraday_engine.scanner.STRATS", (_long_strat,)), \
         patch("agents.intraday_engine.scanner.should_square_off_now", return_value=False):
        
        # When NIFTY is BULLISH, LONG signal is approved
        signals = scan_intraday_setups(["RELIANCE"])
        assert len(signals) == 1
        assert signals[0]["direction"] == "LONG"


def test_ist_market_cutoffs():
    # 14:15 IST: entry allowed, no square-off
    t_1415 = datetime(2026, 11, 2, 14, 15, tzinfo=_IST)
    assert not is_entry_cutoff_active(t_1415)
    assert not should_square_off_now(t_1415)

    # 14:35 IST: entry cutoff active, no square-off yet
    t_1435 = datetime(2026, 11, 2, 14, 35, tzinfo=_IST)
    assert is_entry_cutoff_active(t_1435)
    assert not should_square_off_now(t_1435)

    # 15:16 IST: mandatory square-off window active
    t_1516 = datetime(2026, 11, 2, 15, 16, tzinfo=_IST)
    assert should_square_off_now(t_1516)


def test_api_intraday_live_quote_endpoint():
    from fastapi.testclient import TestClient
    from server import app

    fake_quote = NSELiveQuote(
        symbol="INFY",
        ltp=1850.5,
        change_pct=1.4,
        open=1830.0,
        high=1860.0,
        low=1825.0,
        previous_close=1825.0,
        vwap=1845.0,
        total_volume=800000.0,
        delivery_pct=52.0,
        rvol=1.9,
        source="direct_nse",
        timestamp_ist=datetime.now(_IST).isoformat(),
    )

    client = TestClient(app)
    with patch("agents.data_ingestion.nse_live_quote.get_nse_live_quote", return_value=fake_quote):
        res = client.get("/api/intraday/live-quote/INFY")
        assert res.status_code == 200
        data = res.json()
        assert data["symbol"] == "INFY"
        assert data["ltp"] == 1850.5
        assert data["vwap"] == 1845.0
        assert data["rvol"] == 1.9
