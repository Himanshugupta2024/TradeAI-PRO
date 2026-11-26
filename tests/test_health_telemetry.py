"""
Unit and integration tests for Phase 1 Observability & Telemetry.
Tests:
  - build_sha, boot_time, config_hash metadata envelope.
  - /api/health and /api/health/live metadata contracts.
  - db.health_store probe recording, querying, aggregation, and pruning.
  - /api/telemetry/probe and /api/telemetry/probes HTTP endpoints.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from api.routers.health import (
    _get_build_sha,
    _get_boot_time,
    _get_config_hash,
    router,
)
from db.health_store import (
    record_health_probe,
    get_recent_probes,
    get_health_summary,
    prune_old_probes,
)


def test_metadata_envelope_generation():
    """Verify that build_sha, boot_time, and config_hash generate valid non-empty tokens."""
    sha = _get_build_sha()
    assert isinstance(sha, str)
    assert len(sha) >= 3

    boot = _get_boot_time()
    assert isinstance(boot, str)
    # Must be valid ISO timestamp
    dt = datetime.fromisoformat(boot)
    assert dt.year >= 2024

    cfg_hash = _get_config_hash()
    assert isinstance(cfg_hash, str)
    assert len(cfg_hash) == 12
    # Deterministic: repeated call gives same hash
    assert cfg_hash == _get_config_hash()


def test_health_store_record_and_query():
    """Verify writing health probes and retrieving recent history."""
    test_sha = "testsha1"
    test_cfg = "testcfg12345"

    ok = record_health_probe(
        box="edge",
        status="HEALTHY",
        latency_ms=115.4,
        memory_rss_mb=245.5,
        uptime_seconds=3600.0,
        feed_lag_seconds=120.0,
        sidecar_active=True,
        build_sha=test_sha,
        config_hash=test_cfg,
        details={"colo": "delhi", "status_code": 200},
    )
    assert ok is True

    recent = get_recent_probes(box="edge", limit=10, hours=1)
    assert len(recent) >= 1
    latest = recent[0]
    assert latest["box"] == "edge"
    assert latest["status"] == "HEALTHY"
    assert latest["latency_ms"] == 115.4
    assert latest["build_sha"] == test_sha
    assert latest["config_hash"] == test_cfg
    assert latest["sidecar_active"] is True
    assert latest["details"]["colo"] == "delhi"


def test_health_store_summary_aggregation():
    """Verify health summary computes uptime percentage, peak RSS, and avg latency."""
    # Write a degraded probe and a healthy probe
    record_health_probe(
        box="box_a",
        status="HEALTHY",
        latency_ms=50.0,
        memory_rss_mb=250.0,
        uptime_seconds=100.0,
        feed_lag_seconds=30.0,
        sidecar_active=True,
    )
    record_health_probe(
        box="box_a",
        status="DEGRADED",
        latency_ms=150.0,
        memory_rss_mb=310.0,
        uptime_seconds=150.0,
        feed_lag_seconds=900.0,
        sidecar_active=False,
    )

    summary = get_health_summary(hours=1)
    assert summary["total_probes"] >= 2
    assert summary["peak_rss_mb"] >= 310.0
    assert summary["uptime_pct"] > 0.0
    assert summary["avg_latency_ms"] > 0.0
    assert summary["max_feed_lag_seconds"] >= 900.0


def test_prune_old_probes():
    """Verify old probe pruning runs without error."""
    pruned = prune_old_probes(retention_days=7)
    assert isinstance(pruned, int)
    assert pruned >= 0


def test_health_endpoints_schema_contract(monkeypatch):
    """Test /api/health/live and /api/telemetry routes via TestClient."""
    from fastapi import FastAPI
    monkeypatch.setenv("CRON_SECRET", "test-cron-secret")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # 1. GET /api/health/live
    live_res = client.get("/api/health/live")
    assert live_res.status_code == 200
    live_data = live_res.json()
    assert live_data["status"] == "alive"
    assert "build_sha" in live_data
    assert "boot_time" in live_data
    assert "config_hash" in live_data
    assert "uptime_seconds" in live_data

    # 2. POST /api/telemetry/probe
    probe_payload = {
        "box": "edge",
        "status": "HEALTHY",
        "latency_ms": 88.5,
        "details": {"source": "test_client"},
    }
    probe_res = client.post(
        "/api/telemetry/probe",
        json=probe_payload,
        headers={"X-Cron-Secret": "test-cron-secret"},
    )
    assert probe_res.status_code == 200
    probe_data = probe_res.json()
    assert probe_data["recorded"] is True
    assert "build_sha" in probe_data

    # Give async task a brief moment to commit
    time.sleep(0.1)

    # 3. GET /api/telemetry/probes
    history_res = client.get("/api/telemetry/probes?hours=1&limit=5")
    assert history_res.status_code == 200
    hist_data = history_res.json()
    assert "summary" in hist_data
    assert "probes" in hist_data
    assert hist_data["summary"]["total_probes"] >= 1
