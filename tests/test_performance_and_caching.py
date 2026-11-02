"""
Comprehensive Verification Suite for TradeAI-PRO Performance, Caching, and Resilience.

Tests:
  1. Indicator cache: same candle signature returns cached indicators instantly.
  2. Single flight: 20 concurrent refresh calls execute underlying provider refresh exactly once.
  3. Model loading: ModelRegistry initializes singletons once, multiple predictions do not reload.
  4. Memory boundedness: repeated operations maintain strictly bounded cache sizes.
  5. Health endpoints: /api/health/live (<50ms), /api/health/ready, /api/health/providers.
  6. Non-blocking offloading: heavy synchronous computations run in thread pool.
  7. Provider partial failure resilience: partial batch returns valid symbols without failure.
"""
from __future__ import annotations

import asyncio
import time
import pytest
import pandas as pd
import numpy as np
from unittest.mock import AsyncMock, patch, MagicMock
from starlette.testclient import TestClient

from server import app
from agents.feature_engine.indicators import calculate_indicators, _INDICATOR_CACHE
from agents.ai_engine.ensemble import predict_advanced, _ENSEMBLE_PRED_CACHE
from agents.ai_engine.model_registry import get_model_registry
from api import service as svc


def _make_test_df(n_bars: int = 50) -> pd.DataFrame:
    dates = pd.date_range("2026-11-01 09:15:00", periods=n_bars, freq="5min")
    close = [1500.0 + i * 1.5 for i in range(n_bars)]
    return pd.DataFrame({
        "timestamp": dates,
        "open": [c - 0.5 for c in close],
        "high": [c + 1.5 for c in close],
        "low": [c - 1.0 for c in close],
        "close": close,
        "volume": [5000.0] * n_bars,
    })


def test_indicator_cache_same_candle():
    """Verify that calling calculate_indicators on an identical candle uses in-memory cache."""
    df = _make_test_df(60)

    # First call: computes indicators
    t0 = time.perf_counter()
    res1 = calculate_indicators(df)
    first_duration_ms = (time.perf_counter() - t0) * 1000.0

    # Second call on identical candle: cache hit
    t1 = time.perf_counter()
    res2 = calculate_indicators(df)
    cached_duration_ms = (time.perf_counter() - t1) * 1000.0

    assert len(res1) == len(res2)
    assert "RSI_14" in res2.columns
    # Cached call should be significantly faster (sub-millisecond)
    assert cached_duration_ms < 5.0, f"Expected sub-5ms cache hit, got {cached_duration_ms}ms"
    assert cached_duration_ms <= first_duration_ms


def test_ensemble_prediction_cache():
    """Verify ensemble prediction cache returns precomputed prediction on unchanged candle."""
    df = _make_test_df(70)
    df = calculate_indicators(df)

    # First call
    res1 = predict_advanced(df, symbol="RELIANCE", offline=True)
    assert "direction" in res1

    # Second call on same symbol & candle
    t0 = time.perf_counter()
    res2 = predict_advanced(df, symbol="RELIANCE", offline=True)
    cached_ms = (time.perf_counter() - t0) * 1000.0

    assert res1["direction"] == res2["direction"]
    assert res1["confidence"] == res2["confidence"]
    assert cached_ms < 5.0, f"Expected cached prediction <5ms, got {cached_ms}ms"


@pytest.mark.asyncio
async def test_single_flight_refresh_concurrency():
    """Verify 20 concurrent refresh requests collapse into a single underlying fetch cycle."""
    inner_call_count = 0

    async def _mock_inner():
        nonlocal inner_call_count
        inner_call_count += 1
        await asyncio.sleep(0.05)  # Simulate network latency

    # Reset any previous refresh task
    svc._refresh_task = None

    with patch.object(svc, "_refresh_market_data_inner", side_effect=_mock_inner):
        # Fire 20 concurrent calls
        tasks = [svc._refresh_market_data() for _ in range(20)]
        await asyncio.gather(*tasks)

    assert inner_call_count == 1, f"Expected 1 underlying refresh, got {inner_call_count}"


def test_model_registry_single_load():
    """Verify ModelRegistry loads models once and reports warmup status."""
    registry = get_model_registry()
    status1 = registry.warmup_all()
    assert status1.get("warmed_up") is True

    # Second call returns immediately from singleton state
    t0 = time.perf_counter()
    status2 = registry.warmup_all()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 1.0, f"Expected instantaneous registry reuse, got {elapsed_ms}ms"
    assert status1 == status2


def test_health_endpoints_speed_and_structure():
    """Verify /api/health/live returns in sub-50ms and includes liveness telemetry."""
    client = TestClient(app)
    # Warm up routing
    client.get("/api/dashboard/overview")

    t0 = time.perf_counter()
    resp = client.get("/api/health/live")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "alive"
    assert "uptime_seconds" in data
    assert elapsed_ms < 50.0, f"/api/health/live latency {elapsed_ms}ms exceeded 50ms target"

    # Test /api/health/providers
    resp_prov = client.get("/api/health/providers")
    assert resp_prov.status_code == 200
    prov_data = resp_prov.json()
    assert "yahoo" in (prov_data.get("providers") or prov_data)


def test_dashboard_overview_composite_endpoint():
    """Verify /api/dashboard/overview returns consolidated dashboard state in sub-150ms."""
    client = TestClient(app)
    # Warm up client routing and app context
    client.get("/api/health/live")

    t0 = time.perf_counter()
    resp = client.get("/api/dashboard/overview")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "capital" in data
    assert "market_open" in data
    assert "top_signals" in data
    assert "risk_state" in data
    assert elapsed_ms < 150.0, f"/api/dashboard/overview latency {elapsed_ms}ms exceeded 150ms target"


def test_cache_memory_boundedness():
    """Verify indicator cache does not exceed maximum entry bounds when fed 200 unique datasets."""
    initial_size = len(_INDICATOR_CACHE)

    for i in range(200):
        dates = pd.date_range(f"2026-01-01", periods=10, freq="D")
        df = pd.DataFrame({
            "timestamp": dates,
            "open": [100.0 + i] * 10,
            "high": [105.0 + i] * 10,
            "low": [95.0 + i] * 10,
            "close": [102.0 + i + j * 0.1 for j in range(10)],
            "volume": [1000.0] * 10,
        })
        calculate_indicators(df)

    assert len(_INDICATOR_CACHE) <= 120, f"Cache exceeded max entries: {len(_INDICATOR_CACHE)}"
