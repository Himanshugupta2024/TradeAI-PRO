"""
Tests for Trailing Stop-Loss Manager.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from agents.execution.trailing_sl import TrailingStopManager, TrailingState


class TestTrailingStopManager:
    def _manager(self):
        return TrailingStopManager(trailing_atr_mult=2.0, breakeven_atr_threshold=1.0)

    def test_register_and_get(self):
        tsm = self._manager()
        tsm.register(trade_id=1, direction="BUY", entry=100, sl=95, atr=5)
        sl = tsm.get_sl(1)
        assert sl == 95  # Initial SL unchanged

    def test_sl_tightens_on_price_increase_buy(self):
        tsm = self._manager()
        tsm.register(trade_id=1, direction="BUY", entry=100, sl=90, atr=5)

        # Price moves up significantly
        new_sl = tsm.update(1, "BUY", current_high=120, current_low=115, atr=5)
        # Trailing SL = 120 - (2.0 * 5) = 110 — should be tighter than 90
        assert new_sl is not None
        assert new_sl > 90

    def test_sl_never_loosens_buy(self):
        tsm = self._manager()
        tsm.register(trade_id=1, direction="BUY", entry=100, sl=90, atr=5)

        # Price goes up
        sl1 = tsm.update(1, "BUY", current_high=120, current_low=115, atr=5)
        # Price comes back down
        sl2 = tsm.update(1, "BUY", current_high=105, current_low=100, atr=5)

        # SL should never decrease
        assert sl2 >= sl1

    def test_sl_tightens_on_price_decrease_sell(self):
        tsm = self._manager()
        tsm.register(trade_id=1, direction="SELL", entry=100, sl=110, atr=5)

        # Price moves down significantly
        new_sl = tsm.update(1, "SELL", current_high=85, current_low=80, atr=5)
        # Trailing SL = 80 + (2.0 * 5) = 90 — should be tighter than 110
        assert new_sl is not None
        assert new_sl < 110

    def test_sl_never_loosens_sell(self):
        tsm = self._manager()
        tsm.register(trade_id=1, direction="SELL", entry=100, sl=110, atr=5)

        sl1 = tsm.update(1, "SELL", current_high=85, current_low=80, atr=5)
        sl2 = tsm.update(1, "SELL", current_high=105, current_low=100, atr=5)

        assert sl2 <= sl1

    def test_remove_trade(self):
        tsm = self._manager()
        tsm.register(trade_id=1, direction="BUY", entry=100, sl=90, atr=5)
        tsm.remove(1)
        assert tsm.get_sl(1) is None

    def test_unregistered_trade_returns_none(self):
        tsm = self._manager()
        result = tsm.update(999, "BUY", current_high=100, current_low=90, atr=5)
        assert result is None

    def test_multiple_trades_independent(self):
        tsm = self._manager()
        tsm.register(trade_id=1, direction="BUY", entry=100, sl=90, atr=5)
        tsm.register(trade_id=2, direction="SELL", entry=200, sl=210, atr=10)

        tsm.update(1, "BUY", current_high=120, current_low=115, atr=5)
        tsm.update(2, "SELL", current_high=185, current_low=180, atr=10)

        sl1 = tsm.get_sl(1)
        sl2 = tsm.get_sl(2)

        assert sl1 > 90   # BUY tightened
        assert sl2 < 210  # SELL tightened
