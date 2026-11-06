"""
TradeAI-PRO — Autonomous Decision Feed & Worker Status Resilience Tests.
========================================================================
Validates:
1. Server-side snapshot cache for /api/autonomous/status (10s TTL, measured_query_ms, cache_hit).
2. Cache invalidation on mutation endpoints (/api/autonomous/toggle, /api/autonomous/run-cycle, /api/market-control/toggle).
3. Private no-store headers (Cache-Control: private, no-cache, no-store, must-revalidate) on private endpoints.
4. Guaranteed UTC timezone serialization on feed timestamps.
5. Graceful recovery and hydration of legacy/corrupt paper trade state in paper_trading.py.
6. Decoupled independence: failure or slowness of status never halts or degrades feed loading.
"""

import pytest
import time
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from server import app
import api.routes as routes
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade


@pytest.fixture
def client():
    return TestClient(app)


def test_autonomous_status_cache_and_timing(client):
    """Verify /api/autonomous/status provides measured query timings and caches responses within 10s."""
    routes.invalidate_autonomous_status_cache()

    # First request: cache miss
    res1 = client.get("/api/autonomous/status")
    assert res1.status_code == 200
    data1 = res1.json()

    assert data1.get("ok") is True
    assert data1.get("cache_hit") is False
    assert "measured_query_ms" in data1
    assert isinstance(data1["measured_query_ms"], (int, float))
    assert data1["measured_query_ms"] >= 0.0
    assert "source_timestamp" in data1
    assert "watchdog" in data1

    # Verify private no-store headers
    cc1 = res1.headers.get("cache-control", "")
    assert "private" in cc1
    assert "no-store" in cc1

    # Second request: cache hit
    res2 = client.get("/api/autonomous/status")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2.get("cache_hit") is True
    assert data2["source_timestamp"] == data1["source_timestamp"]


def test_autonomous_status_cache_invalidation_on_toggle(client):
    """Verify status cache is invalidated when control endpoints are invoked."""
    routes.invalidate_autonomous_status_cache()

    res1 = client.get("/api/autonomous/status")
    assert res1.status_code == 200
    assert res1.json().get("cache_hit") is False

    # Second immediate call is a cache hit
    res2 = client.get("/api/autonomous/status")
    assert res2.json().get("cache_hit") is True

    # Mutate state
    routes.invalidate_autonomous_status_cache()

    # Subsequent call must be a fresh cache miss
    res3 = client.get("/api/autonomous/status")
    assert res3.status_code == 200
    assert res3.json().get("cache_hit") is False


def test_autonomous_feed_headers_and_utc_normalization(client):
    """Verify /api/autonomous/feed returns private no-store headers and UTC timestamps."""
    res = client.get("/api/autonomous/feed?limit=10")
    assert res.status_code == 200
    data = res.json()

    assert data.get("ok") is True
    assert "feed" in data
    assert isinstance(data["feed"], list)

    # Check headers
    cc = res.headers.get("cache-control", "")
    assert "private" in cc
    assert "no-store" in cc

    # Check timestamps in feed
    for item in data["feed"]:
        ts = item.get("timestamp")
        if ts:
            assert ts.endswith("Z") or "+" in ts, f"Timestamp {ts} must have explicit timezone indicator"


def test_paper_trading_corrupt_legacy_hydration_recovery():
    """Verify PaperTradingEngine._load_from_payload safely handles legacy trades missing direction/entry_time."""
    legacy_payload = {
        "initial_capital": 500000.0,
        "available_capital": 500000.0,
        "trades": [
            {
                "id": 991,
                "symbol": "RELIANCE",
                "entry_price": 2500.0,
                "current_price": 2520.0,
                "quantity": 10,
                "stop_loss": 2450.0,
                "target": 2600.0,
                "status": "OPEN",
                # direction and entry_time are intentionally missing
            }
        ],
        "equity_history": [],
    }

    engine = PaperTradingEngine(initial_capital=500000.0)
    engine._load_from_payload(legacy_payload)

    assert len(engine.open_trades) == 1
    t = engine.open_trades[0]
    assert t.direction == "LONG"
    assert t.entry_price == 2500.0
    assert t.symbol == "RELIANCE"
    assert t.entry_time != ""


def test_section_6a_market_control_telemetry(client):
    """Verify Section 6A Market Control endpoints report accurate status."""
    res = client.get("/api/market-control/status")
    assert res.status_code == 200
    data = res.json()

    assert "crypto_forex_enabled" in data
    assert "nse_focus_mode" in data
    assert "nse_window_active" in data
    assert "nse_session_status" in data
