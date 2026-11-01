"""Test portfolio, performance, and trade ledger endpoints."""

import pytest
from starlette.testclient import TestClient
from server import app
from agents.execution.paper_trading import PaperTradingEngine
from api import service as svc


@pytest.fixture
def fresh_engine(tmp_path, monkeypatch):
    test_trades_file = str(tmp_path / "test_portfolio_trades.json")
    engine = PaperTradingEngine(initial_capital=100000.0, log_file=test_trades_file)
    monkeypatch.setattr(svc, "_engine", engine)
    return engine


def test_portfolio_endpoint_structure_and_metrics(fresh_engine):
    client = TestClient(app)

    # Place a trade to have open position
    trade = fresh_engine.execute(
        symbol="INFY",
        direction="LONG",
        entry=1500.0,
        sl=1470.0,
        tp=1560.0,
        atr=30.0,
        signal_type="SWING",
        regime="TRENDING",
    )
    assert trade is not None

    resp = client.get("/api/portfolio")
    assert resp.status_code == 200
    data = resp.json()

    assert "open_positions" in data
    assert data["total_open"] == 1
    assert "total_unrealized_pnl" in data
    assert "risk_metrics" in data
    assert "drawdown" in data
    assert "concentration_warnings" in data

    pos = data["open_positions"][0]
    assert pos["symbol"] == "INFY"
    assert pos["direction"] == "LONG"
    assert "r_multiple" in pos
    assert "heat_contribution_pct" in pos
    assert "time_in_trade_human" in pos
    assert "sl_distance_pct" in pos


def test_performance_endpoint_structure(fresh_engine):
    client = TestClient(app)

    resp = client.get("/api/performance")
    assert resp.status_code == 200
    data = resp.json()

    assert "metrics" in data
    assert "equity_curve" in data
    assert "trade_distribution" in data
    assert "exit_reasons" in data
    assert "monthly_pnl" in data
    assert "symbol_breakdown" in data
    assert "recent_trades" in data


def test_trades_endpoint_enrichment(fresh_engine):
    client = TestClient(app)

    # Place and close a trade
    trade = fresh_engine.execute(
        symbol="WIPRO",
        direction="LONG",
        entry=450.0,
        sl=440.0,
        tp=470.0,
        atr=10.0,
    )
    assert trade is not None
    trade_id = trade.id

    close_resp = client.post(f"/api/paper/close/{trade_id}", json={"exit_price": 465.0})
    assert close_resp.status_code == 200

    resp = client.get("/api/trades?limit=10")
    assert resp.status_code == 200
    trades_data = resp.json()
    assert "trades" in trades_data
    assert len(trades_data["trades"]) >= 1

    t0 = trades_data["trades"][0]
    assert "timestamp" in t0
    assert len(t0["timestamp"]) > 0
    assert "exit_reason" in t0
    assert "CLOSED" in t0["exit_reason"]


def test_manual_close_trade_flow(fresh_engine):
    client = TestClient(app)

    # 1. Place trade
    trade = fresh_engine.execute(
        symbol="SBIN",
        direction="LONG",
        entry=800.0,
        sl=780.0,
        tp=840.0,
        atr=20.0,
    )
    assert trade is not None
    tid = trade.id

    # Verify open in portfolio
    p1 = client.get("/api/portfolio").json()
    assert any(p["id"] == tid for p in p1["open_positions"])

    # 2. Close manually
    close_resp = client.post(f"/api/paper/close/{tid}", json={"exit_price": 820.0})
    assert close_resp.status_code == 200
    c_data = close_resp.json()
    assert c_data["status"] == "closed"
    assert c_data["trade"]["status"] == "CLOSED_MANUAL"

    # 3. Verify closed in portfolio
    p2 = client.get("/api/portfolio").json()
    assert not any(p["id"] == tid for p in p2["open_positions"])
