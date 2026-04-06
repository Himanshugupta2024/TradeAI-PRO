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
