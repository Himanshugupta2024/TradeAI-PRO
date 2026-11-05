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


def test_consecutive_standby_deduplication_preserves_intervening_executions(client):
    """
    Verify that:
    1. Consecutive duplicate MARKET_CLOSED or SYSTEM_STANDBY entries are collapsed into one.
    2. Intervening real trades / actions (e.g. Standby -> AUTO_ENTER -> Standby) are fully preserved.
    """
    from db.runtime_state import set_runtime_state

    synthetic_feed = [
        {"id": "s1", "action": "MARKET_CLOSED", "symbol": "NSE", "timestamp": "2026-11-05T19:00:00Z"},
        {"id": "s2", "action": "MARKET_CLOSED", "symbol": "NSE", "timestamp": "2026-11-05T18:50:00Z"},
        {"id": "s3", "action": "AUTO_ENTER", "symbol": "BTC/USDT", "timestamp": "2026-11-05T18:40:00Z"},
        {"id": "s4", "action": "SYSTEM_STANDBY", "symbol": "SYSTEM", "timestamp": "2026-11-05T18:30:00Z"},
        {"id": "s5", "action": "SYSTEM_STANDBY", "symbol": "SYSTEM", "timestamp": "2026-11-05T18:20:00Z"},
    ]
    set_runtime_state("autonomous_feed:v1", synthetic_feed)

    res = client.get("/api/autonomous/feed?limit=10")
    assert res.status_code == 200
    feed = res.json()["feed"]

    actions = [x["action"] for x in feed]
    assert actions == ["MARKET_CLOSED", "AUTO_ENTER", "SYSTEM_STANDBY"]
