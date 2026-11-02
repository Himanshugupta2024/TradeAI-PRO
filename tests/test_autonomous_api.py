"""
API integration tests for autonomous daemon endpoints.
"""

import pytest
from fastapi.testclient import TestClient
from server import app


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
    assert st.get("max_positions") == 5


def test_get_autonomous_feed(client):
    res = client.get("/api/autonomous/feed?limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True
    assert "feed" in data
    assert isinstance(data["feed"], list)


def test_toggle_autonomous_mode(client):
    # Test toggling off and on
    res = client.post("/api/autonomous/toggle", json={"enabled": False})
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True
    assert data.get("enabled") is False

    # Toggle back on
    res2 = client.post("/api/autonomous/toggle", json={"enabled": True})
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2.get("ok") is True
    assert data2.get("enabled") is True
