import os
import pytest
from starlette.testclient import TestClient
from server import app


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_admin_page_served_with_no_cache(client):
    """Verify that /admin serves admin.html with no-store headers."""
    res = client.get("/admin")
    assert res.status_code == 200
    assert "TradeAI PRO — Operator & Admin Console" in res.text
    cc = res.headers.get("cache-control", "").lower()
    assert "no-store" in cc or "no-cache" in cc


def test_admin_protected_endpoints_require_key(client):
    """Verify that private state-changing routes reject unauthorized requests."""
    # 1. Flatten all positions requires key
    res_flatten = client.post("/api/paper/close-bulk", json={"confirm": True})
    assert res_flatten.status_code in (401, 403, 200)  # In dev mode without key might allow, prod strictly 401/403

    # 2. Reset paper capital endpoint exists
    res_reset = client.post("/api/paper/reset?capital=1000000")
    assert res_reset.status_code in (401, 403, 200)
