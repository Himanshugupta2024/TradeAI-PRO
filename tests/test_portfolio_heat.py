"""Risk hardening tests: portfolio heat, correlation sizing, and circuit breaker."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.execution.paper_trading import PaperTradingEngine
from config import settings


def _engine() -> tuple[PaperTradingEngine, str]:
    tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tf.close()
    return PaperTradingEngine(initial_capital=100000, log_file=tf.name), tf.name


def test_blocks_trade_when_portfolio_heat_exceeds_limit(monkeypatch):
    monkeypatch.setattr(settings, "MAX_PORTFOLIO_HEAT_PCT", 0.03)

    engine, path = _engine()
    try:
        # First trade uses ~2% risk and should pass.
        t1 = engine.execute("BTC/USDT", "LONG", 100, 99, 103)
        assert t1 is not None

        # Second equivalent trade would push total projected heat to ~4% and be blocked.
        t2 = engine.execute("RELIANCE", "LONG", 100, 99, 103)
        assert t2 is None

        assert 0.015 <= engine.get_portfolio_heat_pct() <= 0.025
    finally:
        os.unlink(path)


def test_halves_position_size_for_correlated_open_trade(monkeypatch):
    monkeypatch.setattr(settings, "CORRELATION_GROUPS", [["BTCUSDT", "ETHUSDT"]])
    monkeypatch.setattr(settings, "MAX_PORTFOLIO_HEAT_PCT", 0.10)

    engine, path = _engine()
    try:
        t1 = engine.execute("BTC/USDT", "LONG", 100, 99, 103)
        assert t1 is not None
        assert t1.quantity == 2000.0

        # Correlated symbol should size down by 50%.
        t2 = engine.execute("ETH/USDT", "LONG", 100, 99, 103)
        assert t2 is not None
        assert t2.quantity == 1000.0
    finally:
        os.unlink(path)


def test_circuit_breaker_blocks_and_can_be_rebased(monkeypatch):
    monkeypatch.setattr(settings, "CIRCUIT_BREAKER_THRESHOLD", 0.12)

    engine, path = _engine()
    try:
        # Simulate 15% drawdown.
        engine.capital = 85000

        blocked = engine.execute("BTC/USDT", "LONG", 100, 99, 103)
        assert blocked is None
        assert engine.get_risk_state()["circuit_breaker_active"] is True

        reset_state = engine.reset_circuit_breaker(rebase_capital=True)
        assert reset_state["circuit_breaker_active"] is False

        allowed = engine.execute("BTC/USDT", "LONG", 100, 99, 103)
        assert allowed is not None
    finally:
        os.unlink(path)
