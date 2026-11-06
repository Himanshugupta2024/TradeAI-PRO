"""Tests for the NSE transaction-cost model and the single-count cost fix."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd

from agents.backtester.costs import (
    DELIVERY,
    INTRADAY,
    CostModel,
    FlatCostModel,
    get_profile,
)
from agents.backtester.engine import Backtester


class TestCostProfiles:
    def test_delivery_roundtrip_is_realistic(self):
        # STT alone is 20 bps round trip for delivery; total must sit above it.
        assert 28.0 <= DELIVERY.roundtrip_bps() <= 34.0

    def test_intraday_roundtrip_is_cheaper_than_delivery(self):
        assert 6.0 <= INTRADAY.roundtrip_bps() <= 11.0
        assert INTRADAY.roundtrip_bps() < DELIVERY.roundtrip_bps()

    def test_unknown_profile_falls_back_to_delivery(self):
        assert get_profile("does-not-exist") is DELIVERY

    def test_slippage_is_added_separately(self):
        assert DELIVERY.roundtrip_bps_with_slippage() > DELIVERY.roundtrip_bps()


class TestCostModel:
    def test_cost_scales_with_notional(self):
        model = CostModel(DELIVERY)
        small = model.trade_cost(10_000, 11_000, "LONG")
        large = model.trade_cost(100_000, 110_000, "LONG")
        assert large > small > 0

    def test_long_and_short_have_same_roundtrip_legs(self):
        model = CostModel(DELIVERY)
        # LONG: buy 100k, sell 110k. SHORT: sell 100k, buy 110k.
        long_cost = model.trade_cost(100_000, 110_000, "LONG")
        short_cost = model.trade_cost(100_000, 110_000, "SHORT")
        # Stamp duty sits on the buy leg, so the SHORT pays it on exit.
        assert short_cost != long_cost
        assert abs(short_cost - long_cost) / long_cost < 0.05

    def test_flat_model_is_backward_compatible(self):
        model = FlatCostModel(roundtrip_bps=20.0)
        # 20 bps round trip = 0.10% of each leg, 0.20% of combined notional.
        assert abs(model.trade_cost(100_000, 100_000, "LONG") - 200.0) < 0.01


class TestEngineCostsAreCountedOnce:
    def _one_trade_df(self):
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-01 09:15", "2024-01-01 09:20"]),
                "open": [100.0, 100.0],
                "high": [100.0, 111.0],
                "low": [100.0, 99.0],
                "close": [100.0, 105.0],
                "volume": [10_000, 10_000],
                "signal": ["LONG", "HOLD"],
                "stop_loss": [95.0, float("nan")],
                "target": [110.0, float("nan")],
            }
        )

    def test_trade_cost_matches_model_not_double_slippage(self):
        bt = Backtester(slippage_pct=0.0, cost_profile="delivery")
        result = bt.run(self._one_trade_df())

        assert result.total_trades == 1
        trade = result.trades[0]
        expected = bt.cost_model.trade_cost(
            trade.entry_price * trade.shares,
            trade.exit_price * trade.shares,
            trade.direction,
        )
        assert abs(trade.trade_cost_inr - expected) < 0.01

    def test_delivery_cost_is_higher_than_old_ten_bps_flat(self):
        bt = Backtester(slippage_pct=0.0, cost_profile="delivery")
        result = bt.run(self._one_trade_df())
        trade = result.trades[0]
        notional = trade.entry_price * trade.shares + trade.exit_price * trade.shares
        assert trade.trade_cost_inr > notional * 0.001  # > 10 bps

    def test_intraday_profile_is_cheaper_for_same_trade(self):
        delivery = Backtester(slippage_pct=0.0, cost_profile="delivery").run(self._one_trade_df())
        intraday = Backtester(slippage_pct=0.0, cost_profile="intraday").run(self._one_trade_df())
        assert intraday.trades[0].trade_cost_inr < delivery.trades[0].trade_cost_inr
