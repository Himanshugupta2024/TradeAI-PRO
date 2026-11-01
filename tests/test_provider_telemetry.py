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
