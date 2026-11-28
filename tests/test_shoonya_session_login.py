"""Unit tests for Box C Shoonya interactive login, Box A admin proxy, and WS live tick overlay."""

from __future__ import annotations

import asyncio
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_feed_manager_login_interactive_stops_sim_and_starts_ws(monkeypatch):
    from feed.bar_builder import BarBuilder
    from feed.shoonya_client import DEFAULT_LIVE_WATCHLIST, ShoonyaFeedManager
    from feed.token_master import TokenMaster

    mgr = ShoonyaFeedManager(bar_builder=BarBuilder(), token_master=TokenMaster())
    assert len(mgr.watchlist) == len(DEFAULT_LIVE_WATCHLIST) >= 35
    mgr.is_simulated = True

    ws_started = {"n": 0}
    monkeypatch.setattr(mgr, "login", lambda: (setattr(mgr, "api", object()), True)[1])
    monkeypatch.setattr(mgr, "_start_websocket", lambda: ws_started.__setitem__("n", ws_started["n"] + 1))
    monkeypatch.setattr(mgr, "_start_session_watchdog", lambda: None)

    ok, msg = mgr.login_interactive(
        userid="FA99887",
        password="pw",
        api_key="key",
        vendor_code="FA99887_U",
        totp="123456",
    )
    assert ok is True
    assert "FA***87" in msg
    assert mgr.is_simulated is False
    assert mgr._sim_stop_event.is_set()
    assert ws_started["n"] == 1


def test_feed_server_session_login_fail_closed_and_success(monkeypatch):
    import feed_server

    monkeypatch.setenv("FEED_INTERNAL_TOKEN", "feed-secret-xyz")
    client = TestClient(feed_server.app)

    # Without token -> 403
    unauth = client.post("/session/login", json={"userid": "FA1"})
    assert unauth.status_code == 403

    class _DummyMgr:
        is_simulated = False
        is_connected = True
        last_login_time = None
        last_error = None
        watchlist = ["NIFTY 50", "RELIANCE"]

        def has_credentials(self):
            return True

        def masked_uid(self):
            return "FA***01"

        def login_interactive(self, **kwargs):
            return kwargs.get("userid") == "FA10001", "ok" if kwargs.get("userid") == "FA10001" else "bad otp"

    monkeypatch.setattr(feed_server, "feed_manager", _DummyMgr())

    bad = client.post(
        "/session/login",
        headers={"X-Internal-Token": "feed-secret-xyz"},
        json={"userid": "WRONG"},
    )
    assert bad.status_code == 401

    good = client.post(
        "/session/login",
        headers={"X-Internal-Token": "feed-secret-xyz"},
        json={"userid": "FA10001", "totp": "112233"},
    )
    assert good.status_code == 200
    body = good.json()
    assert body["feed_state"] == "LIVE"
    assert body["masked_uid"] == "FA***01"


def test_admin_shoonya_routes_enforce_admin_and_proxy(monkeypatch):
    import api.feed_client as fc
    from server import app

    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-secret")
    monkeypatch.setenv("ENVIRONMENT", "development")

    client = TestClient(app)
    no_key = client.post("/api/admin/feed/shoonya-login", json={"userid": "FA1"})
    assert no_key.status_code in (401, 403)

    async def _fake_login(payload):
        return {
            "ok": True,
            "status_code": 200,
            "feed_state": "LIVE",
            "feed_source": "SHOONYA",
            "masked_uid": "FA***11",
            "message": "connected",
        }

    monkeypatch.setattr(fc, "login_feed_session", _fake_login)
    resp = client.post(
        "/api/admin/feed/shoonya-login",
        headers={"X-Admin-Key": "test-admin-secret"},
        json={"userid": "FA11111", "totp": "654321"},
    )
    assert resp.status_code == 200
    assert resp.json()["feed_state"] == "LIVE"


def test_broadcaster_overlays_live_shoonya_ticks(monkeypatch):
    import api.feed_client as fc
    import api.service as svc
    from data.providers.base import get_shared_provider_health

    h = get_shared_provider_health("shoonya_feed")
    h.record_success(12.0)

    monkeypatch.setattr(fc, "is_feed_configured", lambda: True)

    async def _fake_snap(symbols, timeout=1.5, allow_simulated=False):
        return {
            "source": "SHOONYA_LIVE",
            "quotes": {
                "RELIANCE": {"lp": 1412.5, "prev_close": 1400.0, "high": 1415.0, "low": 1395.0},
            },
        }

    monkeypatch.setattr(fc, "fetch_feed_snapshot", _fake_snap)
    payload = asyncio.run(svc.broadcaster._build_payload())
    assert payload["source"] == "SHOONYA_LIVE"
    assert payload["prices"]["RELIANCE"]["close"] == 1412.5
    assert payload["prices"]["RELIANCE"]["data_status"] == "LIVE"
