"""
Test Section 6B API Endpoints:
  - GET /api/research/next-day-picks
  - POST /api/research/next-day-picks/generate
  - GET /api/research/next-day-picks/history
  - GET /api/research/next-day-picks/{report_id}
  - POST /api/research/next-day-picks/resolve
"""

from fastapi.testclient import TestClient
import pytest
from server import app

client = TestClient(app)

def test_get_next_day_picks_api():
    res = client.get("/api/research/next-day-picks")
    assert res.status_code == 200
    data = res.json()
    assert "report_id" in data
    assert "target_session_date" in data
    assert "picks" in data
    assert isinstance(data["picks"], list)


def test_get_next_day_picks_latest_alias_api():
    res = client.get("/api/research/next-day-picks/latest")
    assert res.status_code == 200
    data = res.json()
    assert "report_id" in data
    assert "target_session_date" in data
    assert "picks" in data


def test_generate_next_day_picks_api():
    res = client.post("/api/research/next-day-picks/generate", json={"force": True, "max_picks": 3})
    assert res.status_code == 200
    data = res.json()
    assert "report_id" in data
    assert data["picks_count"] <= 3


def test_next_day_picks_history_api():
    res = client.get("/api/research/next-day-picks/history")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "history" in data
    assert isinstance(data["history"], list)


def test_get_next_day_pick_by_id_api():
    # First get latest to know a valid report_id
    res_latest = client.get("/api/research/next-day-picks")
    report_id = res_latest.json()["report_id"]

    res = client.get(f"/api/research/next-day-picks/{report_id}")
    assert res.status_code == 200
    assert res.json()["report_id"] == report_id

    # 404 on invalid id
    res_404 = client.get("/api/research/next-day-picks/invalid-id-xyz")
    assert res_404.status_code == 404


def test_resolve_next_day_picks_api():
    res_latest = client.get("/api/research/next-day-picks")
    report = res_latest.json()
    report_id = report["report_id"]
    picks = report["picks"]

    if not picks:
        pytest.skip("No picks in report to resolve.")

    sym = picks[0]["symbol"]
    t1 = picks[0]["trade_plan"]["target_1"]
    stop = picks[0]["trade_plan"]["stop_loss"]

    payload = {
        "report_id": report_id,
        "actual_candles": {
            sym: {
                "open": picks[0]["trade_plan"]["trigger_price"],
                "high": t1 + 2.0,
                "low": stop + 1.0,
                "close": t1,
            }
        }
    }
    res = client.post("/api/research/next-day-picks/resolve", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["resolved_count"] >= 1
