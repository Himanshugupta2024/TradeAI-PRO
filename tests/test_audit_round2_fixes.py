"""
Unit tests verifying the 10 production hardening fixes for PaperTradingEngine.
"""
import math
import uuid
import pytest
from unittest.mock import patch

from agents.execution.paper_trading import PaperTradingEngine, PaperTrade
from config import settings


@pytest.fixture
def clean_engine(tmp_path):
    log_file = str(tmp_path / "test_paper_trades.json")
    state_key = f"test_round2_{uuid.uuid4().hex}"
    engine = PaperTradingEngine(
        initial_capital=100_000.0,
        log_file=log_file,
        state_key=state_key,
    )
    return engine


def test_missing_and_malformed_candle_bars_do_not_trigger_fake_sl_tp(clean_engine):
    """Bug 1: missing low=0 or malformed high/low must never trigger fake SL/TP."""
    # Place a LONG trade: entry=100, sl=95, tp=115
    long_trade = clean_engine.execute(
        symbol="INFY",
        direction="LONG",
        entry=100.0,
        sl=95.0,
        tp=115.0,
        atr=2.0,
    )
    assert long_trade is not None
    assert len(clean_engine.open_trades) == 1

    # Place a SHORT trade: entry=200, sl=210, tp=180
    short_trade = clean_engine.execute(
        symbol="TCS",
        direction="SHORT",
        entry=200.0,
        sl=210.0,
        tp=180.0,
        atr=4.0,
    )
    assert short_trade is not None
    assert len(clean_engine.open_trades) == 2

    # Malformed bars that previously triggered fake SL/TP due to default 0
    malformed_prices = {
        "INFY": {"high": 102.0, "low": None},  # Missing low -> would default to 0 <= 95 (fake SL)
        "TCS": {"high": 202.0, "low": 0.0},    # Low=0 <= 180 (fake TP)
    }
    closed = clean_engine.update_prices(malformed_prices)
    assert len(closed) == 0
    assert len(clean_engine.open_trades) == 2

    # More malformed cases: low > high, negative price, NaN
    more_bad_prices = {
        "INFY": {"high": 90.0, "low": 98.0},    # low > high
        "TCS": {"high": float("nan"), "low": 185.0},
    }
    closed2 = clean_engine.update_prices(more_bad_prices)
    assert len(closed2) == 0
    assert len(clean_engine.open_trades) == 2


def test_trailing_sl_persisted_to_db_and_file_even_if_trade_not_closed(clean_engine):
    """Bug 2: Trailing stop updates must trigger _save() and persist even if trade stays open."""
    trade = clean_engine.execute(
        symbol="RELIANCE",
        direction="LONG",
        entry=1000.0,
        sl=980.0,
        tp=1050.0,
        atr=10.0,
    )
    assert trade is not None
    original_sl = trade.stop_loss

    with patch.object(clean_engine, "_save", wraps=clean_engine._save) as mock_save:
        mock_save.reset_mock()

        # Feed price that moves trailing SL up
        # ATR=10, 1.5 ATR trailing = 15 points below peak
        # Price moves to high 1025, low 1010
        prices = {"RELIANCE": {"high": 1025.0, "low": 1010.0, "close": 1020.0}}
        atr_values = {"RELIANCE": 10.0}

        closed = clean_engine.update_prices(prices, atr_values=atr_values)
        assert len(closed) == 0  # Trade did NOT close
        assert trade.stop_loss > original_sl  # SL moved up
        # CRUCIAL: mock_save must have been called because state_changed=True
        assert mock_save.called


def test_partial_profit_persisted_even_if_trade_remains_open(clean_engine):
    """Bug 3: Partial TP hit must persist state changes (qty, capital, partial_taken) immediately."""
    trade = clean_engine.execute(
        symbol="HDFC",
        direction="LONG",
        entry=100.0,
        sl=95.0,
        tp=115.0,
        atr=2.0,
    )
    assert trade is not None
    orig_qty = trade.quantity
    partial_tp = trade.partial_target  # Midpoint ~ 107.5

    with patch.object(clean_engine, "_save", wraps=clean_engine._save) as mock_save:
        mock_save.reset_mock()

        # Bar reaches partial TP (high 108.0) with low 106.0 safely above trailing SL (104.0)
        prices = {"HDFC": {"high": 108.0, "low": 106.0, "close": 107.0}}
        closed = clean_engine.update_prices(prices, atr_values={"HDFC": 2.0})

        assert len(closed) == 0  # Still open
        assert trade.partial_taken is True
        assert trade.quantity < orig_qty
        # _save must have been invoked
        assert mock_save.called


def test_min_rr_enforced_after_regime_scaling(clean_engine):
    """Bug 4: Regime scaling must not push trade RR below minimum required."""
    # Suppose entry=100, sl=95 (risk=5), raw tp=110 (reward=10, raw rr=2.0).
    # If regime="RANGING" scales target by 0.7:
    # adjusted reward = 7, adjusted RR = 1.4.
    # If required_rr = 1.5, trade must be rejected.
    with patch.object(clean_engine, "_target_multiplier_for_regime", return_value=0.7):
        rejected_trade = clean_engine.execute(
            symbol="SBIN",
            direction="LONG",
            entry=100.0,
            sl=95.0,
            tp=110.0,
            min_rr=1.5,
            regime="RANGING",
        )
        assert rejected_trade is None
        assert "below minimum" in clean_engine.last_rejection_reason


def test_portfolio_heat_zero_when_trailing_sl_in_profit(clean_engine):
    """Bug 5: Portfolio heat should be 0 when trailing SL has moved beyond entry (profit locked)."""
    trade = clean_engine.execute(
        symbol="WIPRO",
        direction="LONG",
        entry=100.0,
        sl=95.0,
        tp=115.0,
        atr=2.0,
    )
    assert trade is not None
    # Initially SL is below entry, heat > 0
    assert clean_engine.get_portfolio_heat_pct() > 0.0

    # Move SL to 105.0 (in profit!)
    trade.stop_loss = 105.0
    # Downside risk from capital perspective is 0.0
    assert clean_engine.get_portfolio_heat_pct() == 0.0


def test_profit_factor_returns_none_when_no_losses(clean_engine):
    """Bug 6: Profit factor must be None (not 10000 or fake number) when gross_loss is 0."""
    trade = clean_engine.execute(
        symbol="ITC",
        direction="LONG",
        entry=100.0,
        sl=95.0,
        tp=115.0,
        atr=2.0,
    )
    # Manually close with profit
    clean_engine.close_manual(trade.id, exit_price=110.0)

    stats = clean_engine.get_stats()
    assert stats["total_trades"] == 1
    assert stats["winning_trades"] == 1
    assert stats["losing_trades"] == 0
    assert stats["profit_factor"] is None  # Undefined mathematically, not 10000!


def test_cooldown_symbol_normalization(clean_engine):
    """Bug 8: Cooldown must normalize symbol keys across exchange suffixes."""
    trade = clean_engine.execute(
        symbol="RELIANCE.NS",
        direction="LONG",
        entry=1000.0,
        sl=980.0,
        tp=1050.0,
        atr=10.0,
    )
    clean_engine.close_manual(trade.id, exit_price=990.0)

    # Next attempt using un-suffixed "RELIANCE" should be blocked by cooldown
    blocked_trade = clean_engine.execute(
        symbol="RELIANCE",
        direction="LONG",
        entry=1000.0,
        sl=980.0,
        tp=1050.0,
        atr=10.0,
    )
    assert blocked_trade is None
    assert "on cooldown" in clean_engine.last_rejection_reason


def test_manual_exit_price_validation(clean_engine):
    """Bug 9: close_manual must defensively reject non-positive or malformed exit prices."""
    trade = clean_engine.execute(
        symbol="TITAN",
        direction="LONG",
        entry=100.0,
        sl=95.0,
        tp=115.0,
        atr=2.0,
    )
    assert trade is not None

    # Try invalid exit prices
    assert clean_engine.close_manual(trade.id, -100.0) is None
    assert clean_engine.close_manual(trade.id, 0.0) is None
    assert clean_engine.close_manual(trade.id, float("nan")) is None
    assert clean_engine.close_manual(trade.id, "invalid") is None

    # Trade must still be OPEN
    assert trade.status == "OPEN"
    assert len(clean_engine.open_trades) == 1


def test_portfolio_wide_gross_leverage_limit(clean_engine):
    """Bug 10: Gross exposure across all open positions must respect MAX_PORTFOLIO_LEVERAGE."""
    # Set capital to 10,000 and max portfolio leverage to 1.0 (max gross 10,000)
    clean_engine.capital = 10_000.0
    clean_engine.initial_capital = 10_000.0

    # Place trade 1 taking 8,000 exposure (entry 1000, sl 980, qty 8)
    trade1 = clean_engine.execute(
        symbol="STOCK_A",
        direction="LONG",
        entry=1000.0,
        sl=980.0,
        tp=1050.0,
        atr=10.0,
    )
    assert trade1 is not None

    # Place trade 2 with high entry that exceeds remaining 2,000 exposure
    with patch.object(settings, "MAX_PORTFOLIO_LEVERAGE", 1.0):
        trade2 = clean_engine.execute(
            symbol="STOCK_B",
            direction="LONG",
            entry=3000.0,  # 1 share is 3000 > remaining 2000
            sl=2900.0,
            tp=3200.0,
            atr=20.0,
        )
        assert trade2 is None
        assert "Portfolio leverage limit exceeded" in clean_engine.last_rejection_reason
