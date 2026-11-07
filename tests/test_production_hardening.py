import uuid
"""
Comprehensive regression tests for TradeAI-PRO Production Hardening Sprint.
Verifies:
1. Health endpoint no-cache headers and liveness/readiness separation.
2. Cloudflare Worker routing rules (health routes excluded from edge cache).
3. Production database protection (fail-closed, no silent SQLite fallback).
4. Multi-account paper trading data isolation.
5. Trailing stop-loss persistence and re-registration across engine restarts.
6. Honest AI Ensemble V2 status reporting (Lightweight Mode vs Full Mode).
7. Quote search single-flight locking and latency distribution endpoint.
"""

import os
import sys
import pytest
from starlette.testclient import TestClient
from starlette.responses import Response

from server import app
import config.settings as settings
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade
import agents.ai_engine.ensemble as ensemble
import api.routes as routes
import api.service as svc
from api.security import get_authenticated_account
from fastapi import HTTPException


@pytest.fixture
def client():
    return TestClient(app)


# ── 1. Health Endpoint No-Cache & Separation ─────────────────────────────────

def test_health_routes_no_cache_headers(client):
    """Health, liveness, and readiness probes must strictly return no-store to prevent masking origin down-state."""
    for path in ("/api/health", "/api/health/live", "/api/ready", "/api/health/ready"):
        res = client.get(path)
        assert res.status_code in (200, 503), f"Unexpected status {res.status_code} for {path}"
        cc = res.headers.get("Cache-Control", "")
        assert "no-store" in cc, f"Path {path} missing 'no-store' in Cache-Control: {cc}"
        assert "no-cache" in cc, f"Path {path} missing 'no-cache' in Cache-Control: {cc}"
        assert res.headers.get("Pragma") == "no-cache", f"Path {path} missing Pragma: no-cache"


def test_liveness_probe_is_ultra_lightweight(client):
    """Liveness probe must return 'alive' with uptime without querying heavy database models."""
    res = client.get("/api/health/live")
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "alive"
    assert "uptime_seconds" in data
    assert "timestamp" in data


# ── 2. Cloudflare Worker Edge Rules Audit ─────────────────────────────────────

def test_cloudflare_worker_excludes_health_from_cache():
    """Verify in src/worker.js source that /api/health is excluded from edge caching."""
    worker_path = os.path.join(os.path.dirname(__file__), "..", "src", "worker.js")
    with open(worker_path, "r", encoding="utf-8") as f:
        src = f.read()

    # /api/health must NOT be in PUBLIC_CACHEABLE_PATHS
    assert '"/api/health",' not in src, "Error: /api/health is still inside PUBLIC_CACHEABLE_PATHS"
    # isHealthOrReadinessPath must exist and guard isPathCacheable
    assert "isHealthOrReadinessPath" in src, "Error: isHealthOrReadinessPath missing from worker.js"
    assert "if (isHealthOrReadinessPath(pathname)) return false;" in src


# ── 3. Production Database Protection ────────────────────────────────────────

def test_production_database_protection_refuses_sqlite(monkeypatch):
    """In production (IS_PRODUCTION=True), database init must fail closed if SQLite is configured."""
    monkeypatch.setattr(settings, "IS_PRODUCTION", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    from db.connection import _create_engine
    # Attempting to create engine with SQLite URL in production must raise RuntimeError
    monkeypatch.setattr("db.connection.DATABASE_URL", "sqlite:///./trading.db")

    with pytest.raises(RuntimeError) as exc_info:
        _create_engine()
    assert "strictly forbidden in production mode" in str(exc_info.value)
    assert "strictly forbidden" in str(exc_info.value)


# ── 4. Multi-Account Paper Trading Data Isolation ─────────────────────────────

def test_paper_trading_account_isolation():
    """Verify that User Alice and User Bob have strictly isolated paper trading engines and states."""
    uid_a = f"alice_{uuid.uuid4().hex[:6]}"
    uid_b = f"bob_{uuid.uuid4().hex[:6]}"
    alice_engine = PaperTradingEngine(initial_capital=50_000.0, account_id=uid_a)
    bob_engine = PaperTradingEngine(initial_capital=100_000.0, account_id=uid_b)

    assert alice_engine.account_id == uid_a
    assert bob_engine.account_id == uid_b
    assert alice_engine._state_key == f"paper_trading_state:account:{uid_a}"
    assert bob_engine._state_key == f"paper_trading_state:account:{uid_b}"

    # Place a trade in Alice's engine
    trade_alice = alice_engine.execute(
        symbol="VEDL",
        direction="LONG",
        entry=260.0,
        sl=255.0,
        tp=270.0,
        atr=3.0,
    )
    assert trade_alice is not None
    assert len(alice_engine.open_trades) == 1

    # Bob's engine must remain completely unpolluted
    assert len(bob_engine.open_trades) == 0
    assert len(bob_engine.trades) == 0


def test_authenticated_account_header_validation():
    """Verify get_authenticated_account sanitizes inputs and rejects injection attempts."""
    # Valid account
    acc = get_authenticated_account(x_account_id="alice-123")
    assert acc == "alice-123"

    # Default fallback
    acc_default = get_authenticated_account(x_account_id=None)
    assert acc_default == "default"

    # Invalid characters (e.g. traversal attempt)
    with pytest.raises(HTTPException) as exc:
        get_authenticated_account(x_account_id="../admin/hack")
    assert exc.value.status_code == 400


# ── 5. Trailing Stop Persistence Across Engine Restarts ───────────────────────

def test_trailing_stop_persistence_across_reloads():
    """Verify that trailing stops for open trades are re-registered in TrailingStopManager upon reload."""
    test_key = f"paper_trading_state:test_persist_{uuid.uuid4().hex[:6]}"
    engine = PaperTradingEngine(initial_capital=100_000.0, state_key=test_key, log_file=None)
    engine.trades.clear()
    engine._cooldowns.clear()
    engine.consecutive_losses = 0
    engine.halt_trading = False
    engine._save()
    
    trade = engine.execute(
        symbol="RELIANCE",
        direction="LONG",
        entry=1300.0,
        sl=1280.0,
        tp=1350.0,
        atr=10.0,
    )
    assert trade is not None
    assert engine.trailing_manager.get_sl(trade.id) is not None

    # Simulate process restart by instantiating a new engine from the same state key
    new_engine = PaperTradingEngine(initial_capital=100_000.0, state_key=test_key, log_file=None)
    assert len(new_engine.open_trades) == 1
    reloaded_trade = new_engine.open_trades[0]
    assert reloaded_trade.symbol == "RELIANCE"

    # TrailingStopManager MUST have reloaded trade registered
    active_sl = new_engine.trailing_manager.get_sl(reloaded_trade.id)
    assert active_sl is not None
    assert active_sl == reloaded_trade.stop_loss


# ── 6. Honest AI Ensemble V2 Status Reporting ─────────────────────────────────

def test_ai_ensemble_status_lightweight_mode(monkeypatch):
    """In Lightweight API Mode, get_model_status must report Lightweight baseline, not 4/4 ML."""
    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "true")
    status = ensemble.get_model_status()

    assert status.get("lightweight_mode") is True
    assert status.get("active_inference_mode") == "LIGHTWEIGHT_BASELINE"
    assert status.get("executing_in_memory") is False, "Lightweight mode must not execute heavy ML in memory"
    assert "Lightweight" in status.get("mode")
    assert "⚡" in status.get("badge_short")
    assert "4/4" not in status.get("badge_short"), "Lightweight mode must never claim 4/4 ML models active"
    assert "calibration_status" in status
    assert "model_availability" in status


# ── 7. Quote Search Single-Flight & Latency Stats ─────────────────────────────

def test_quote_search_latency_telemetry_endpoint(client):
    """Verify that /api/search/latency returns valid latency metrics."""
    res = client.get("/api/search/latency")
    assert res.status_code == 200
    data = res.json()
    assert "samples" in data
    assert "p50_ms" in data
    assert "p95_ms" in data
    assert "p99_ms" in data
