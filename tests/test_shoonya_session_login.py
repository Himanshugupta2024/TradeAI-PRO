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


def test_shoonya_login_preserves_exact_broker_emsg(monkeypatch):
    import requests
    from feed.bar_builder import BarBuilder
    from feed.shoonya_client import ShoonyaFeedManager
    from feed.token_master import TokenMaster

    mgr = ShoonyaFeedManager(bar_builder=BarBuilder(), token_master=TokenMaster())
    mgr.uid = "FN243453"
    mgr.password = "secretpw"
    mgr.api_key = "secretapikey"
    mgr.vendor_code = "FN243453_U"
    mgr.totp_secret = "123 456"

    class _MockResp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    captured = {}

    def _fail_post(url, data=None, timeout=15):
        captured["url"] = url
        captured["data"] = data
        return _MockResp(400, {"stat": "Not_Ok", "emsg": "Invalid Input : Password Expired"})

    monkeypatch.setattr(requests, "post", _fail_post)
    assert mgr.login() is False
    assert mgr.last_error == "Invalid Input : Password Expired"
    assert captured["url"].endswith("/QuickAuth")
    assert '"factor2": "123456"' in captured["data"]

    def _ok_post(url, data=None, timeout=15):
        return _MockResp(200, {"stat": "Ok", "susertoken": "tok-abc-123"})

    monkeypatch.setattr(requests, "post", _ok_post)
    assert mgr.login() is True
    assert mgr.usertoken == "tok-abc-123"
    assert mgr.is_simulated is False
    assert mgr.last_error is None


def test_shoonya_oauth_auth_code_exchange_and_emsg(monkeypatch):
    import hashlib
    import json
    import requests
    from feed.bar_builder import BarBuilder
    from feed.shoonya_client import ShoonyaFeedManager
    from feed.token_master import TokenMaster

    mgr = ShoonyaFeedManager(bar_builder=BarBuilder(), token_master=TokenMaster())
    mgr.uid = "FN243453"
    mgr.vendor_code = "FN243453_U"
    mgr.api_key = "mysecretkey"
    mgr.is_simulated = True

    monkeypatch.setattr(mgr, "_start_websocket", lambda: None)
    monkeypatch.setattr(mgr, "_start_session_watchdog", lambda: None)
    monkeypatch.setattr(mgr, "_start_fno_rotator", lambda: None)

    class _Resp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body
            self.text = json.dumps(body)

        def json(self):
            return self._body

    seen = {}

    def _ok_gen(url, data=None, timeout=15):
        seen["url"] = url
        seen["jdata"] = json.loads(data["jData"])
        return _Resp(200, {"stat": "Ok", "susertoken": "oauth-live-tok"})

    monkeypatch.setattr(requests, "post", _ok_gen)
    ok, msg = mgr.login_interactive(
        auth_code="https://tradeai-pro.tradingpro.workers.dev/?code=auth_999&uid=FN243453"
    )
    assert ok is True
    assert "FN****53" in msg
    assert mgr.usertoken == "oauth-live-tok"
    assert mgr.is_simulated is False
    assert seen["url"].endswith("/GenAcsTok")
    expected_hash = hashlib.sha256(b"FN243453_Umysecretkeyauth_999").hexdigest()
    assert seen["jdata"] == {"uid": "FN243453", "code": "auth_999", "appkey": expected_hash}

    # Exact broker emsg preserved on IP/code failure
    def _bad_gen(url, data=None, timeout=15):
        return _Resp(400, {"stat": "Not_Ok", "emsg": "Invalid IP Address"})

    monkeypatch.setattr(requests, "post", _bad_gen)
    ok2, msg2 = mgr.login_interactive(auth_code="expired_code")
    assert ok2 is False
    assert msg2 == "Invalid IP Address"


def test_feed_server_session_status_exposes_oauth_metadata(monkeypatch):
    import feed_server

    monkeypatch.setenv("FEED_INTERNAL_TOKEN", "feed-secret-xyz")
    monkeypatch.setattr(feed_server, "_get_cached_egress_ip", lambda: "35.160.1.2")

    class _OAuthMgr:
        is_simulated = True
        is_connected = False
        last_login_time = None
        last_error = None
        watchlist = ["NIFTY 50"]
        uid = "FN243453"
        vendor_code = "FN243453_U"
        api_key = "secret"

        def has_credentials(self):
            return False

        def masked_uid(self):
            return "FN***53"

    monkeypatch.setattr(feed_server, "feed_manager", _OAuthMgr())
    client = TestClient(feed_server.app)
    resp = client.get("/session/status", headers={"X-Internal-Token": "feed-secret-xyz"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["oauth_client_id"] == "FN243453_U"
    assert "FN243453_U" in data["oauth_login_url"]
    assert data["has_oauth_credentials"] is True
    assert data["egress_ip"] == "35.160.1.2"
