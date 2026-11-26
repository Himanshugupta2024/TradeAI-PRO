"""Unit tests for market, crypto, and stock screener fixes."""

from __future__ import annotations

import asyncio
import builtins
import json
import sys
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from fastapi import Response, status as fastapi_status
from fastapi.testclient import TestClient

import api.service as svc
from api.app import create_app


@pytest.fixture
def isolated_screener_list(monkeypatch):
    """Ensure opportunity_stocks is empty and restored after each test."""
    import agents.stock_screener as ss
    import api.routers.market as market_mod

    orig = list(ss.opportunity_stocks)
    ss.opportunity_stocks[:] = []
    assert market_mod.opportunity_stocks is ss.opportunity_stocks
    try:
        yield ss.opportunity_stocks
    finally:
        ss.opportunity_stocks[:] = orig


def test_websocket_prices_uses_imported_fastapi_status(monkeypatch):
    """/ws/prices must import fastapi.status (no NameError on policy close paths)."""
    import api.routers.market as market_mod

    assert market_mod.status is fastapi_status

    app = create_app()
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/ws/prices", headers={"Origin": "https://evil.example"}
            ) as ws:
                ws.receive_json()

        monkeypatch.setattr(svc.broadcaster, "register", lambda _ws: False)
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/ws/prices", headers={"Origin": "https://tradeai-pro.pages.dev"}
            ) as ws:
                ws.receive_json()


@pytest.mark.asyncio
async def test_get_all_signals_reads_intraday_store_signals_and_freshness(monkeypatch):
    """DB fallback in /api/signals/all reads intraday_store `signals` and `freshness_status`."""
    from api.routers.market import get_all_signals
    from db import intraday_store

    now_iso = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(svc, "_signal_cache", {})
    monkeypatch.setattr(svc, "_persisted_signals_snapshot", [])
    monkeypatch.setattr(svc, "TOP_NSE_SYMBOLS", ["RELIANCE", "TCS"])
    monkeypatch.setattr("db.runtime_state.get_runtime_state", lambda _k: None)
    monkeypatch.setattr(
        svc,
        "enrich_signal_context_fields",
        lambda sig, cache_only=True: dict(sig),
    )
    monkeypatch.setattr(
        intraday_store,
        "load_latest_scan",
        lambda: {
            "freshness_status": "FRESH",
            "scan_time": now_iso,
            "signals": [
                {
                    "symbol": "RELIANCE.NS",
                    "signal": "LONG",
                    "entry": 1350.0,
                    "stop_loss": 1330.0,
                    "target": 1390.0,
                }
            ],
        },
    )

    out = await get_all_signals(Response())
    by_sym = {s["symbol"]: s for s in out["signals"]}
    assert "RELIANCE.NS" in by_sym or "RELIANCE" in by_sym
    row = by_sym.get("RELIANCE.NS") or by_sym["RELIANCE"]
    assert row["freshness_status"] == "FRESH"
    assert row["data_status"] == "FRESH"
    assert row["stale"] is False


@pytest.mark.asyncio
async def test_screener_opportunities_ranks_db_fallbacks_in_lightweight_mode(
    monkeypatch, isolated_screener_list
):
    """/api/screener/opportunities ranks top-5 across intraday + runtime_state without network/pandas."""
    from api.routers.market import get_screener_opportunities
    from db import intraday_store

    monkeypatch.setattr(
        intraday_store,
        "load_latest_scan",
        lambda: {
            "freshness_status": "FRESH",
            "signals": [
                {
                    "symbol": "INFY",
                    "direction": "LONG",
                    "strategy": "ORB",
                    "quant_score": 82.0,
                    "entry": 1600.0,
                    "target": 1650.0,
                    "stop_loss": 1575.0,
                    "rsi": 54.0,
                }
            ],
        },
    )

    runtime_payloads = {
        "latest_signals:v1": {
            "signals": [
                {
                    "symbol": "TCS",
                    "signal": "SHORT",
                    "quality_score": 78.0,
                    "entry": 3800.0,
                    "stop_loss": 3850.0,
                    "target": 3700.0,
                    "rsi": 68.0,
                },
                {
                    "symbol": "INFY",  # duplicate of intraday signal: must be deduplicated
                    "signal": "LONG",
                    "quality_score": 60.0,
                    "entry": 1600.0,
                },
            ]
        },
        "latest_market_snapshot": {
            "prices": {
                "NIFTY 50": {"close": 24000.0, "high": 24500.0, "low": 23800.0, "atr": 200.0, "rsi": 25.0, "regime": "TRENDING"},
                "SBIN": {"close": 800.0, "high": 805.0, "low": 798.0, "atr": 10.0, "rsi": 31.0, "regime": "RANGING"},
                "HDFCBANK": {"close": 1700.0, "high": 1715.0, "low": 1695.0, "atr": 18.0, "rsi": 72.0, "regime": "RANGING"},
                "LT": {"close": 3500.0, "high": 3620.0, "low": 3480.0, "atr": 70.0, "rsi": 56.0, "regime": "TRENDING"},
                "ITC": {"close": 450.0, "high": 451.0, "low": 449.5, "atr": 4.0, "rsi": 50.0, "regime": "RANGING"},
            }
        },
        "latest_swing_scan": {
            "setups": [
                {
                    "symbol": "HAL",
                    "direction": "LONG",
                    "strength": 88.0,
                    "entry_zone": 4200.0,
                    "stop_loss": 4050.0,
                    "target_1": 4550.0,
                    "pattern": "PULLBACK",
                    "rsi": 48.0,
                }
            ]
        },
    }
    monkeypatch.setattr(
        "db.runtime_state.get_runtime_state",
        lambda k: runtime_payloads.get(k),
    )

    out = await get_screener_opportunities()
    assert out["status"] == "ok"
    assert out["count"] == 5
    opps = out["opportunities"]
    assert len(opps) == 5

    syms = [o["symbol"] for o in opps]
    assert len(syms) == len(set(syms))
    assert "NIFTY 50" not in syms
    assert "ITC" not in syms
    assert syms[0] == "INFY"
    assert set(syms) == {"INFY", "TCS", "HAL", "SBIN", "HDFCBANK"} or "LT" in syms

    # Verify snapshot candidate fields (ratio/close/rsi) when only snapshot has entries
    monkeypatch.setattr(intraday_store, "load_latest_scan", lambda: None)
    monkeypatch.setattr(
        "db.runtime_state.get_runtime_state",
        lambda k: {"prices": runtime_payloads["latest_market_snapshot"]["prices"]}
        if k == "latest_market_snapshot"
        else None,
    )
    snap_out = await get_screener_opportunities()
    assert snap_out["status"] == "ok"
    assert snap_out["count"] == 3
    by_sym = {o["symbol"]: o for o in snap_out["opportunities"]}
    assert by_sym["SBIN"]["signal"] == "OVERSOLD_BUY"
    assert by_sym["HDFCBANK"]["signal"] == "OVERBOUGHT_SELL"
    assert by_sym["LT"]["signal"] == "BREAKOUT"
    for sym in ("SBIN", "HDFCBANK", "LT"):
        assert by_sym[sym]["close"] > 0
        assert by_sym[sym]["rsi"] is not None
        assert by_sym[sym]["ratio"] > 0


@pytest.mark.asyncio
async def test_screener_opportunities_empty_and_live_schema(monkeypatch, isolated_screener_list):
    """Preserve empty_scan_payload shape when empty and ok/count schema when opportunity_stocks populated."""
    from api.routers.market import get_screener_opportunities
    from db import intraday_store

    monkeypatch.setattr(intraday_store, "load_latest_scan", lambda: None)
    monkeypatch.setattr("db.runtime_state.get_runtime_state", lambda _k: None)

    empty_out = await get_screener_opportunities()
    assert empty_out["status"] == "empty"
    assert empty_out["opportunities"] == []
    assert empty_out["count"] == 0

    isolated_screener_list[:] = [{"symbol": "RELIANCE", "price": 1350.0, "signal": "BREAKOUT"}]
    live_out = await get_screener_opportunities()
    assert live_out["status"] == "ok"
    assert live_out["count"] == 1
    assert live_out["opportunities"][0]["symbol"] == "RELIANCE"


@pytest.mark.asyncio
async def test_predictions_never_import_transformers_and_score_lexically(monkeypatch):
    """Both /api/nse/prediction and /api/crypto/prediction use lightweight keyword scoring without transformers."""
    import api.routers.crypto as crypto_mod
    import api.routers.market as market_mod

    orig_import = builtins.__import__

    def _forbid_transformers(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "transformers" or name.startswith("transformers."):
            raise AssertionError("transformers must not be imported on Box A")
        return orig_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _forbid_transformers)
    monkeypatch.delitem(sys.modules, "transformers", raising=False)

    # 1. NSE prediction
    monkeypatch.setattr(svc, "_signal_cache", {})
    monkeypatch.setattr(
        market_mod,
        "get_nse_news",
        AsyncMock(
            return_value={
                "items": [
                    {"title": "Nifty rally and breakout on strong inflow"},
                    {"title": "IT stocks surge to record high after beats"},
                ],
                "sources_ok": ["stub"],
                "failed_sources": [],
            }
        ),
    )
    monkeypatch.setattr(
        market_mod,
        "get_market_mood_api",
        AsyncMock(return_value={"market_mood": "BULLISH"}),
    )

    nse_out = await market_mod.get_nse_prediction()
    assert nse_out["news"]["positive_hits"] == 2
    assert nse_out["news"]["sentiment_score"] > 0.5
    assert nse_out["prediction"]["inputs"]["external_news_sentiment"] > 0.5

    # 2. Crypto prediction
    monkeypatch.setattr(
        "agents.execution.market_control.is_crypto_forex_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        crypto_mod,
        "get_market_mood_api",
        AsyncMock(
            return_value={
                "crypto_mood": "RISK_ON",
                "active_event_risk": False,
                "avg_news_sentiment": 0.25,
            }
        ),
    )
    monkeypatch.setattr(
        crypto_mod,
        "get_crypto_news",
        AsyncMock(
            return_value={
                "items": [
                    {"title": "Bitcoin crash and selloff amid hack warning"},
                    {"title": "Exchange liquidation and lawsuit drop prices"},
                ],
                "sources_ok": ["stub"],
                "failed_sources": [],
            }
        ),
    )

    class _FakeResp:
        def __init__(self, payload):
            self._raw = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "ticker/24hr" in url:
            return _FakeResp(
                [
                    {"symbol": "BTCUSDT", "priceChangePercent": "2.5", "quoteVolume": "500000000"},
                    {"symbol": "ETHUSDT", "priceChangePercent": "1.5", "quoteVolume": "250000000"},
                ]
            )
        if "alternative.me" in url:
            return _FakeResp({"data": [{"value": "45"}]})
        if "klines" in url:
            return _FakeResp([[0, "100", "105", "99", str(100 + i * 0.5), "1000"] for i in range(40)])
        return _FakeResp([])

    monkeypatch.setattr(crypto_mod, "urlopen", _fake_urlopen)

    crypto_out = await crypto_mod.get_crypto_prediction(timeframe="1h")
    assert crypto_out["real_data"] is True
    assert crypto_out["prediction"]["inputs"]["external_news_sentiment"] < -0.5


@pytest.mark.asyncio
async def test_market_and_fno_handlers_offload_sync_db_and_compute(monkeypatch):
    """Async handlers in market.py must offload blocking DB/indicator calls via asyncio.to_thread."""
    import api.routers.market as market_mod

    loop_tid = threading.get_ident()
    observed: dict[str, list[int]] = {
        "mood": [],
        "indicators": [],
        "set_probe": [],
        "get_snap": [],
    }

    monkeypatch.setattr(
        svc,
        "_compute_market_mood",
        lambda: observed["mood"].append(threading.get_ident()) or {"mood": "NEUTRAL"},
    )
    mood_out = await market_mod.get_market_data("mood")
    assert mood_out["mood"] == "NEUTRAL"
    assert observed["mood"] == [x for x in observed["mood"] if x != loop_tid]
    assert len(observed["mood"]) == 1

    # Exercise Forex /api/market/{symbol} path to verify calculate_indicators & _build_market_response offload
    sample_df = pd.DataFrame(
        {
            "timestamp": [datetime.now(timezone.utc)],
            "open": [83.0],
            "high": [83.5],
            "low": [82.8],
            "close": [83.2],
            "volume": [1000],
        }
    )
    monkeypatch.setattr("agents.data_ingestion.forex_fetcher.get_forex_ohlcv", lambda *_a, **_k: sample_df.copy())
    monkeypatch.setattr(
        market_mod,
        "calculate_indicators",
        lambda df: observed["indicators"].append(threading.get_ident()) or df,
    )
    monkeypatch.setattr(market_mod, "_LAST_BAR_PROBE_WRITE", 0.0)
    monkeypatch.setattr(
        "db.runtime_state.set_runtime_state",
        lambda _k, _v: observed["set_probe"].append(threading.get_ident()),
    )

    fx_out = await market_mod.get_market_data("USD-INR", limit=10, interval="1h")
    assert fx_out["symbol"] == "USD/INR"
    assert observed["indicators"] and all(t != loop_tid for t in observed["indicators"])
    assert observed["set_probe"] and all(t != loop_tid for t in observed["set_probe"])

    # Exercise F&O endpoints offloading get_runtime_state("latest_market_snapshot")
    monkeypatch.setattr(
        "db.runtime_state.get_runtime_state",
        lambda _k: observed["get_snap"].append(threading.get_ident())
        or {"prices": {"RELIANCE": {"close": 1350.0, "atr": 25.0}}},
    )
    monkeypatch.setattr(
        "api.fno_client.fetch_fno_analytics",
        AsyncMock(return_value={"symbol": "RELIANCE", "spot_price": 1350.0}),
    )

    fno_sym = await market_mod.get_fno_symbol_analytics("RELIANCE")
    assert fno_sym["spot_price"] == 1350.0
    fno_ov = await market_mod.get_fno_overview()
    assert fno_ov["count"] > 0
    assert len(observed["get_snap"]) == 2
    assert all(t != loop_tid for t in observed["get_snap"])


def test_stock_screener_mutates_opportunity_stocks_in_place(monkeypatch):
    """scan_opportunities() must mutate opportunity_stocks[:] in place so importers see updates."""
    import agents.stock_screener as ss
    from agents.stock_screener import opportunity_stocks as imported_ref

    orig = list(ss.opportunity_stocks)
    try:
        ss.opportunity_stocks[:] = []
        monkeypatch.setattr(ss, "TOP_200_NSE", ["RELIANCE.NS"])

        dates = pd.date_range("2026-01-01", periods=25, freq="D")
        closes = [100.0] * 24 + [115.0]
        highs = [101.0] * 24 + [116.0]
        vols = [1000.0] * 24 + [5000.0]
        fake_df = pd.DataFrame(
            {"Close": closes, "High": highs, "Volume": vols}, index=dates
        )
        monkeypatch.setattr(ss.yf, "download", lambda *_a, **_k: fake_df)

        ss.scan_opportunities()

        assert imported_ref is ss.opportunity_stocks
        assert len(imported_ref) == 1
        assert imported_ref[0]["symbol"] == "RELIANCE"
    finally:
        ss.opportunity_stocks[:] = orig
