"""
Regression tests for market-hours stability, event-loop responsiveness, lightweight cron/warmup
OOM protection, Shoonya WS reconnect/daily login, and lazy ML/scipy imports on Box A.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from server import app


@pytest.fixture
def client():
    return TestClient(app)


def test_cron_refresh_lightweight_skips_heavy_market_refresh(client, monkeypatch):
    """POST /api/cron/refresh must NEVER call heavy _refresh_market_data() in LIGHTWEIGHT_API_MODE=true."""
    import api.service as svc

    heavy_mock = AsyncMock()
    light_mock = AsyncMock()
    monkeypatch.setattr(svc, "_refresh_market_data", heavy_mock)
    monkeypatch.setattr(svc, "_refresh_lightweight_state", light_mock)

    with patch.dict("os.environ", {
        "CRON_SECRET": "test-cron-secret",
        "LIGHTWEIGHT_API_MODE": "true",
        "BACKGROUND_REFRESH_ENABLED": "true",
    }), patch(
        "agents.execution.market_control.is_nse_activity_window_active",
        return_value=(True, "NSE_OPEN"),
    ):
        resp = client.post("/api/cron/refresh", headers={"X-Cron-Secret": "test-cron-secret"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "DISPATCHED"
        assert body["mode"] == "lightweight_sync"
        heavy_mock.assert_not_called()
        light_mock.assert_awaited_once()


def test_warmup_lightweight_does_not_launch_heavy_refresh(monkeypatch):
    """GET /api/warmup in LIGHTWEIGHT_API_MODE=true must hydrate DB state instead of calling _refresh_market_data."""
    import api.service as svc
    from api.routers.health import warmup_check

    heavy_mock = AsyncMock()
    hydrate_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "_refresh_market_data", heavy_mock)
    monkeypatch.setattr(svc, "_hydrate_signals_cache_from_db", hydrate_mock)
    monkeypatch.setattr(svc, "_market_data", {})
    monkeypatch.setattr(svc, "_persisted_signals_snapshot", [{"symbol": "RELIANCE"}])
    monkeypatch.setattr(svc, "_loading_status", {"ready": True, "phase": "ready_on_demand"})

    with patch.dict("os.environ", {"LIGHTWEIGHT_API_MODE": "true"}):
        res = asyncio.run(warmup_check())
        assert res["status"] == "READY"
        assert res["is_ready"] is True
        heavy_mock.assert_not_called()


def test_risk_gauge_endpoint_offloads_to_thread_and_uses_swr(tmp_path, monkeypatch):
    """
    /api/edge/risk-gauge must never block the FastAPI event loop:
    1. Cold fetch runs in a worker thread (not the event-loop thread).
    2. Stale cache during market hours serves immediately (<100ms) and schedules background refresh.
    """
    from agents.macro.risk_gauge import RiskGauge, RiskIndicators
    from api.routers import research as res_mod

    cache_path = tmp_path / "risk_cache.json"
    caller_threads: list[int] = []
    event_loop_thread_id = threading.get_ident()

    sample_ind = RiskIndicators(
        vix=15.0,
        vix_change_pct=-2.0,
        vix_1d_change_pct=-1.0,
        btc_trend="FLAT",
        nifty_trend="RISING",
        usdjpy_level=154.0,
        usdjpy_trend="FLAT",
        gold_trend="FLAT",
        risk_regime="RISK_ON",
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=120),
        india_vix=14.0,
        india_vix_change_pct=-1.0,
        india_vix_7d_change_pct=-2.0,
        india_vix_trend="FALLING",
        ad_ratio=1.2,
        above_ema20_pct=60.0,
        breadth_regime="STRONG_BREADTH",
    )

    def _mock_fetch(self):
        caller_threads.append(threading.get_ident())
        time.sleep(0.05)
        fresh = RiskIndicators.from_dict({**sample_ind.to_dict(), "timestamp": datetime.now(timezone.utc).isoformat()})
        self._save_cache(fresh)
        return fresh

    monkeypatch.setattr(RiskGauge, "fetch_risk_indicators", _mock_fetch)
    orig_init = RiskGauge.__init__

    def _patched_init(self, cache_file=str(cache_path), cache_ttl_seconds=300, fallback_enabled=True):
        orig_init(self, cache_file=str(cache_path), cache_ttl_seconds=cache_ttl_seconds, fallback_enabled=fallback_enabled)

    monkeypatch.setattr(RiskGauge, "__init__", _patched_init)
    res_mod._RISK_GAUGE_REFRESH_TASK = None

    async def _exercise():
        # 1. Cold fetch: offloaded to worker thread
        cold_out = await res_mod.get_edge_risk_gauge(force_refresh=False)
        assert "risk_score" in cold_out
        assert caller_threads, "Expected fetch_risk_indicators to run"
        assert caller_threads[0] != event_loop_thread_id

        # 2. Seed stale cache -> SWR returns immediately without waiting for slow fetch
        RiskGauge()._save_cache(sample_ind)

        def _slow_fetch(self):
            time.sleep(2.0)
            return sample_ind

        monkeypatch.setattr(RiskGauge, "fetch_risk_indicators", _slow_fetch)
        res_mod._RISK_GAUGE_REFRESH_TASK = None
        with patch("agents.execution.session_guard.is_nse_market_open", return_value=(True, "SESSION_ACTIVE")):
            t0 = time.perf_counter()
            swr_out = await res_mod.get_edge_risk_gauge(force_refresh=False)
            elapsed = time.perf_counter() - t0
        assert "risk_score" in swr_out
        assert elapsed < 0.5, f"SWR response blocked for {elapsed:.2f}s"

        # Verify closed market tuple (False, reason) keeps full 300s TTL instead of truthy-tuple 60s bug
        closed_gauge = RiskGauge(cache_ttl_seconds=300)
        with patch("agents.execution.session_guard.is_nse_market_open", return_value=(False, "MARKET_CLOSED")):
            assert closed_gauge._is_cache_fresh(sample_ind) is True

        if res_mod._RISK_GAUGE_REFRESH_TASK is not None:
            res_mod._RISK_GAUGE_REFRESH_TASK.cancel()
            try:
                await res_mod._RISK_GAUGE_REFRESH_TASK
            except BaseException:
                pass

    asyncio.run(_exercise())


@pytest.mark.asyncio
async def test_system_data_health_has_no_name_errors():
    """/api/system/data-health must import intraday_store and get_journal cleanly (no NameError)."""
    from api.routers.health import get_system_data_health

    res = await get_system_data_health()
    subs = res["subsystems"]
    assert "not defined" not in str(subs["intraday_scanner"].get("error", ""))
    assert "not defined" not in str(subs["signal_scorecard"].get("error", ""))


def test_shoonya_reconnect_and_daily_relogin():
    """ShoonyaFeedManager._on_close triggers reconnect scheduling and detects stale prior-day sessions."""
    from feed.shoonya_client import ShoonyaFeedManager

    mgr = ShoonyaFeedManager()
    mgr.uid = "USER1"
    mgr.password = "PASS"
    mgr.api_key = "KEY"
    mgr.vendor_code = "VC"
    mgr.totp_secret = "JBSWY3DPEHPK3PXP"

    # Needs relogin at 08:50 IST on weekday when last login was yesterday
    ist = ZoneInfo("Asia/Kolkata")
    mgr.last_login_time = datetime(2026, 11, 25, 9, 0, tzinfo=ist).astimezone(timezone.utc)
    morning_now = datetime(2026, 11, 26, 8, 50, tzinfo=ist).astimezone(timezone.utc)
    assert mgr._needs_daily_relogin(now_utc=morning_now) is True

    # Does not relogin again once today's login succeeded
    mgr.last_login_time = datetime(2026, 11, 26, 8, 46, tzinfo=ist).astimezone(timezone.utc)
    assert mgr._needs_daily_relogin(now_utc=morning_now) is False

    # _on_close must invoke _schedule_reconnect
    mgr.is_connected = True
    called = []
    mgr._schedule_reconnect = lambda: called.append(True)
    mgr._on_close()
    assert mgr.is_connected is False
    assert called == [True]


def test_feed_client_retries_once_on_timeout(monkeypatch):
    """fetch_feed_snapshot retries once on transient ConnectError/TimeoutException."""
    from api import feed_client

    assert feed_client.FEED_TIMEOUT_SEC >= 5.0
    monkeypatch.setenv("FEED_SERVICE_URL", "http://mock-box-c.local")

    calls = {"count": 0}
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"source": "SHOONYA_LIVE", "quotes": {"RELIANCE": {"lp": 1380.0}}, "bars": {}}

    async def _flaky_get(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ReadTimeout("transient timeout")
        return ok_resp

    mock_client = MagicMock()
    mock_client.get = _flaky_get
    with patch("api.feed_client._get_async_client", return_value=mock_client):
        snap = asyncio.run(feed_client.fetch_feed_snapshot(["RELIANCE"]))
    assert snap is not None
    assert calls["count"] == 2


def test_lightweight_server_boot_defers_scipy_and_sklearn():
    """Importing server in LIGHTWEIGHT_API_MODE=true must not eagerly import scipy or sklearn."""
    code = (
        "import os, sys; "
        "os.environ['LIGHTWEIGHT_API_MODE']='true'; "
        "os.environ['INITIAL_LOAD_ENABLED']='false'; "
        "import server; "
        "loaded = [m for m in ('scipy', 'sklearn') if m in sys.modules]; "
        "print('LOADED:' + ','.join(loaded))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "LOADED:" in proc.stdout
    loaded_line = [ln for ln in proc.stdout.splitlines() if ln.startswith("LOADED:")][-1]
    assert loaded_line == "LOADED:", f"Heavy ML libs eagerly loaded at API boot: {loaded_line}"
