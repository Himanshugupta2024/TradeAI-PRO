"""
Tests for Position Sizing Module.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from agents.execution.position_sizing import calculate_position_size, kelly_criterion


class TestPositionSizing:
    def test_basic_calculation(self):
        # Risk 2% of 100k on a 100-point risk per unit
        qty = calculate_position_size(capital=100000, risk_pct=2.0, entry=65000, stop_loss=64900)
        # Risk amount = 2000, risk per unit = 100 → qty = 20
        assert qty == 20.0

    def test_small_risk(self):
        qty = calculate_position_size(capital=100000, risk_pct=1.0, entry=100, stop_loss=95)
        # Risk = 1000, risk/unit = 5 → qty = 200
        assert qty == 200.0

    def test_zero_capital(self):
        qty = calculate_position_size(capital=0, risk_pct=2.0, entry=100, stop_loss=95)
        assert qty == 0.0

    def test_zero_risk_pct(self):
        qty = calculate_position_size(capital=100000, risk_pct=0, entry=100, stop_loss=95)
        assert qty == 0.0

    def test_sl_equals_entry(self):
        qty = calculate_position_size(capital=100000, risk_pct=2.0, entry=100, stop_loss=100)
        assert qty == 0.0

    def test_sell_position(self):
        # Sell: entry=100, SL=105 → risk/unit = 5
        qty = calculate_position_size(capital=100000, risk_pct=2.0, entry=100, stop_loss=105)
        assert qty == 400.0  # 2000 / 5

    def test_max_leverage_bound(self):
        # 100k capital, 2% risk = 2000 risk.
        # With super tight stop (entry=100, SL=99.99, risk/unit=0.01), unbounded qty = 200,000 units.
        # But with max_leverage=1.0, max allowed qty = 100,000 / 100 = 1,000 units.
        qty = calculate_position_size(capital=100000, risk_pct=2.0, entry=100, stop_loss=99.99, max_leverage=1.0)
        assert qty == 1000.0

    def test_default_unbounded_when_no_leverage(self):
        # Without max_leverage specified, existing tests remain backward compatible
        qty = calculate_position_size(capital=100000, risk_pct=2.0, entry=100, stop_loss=95)
        assert qty == 400.0

    def test_actual_monetary_loss_bounded_by_risk_budget(self):
        """
        Verify mathematical invariant:
        Actual monetary loss at stop loss MUST ALWAYS be <= configured risk budget.
        """
        capital = 100000.0
        risk_pct = 1.0
        budget = capital * (risk_pct / 100.0)  # ₹1000.00

        test_cases = [
            (2510.0, 2477.0, 0.25),   # RELIANCE case (tight stop, allocation capped at 25%)
            (3525.0, 3480.0, 0.25),   # TCS case
            (68800.0, 67450.0, 0.25),  # BTC/USDT case
            (500.0, 480.0, 0.25),     # Mid-cap stock
            (100.0, 90.0, 0.25),      # Wide stop
            (100.0, 99.5, 0.25),      # Super tight stop
        ]

        for entry, sl, max_lev in test_cases:
            qty = calculate_position_size(
                capital=capital,
                risk_pct=risk_pct,
                entry=entry,
                stop_loss=sl,
                max_leverage=max_lev,
            )
            actual_loss_at_stop = qty * abs(entry - sl)
            # Must strictly not exceed configured risk budget
            assert actual_loss_at_stop <= budget + 1e-6, (
                f"Loss ₹{actual_loss_at_stop:.2f} exceeded budget ₹{budget:.2f} for entry={entry}, sl={sl}"
            )
            # Position value must strictly respect allocation cap (within 0.01 INR floating point rounding)
            position_value = qty * entry
            max_allowed_value = capital * max_lev
            assert position_value <= max_allowed_value + 0.01, (
                f"Position value ₹{position_value:.2f} exceeded cap ₹{max_allowed_value:.2f}"
            )

    def test_reliance_9_9602_exact_allocation_clamping(self):
        """
        Prove why RELIANCE with Capital=100k, Risk=1%, Entry=2510, SL=2477 resulted in 9.9602 shares.
        Pure risk formula: 1000 / 33 = 30.303 shares (₹76,060 position value, 76% of account).
        Position Allocation Cap (settings.MAX_POSITION_LEVERAGE = 0.25) restricts position to ₹25,000.
        Max allowed qty = ₹25,000 / ₹2510 = 9.960159 shares (~9.9602).
        """
        capital = 100000.0
        risk_pct = 1.0
        entry = 2510.0
        sl = 2477.0
        max_leverage = 0.25

        qty = calculate_position_size(
            capital=capital,
            risk_pct=risk_pct,
            entry=entry,
            stop_loss=sl,
            max_leverage=max_leverage,
        )

        assert round(qty, 4) == 9.9602
        # Position value is capped at exactly 25% of capital (₹25,000)
        assert abs(qty * entry - 25000.0) < 1.0
        # Actual loss at stop is ₹328.69 (0.33% risk), well below ₹1000 (1.0% budget)
        actual_loss = qty * (entry - sl)
        assert round(actual_loss, 2) == 328.69
        assert actual_loss < 1000.0


class TestKellyCriterion:
    def test_positive_edge(self):
        # 60% win rate, avg win = 200, avg loss = 100
        f = kelly_criterion(win_rate=0.6, avg_win=200, avg_loss=100)
        assert f > 0
        assert f <= 0.25  # Capped

    def test_no_edge(self):
        # 30% win rate, avg win = 100, avg loss = 100 → negative edge
        f = kelly_criterion(win_rate=0.3, avg_win=100, avg_loss=100)
        assert f == 0

    def test_perfect_record(self):
        # 100% win rate is invalid (edge case)
        f = kelly_criterion(win_rate=1.0, avg_win=100, avg_loss=50)
        assert f == 0  # win_rate >= 1 returns 0

    def test_zero_avg_win(self):
        f = kelly_criterion(win_rate=0.6, avg_win=0, avg_loss=100)
        assert f == 0

    def test_cap_at_max_fraction(self):
        # Very high edge should still be capped
        f = kelly_criterion(win_rate=0.8, avg_win=500, avg_loss=50, max_fraction=0.10)
        assert f <= 0.10
