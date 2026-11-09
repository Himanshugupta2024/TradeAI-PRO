"""
Unit tests for /api/market-control/toggle endpoint authentication and safeguards.
"""
import pytest
from fastapi.testclient import TestClient
from server import app
from agents.execution.market_control import _hash_password, _FAILED_ATTEMPTS

@pytest.fixture
def client():
    return TestClient(app)

def test_market_control_toggle_flow(client, monkeypatch):
    test_pwd = "my-secret-operator-pass"
    pwd_hash = _hash_password(test_pwd)

    monkeypatch.setenv("MARKET_CONTROL_PASSWORD_HASH", pwd_hash)
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-xyz")
    _FAILED_ATTEMPTS.clear()

    # 1. Enabling with wrong password fails (403)
    res = client.post("/api/market-control/toggle", json={"enabled": True, "password": "wrong-password"})
    assert res.status_code == 403
    data = res.json()
    assert data["success"] is False
    assert "Invalid" in data["message"]

    # 2. Enabling with correct password in body succeeds (200)
    res = client.post("/api/market-control/toggle", json={"enabled": True, "password": test_pwd})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["state"]["crypto_forex_enabled"] is True

    # 3. Disabling without authorization fails (403)
    res = client.post("/api/market-control/toggle", json={"enabled": False, "password": "wrong"})
    assert res.status_code == 403

    # 4. Disabling with operator password succeeds (200)
    res = client.post("/api/market-control/toggle", json={"enabled": False, "password": test_pwd})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["state"]["crypto_forex_enabled"] is False

    # 5. Enabling with header X-Admin-Key matching operator pass succeeds
    res = client.post(
        "/api/market-control/toggle",
        headers={"X-Admin-Key": test_pwd},
        json={"enabled": True}
    )
    assert res.status_code == 200
    assert res.json()["success"] is True

    # Cleanup back to False
    client.post("/api/market-control/toggle", json={"enabled": False, "password": test_pwd})
