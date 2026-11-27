"""Offline unit tests for Priority 1 backend RAM optimization, caching, single-flight locks, and DB probe resilience."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest


@pytest.mark.asyncio
async def test_risk_gauge_single_flight_and_payload_cache(tmp_path, monkeypatch):
    """Concurrent cold /api/edge/risk-gauge calls coalesce to 1 fetch and repeated fresh calls reuse cached payload."""
    from agents.macro.risk_gauge import RiskGauge, RiskIndicators
    from api import service as svc
    from api.routers import research as res_mod

    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "true")
    monkeypatch.setattr(res_mod, "_RISK_GAUGE_REFRESH_TASK", None)
    res_mod._RISK_GAUGE_PAYLOAD_CACHE.clear()

    cache_path = tmp_path / "risk_gauge_cache.json"
    orig_init = RiskGauge.__init__

    def _patched_init(self, cache_file=str(cache_path), cache_ttl_seconds=300, fallback_enabled=True):
        orig_init(self, cache_file=str(cache_path), cache_ttl_seconds=cache_ttl_seconds, fallback_enabled=fallback_enabled)

    monkeypatch.setattr(RiskGauge, "__init__", _patched_init)

    sample_ind = RiskIndicators(
        vix=15.0,
        vix_change_pct=-1.0,
        vix_1d_change_pct=-0.5,
        btc_trend="RISING",
        nifty_trend="RISING",
        usdjpy_level=152.0,
        usdjpy_trend="FLAT",
        gold_trend="FLAT",
        risk_regime="RISK_ON",
        timestamp=datetime.now(timezone.utc),
        india_vix=14.2,
        india_vix_change_pct=-0.8,
        india_vix_7d_change_pct=-1.5,
        india_vix_trend="FALLING",
        ad_ratio=1.4,
        above_ema20_pct=62.0,
        breadth_regime="STRONG_BREADTH",
    )

    fetch_calls = 0

    def _mock_fetch(self):
        nonlocal fetch_calls
        fetch_calls += 1
        time.sleep(0.05)
        self._save_cache(sample_ind)
        return sample_ind

    monkeypatch.setattr(RiskGauge, "fetch_risk_indicators", _mock_fetch)

    gc_calls = 0
    orig_free = svc._force_free_memory

    def _spy_free():
        nonlocal gc_calls
        gc_calls += 1
        orig_free()

    monkeypatch.setattr(svc, "_force_free_memory", _spy_free)

    with patch("agents.execution.session_guard.is_nse_market_open", return_value=(False, "MARKET_CLOSED")):
        results = await asyncio.gather(
            res_mod.get_edge_risk_gauge(force_refresh=False),
            res_mod.get_edge_risk_gauge(force_refresh=False),
            res_mod.get_edge_risk_gauge(force_refresh=False),
        )
        assert len(results) == 3
        assert all("risk_score" in r for r in results)
        assert fetch_calls == 1, f"Expected single-flight coalescing to 1 fetch, got {fetch_calls}"
        assert gc_calls >= 1

        # Subsequent call should hit payload cache without re-running get_risk_breakdown
        breakdown_spy = MagicMock(side_effect=AssertionError("breakdown should not recompute on payload cache hit"))
        monkeypatch.setattr(RiskGauge, "get_risk_breakdown", breakdown_spy)
        cached_res = await res_mod.get_edge_risk_gauge(force_refresh=False)
        assert cached_res["risk_score"] == results[0]["risk_score"]


@pytest.mark.asyncio
async def test_prediction_engine_single_flight_bounded_cache_and_gc(monkeypatch):
    """Concurrent predict_stock_price calls coalesce to 1 Yahoo download, cache is bounded, and GC runs in lightweight mode."""
    from agents.ai_engine import prediction_engine as pe
    from data.providers.yahoo import YahooProvider

    pe._PREDICTION_CACHE.clear()
    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "true")
    monkeypatch.setattr(pe, "_PREDICTION_CACHE_MAX_ENTRIES", 2)

    n = 120
    dates = pd.date_range("2025-06-01", periods=n, freq="B")
    close = np.linspace(100.0, 130.0, n)
    mock_df = pd.DataFrame(
        {"open": close, "high": close + 1.5, "low": close - 1.5, "close": close, "volume": np.full(n, 2000.0)},
        index=dates,
    )
    mock_nifty = pd.DataFrame({"close": np.linspace(24000.0, 25000.0, n)}, index=dates)
    mock_vix = pd.DataFrame({"close": np.full(n, 14.5)}, index=dates)

    download_calls = 0

    async def _mock_batch_ohlcv(self, symbols, period="1y", interval="1d"):
        nonlocal download_calls
        download_calls += 1
        await asyncio.sleep(0.03)
        sym = symbols[0]
        return {sym: mock_df.copy(), "^NSEI": mock_nifty.copy(), "^INDIAVIX": mock_vix.copy()}

    monkeypatch.setattr(YahooProvider, "get_batch_ohlcv", _mock_batch_ohlcv)
    monkeypatch.setattr("agents.data_ingestion.nse_live_quote.is_nse_market_open", lambda: False)

    # Mock TradingViewProvider so test stays 100% offline
    from data.providers.tradingview import TradingViewProvider
    monkeypatch.setattr(TradingViewProvider, "get_quote", AsyncMock(return_value=None))

    gc_calls = 0
    orig_trim = pe._trim_memory_if_lightweight

    def _spy_trim():
        nonlocal gc_calls
        gc_calls += 1
        orig_trim()

    monkeypatch.setattr(pe, "_trim_memory_if_lightweight", _spy_trim)

    res_list = await asyncio.gather(
        pe.predict_stock_price("RELIANCE"),
        pe.predict_stock_price("RELIANCE"),
        pe.predict_stock_price("RELIANCE"),
    )
    assert len(res_list) == 3
    assert all(r["status"] == "PREDICTION_ACTIVE" for r in res_list)
    assert download_calls == 1, f"Expected single-flight coalescing to 1 download, got {download_calls}"
    assert gc_calls >= 1

    # Verify bounded LRU eviction when max entries = 2
    await pe.predict_stock_price("TCS")
    await pe.predict_stock_price("INFY")
    assert len(pe._PREDICTION_CACHE) == 2
    assert "RELIANCE" not in pe._PREDICTION_CACHE
    assert list(pe._PREDICTION_CACHE.keys()) == ["TCS", "INFY"]


@pytest.mark.asyncio
async def test_crypto_intelligence_cache_single_flight_and_error_skip(monkeypatch):
    """/api/crypto/intelligence deduplicates concurrent hits, caches valid responses, and skips caching errors."""
    from api import service as svc
    from api.routers import crypto as crypto_router

    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "true")
    monkeypatch.setattr("agents.execution.market_control.is_crypto_research_allowed", lambda: True)
    crypto_router._CRYPTO_INTEL_CACHE.update({"ts": 0.0, "payload": None})

    compute_calls = 0

    def _mock_compute():
        nonlocal compute_calls
        compute_calls += 1
        time.sleep(0.03)
        return {"top_setups": [], "avoid_list": [], "ranking_meta": {"actionable": 0}}

    monkeypatch.setattr(svc, "_compute_crypto_intelligence", _mock_compute)
    free_spy = MagicMock()
    monkeypatch.setattr(svc, "_force_free_memory", free_spy)

    out = await asyncio.gather(
        crypto_router.get_crypto_intelligence(force_refresh=False),
        crypto_router.get_crypto_intelligence(force_refresh=False),
        crypto_router.get_crypto_intelligence(force_refresh=False),
    )
    assert len(out) == 3
    assert compute_calls == 1
    assert free_spy.call_count >= 1

    # Repeated call hits TTL cache
    cached_out = await crypto_router.get_crypto_intelligence(force_refresh=False)
    assert cached_out == out[0]
    assert compute_calls == 1

    # Error payload must not poison cache
    crypto_router._CRYPTO_INTEL_CACHE.update({"ts": 0.0, "payload": None})
    monkeypatch.setattr(svc, "_compute_crypto_intelligence", lambda: {"error": "boom", "top_setups": []})
    err_out = await crypto_router.get_crypto_intelligence(force_refresh=False)
    assert err_out["error"] == "boom"
    assert crypto_router._CRYPTO_INTEL_CACHE["payload"] is None


@pytest.mark.asyncio
async def test_nse_prediction_cache_and_single_flight(monkeypatch):
    """/api/nse/prediction coalesces concurrent calls and caches by state fingerprint."""
    from api import service as svc
    from api.routers import market as market_router

    market_router._NSE_PRED_CACHE.update({"ts": 0.0, "payload": None, "fp": None})
    monkeypatch.setattr(svc, "_signal_cache", {})
    monkeypatch.setattr(svc, "_persisted_signals_snapshot", [])

    news_calls = 0

    async def _mock_news(limit=24):
        nonlocal news_calls
        news_calls += 1
        await asyncio.sleep(0.02)
        return {"items": [{"title": "Nifty rally"}], "sources_ok": ["stub"], "failed_sources": []}

    monkeypatch.setattr(market_router, "get_nse_news", _mock_news)
    monkeypatch.setattr(market_router, "get_market_mood_api", AsyncMock(return_value={"market_mood": "BULLISH"}))

    res = await asyncio.gather(
        market_router.get_nse_prediction(force_refresh=False),
        market_router.get_nse_prediction(force_refresh=False),
    )
    assert len(res) == 2
    assert news_calls == 1
    # Cache hit
    res2 = await market_router.get_nse_prediction(force_refresh=False)
    assert res2["calculated_at"] == res[0]["calculated_at"]
    assert news_calls == 1


@pytest.mark.asyncio
async def test_health_db_probe_retry_recovers_and_honest_failure(monkeypatch):
    """DB probe recovers from a transient failure/timeout on attempt 1, fails honestly when down, and never caches degraded DB state."""
    from api import service as svc
    from api.routers import health as health_router

    # 1. Transient False on attempt 1 -> True on attempt 2
    attempts = {"n": 0}

    def _flaky_then_ok():
        attempts["n"] += 1
        return attempts["n"] >= 2

    monkeypatch.setattr(health_router, "check_db_connectivity", _flaky_then_ok)
    assert await health_router._check_db_connectivity_with_retry() is True
    assert attempts["n"] == 2

    # 2. Persistent outage -> returns False honestly
    down_attempts = {"n": 0}

    def _always_down():
        down_attempts["n"] += 1
        return False

    monkeypatch.setattr(health_router, "check_db_connectivity", _always_down)
    assert await health_router._check_db_connectivity_with_retry() is False
    assert down_attempts["n"] == 2

    # 3. Exception on attempt 1 -> recovers on attempt 2
    exc_attempts = {"n": 0}

    def _exc_then_ok():
        exc_attempts["n"] += 1
        if exc_attempts["n"] == 1:
            raise RuntimeError("transient pool timeout")
        return True

    monkeypatch.setattr(health_router, "check_db_connectivity", _exc_then_ok)
    assert await health_router._check_db_connectivity_with_retry() is True

    # 4. Degraded DB response is NOT cached across /api/health calls so recovery is immediate
    monkeypatch.setattr(health_router, "_HEALTH_RESPONSE_CACHE", {})
    monkeypatch.setattr(health_router, "_HEALTH_RESPONSE_TS", 0.0)
    monkeypatch.setattr(svc, "_get_feed_status", lambda: {"state": "EOD", "market_snapshot_health": "EOD", "worker_health": "WORKER_OK"})
    monkeypatch.setattr("agents.ai_engine.ensemble.get_model_status", lambda: {"sidecar_active": False, "mode": "Heuristic"})
    monkeypatch.setattr(health_router, "_fetch_runtime_state_safe", AsyncMock(return_value=None))

    probe_state = {"ok": False}
    monkeypatch.setattr(health_router, "_check_db_connectivity_with_retry", AsyncMock(side_effect=lambda: probe_state["ok"]))

    degraded = await health_router.health_check()
    assert degraded["database"]["connected"] is False
    assert degraded["status"] == "degraded"
    assert health_router._HEALTH_RESPONSE_CACHE == {}

    probe_state["ok"] = True
    recovered = await health_router.health_check()
    assert recovered["database"]["connected"] is True
    assert health_router._HEALTH_RESPONSE_CACHE != {}
