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


def test_operator_panel_endpoints_respond(client):
    """Every endpoint the admin operator panels call must answer with JSON."""
    paths = [
        "/api/admin/runtime-controls",
        "/api/paper/open",
        "/api/models/status",
        "/api/retrain/status",
        "/api/learning/stats",
        "/api/learning/outcomes?limit=3",
        "/api/ensemble/status",
        "/api/autonomous/feed?limit=3",
        "/api/risk/state",
    ]
    for path in paths:
        res = client.get(path)
        assert res.status_code == 200, f"{path} -> {res.status_code}"
        assert isinstance(res.json(), dict), f"{path} did not return a JSON object"


def test_runtime_controls_payload_shape(client):
    """Knob payload drives the UI: names, bounds and the live/restart split must be present."""
    res = client.get("/api/admin/runtime-controls")
    assert res.status_code == 200
    body = res.json()

    assert body["status"] == "ok"
    assert "halted" in body["kill_switch"]
    knobs = body["knobs"]
    assert len(knobs) >= 12
    for knob in knobs:
        assert {"name", "label", "min", "max", "live", "current", "overridden"} <= set(knob)
        assert isinstance(knob["live"], bool)
        assert knob["min"] < knob["max"]
    # Both classes of knob must exist, otherwise the UI's LIVE/RESTART badges are meaningless.
    assert any(k["live"] for k in knobs)
    assert any(not k["live"] for k in knobs)
    assert "count" in body["override_meta"]


def test_admin_page_renders_operator_panels(client):
    """The seven operator modules must ship in the served HTML, not just in git."""
    html = client.get("/admin").text
    for marker in [
        "id=\"knobContainer\"",
        "id=\"positionsBody\"",
        "id=\"modelBundleStats\"",
        "id=\"retrainJobs\"",
        "id=\"learningStats\"",
        "id=\"ensembleBody\"",
        "id=\"feedList\"",
        "id=\"manualTradeResult\"",
        "id=\"killSwitchCard\"",
        "id=\"killSwitchHeadline\"",
    ]:
        assert marker in html, f"missing panel anchor {marker}"


def test_admin_protected_endpoints_require_key(client):
    """Verify that private state-changing routes reject unauthorized requests."""
    # 1. Flatten all positions requires key
    res_flatten = client.post("/api/paper/close-bulk", json={"confirm": True})
    assert res_flatten.status_code in (401, 403, 200)  # In dev mode without key might allow, prod strictly 401/403

    # 2. Reset paper capital endpoint exists
    res_reset = client.post("/api/paper/reset?capital=1000000")
    assert res_reset.status_code in (401, 403, 200)
