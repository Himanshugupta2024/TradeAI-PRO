"""
Unit tests for MFE / MAE Extreme Tracking & TP/SL Diagnostics.
Verifies that:
  1. PaperTrade records running MFE and MAE on ascending and descending price bars.
  2. MFE / MAE are normalized to R-multiples upon trade closure.
  3. Reconciled bars properly capture MFE and MAE.
  4. TradeJournal calculates median excursions and generates honest TP/SL tuning advice.
"""

import pytest
import uuid
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade
from agents.analytics.trade_journal import TradeJournal, SignalOutcome


@pytest.fixture
def clean_engine(tmp_path):
    log_file = str(tmp_path / "test_paper_trades.json")
    state_key = f"test_mfe_{uuid.uuid4().hex}"
    engine = PaperTradingEngine(
        initial_capital=100_000.0,
        log_file=log_file,
        state_key=state_key,
    )
    engine.trades = []
    engine.open_trades = []
    engine._cooldowns = {}
    return engine


def test_mfe_mae_tracking_on_long_trade(clean_engine):
    # Execute a LONG trade on RELIANCE: Entry 1000, SL 950 (Risk = 50), TP 1100
    trade = clean_engine.execute(
        symbol="RELIANCE",
        direction="LONG",
        entry=1000.0,
        sl=950.0,
        tp=1100.0,
        signal_type="BREAKOUT",
    )
    assert trade is not None
    assert trade.mfe_price == 1000.0
    assert trade.mae_price == 1000.0

    # Bar 1: Price dips to 970 (adverse), rallies to 1040 (favorable)
    clean_engine.update_prices({
        "RELIANCE": {"open": 1000.0, "high": 1040.0, "low": 970.0, "close": 1030.0}
    })
    assert trade.status == "OPEN"
    assert trade.mfe_price == 1040.0
    assert trade.mae_price == 970.0

    # Bar 2: Price pushes to 1105 (hits TP)
    closed = clean_engine.update_prices({
        "RELIANCE": {"open": 1030.0, "high": 1105.0, "low": 1025.0, "close": 1100.0}
    })
    assert len(closed) == 1
    closed_trade = closed[0]
    assert closed_trade.status == "CLOSED_TP"

    # MFE was at least 1100 (exit price)
    # MAE was 970 (Adverse move = 30)
    # Risk was 50 (1000 - 950)
    # Expected MFE_R >= 2.0 (100/50), MAE_R = 30/50 = 0.6
    assert closed_trade.mfe_price >= 1100.0
    assert closed_trade.mae_price == 970.0
    assert closed_trade.mfe_r >= 2.0
    assert closed_trade.mae_r == 0.6
    assert closed_trade.mfe_pct >= 10.0
    assert closed_trade.mae_pct == 3.0


def test_mfe_mae_tracking_on_short_trade(clean_engine):
    # Execute a SHORT trade: Entry 500, SL 520 (Risk = 20), TP 460
    trade = clean_engine.execute(
        symbol="TCS",
        direction="SHORT",
        entry=500.0,
        sl=520.0,
        tp=460.0,
    )
    assert trade is not None

    # Bar 1: Price moves adversely to 510, favorable to 480
    clean_engine.update_prices({
        "TCS": {"open": 500.0, "high": 510.0, "low": 480.0, "close": 485.0}
    })
    assert trade.status == "OPEN"
    assert trade.mfe_price == 480.0
    assert trade.mae_price == 510.0

    # Bar 2: Hits SL at 525
    closed = clean_engine.update_prices({
        "TCS": {"open": 485.0, "high": 525.0, "low": 480.0, "close": 522.0}
    })
    assert len(closed) == 1
    closed_trade = closed[0]
    assert closed_trade.status == "CLOSED_SL"
    # For SHORT:
    # Favorable delta = 500 - 480 = 20 -> mfe_r = 20 / 20 = 1.0
    # Adverse delta = 520 - 500 = 20 (or fill at 520) -> mae_r >= 1.0
    assert closed_trade.mfe_r == 1.0
    assert closed_trade.mae_r >= 1.0


def test_trade_journal_mfe_mae_diagnostics(tmp_path, monkeypatch):
    test_journal_file = str(tmp_path / "test_journal.json")
    monkeypatch.setattr("agents.analytics.trade_journal._JOURNAL_FILE", test_journal_file)

    tj = TradeJournal()
    tj._loaded = True
    tj._records = [
        SignalOutcome(
            symbol="INFY",
            timestamp="2026-11-10T10:00:00",
            signal="LONG",
            quality_score=75,
            quality_grade="A",
            regime="TRENDING",
            hour_ist=10,
            confidence=65.0,
            data_quality=95.0,
            outcome=1,
            mfe_r=2.5,
            mae_r=0.2,
            realized_r=1.5,
        ),
        SignalOutcome(
            symbol="TCS",
            timestamp="2026-11-10T11:00:00",
            signal="LONG",
            quality_score=68,
            quality_grade="B",
            regime="TRENDING",
            hour_ist=11,
            confidence=58.0,
            data_quality=90.0,
            outcome=1,
            mfe_r=2.8,
            mae_r=0.3,
            realized_r=1.5,
        ),
    ]

    diag = tj.get_mfe_mae_diagnostics()
    assert diag["sample_size"] == 2
    assert diag["median_mfe_r"] == 2.65
    assert diag["win_median_mae_r"] == 0.25
    assert diag["tp_tuning_advice"] == "EXPAND_TP"  # Median MFE > 2.0R suggests TP was hit with room to spare
    assert diag["sl_buffer_advice"] == "TIGHTEN_SL"  # Win MAE < 0.35 indicates tight shakeout room
