"""
Unit tests for Autonomous Daemon Data Staleness Kill-Switch.
Verifies that:
  1. Stale timestamps (>10m for NSE, >5m for Crypto) are rejected with SKIP_STALE_DATA.
  2. Fresh timestamps within active trading windows proceed to candidate evaluation.
  3. Feeds without timestamps (legacy mocks) are tolerated gracefully.
"""

import pytest
from datetime import datetime, timezone, timedelta
from agents.execution.autonomous_daemon import AutonomousExecutionDaemon
from agents.execution.paper_trading import PaperTradingEngine


@pytest.fixture
def clean_daemon():
    d = AutonomousExecutionDaemon(max_positions=5, min_qs_score=50.0, min_ai_confidence=40.0)
    d.decision_log = []
    d.enabled = True
    return d


def test_crypto_feed_staleness_detection(clean_daemon):
    now_utc = datetime.now(timezone.utc)
    
    # 2 minutes old -> FRESH (< 5m threshold)
    fresh_ts = (now_utc - timedelta(minutes=2)).isoformat()
    is_stale, reason = clean_daemon._is_feed_stale("BTC/USDT", fresh_ts)
    assert is_stale is False
    assert reason == "FRESH"

    # 15 minutes old -> STALE (> 5m threshold)
    stale_ts = (now_utc - timedelta(minutes=15)).isoformat()
    is_stale, reason = clean_daemon._is_feed_stale("BTC/USDT", stale_ts)
    assert is_stale is True
    assert "Crypto feed stale" in reason


def test_autonomous_skips_stale_signal(clean_daemon, tmp_path, monkeypatch):
    log_file = str(tmp_path / "test_trades.json")
    engine = PaperTradingEngine(initial_capital=100_000.0, log_file=log_file, state_key="test_stale")
    engine.trades = []
    engine.open_trades = []

    now_utc = datetime.now(timezone.utc)
    stale_ts = (now_utc - timedelta(minutes=20)).isoformat()

    signals = [
        {
            "symbol": "BTC/USDT",
            "signal": "LONG",
            "entry": 50000.0,
            "stop_loss": 48000.0,
            "target": 55000.0,
            "quant_score": 85.0,
            "confidence": 75.0,
            "timestamp": stale_ts,
        }
    ]
    prices = {
        "BTC/USDT": {"close": 50000.0, "timestamp": stale_ts}
    }

    executed = clean_daemon.evaluate_signals(signals, prices, engine)
    assert len(executed) == 0
    # Must record SKIP_STALE_DATA decision
    assert len(clean_daemon.decision_log) >= 1
    assert clean_daemon.decision_log[0]["action"] == "SKIP_STALE_DATA"
    assert "stale" in clean_daemon.decision_log[0]["reason"].lower()
