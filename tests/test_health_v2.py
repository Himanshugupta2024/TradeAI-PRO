"""
Tests for Health v2 3-Tier Composite Health Model & Data Freshness Engine.
==========================================================================
"""

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pytest

from api.health_v2 import evaluate_health_v2, format_human_data_age

_IST = ZoneInfo("Asia/Kolkata")


def test_health_v2_market_open_fresh():
    """During active market hours with fresh feed (< 30m) and healthy worker, status is HEALTHY."""
    # Wednesday 11:30 IST (active trading session)
    dt_active = datetime(2026, 11, 11, 6, 0, 0, tzinfo=timezone.utc)  # 11:30 AM IST
    now_utc = dt_active
    snap_dt = now_utc - timedelta(minutes=5)

    feed = {
        "market_snapshot_health": "CURRENT",
        "worker_health": "WORKER_OK",
        "age_seconds": 300.0,
        "last_update": snap_dt.isoformat(),
        "is_market_open": True,
    }
    worker = {"status": "COMPLETED", "timestamp": (now_utc - timedelta(minutes=10)).isoformat()}

    res = evaluate_health_v2(db_ok=True, feed_status=feed, worker_status=worker, now=now_utc)

    assert res["status"] == "HEALTHY"
    assert res["healthy"] is True
    assert res["health_tier"]["infra"] == "UP"
    assert res["health_tier"]["worker"] == "WORKER_OK"
    assert res["health_tier"]["freshness"] == "DATA_FRESH"
    assert "15m Delayed" in res["data_age_human"]
    assert "5m ago" in res["data_age_human"]


def test_health_v2_market_open_stale_feed():
    """During active market hours, feed age > 30m must downgrade status to STALE_DATA and healthy=False."""
    # Wednesday 13:00 IST
    now_utc = datetime(2026, 11, 11, 7, 30, 0, tzinfo=timezone.utc)
    snap_dt = now_utc - timedelta(minutes=45)  # 45 mins old

    feed = {
        "market_snapshot_health": "STALE",
        "worker_health": "WORKER_OK",
        "age_seconds": 2700.0,
        "last_update": snap_dt.isoformat(),
        "is_market_open": True,
    }
    worker = {"status": "COMPLETED", "timestamp": (now_utc - timedelta(minutes=10)).isoformat()}

    res = evaluate_health_v2(db_ok=True, feed_status=feed, worker_status=worker, now=now_utc)

    assert res["status"] == "STALE_DATA"
    assert res["healthy"] is False
    assert res["health_tier"]["freshness"] == "STALE_DATA"
    assert "⚠️ Stale Feed" in res["data_age_human"]
    assert "45m ago" in res["data_age_human"]


def test_health_v2_market_open_worker_down():
    """During active market hours, if worker hasn't run, status must be WORKER_DOWN."""
    now_utc = datetime(2026, 11, 11, 7, 30, 0, tzinfo=timezone.utc)
    snap_dt = now_utc - timedelta(minutes=5)

    feed = {
        "market_snapshot_health": "CURRENT",
        "worker_health": "WORKER_STALE",
        "age_seconds": 300.0,
        "last_update": snap_dt.isoformat(),
        "is_market_open": True,
    }
    worker = {"status": "COMPLETED", "timestamp": (now_utc - timedelta(hours=2)).isoformat()}

    res = evaluate_health_v2(db_ok=True, feed_status=feed, worker_status=worker, now=now_utc)

    assert res["status"] == "WORKER_DOWN"
    assert res["healthy"] is False
    assert res["health_tier"]["worker"] == "WORKER_STALE"


def test_health_v2_market_closed_eod_valid():
    """When market is closed (weekend/night), EOD snapshot is valid and does not cause false STALE_DATA."""
    # Saturday 15:30 IST
    now_utc = datetime(2026, 11, 7, 10, 0, 0, tzinfo=timezone.utc)
    # Friday 15:30 EOD snapshot (~18 hours old)
    snap_dt = datetime(2026, 11, 6, 10, 0, 0, tzinfo=timezone.utc)

    feed = {
        "market_snapshot_health": "EOD",
        "worker_health": "NO_RECENT_RUN",
        "age_seconds": 86400.0,
        "last_update": snap_dt.isoformat(),
        "is_market_open": False,
    }
    worker = {"status": "COMPLETED", "timestamp": snap_dt.isoformat()}

    res = evaluate_health_v2(db_ok=True, feed_status=feed, worker_status=worker, now=now_utc)

    assert res["status"] == "HEALTHY"
    assert res["healthy"] is True
    assert res["health_tier"]["infra"] == "UP"
    assert res["health_tier"]["worker"] == "STANDBY_OFF_HOURS"
    assert res["health_tier"]["freshness"] == "EOD_VALID"
    assert "EOD Snapshot" in res["data_age_human"]
    assert "Market Closed" in res["data_age_human"]


def test_health_v2_database_down():
    """If database connectivity fails, status must immediately report DEGRADED regardless of other tiers."""
    feed = {
        "market_snapshot_health": "CURRENT",
        "worker_health": "WORKER_OK",
        "age_seconds": 100.0,
        "last_update": datetime.now(timezone.utc).isoformat(),
        "is_market_open": True,
    }

    res = evaluate_health_v2(db_ok=False, feed_status=feed)

    assert res["status"] == "DEGRADED"
    assert res["healthy"] is False
    assert res["health_tier"]["infra"] == "DOWN"
    assert "Database" in res["overall_reason"]


def test_format_human_data_age_variations():
    """Test honest text variations for user display."""
    now_ist = datetime(2026, 11, 10, 11, 30, 0, tzinfo=_IST)

    # 1. Market closed
    snap_eod = datetime(2026, 11, 6, 15, 30, 0, tzinfo=_IST)
    eod_label = format_human_data_age(snap_eod, age_seconds=72000, is_nse_open=False, now_ist=now_ist)
    assert "EOD Snapshot" in eod_label
    assert "Market Closed" in eod_label

    # 2. Market open & fresh (4 mins ago)
    snap_fresh = now_ist - timedelta(minutes=4)
    fresh_label = format_human_data_age(snap_fresh, age_seconds=240, is_nse_open=True, now_ist=now_ist)
    assert fresh_label == "15m Delayed · Updated 4m ago"

    # 3. Market open & stale (45 mins ago)
    snap_stale = now_ist - timedelta(minutes=45)
    stale_label = format_human_data_age(snap_stale, age_seconds=2700, is_nse_open=True, now_ist=now_ist)
    assert "⚠️ Stale Feed" in stale_label
    assert "45m ago" in stale_label


def test_api_health_endpoint_response_structure():
    """Verify FastAPI /api/health endpoint returns health_tier and honest data age."""
    from starlette.testclient import TestClient
    from server import app

    client = TestClient(app)
    response = client.get("/api/health")

    assert response.status_code == 200
    data = response.json()

    assert "status" in data
    assert "healthy" in data
    assert "health_tier" in data
    assert "infra" in data["health_tier"]
    assert "worker" in data["health_tier"]
    assert "freshness" in data["health_tier"]
    assert "health_reasons" in data
    assert "data_age_human" in data
    assert "signals_cached" in data
    assert isinstance(data["signals_cached"], int)
