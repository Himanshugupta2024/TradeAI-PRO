"""
Unit and integration tests for AutonomousExecutionDaemon, EOD squareoff, and Self-Learning EOD review.
"""

import uuid
import pytest
from datetime import datetime, timezone, timedelta
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade
from agents.execution.autonomous_daemon import AutonomousExecutionDaemon, get_autonomous_daemon
from agents.analytics.feedback_loop import FeedbackAnalyser, WeightAdapter

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def clean_engine(tmp_path):
    """Create an isolated clean engine with dedicated state key and temp log file."""
    log_file = str(tmp_path / "test_paper_trades.json")
    state_key = f"test_auto_{uuid.uuid4().hex}"
    engine = PaperTradingEngine(
        initial_capital=500_000.0,
        log_file=log_file,
        state_key=state_key,
    )
    engine.trades = []
    engine.open_trades = []
    engine._cooldowns = {}
    return engine


@pytest.fixture
def daemon():
    """Create a fresh daemon instance with test thresholds."""
    d = AutonomousExecutionDaemon(
        max_positions=5,
        min_qs_score=60.0,
        min_ai_confidence=50.0,
    )
    d.decision_log = []
    d.enabled = True
    return d


def test_daemon_initialization(daemon):
    assert daemon.enabled is True
    assert daemon.max_positions == 5
    assert daemon.min_qs_score == 60.0
    assert daemon.min_ai_confidence == 50.0
    assert isinstance(daemon.decision_log, list)


def test_daemon_evaluates_and_executes_high_conviction_signals(daemon, clean_engine):
    # Use reasonable stop losses (e.g. 10% distance) so position sizes remain within portfolio limits
    signals = [
        # Setup 1: High conviction - should execute
        {
            "symbol": "INFY",
            "signal": "LONG",
            "entry": 1500.0,
            "stop_loss": 1350.0,
            "target": 1800.0,
            "quant_score": 75.0,
            "confidence": 65.0,
            "atr": 30.0,
            "strategy": "BREAKOUT",
            "regime": "BULLISH_TRENDING",
        },
        # Setup 2: Below QS score threshold - should skip
        {
            "symbol": "TCS",
            "signal": "LONG",
            "entry": 3500.0,
            "stop_loss": 3150.0,
            "target": 4200.0,
            "quant_score": 52.0,  # Below 60
            "confidence": 60.0,
            "atr": 60.0,
        },
        # Setup 3: Below AI confidence threshold - should skip
        {
            "symbol": "RELIANCE",
            "signal": "LONG",
            "entry": 2500.0,
            "stop_loss": 2250.0,
            "target": 3000.0,
            "quant_score": 80.0,
            "confidence": 42.0,  # Below 50
            "atr": 50.0,
        },
        # Setup 4: Valid Crypto setup - should execute
        {
            "symbol": "BTC/USDT",
            "signal": "LONG",
            "entry": 65000.0,
            "stop_loss": 58500.0,
            "target": 78000.0,
            "quant_score": 70.0,
            "confidence": 58.0,
            "atr": 1500.0,
        },
    ]

    prices = {
        "INFY": {"close": 1500.0, "atr": 30.0},
        "BTC/USDT": {"close": 65000.0, "atr": 1500.0},
    }

    executed = daemon.evaluate_signals(signals, prices, clean_engine)
    assert len(executed) == 2

    symbols_executed = {t.symbol for t in executed}
    assert "INFY" in symbols_executed
    assert "BTC/USDT" in symbols_executed

    # Verify decision log entries
    assert len(daemon.decision_log) == 2
    actions = {d["action"] for d in daemon.decision_log}
    assert "AUTO_ENTER" in actions


def test_daemon_respects_max_concurrency_cap(daemon, clean_engine):
    daemon.max_positions = 2

    # Put 2 dummy open trades with reasonable size
    t1 = clean_engine.execute("INFY", "LONG", 1500.0, 1350.0, 1800.0, 30.0)
    t2 = clean_engine.execute("TCS", "LONG", 3500.0, 3150.0, 4200.0, 60.0)
    assert len(clean_engine.open_trades) == 2

    # Attempt to evaluate another high-conviction signal
    signals = [{
        "symbol": "RELIANCE",
        "signal": "LONG",
        "entry": 2500.0,
        "stop_loss": 2250.0,
        "target": 3000.0,
        "quant_score": 85.0,
        "confidence": 75.0,
    }]
    prices = {"RELIANCE": {"close": 2500.0}}

    executed = daemon.evaluate_signals(signals, prices, clean_engine)
    assert len(executed) == 0  # No slots left
    assert len(clean_engine.open_trades) == 2


def test_engine_check_eod_squareoff_logic(clean_engine, monkeypatch):
    """Test that intraday equity positions auto-square off at 15:15 IST, while crypto holds."""
    mock_now = datetime(2026, 11, 4, 15, 20, 0, tzinfo=_IST)
    monkeypatch.setattr("agents.execution.paper_trading.datetime", type("MockDT", (), {
        "now": lambda tz=None: mock_now,
        "fromtimestamp": datetime.fromtimestamp,
    }))

    # Open 1 equity and 1 crypto position
    eq_trade = clean_engine.execute("INFY", "LONG", 1500.0, 1350.0, 1800.0, 30.0, signal_type="INTRADAY")
    crypto_trade = clean_engine.execute("BTC/USDT", "LONG", 65000.0, 58500.0, 78000.0, 1500.0)

    assert len(clean_engine.open_trades) == 2

    prices = {
        "INFY": {"close": 1510.0},
        "BTC/USDT": {"close": 65500.0},
    }

    closed = clean_engine.check_eod_squareoff(prices)
    assert len(closed) == 1
    assert closed[0].symbol == "INFY"
    assert closed[0].status == "CLOSED_EOD_SQUAREOFF"
    assert closed[0].exit_price == 1510.0

    # Crypto position remains open for 24/7 trailing
    remaining_open = [t.symbol for t in clean_engine.open_trades]
    assert remaining_open == ["BTC/USDT"]


def test_feedback_analyser_eod_review():
    """Test run_daily_eod_review returns structured summary and updates weights."""
    analyser = FeedbackAnalyser()
    review = analyser.run_daily_eod_review()
    assert isinstance(review, dict)
    assert "review_date" in review
    assert "today_trade_count" in review
    assert "today_win_rate" in review
    assert "updated_weights" in review
    assert isinstance(review["updated_weights"], dict)


def test_autonomous_status_and_feed(daemon, clean_engine):
    status = daemon.get_status(clean_engine)
    assert status["enabled"] is True
    assert status["max_positions"] == 5
    assert "open_positions" in status

    daemon.record_decision(
        action="AUTO_ENTER",
        symbol="TCS",
        direction="LONG",
        price=3500.0,
        reason="Test entry",
        qs_score=75.0,
        ai_confidence=60.0,
    )
    feed = daemon.get_decision_feed(10)
    assert len(feed) == 1
    assert feed[0]["symbol"] == "TCS"
    assert feed[0]["action"] == "AUTO_ENTER"
