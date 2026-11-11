"""
Tests for Market Focus & Asset Class Dynamic Control.
"""

import pytest
from starlette.testclient import TestClient
from server import app
from agents.execution.market_control import (
    get_market_focus_state,
    set_market_focus_state,
    get_market_control_state,
)


def test_market_focus_defaults():
    state = get_market_focus_state()
    assert isinstance(state, dict)
    assert state["active_focus"] in ("nse", "crypto", "forex", "all")
    assert "nse_enabled" in state
    assert "crypto_enabled" in state
    assert "forex_enabled" in state


def test_set_market_focus_transitions():
    # Test setting to crypto
    ok, msg, state = set_market_focus_state(
        active_focus="crypto",
        nse_enabled=False,
        crypto_enabled=True,
        forex_enabled=False,
        operator="test_operator",
    )
    assert ok is True
    assert state["active_focus"] == "crypto"
    assert state["crypto_enabled"] is True
    assert state["nse_enabled"] is False

    # Verify Section 6A synchronization
    mc_state = get_market_control_state()
    assert mc_state["crypto_forex_enabled"] is True

    # Test setting to nse
    ok, msg, state = set_market_focus_state(
        active_focus="nse",
        nse_enabled=True,
        crypto_enabled=False,
        forex_enabled=False,
        operator="test_operator",
    )
    assert ok is True
    assert state["active_focus"] == "nse"
    assert state["nse_enabled"] is True
    assert state["crypto_enabled"] is False

    # Verify Section 6A synchronization
    mc_state = get_market_control_state()
    assert mc_state["crypto_forex_enabled"] is False

    # Test invalid focus value
    ok, msg, _ = set_market_focus_state(active_focus="invalid_market")
    assert ok is False
    assert "Invalid active_focus" in msg

    # Reset back to all
    ok, msg, state = set_market_focus_state(
        active_focus="all",
        nse_enabled=True,
        crypto_enabled=True,
        forex_enabled=True,
        operator="test_operator",
    )
    assert ok is True
    assert state["active_focus"] == "all"


def test_market_focus_routes(monkeypatch):
    client = TestClient(app)

    # 1. GET focus is public & fast
    resp = client.get("/api/market-control/focus")
    assert resp.status_code == 200
    data = resp.json()
    assert "active_focus" in data

    # 2. POST without credentials fails with 403
    resp_unauth = client.post(
        "/api/market-control/focus",
        json={"active_focus": "crypto", "crypto_enabled": True},
    )
    assert resp_unauth.status_code == 403

    # 3. POST with admin key succeeds
    monkeypatch.setenv("ADMIN_API_KEY", "operator_secret_123")
    resp_auth = client.post(
        "/api/market-control/focus",
        headers={"X-Admin-Key": "operator_secret_123"},
        json={
            "active_focus": "all",
            "nse_enabled": True,
            "crypto_enabled": True,
            "forex_enabled": True,
        },
    )
    assert resp_auth.status_code == 200
    res_data = resp_auth.json()
    assert res_data["success"] is True
    assert res_data["state"]["active_focus"] == "all"
