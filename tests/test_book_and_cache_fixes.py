"""Unit tests for Priority 2 legacy book quarantine, edge cache header scoping, and audit checks."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_security_cache_headers_allow_public_risk_gauge_but_protect_risk_state():
    from server import app

    client = TestClient(app)

    pub_resp = client.get("/api/edge/risk-gauge")
    assert pub_resp.status_code == 200
    assert "no-store" not in (pub_resp.headers.get("cache-control") or "").lower()

    priv_resp = client.get("/api/risk/state")
    assert priv_resp.status_code == 200
    cc_priv = (priv_resp.headers.get("cache-control") or "").lower()
    assert "private" in cc_priv and "no-store" in cc_priv


def test_classify_legacy_trade_quarantines_uncosted_zero_net_and_preserves_profit_trail():
    from agents.execution.paper_trading import classify_legacy_trade

    # Nov 2 off-hours uncosted zeroed-net legacy row (#22/#23/#25 shape)
    dq_uncosted, reason_uncosted = classify_legacy_trade(
        {
            "id": 22,
            "symbol": "INFY",
            "direction": "LONG",
            "status": "CLOSED_TRAIL",
            "entry_price": 1179.15,
            "stop_loss": 1175.46,
            "initial_stop_loss": 0.0,
            "stop_raw": 0.0,
            "target": 1190.78,
            "entry_time": "2026-11-02T22:58:40+05:30",
            "total_pnl": -77.81,
            "net_pnl": 0.0,
            "estimated_costs": 0.0,
        }
    )
    assert dq_uncosted == "LEGACY_EXCLUDED"
    assert reason_uncosted == "UNCOSTED_OFFHOURS_LEGACY"

    # Regular-session synthetic fixture without net_pnl stays VALID
    dq_reg, _ = classify_legacy_trade(
        {
            "id": 999,
            "symbol": "IRCTC",
            "direction": "LONG",
            "status": "CLOSED_TP",
            "entry_price": 400.0,
            "stop_loss": 380.0,
            "entry_time": "2026-11-05T10:00:00+05:30",
            "total_pnl": 300.0,
        }
    )
    assert dq_reg == "VALID"

    # Winning trailing-stop trade (#47 shape: initial_stop_loss < entry < stop_loss)
    dq_trail, reason_trail = classify_legacy_trade(
        {
            "id": 47,
            "symbol": "TATAMOTORS",
            "direction": "LONG",
            "status": "CLOSED_EOD_SQUAREOFF",
            "entry_price": 440.55,
            "stop_loss": 442.21,
            "initial_stop_loss": 437.81,
            "stop_raw": 437.81,
            "target": 448.8,
            "total_pnl": 242.66,
            "net_pnl": 221.95,
            "estimated_costs": 20.71,
        }
    )
    assert dq_trail == "VALID"
    assert reason_trail == ""
