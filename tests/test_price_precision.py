"""
Tests for price precision preservation, honest levels, and crypto/alias routing (P1-3).
"""

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import pytest
from starlette.testclient import TestClient

from api.data_honesty import price_decimals, apply_signal_freshness
from server import app

_IST = ZoneInfo("Asia/Kolkata")


def test_price_decimals_magnitudes():
    """1. price_decimals helper correctly maps magnitude to decimal precision."""
    assert price_decimals(0.0742) == 5
    assert price_decimals(1320.5) == 2
    assert price_decimals(0.0000123) == 7
    # Boundary tests
    assert price_decimals(0.0005) == 7
    assert price_decimals(0.005) == 6
    assert price_decimals(0.05) == 5
    assert price_decimals(0.5) == 4
    assert price_decimals(5.0) == 3
    assert price_decimals(50.0) == 2


def test_doge_signal_pairwise_precision():
    """2. DOGE-like signal: entry, stop_loss, target serialise with >=4 significant digits and pairwise distinct."""
    raw_signal = {
        "symbol": "DOGE/USDT",
        "signal": "LONG",
        "entry": 0.07418,
        "stop_loss": 0.07123,
        "target": 0.08055,
        "close": 0.07425,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actionable": True,
    }

    res = apply_signal_freshness(raw_signal)

    assert res["entry"] == 0.07418
    assert res["stop_loss"] == 0.07123
    assert res["target"] == 0.08055
    assert res["entry"] != res["stop_loss"]
    assert res["entry"] != res["target"]
    assert res["stop_loss"] != res["target"]
    assert res["levels_source"] == "ENGINE"


def test_degenerate_levels_nulling():
    """3. Degenerate levels -> levels_source == 'UNAVAILABLE', actionable is False, levels null."""
    degenerate_signal = {
        "symbol": "DOGE/USDT",
        "signal": "LONG",
        "entry": 0.07,
        "stop_loss": 0.07,
        "target": 0.07,
        "close": 0.07,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actionable": True,
    }

    res = apply_signal_freshness(degenerate_signal)

    assert res["stop_loss"] is None
    assert res["target"] is None
    assert res["levels_source"] == "UNAVAILABLE"
    assert res["actionable"] is False


def test_tatamotors_alias_hit(monkeypatch):
    """4. TATAMOTORS empty fetch + alias hit -> returns bars, not an error."""
    client = TestClient(app)

    now_ist = datetime.now(_IST)
    timestamps = [now_ist - timedelta(minutes=5 * i) for i in range(5, 0, -1)]
    fake_df = pd.DataFrame({
        "timestamp": timestamps,
        "open": [750.0, 751.0, 752.0, 753.0, 754.0],
        "high": [752.0, 753.0, 754.0, 755.0, 756.0],
        "low": [749.0, 750.0, 751.0, 752.0, 753.0],
        "close": [751.0, 752.0, 753.0, 754.0, 755.0],
        "volume": [1000, 1100, 1200, 1300, 1400],
    })

    # Monkeypatch fetchers
    def mock_fetch_ohlcv(symbol, period, interval):
        return pd.DataFrame()

    def mock_fetch_arbitrary(symbol, period, interval):
        # TATAMOTORS and TATAMOTORS.NS return empty, but alias TMPV returns data
        if symbol in ("TATAMOTORS", "TATAMOTORS.NS"):
            return pd.DataFrame()
        if symbol == "TMPV":
            return fake_df.copy()
        return pd.DataFrame()

    monkeypatch.setattr("api.routes.fetch_ohlcv", mock_fetch_ohlcv)
    monkeypatch.setattr("api.routes.fetch_arbitrary_symbol", mock_fetch_arbitrary)

    response = client.get("/api/market/TATAMOTORS?limit=5&interval=5m")
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    assert len(data["data"]) == 5
    assert data["symbol"] == "TATAMOTORS"


def test_unknown_symbol_404(monkeypatch):
    """5. /api/market unknown symbol -> 404 and body has error."""
    client = TestClient(app)

    monkeypatch.setattr("api.routes.fetch_ohlcv", lambda s, p, i: pd.DataFrame())
    monkeypatch.setattr("api.routes.fetch_arbitrary_symbol", lambda s, p, i: pd.DataFrame())

    response = client.get("/api/market/UNKNOWN_SYMBOL_9999?limit=5&interval=5m")
    assert response.status_code == 404
    body = response.json()
    # FastAPI detail envelope
    detail = body.get("detail", body)
    assert detail["error"] == "SYMBOL_NOT_FOUND"
    assert "UNKNOWN_SYMBOL_9999" in detail["symbol"]


def test_infy_precision_unchanged():
    """6. Existing NSE symbols (INFY) unchanged: 2 dp, same numbers as before."""
    infy_signal = {
        "symbol": "INFY",
        "signal": "LONG",
        "entry": 1850.55,
        "stop_loss": 1820.00,
        "target": 1910.00,
        "close": 1852.10,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actionable": True,
    }

    res = apply_signal_freshness(infy_signal)
    assert res["entry"] == 1850.55
    assert res["stop_loss"] == 1820.00
    assert res["target"] == 1910.00
    assert res["close"] == 1852.10
    assert price_decimals(res["entry"]) == 2


def test_crypto_routing_variants(monkeypatch):
    """Crypto routes match DOGE-USDT, DOGE/USDT, and BTCUSDT."""
    client = TestClient(app)

    now_ist = datetime.now(_IST)
    fake_df = pd.DataFrame({
        "timestamp": [now_ist],
        "open": [0.074],
        "high": [0.075],
        "low": [0.073],
        "close": [0.0742],
        "volume": [100000],
    })

    def mock_fetch_hist(symbol, interval, days):
        return fake_df.copy()

    monkeypatch.setattr("agents.data_ingestion.binance_fetcher.fetch_historical", mock_fetch_hist)

    r1 = client.get("/api/market/DOGE-USDT?limit=5&interval=5m")
    assert r1.status_code == 200
    assert r1.json()["symbol"] == "DOGE/USDT"

    r2 = client.get("/api/market/DOGE/USDT?limit=5&interval=5m")
    assert r2.status_code == 200
    assert r2.json()["symbol"] == "DOGE/USDT"

    r3 = client.get("/api/market/BTCUSDT?limit=5&interval=5m")
    assert r3.status_code == 200
    assert r3.json()["symbol"] == "BTC/USDT"
