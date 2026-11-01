import pytest
from starlette.testclient import TestClient
from server import app
from agents.execution.paper_trading import PaperTradingEngine
from api import service as svc


@pytest.fixture
def test_engine(tmp_path, monkeypatch):
    test_trades_file = str(tmp_path / "test_trades.json")
    engine = PaperTradingEngine(initial_capital=100000.0, log_file=test_trades_file)
    monkeypatch.setattr(svc, "_engine", engine)
    return engine


def test_paper_engine_stats_includes_open_trades(tmp_path):
    test_trades_file = str(tmp_path / "test_trades.json")
    engine = PaperTradingEngine(initial_capital=100000.0, log_file=test_trades_file)
    stats_empty = engine.get_stats()
    assert "open_trades" in stats_empty
    assert stats_empty["open_trades"] == 0
    assert stats_empty["total_trades"] == 0

    # Execute a trade
    trade = engine.execute(
        symbol="RELIANCE",
        direction="LONG",
        entry=1200.0,
        sl=1180.0,
        tp=1240.0,
        atr=20.0,
        signal_type="MANUAL",
        regime="TRENDING",
    )
    assert trade is not None
    assert trade.status == "OPEN"

    stats_with_open = engine.get_stats()
    assert stats_with_open["open_trades"] == 1
    # total_trades only counts closed trades in traditional stats
    assert stats_with_open["total_trades"] == 0


def test_api_paper_trade_flow(test_engine):
    client = TestClient(app)

    # 1. Place a trade
    payload = {
        "symbol": "TCS",
        "direction": "LONG",
        "entry": 3500.0,
        "stop_loss": 3450.0,
        "target": 3600.0,
        "atr": 50.0,
        "strategy": "BREAKOUT",
    }
    resp = client.post("/api/paper/trade", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["trade"]["symbol"] == "TCS"
    assert data["trade"]["direction"] == "LONG"
    trade_id = data["trade"]["id"]
    assert data["stats"]["open_trades"] >= 1

    # 2. Get open trades
    resp_open = client.get("/api/paper/open")
    assert resp_open.status_code == 200
    open_trades = resp_open.json().get("open_trades", [])
    assert any(t["id"] == trade_id for t in open_trades)

    # 3. Close the trade
    resp_close = client.post(f"/api/paper/close/{trade_id}", json={"exit_price": 3600.0})
    assert resp_close.status_code == 200
    close_data = resp_close.json()
    assert close_data["status"] == "closed"
    assert "CLOSED" in close_data["trade"]["status"]
    assert close_data["stats"]["total_trades"] >= 1
    assert close_data["stats"]["open_trades"] == 0
