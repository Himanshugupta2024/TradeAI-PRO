import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta
from fastapi import Response
from api.routers.market import get_all_signals, _get_major_indices_sync
import api.service as svc
from api.data_honesty import IST

@pytest.mark.asyncio
async def test_get_all_signals_merges_partial_live_with_persisted_snapshot(monkeypatch):
    """When _signal_cache only has 1 symbol (e.g. SENSEX from an ad-hoc quote),

    get_all_signals must not hide the full persisted equity universe.
    """
    # 1. Setup persisted snapshot with equities
    persisted_equities = [
        {"symbol": "RELIANCE", "close": 1330.0, "signal": "BUY", "timestamp": "2026-11-11T12:00:00"},
        {"symbol": "TCS", "close": 4100.0, "signal": "HOLD", "timestamp": "2026-11-11T12:00:00"},
        {"symbol": "INFY", "close": 1850.0, "signal": "BUY", "timestamp": "2026-11-11T12:00:00"},
    ]
    monkeypatch.setattr(svc, "_persisted_signals_snapshot", persisted_equities)

    # 2. Setup _signal_cache with only SENSEX (1 symbol)
    sensex_df = pd.DataFrame([
        {"timestamp": datetime.now(timezone.utc), "open": 81000.0, "high": 81200.0, "low": 80900.0, "close": 81100.0, "volume": 1000}
    ])
    monkeypatch.setattr(svc, "_signal_cache", {"SENSEX": sensex_df})

    # Mock _get_signal_for_symbol for SENSEX
    def mock_get_signal(symbol):
        if symbol == "SENSEX":
            return {"symbol": "SENSEX", "close": 81100.0, "signal": "HOLD", "timestamp": "2026-11-11T15:00:00"}
        return None
    monkeypatch.setattr(svc, "_get_signal_for_symbol", mock_get_signal)

    # 3. Call endpoint
    res = Response()
    payload = await get_all_signals(res)
    assert "signals" in payload
    returned_symbols = [s["symbol"] for s in payload["signals"]]

    # Both SENSEX and the 3 equities must be present!
    assert "SENSEX" in returned_symbols
    assert "RELIANCE" in returned_symbols
    assert "TCS" in returned_symbols
    assert "INFY" in returned_symbols
    assert len(payload["signals"]) == 4
    assert payload["source"] == "merged_snapshot_live"


def test_get_major_indices_sync_ignores_stale_in_memory_bar(monkeypatch):
    """If in-memory DataFrame is older than 30m during market hours,

    _get_major_indices_sync should not use the stale bar.
    """
    stale_time = datetime.now(IST) - timedelta(minutes=45)
    stale_df = pd.DataFrame([
        {"timestamp": stale_time - timedelta(minutes=5), "open": 82000.0, "close": 82050.0},
        {"timestamp": stale_time, "open": 82050.0, "close": 82142.06},
    ])
    monkeypatch.setattr(svc, "_market_data", {"SENSEX": stale_df})
    monkeypatch.setattr(svc, "_signal_cache", {})
    # Clear fallback cache
    from api.routers import market as m_mod
    monkeypatch.setattr(m_mod, "_INDICES_FALLBACK_CACHE", {})
    monkeypatch.setattr(m_mod, "_INDICES_FALLBACK_TS", 0.0)

    # Mock session guard to simulate market open
    monkeypatch.setattr("agents.execution.session_guard.is_nse_market_open", lambda now: (True, "Live trading"))

    # When fallback yfinance runs, return fresh price
    class MockTicker:
        def history(self, *args, **kwargs):
            return pd.DataFrame([{"Open": 80900.0, "Close": 80999.40}], index=[datetime.now(IST)])
    class MockTickers:
        tickers = {"^BSESN": MockTicker()}
    monkeypatch.setattr("yfinance.Tickers", lambda syms: MockTickers())

    res = _get_major_indices_sync()
    assert res["sensex"][0] == 80999.40
