"""
Unit tests for the Enhanced Signal Scorecard & Edge Performance Attribution Engine.
"""

from __future__ import annotations

import pytest
from agents.analytics.trade_journal import SignalOutcome, TradeJournal, get_journal
from agents.market_intelligence.watchlist_analyzer import calculate_trade_levels


def test_scorecard_expectancy_and_profit_factor_math(monkeypatch):
    """Verify exact mathematical calculation of Expectancy, Profit Factor, and Payoff Ratio."""
    journal = TradeJournal()
    journal._loaded = True
    monkeypatch.setattr(journal, "_hydrate_from_paper_trades", lambda: None)
    journal._records = []

    # 4 Wins (+2.0R each) and 2 Losses (-1.0R each)
    for i in range(4):
        journal._records.append(
            SignalOutcome(
                symbol="RELIANCE",
                timestamp=f"2026-01-0{i+1}T10:00:00",
                signal="LONG",
                quality_score=75,
                quality_grade="A",
                regime="TRENDING",
                hour_ist=10,
                confidence=75.0,
                data_quality=100.0,
                outcome=1,
                realized_r=2.0,
            )
        )
    for i in range(2):
        journal._records.append(
            SignalOutcome(
                symbol="TCS",
                timestamp=f"2026-01-0{i+5}T10:00:00",
                signal="SHORT",
                quality_score=70,
                quality_grade="B",
                regime="TRENDING",
                hour_ist=10,
                confidence=70.0,
                data_quality=100.0,
                outcome=-1,
                realized_r=-1.0,
            )
        )

    sc = journal.get_scorecard()

    # Total resolved: 6 (4W, 2L)
    assert sc["total_signals"] == 6
    assert sc["resolved"] == 6
    assert sc["wins"] == 4
    assert sc["losses"] == 2
    assert sc["resolved_win_rate"] == round(4 / 6, 4)

    # Total gain: 4 * 2.0 = 8.0R
    # Total loss: 2 * 1.0 = 2.0R
    # Avg Win: 2.0R, Avg Loss: 1.0R
    # Profit Factor: 8.0 / 2.0 = 4.0
    # Payoff Ratio: 2.0 / 1.0 = 2.0
    # Expectancy: (4/6 * 2.0) - (2/6 * 1.0) = 1.333 - 0.333 = 1.0R
    assert sc["profit_factor"] == 4.0
    assert sc["payoff_ratio"] == 2.0
    assert sc["expectancy_r"] == 1.0
    assert sc["max_consecutive_losses"] == 2
    assert sc["max_drawdown_r"] == 2.0


def test_get_edge_score_ratings():
    """Verify get_edge_score accurately tags strong vs negative expectancy regimes."""
    journal = TradeJournal()
    journal._loaded = True
    journal._records = []

    # Populate 10 consecutive wins in TRENDING regime
    for i in range(10):
        journal._records.append(
            SignalOutcome(
                symbol="RELIANCE",
                timestamp=f"2026-02-0{i+1}T10:00:00",
                signal="LONG",
                quality_score=80,
                quality_grade="A",
                regime="TRENDING",
                hour_ist=10,
                confidence=80.0,
                data_quality=100.0,
                outcome=1,
                realized_r=2.0,
            )
        )

    # Populate 8 consecutive losses in RANGING regime
    for i in range(8):
        journal._records.append(
            SignalOutcome(
                symbol="INFY",
                timestamp=f"2026-02-1{i+1}T10:00:00",
                signal="SHORT",
                quality_score=70,
                quality_grade="B",
                regime="RANGING",
                hour_ist=11,
                confidence=65.0,
                data_quality=100.0,
                outcome=-1,
                realized_r=-1.0,
            )
        )

    # TRENDING should yield STRONG_EDGE
    edge_trend = journal.get_edge_score(symbol="RELIANCE", regime="TRENDING")
    assert edge_trend["edge_rating"] == "STRONG_EDGE"
    assert edge_trend["recommended_action"] == "BOOST_SETUP"
    assert edge_trend["expectancy_r"] > 0.4

    # RANGING should yield NEGATIVE_EXPECTANCY
    edge_range = journal.get_edge_score(symbol="INFY", regime="RANGING")
    assert edge_range["edge_rating"] == "NEGATIVE_EXPECTANCY"
    assert edge_range["recommended_action"] == "SUPPRESS_TO_HOLD"
    assert edge_range["expectancy_r"] < 0.0


def test_edge_gating_suppresses_trade_levels(monkeypatch):
    """Verify calculate_trade_levels suppresses actionable trade if regime expectancy is negative."""
    journal = get_journal()
    journal._loaded = True
    monkeypatch.setattr(journal, "_hydrate_from_paper_trades", lambda: None)
    journal._records = []

    # Insert losing records to trigger negative expectancy for INFY in RANGING
    for i in range(8):
        journal._records.append(
            SignalOutcome(
                symbol="INFY",
                timestamp=f"2026-03-0{i+1}T10:00:00",
                signal="SHORT",
                quality_score=70,
                quality_grade="B",
                regime="RANGING",
                hour_ist=11,
                confidence=65.0,
                data_quality=100.0,
                outcome=-1,
                realized_r=-1.0,
            )
        )

    # Even with high score (75) and ADX (25), setup should be suppressed to HOLD
    levels = calculate_trade_levels(
        close=1500.0,
        high_last=1510.0,
        low_last=1490.0,
        atr14=20.0,
        supports=[1480.0],
        resistances=[1530.0],
        trend_bias="BEARISH",
        total_score=75,
        adx=25.0,
        symbol="INFY",
    )

    assert levels["is_actionable"] is False
    assert levels["signal"] == "HOLD"
    assert levels.get("edge_gated") is True
    assert "Negative statistical edge" in levels.get("edge_rationale", "")


@pytest.mark.asyncio
async def test_bootstrap_scorecard_from_history(monkeypatch):
    """Verify scorecard bootstrapper generates resolved trade records from historical daily bars."""
    import pandas as pd
    import numpy as np
    from agents.analytics.scorecard_bootstrapper import bootstrap_scorecard_from_history
    from data.providers.yahoo import YahooProvider

    # Generate 150 daily bars with an upward trending sequence
    dates = pd.date_range("2025-01-01", periods=150, freq="B")
    close = np.linspace(1000.0, 1500.0, 150) + np.sin(np.linspace(0, 10, 150)) * 20.0
    high = close + 10.0
    low = close - 10.0
    volume = np.full(150, 1000000.0)

    mock_df = pd.DataFrame(
        {"timestamp": dates, "open": close, "high": high, "low": low, "close": close, "volume": volume}
    )

    async def mock_get_batch_ohlcv(self, symbols, period="1y", interval="1d"):
        return {s: mock_df.copy() for s in symbols}

    monkeypatch.setattr(YahooProvider, "get_batch_ohlcv", mock_get_batch_ohlcv)

    journal = get_journal()
    journal._loaded = True
    journal._records = []
    monkeypatch.setattr(journal, "_save", lambda: None)
    monkeypatch.setattr(journal, "_hydrate_from_paper_trades", lambda: None)

    added = await bootstrap_scorecard_from_history(symbols=["RELIANCE", "TCS"], min_trades=5, force=True)

    assert added > 0
    sc = journal.get_scorecard()
    assert sc["total_signals"] > 0
    assert (sc["resolved"] + sc["timeouts"]) > 0
