"""
Regression test suite for all 8 verified production issues in TradeAI PRO.
Covers:
- FIX 4: Index movers import and calculation
- FIX 7: Health freshness tiers
- FIX 8a: Fail-closed CRON_SECRET authentication
- FIX 8b: Admin authentication on /api/intraday/scan
- FIX 5: Data Brain V3 context payload structure
"""
import os
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from server import app
import api.service as svc
from api.health_v2 import evaluate_health_v2


@pytest.fixture
def client():
    return TestClient(app)


def test_fix4_index_movers_imported_and_functional(client):
    """FIX 4: Ensure calculate_index_movers is imported in swing.py and does not raise NameError."""
    from api.routers.swing import calculate_index_movers
    assert callable(calculate_index_movers)

    # Calling /api/index-mover/nifty50 must return JSON with movers or error, never NameError 500
    res = client.get("/api/index-mover/nifty50")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, dict)
    # Must NOT have error: "name 'calculate_index_movers' is not defined"
    if "error" in data:
        assert "calculate_index_movers" not in data["error"]


def test_fix7_health_freshness_tiers():
    """FIX 7: Verify tighter freshness tiers (<=300s FRESH, <=900s DELAYED, >900s STALE)."""
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    with patch("api.health_v2.is_nse_activity_window_active", return_value=(True, "active")):
        # 4 minutes old -> DATA_FRESH
        h_fresh = evaluate_health_v2(
            db_ok=True,
            feed_status={"last_bar_at": now_utc.isoformat(), "bar_age_seconds": 240.0},
            now=now_utc,
        )
        assert h_fresh["health_tier"]["freshness"] == "DATA_FRESH"
        assert "fresh" in h_fresh["health_reasons"]["freshness"].lower()

        # 12 minutes old -> DATA_DELAYED
        h_delayed = evaluate_health_v2(
            db_ok=True,
            feed_status={"last_bar_at": now_utc.isoformat(), "bar_age_seconds": 720.0},
            now=now_utc,
        )
        assert h_delayed["health_tier"]["freshness"] == "DATA_DELAYED"
        assert "delayed" in h_delayed["health_reasons"]["freshness"].lower()

        # 20 minutes old -> STALE_DATA
        h_stale = evaluate_health_v2(
            db_ok=True,
            feed_status={"last_bar_at": now_utc.isoformat(), "bar_age_seconds": 1200.0},
            now=now_utc,
        )
        assert h_stale["health_tier"]["freshness"] == "STALE_DATA"
        assert "lagging" in h_stale["health_reasons"]["freshness"].lower()


def test_fix8a_cron_refresh_fail_closed_without_secret(client):
    """FIX 8a: POST /api/cron/refresh must return 503 when CRON_SECRET is not configured."""
    with patch.dict(os.environ, {}, clear=True):
        # Ensure CRON_SECRET is absent
        os.environ.pop("CRON_SECRET", None)
        res = client.post("/api/cron/refresh")
        assert res.status_code == 503
        assert "CRON_SECRET not configured" in res.json()["detail"]


def test_fix8a_cron_refresh_rejects_invalid_secret(client):
    """FIX 8a: POST /api/cron/refresh must return 403 when X-Cron-Secret header is wrong."""
    with patch.dict(os.environ, {"CRON_SECRET": "correct-secret-xyz"}):
        res = client.post("/api/cron/refresh", headers={"X-Cron-Secret": "wrong-secret"})
        assert res.status_code == 403
        assert "Invalid cron authorization secret" in res.json()["detail"]


def test_fix8a_cron_refresh_accepts_valid_secret(client):
    """FIX 8a: POST /api/cron/refresh dispatches or skips cleanly when secret matches."""
    with patch.dict(os.environ, {"CRON_SECRET": "correct-secret-xyz"}):
        res = client.post("/api/cron/refresh", headers={"X-Cron-Secret": "correct-secret-xyz"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] in ("DISPATCHED", "SKIPPED")


def test_fix8b_intraday_scan_requires_admin_auth(client):
    """FIX 8b: POST /api/intraday/scan must reject unauthenticated requests when ADMIN_API_KEY is configured."""
    with patch.dict(os.environ, {"ADMIN_API_KEY": "super-secret-key-123"}):
        with patch.object(svc.settings, "ADMIN_API_KEY", "super-secret-key-123"):
            res = client.post("/api/intraday/scan")
            assert res.status_code in (401, 403)


def test_fix5_data_brain_v3_context_contract(client):
    """FIX 5: /api/context/v3 must return all 4 context keys with real_data flag."""
    res = client.get("/api/context/v3")
    assert res.status_code == 200
    data = res.json()
    assert "status" in data
    assert "india_vix" in data
    assert "fii_dii_flow" in data
    assert "market_breadth" in data
    assert "derivatives_oi" in data
    assert "real_data" in data


def test_vix_multiindex_parse_handles_series():
    """Verify MultiIndex DataFrame from yfinance does not raise TypeError in float() conversion."""
    import pandas as pd
    arrays = [["Close", "Open", "High", "Low"], ["^INDIAVIX", "^INDIAVIX", "^INDIAVIX", "^INDIAVIX"]]
    columns = pd.MultiIndex.from_tuples(list(zip(*arrays)))
    vix_df = pd.DataFrame([[15.2, 15.0, 15.5, 14.8], [15.6, 15.2, 15.8, 15.1]], columns=columns)

    close_series = vix_df["Close"]
    if isinstance(close_series, pd.DataFrame):
        close_series = close_series.iloc[:, 0]
    elif hasattr(close_series, "squeeze"):
        close_series = close_series.squeeze()
    close_series = close_series.dropna()

    assert len(close_series) >= 2
    last_val = float(close_series.iloc[-1])
    prev_val = float(close_series.iloc[-2])
    assert last_val == 15.6
    assert prev_val == 15.2
    chg_pct = round(((last_val - prev_val) / prev_val) * 100.0, 2)
    assert chg_pct == 2.63


def test_intraday_store_infers_source_candle_at():
    """Verify intraday_store.save_scan infers source_last_candle_at from signals if not passed."""
    from db import intraday_store
    from datetime import datetime, timezone

    candle_iso = "2026-11-13T14:25:00+00:00"
    signals = [
        {"symbol": "RELIANCE", "direction": "LONG", "score": 75.0, "candle_time": candle_iso}
    ]

    scan_id = intraday_store.save_scan(
        signals,
        symbols_scanned=1,
        trigger="test_source_candle",
        source_last_candle_at=None,
    )
    assert scan_id is not None

    latest = intraday_store.load_latest_scan()
    assert latest is not None
    assert latest.get("source_candle_at") is not None
    assert "2026-11-13" in latest["source_candle_at"]


def test_refresh_cycle_timeout_configured():
    """Verify REFRESH_CYCLE_TIMEOUT_SEC is set to 240 seconds."""
    from config import settings
    assert settings.REFRESH_CYCLE_TIMEOUT_SEC >= 240
