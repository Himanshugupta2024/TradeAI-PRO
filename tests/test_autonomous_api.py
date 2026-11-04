"""
API integration tests for autonomous daemon endpoints.
"""

import os
import pytest
from fastapi.testclient import TestClient
from server import app
from config import settings


@pytest.fixture
def client():
    # Use TestClient with raise_server_exceptions=True
    return TestClient(app, raise_server_exceptions=True)


def test_get_autonomous_status(client):
    res = client.get("/api/autonomous/status")
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True
    assert "status" in data
    st = data["status"]
    assert "enabled" in st
    assert st.get("mode") == "100% Autonomous Zero-Click"
    assert st.get("max_positions") == settings.AUTONOMOUS_MAX_POSITIONS


def test_get_autonomous_feed(client):
    res = client.get("/api/autonomous/feed?limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True
    assert "feed" in data
    assert isinstance(data["feed"], list)


def test_toggle_autonomous_mode(client, monkeypatch):
    # Configure test admin key
    test_key = "test-secret-admin-key-456"
    monkeypatch.setenv("ADMIN_API_KEY", test_key)
    headers = {"X-Admin-Key": test_key}

    # Test toggling off and on
    res = client.post("/api/autonomous/toggle", json={"enabled": False}, headers=headers)
    assert res.status_code == 200
    assert res.json().get("ok") is True
    assert res.json()["status"]["enabled"] is False

    res = client.post("/api/autonomous/toggle", json={"enabled": True}, headers=headers)
    assert res.status_code == 200
    assert res.json().get("ok") is True
    assert res.json()["status"]["enabled"] is True


def test_trigger_autonomous_cycle_endpoint(client):
    # Test triggering a single cycle
    res = client.post("/api/autonomous/run-cycle", json={"universe": "BTC/USDT", "dry_run": True})
    assert res.status_code == 200
    data = res.json()
    assert "ok" in data
    # Either successfully dispatched (True) or rate-limited if run right before
    if data["ok"]:
        assert "method" in data
        assert data["method"] in ("local_async", "github_actions")
    else:
        assert data.get("error") == "RATE_LIMITED"
