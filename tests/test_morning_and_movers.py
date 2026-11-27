"""Unit tests for lightweight-mode movers hydration, morning scan wiring, and Binance spot mirror fallback."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd


def test_lightweight_movers_hydration_and_filtering():
    from api import service as svc
    from api.routers.research import get_nse_dashboard_snapshot

    orig_market = svc._market_data
    orig_persisted = list(svc._persisted_signals_snapshot)
    orig_movers = dict(svc._movers_cache)
    try:
        svc._market_data = {}
        svc._movers_cache = {"gainers": [], "losers": [], "updated_at": None}
        svc._persisted_signals_snapshot = [
            {"symbol": "RELIANCE", "close": 1300.0, "change_pct": 2.45, "volume": 1_500_000},
            {"symbol": "TCS", "close": 4100.0, "change_pct": -1.35, "volume": 800_000},
            {"symbol": "NIFTY 50", "close": 24100.0, "change_pct": 0.8},
            {"symbol": "BTC/USDT", "close": 95000.0, "change_pct": 4.2},
            {"symbol": "BADPRINT", "close": 100.0, "change_pct": 55.0},
        ]

        snap = asyncio.run(get_nse_dashboard_snapshot())
        movers = snap["movers"]
        gainer_syms = [r["symbol"] for r in movers["gainers"]]
        loser_syms = [r["symbol"] for r in movers["losers"]]

        assert gainer_syms == ["RELIANCE"]
        assert loser_syms == ["TCS"]
        assert movers["gainers"][0]["pct_change"] == 2.45
        assert movers["losers"][0]["pct_change"] == -1.35
    finally:
        svc._market_data = orig_market
        svc._persisted_signals_snapshot = orig_persisted
        svc._movers_cache = orig_movers


def test_autonomous_cycle_premarket_runs_morning_scan_and_preserves_empty_snapshot(monkeypatch):
    from scripts import autonomous_cycle

    state_writes: list[tuple[str, object]] = []
    morning_calls: list[bool] = []

    class DummyLockMgr:
        def __init__(self, *a, **kw):
            pass

        def acquire(self, **kw):
            return True

        def release(self, **kw):
            return True

    def fake_set_runtime_state(key, value):
        state_writes.append((key, value))
        return True

    def fake_get_runtime_state(key, default=None):
        return default

    def fake_morning_scan():
        morning_calls.append(True)
        return [{"symbol": "RELIANCE", "entry_price": 1300.0}]

    monkeypatch.setattr(autonomous_cycle, "init_db", lambda: None)
    monkeypatch.setattr(autonomous_cycle, "DistributedLockManager", DummyLockMgr)
    monkeypatch.setattr(autonomous_cycle, "set_runtime_state", fake_set_runtime_state)
    monkeypatch.setattr(autonomous_cycle, "get_runtime_state", fake_get_runtime_state)
    monkeypatch.setattr(autonomous_cycle, "record_autonomous_decision_to_db", lambda **kw: None)
    monkeypatch.setattr(autonomous_cycle, "is_nse_market_open", lambda _dt: (False, "Pre-market"))
    monkeypatch.setattr(autonomous_cycle, "resolve_universe", lambda *_a, **_kw: [])
    monkeypatch.setattr("agents.execution.market_control.is_crypto_forex_enabled", lambda: False)
    monkeypatch.setattr("agents.stock_screener.run_morning_scan_once", fake_morning_scan)

    res = asyncio.run(
        autonomous_cycle.run_single_autonomous_cycle(
            universe_arg="nse",
            run_id="test-premarket",
            dry_run=True,
            premarket_prep=True,
        )
    )

    assert len(morning_calls) == 1
    assert res["morning_scan"]["candidates"] == 1
    assert not any(k == "latest_market_snapshot" for k, _ in state_writes)


def test_binance_fetch_historical_routes_geo_blocked_to_spot_mirror():
    from agents.data_ingestion.binance_fetcher import fetch_historical
    from agents.data_ingestion.binance_http import (
        mark_host_geo_blocked,
        reset_binance_geo_cooldown,
    )

    sample_df = pd.DataFrame(
        [
            {
                "timestamp": datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc).replace(tzinfo=None),
                "open": 95000.0,
                "high": 95500.0,
                "low": 94800.0,
                "close": 95200.0,
                "volume": 120.0,
            }
        ]
    )

    reset_binance_geo_cooldown()
    mark_host_geo_blocked("https://api.binance.com", status_code=451)
    try:
        with patch(
            "agents.data_ingestion.binance_fetcher._fetch_binance_spot_mirror_klines",
            return_value=sample_df,
        ) as mock_mirror, patch(
            "agents.data_ingestion.binance_fetcher._init_exchange"
        ) as mock_ccxt:
            df = fetch_historical("BTC/USDT", "5m", since_days=1)

        assert mock_mirror.called
        assert not mock_ccxt.called
        assert len(df) == 1
        assert float(df["close"].iloc[0]) == 95200.0
    finally:
        reset_binance_geo_cooldown()
