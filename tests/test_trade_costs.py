"""Tests for cost-aware paper-trade accounting."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.backtester.costs import DELIVERY, INTRADAY  # noqa: E402
from agents.execution.trade_costs import cost_profile_for, estimate_trade_costs  # noqa: E402
from api.track_record import build_track_record  # noqa: E402


class TestProfileSelection:
    def test_swing_equity_uses_delivery(self):
        assert cost_profile_for("TCS", "SWING").name == "delivery"

    def test_intraday_equity_uses_intraday(self):
        assert cost_profile_for("TCS", "INTRADAY").name == "intraday"

    def test_crypto_has_no_nse_cost_profile(self):
        assert cost_profile_for("BTC/USDT", "SWING") is None
        assert cost_profile_for("ETHUSDT", "INTRADAY") is None


class TestEstimate:
    def test_delivery_round_trip_matches_backtester(self):
        est = estimate_trade_costs(
            entry_price=100.0, exit_price=100.0, quantity=10.0,
            symbol="TCS", signal_type="SWING",
        )
        expected = 100.0 * 10.0 * DELIVERY.roundtrip_bps() / 10_000.0
        assert est["profile"] == "delivery"
        assert abs(est["total_cost"] - expected) < 0.1
        assert est["roundtrip_bps"] == round(DELIVERY.roundtrip_bps(), 2)

    def test_intraday_round_trip_matches_backtester(self):
        est = estimate_trade_costs(
            entry_price=500.0, exit_price=505.0, quantity=20.0,
            symbol="INFY", signal_type="INTRADAY",
        )
        assert est["profile"] == "intraday"
        assert abs(est["roundtrip_bps"] - round(INTRADAY.roundtrip_bps(), 2)) < 0.001
        assert est["total_cost"] > 0

    def test_buy_and_sell_legs_split(self):
        est = estimate_trade_costs(
            entry_price=100.0, exit_price=110.0, quantity=5.0,
            symbol="TCS", signal_type="SWING",
        )
        assert est["buy_cost"] > est["sell_cost"]  # delivery buy leg carries stamp + equal STT

    def test_crypto_is_zero_cost(self):
        est = estimate_trade_costs(
            entry_price=100.0, exit_price=120.0, quantity=2.0,
            symbol="BTC/USDT",
        )
        assert est["profile"] == "NONE"
        assert est["total_cost"] == 0.0

    def test_zero_quantity_is_safe(self):
        est = estimate_trade_costs(
            entry_price=100.0, exit_price=100.0, quantity=0.0,
            symbol="TCS", signal_type="INTRADAY",
        )
        assert est["total_cost"] == 0.0
        assert est["profile"] == "intraday"


class TestTrackRecordNet:
    def _trade(self, **overrides):
        trade = {
            "id": 1,
            "symbol": "TCS",
            "direction": "LONG",
            "status": "CLOSED",
            "entry_price": 100.0,
            "exit_price": 110.0,
            "quantity": 10.0,
            "total_pnl": 1000.0,
            "net_pnl": 700.0,
            "estimated_costs": 300.0,
            "cost_profile": "delivery",
            "entry_time": "2026-01-05T09:30:00",
            "exit_time": "2026-01-05T15:00:00",
            "signal_type": "SWING",
        }
        trade.update(overrides)
        return trade

    def test_track_record_uses_net_when_costs_known(self):
        record = build_track_record([self._trade()])
        assert record["pnl"] == 700.0
        assert record["trades"][0]["pnl"] == 700.0
        assert record["trades"][0]["gross_pnl"] == 1000.0
        assert record["trades"][0]["estimated_costs"] == 300.0

    def test_legacy_trade_without_costs_uses_gross(self):
        legacy = self._trade(cost_profile="", net_pnl=None, estimated_costs=0.0)
        record = build_track_record([legacy])
        assert record["pnl"] == 1000.0
