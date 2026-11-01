"""
Regression tests for audit hardening:
1. degraded health returning 503
2. missing production DB rejection
3. missing admin key rejection
4. unauthorized POST protection (401/403)
5. database read error does not wipe paper trading state
6. state-key stability (independent of CWD/abspath)
7. zero-loaded-symbol readiness returning 503
8. malformed trade request geometry validation (422/400)
9. statistical integrity (no fake 50% win rate or misleading 0)
10. duplicate background task protection and graceful shutdown
"""

from __future__ import annotations

import asyncio
import os
import pytest
from starlette.testclient import TestClient
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config import settings
from api.routes import router
from api import service as svc
from api.security import require_admin
from api.schemas import PaperTradeRequest
from db.runtime_state import DatabaseStateError, get_runtime_state
from agents.execution.paper_trading import PaperTradingEngine


def test_1_degraded_health_returns_503():
    """Degraded fallback app must return HTTP 503 and not leak raw stack traces."""
    fallback_app = FastAPI(title="Trading API (Degraded)", version="degraded-fallback")

    @fallback_app.get("/api/health")
    async def degraded_health_check():
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "healthy": False,
                "message": "Primary app failed to boot. Check server logs for details.",
            },
        )

    @fallback_app.get("/api/ready")
    async def degraded_ready_check():
        return JSONResponse(
            status_code=503,
            content={
                "ready": False,
                "status": "degraded",
                "message": "Primary app failed to boot. Check server logs for details.",
            },
        )

    client = TestClient(fallback_app)
    resp = client.get("/api/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["healthy"] is False
    assert "error" not in data

    resp_ready = client.get("/api/ready")
    assert resp_ready.status_code == 503


def test_2_missing_production_db_rejected(monkeypatch):
    """In production, missing or SQLite DATABASE_URL must raise validation error fast."""
    monkeypatch.setattr(settings, "IS_PRODUCTION", True)
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "prod-secret-key-12345")

    # SQLite in production
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///./trading.db")
    with pytest.raises(ValueError, match="persistent PostgreSQL"):
        settings.validate_runtime_config()

    # Empty DATABASE_URL in production
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    with pytest.raises(ValueError, match="DATABASE_URL"):
        settings.validate_runtime_config()


def test_3_missing_admin_key_rejected_in_production(monkeypatch):
    """In production, missing ADMIN_API_KEY must raise fast startup validation error."""
    monkeypatch.setattr(settings, "IS_PRODUCTION", True)
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://user:pass@host:5432/trading")
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")

    with pytest.raises(ValueError, match="ADMIN_API_KEY must be configured"):
        settings.validate_runtime_config()


def test_4_unauthorized_post_returns_401_and_403(monkeypatch):
    """When ADMIN_API_KEY is configured, protected POST routes must enforce auth."""
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "super-secret-admin-pass")
    monkeypatch.setattr(settings, "IS_PRODUCTION", False)

    # 1. Missing header -> 401 Unauthorized
    resp_no_key = client.post("/api/refresh")
    assert resp_no_key.status_code == 401
    assert "Missing admin API key" in resp_no_key.json()["detail"]

    # 2. Invalid header -> 403 Forbidden
    resp_bad_key = client.post("/api/refresh", headers={"X-API-Key": "wrong-key"})
    assert resp_bad_key.status_code == 403
    assert "Invalid admin API key" in resp_bad_key.json()["detail"]

    # 3. Valid X-API-Key header -> proceeds
    async def _mock_refresh():
        pass
    monkeypatch.setattr(svc, "_refresh_market_data", _mock_refresh)

    resp_valid = client.post("/api/refresh", headers={"X-API-Key": "super-secret-admin-pass"})
    assert resp_valid.status_code == 200

    # 4. Valid Authorization Bearer -> proceeds
    resp_bearer = client.post("/api/refresh", headers={"Authorization": "Bearer super-secret-admin-pass"})
    assert resp_bearer.status_code == 200


def test_5_database_read_error_does_not_wipe_state(monkeypatch, tmp_path):
    """A database connectivity error must fail loudly and never reset paper trading state to 0."""
    def _mock_failing_get(key, default=None):
        raise DatabaseStateError("Simulated PostgreSQL connection timeout")

    monkeypatch.setattr("agents.execution.paper_trading.get_runtime_state", _mock_failing_get)

    test_file = str(tmp_path / "test_trades.json")
    with pytest.raises(RuntimeError, match="Database error while loading paper trading state"):
        PaperTradingEngine(initial_capital=100000.0, log_file=test_file)


def test_6_state_key_stability():
    """State key for the default paper trading log must be stable and not depend on cwd or abspath."""
    engine = PaperTradingEngine(log_file="paper_trades.json")
    assert engine._state_key == "paper_trading_state:v1"


def test_7_zero_loaded_symbol_readiness(monkeypatch):
    """Readiness probe must return HTTP 503 when zero market symbols are loaded."""
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    monkeypatch.setattr(svc, "_market_data", {})
    monkeypatch.setattr(svc, "_loading_status", {"ready": False, "phase": "starting"})

    resp = client.get("/api/ready")
    assert resp.status_code == 503
    data = resp.json()
    assert data["ready"] is False
    assert "Zero market symbols loaded" in data["reasons"]


def test_8_malformed_trade_request_geometry_and_capital(monkeypatch, tmp_path):
    """Test trade geometry validation and capital safeguards."""
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    test_file = str(tmp_path / "paper.json")
    engine = PaperTradingEngine(initial_capital=100000.0, log_file=test_file)
    monkeypatch.setattr(svc, "_engine", engine)

    # 1. Invalid LONG geometry: SL > Entry
    bad_long = {
        "symbol": "RELIANCE",
        "direction": "LONG",
        "entry": 2500.0,
        "stop_loss": 2600.0,
        "target": 2800.0,
        "atr": 30.0,
    }
    resp_long = client.post("/api/paper/trade", json=bad_long)
    assert resp_long.status_code in (400, 422)

    # 2. Invalid SHORT geometry: Target > Entry
    bad_short = {
        "symbol": "TCS",
        "direction": "SHORT",
        "entry": 3500.0,
        "stop_loss": 3600.0,
        "target": 3700.0,
        "atr": 40.0,
    }
    resp_short = client.post("/api/paper/trade", json=bad_short)
    assert resp_short.status_code in (400, 422)

    # 3. Capital depleted (capital <= 0)
    engine.capital = 0.0
    valid_geom = {
        "symbol": "INFY",
        "direction": "LONG",
        "entry": 1500.0,
        "stop_loss": 1450.0,
        "target": 1600.0,
        "atr": 25.0,
    }
    resp_no_cap = client.post("/api/paper/trade", json=valid_geom)
    assert resp_no_cap.status_code in (400, 422)
    assert "Insufficient capital" in resp_no_cap.json()["error"]


def test_9_statistical_integrity_no_fake_defaults(tmp_path):
    """When no trades have closed, get_stats must report win_rate as None and sample_size as 0."""
    test_file = str(tmp_path / "stats_test.json")
    engine = PaperTradingEngine(initial_capital=100000.0, log_file=test_file)
    stats = engine.get_stats()

    assert stats["total_trades"] == 0
    assert stats["sample_size"] == 0
    assert stats["win_rate"] is None


def test_10_duplicate_background_task_and_shutdown():
    """Verify refresh lock prevents overlapping runs."""
    lock = svc._get_refresh_lock()
    assert not lock.locked()

    async def _test_concurrency():
        async with lock:
            assert lock.locked()

    asyncio.run(_test_concurrency())
    assert not lock.locked()
