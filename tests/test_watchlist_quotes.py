import pytest
from httpx import AsyncClient, ASGITransport
import pandas as pd
from server import app
from api import service as svc


@pytest.mark.asyncio
async def test_watchlist_quotes_data_honesty(monkeypatch):
    """
    Verify /api/watchlist/quotes enforces strict Data Honesty:
    1. HOLD/MONITOR symbols must return stop_loss=None and target=None (no fake 2% SL / 3.5% TP).
    2. Actionable LONG/SHORT symbols must return genuine calculated stop_loss and target.
    3. Confidence must not be a flat hardcoded 65.
    4. Snapshot fallback must accurately populate without synthetic multipliers.
    """
    hold_df = pd.DataFrame([
        {
            "timestamp": "2026-11-11T10:00:00",
            "open": 1000.0,
            "high": 1010.0,
            "low": 995.0,
            "close": 1005.0,
            "volume": 50000,
            "signal": "HOLD",
            "confidence": 42.0,
            "RSI_14": 42.5,
            "EMA_20": 1002.0,
            "EMA_50": 1000.0,
        }
    ])

    long_df = pd.DataFrame([
        {
            "timestamp": "2026-11-11T10:00:00",
            "open": 2000.0,
            "high": 2020.0,
            "low": 1990.0,
            "close": 2015.0,
            "volume": 80000,
            "signal": "LONG",
            "confidence": 78.0,
            "RSI_14": 62.0,
            "EMA_20": 2010.0,
            "EMA_50": 1995.0,
            "stop_loss": 1980.0,
            "target": 2085.0,
            "entry": 2015.0,
            "quality_score": 85,
        }
    ])

    short_df = pd.DataFrame([
        {
            "timestamp": "2026-11-11T10:00:00",
            "open": 500.0,
            "high": 505.0,
            "low": 485.0,
            "close": 490.0,
            "volume": 120000,
            "signal": "SHORT",
            "confidence": 72.0,
            "RSI_14": 38.0,
            "EMA_20": 495.0,
            "EMA_50": 502.0,
            "stop_loss": 500.0,
            "target": 470.0,
            "entry": 490.0,
            "quality_score": 80,
        }
    ])

    monkeypatch.setattr(svc, "_signal_cache", {
        "TESTHOLD": hold_df,
        "TESTLONG": long_df,
        "TESTSHORT": short_df,
    })

    # Also test snapshot recovery fallback
    monkeypatch.setattr(svc, "_persisted_signals_snapshot", [
        {
            "symbol": "TESTSNAP",
            "close": 1500.0,
            "entry": 1500.0,
            "signal": "HOLD",
            "confidence": 55,
            "rsi": 55.0,
            "stop_loss": None,
            "target": None,
            "levels_source": "UNAVAILABLE",
            "change_pct": 1.25,
            "source": "persisted_snapshot",
        }
    ])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/watchlist/quotes?symbols=TESTHOLD,TESTLONG,TESTSHORT,TESTSNAP")
        assert resp.status_code == 200
        data = resp.json()

        assert "quotes" in data
        assert data["count"] == 4

        # 1. HOLD symbol
        hold_q = data["quotes"]["TESTHOLD"]
        assert hold_q["symbol"] == "TESTHOLD"
        assert hold_q["signal"] == "HOLD"
        assert hold_q["ltp"] == 1005.0
        assert hold_q["stop_loss"] is None
        assert hold_q["target"] is None
        assert hold_q["levels_source"] == "UNAVAILABLE"
        assert hold_q["confidence"] == 42

        # 2. LONG symbol
        long_q = data["quotes"]["TESTLONG"]
        assert long_q["symbol"] == "TESTLONG"
        assert long_q["signal"] == "LONG"
        assert long_q["ltp"] == 2015.0
        assert long_q["stop_loss"] == 1980.0
        assert long_q["target"] == 2085.0
        assert long_q["confidence"] == 78

        # 3. SHORT symbol
        short_q = data["quotes"]["TESTSHORT"]
        assert short_q["symbol"] == "TESTSHORT"
        assert short_q["signal"] == "SHORT"
        assert short_q["ltp"] == 490.0
        assert short_q["stop_loss"] == 500.0
        assert short_q["target"] == 470.0
        assert short_q["confidence"] == 72

        # 4. SNAPSHOT recovery symbol
        snap_q = data["quotes"]["TESTSNAP"]
        assert snap_q["symbol"] == "TESTSNAP"
        assert snap_q["signal"] == "HOLD"
        assert snap_q["ltp"] == 1500.0
        assert snap_q["stop_loss"] is None
        assert snap_q["target"] is None
        assert snap_q["confidence"] == 55
