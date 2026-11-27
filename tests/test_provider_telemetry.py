"""
Unit tests for Provider Telemetry in /api/health and api.service.
"""
import pytest
from starlette.testclient import TestClient
from api.routes import router
from fastapi import FastAPI
from api import service as svc


def test_health_includes_providers_telemetry():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/api/health")
    # Health returns 200 (or 503 if zero symbols loaded in test runner, but still valid JSON)
    assert resp.status_code in (200, 503)
    data = resp.json()

    assert "providers" in data
    assert isinstance(data["providers"], dict)


def test_service_get_providers_telemetry():
    telemetry = svc._get_providers_telemetry()
    assert isinstance(telemetry, dict)
    assert "yahoo" in telemetry
    assert "twelve_data" in telemetry
    assert "gdelt" in telemetry
    assert telemetry["yahoo"]["healthy"] is True


def test_provider_telemetry_shared_across_direct_instances():
    """Standalone TradingView/Yahoo instances must report into shared /api/health telemetry."""
    from data.providers.tradingview import TradingViewProvider
    from data.providers.yahoo import YahooProvider
    from data.router import get_default_router

    # Ensure router singleton exists first
    _ = get_default_router()

    tv = TradingViewProvider(timeout=2.0)
    tv.health.record_success(35.0)
    yh = YahooProvider(timeout=2.0)
    yh.health.record_success(120.0)

    telemetry = svc._get_providers_telemetry()
    assert telemetry["tradingview"]["total_calls"] >= 1
    assert telemetry["tradingview"]["avg_latency_ms"] >= 35.0
    assert telemetry["yahoo"]["total_calls"] >= 1
    assert telemetry["yahoo"]["avg_latency_ms"] >= 120.0
