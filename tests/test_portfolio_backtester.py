"""Tests for portfolio-level capital allocation and constraints."""

import sys
import os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd

from agents.backtester.portfolio import (
    PortfolioBacktester,
    PortfolioTrade,
    portfolio_trades_from_backtest,
)


def _trade(symbol="AAA", start="2024-01-01 09:15", end="2024-01-05 15:15",
           entry=100.0, exit=110.0, stop=95.0, sector="TECH", direction="LONG"):
    return PortfolioTrade(
        symbol=symbol,
        direction=direction,
        entry_time=pd.Timestamp(start),
        exit_time=pd.Timestamp(end),
        entry_price=entry,
        exit_price=exit,
        stop_loss=stop,
        sector=sector,
        exit_reason="TARGET",
    )


class TestPortfolioConstraints:
    def test_max_positions_is_enforced(self):
        bt = PortfolioBacktester(initial_capital=100_000, max_positions=2)
        result = bt.run([_trade("AAA"), _trade("BBB"), _trade("CCC")])
        assert result.taken_trades == 2
        assert result.skipped_max_positions == 1

    def test_sector_cap_is_enforced(self):
        bt = PortfolioBacktester(
            initial_capital=100_000, max_positions=8, sector_cap_pct=30.0
        )
        result = bt.run([_trade("AAA"), _trade("BBB"), _trade("CCC")])
        assert result.taken_trades == 2
        assert result.skipped_sector_cap == 1

    def test_second_position_in_same_symbol_is_skipped(self):
        bt = PortfolioBacktester(initial_capital=100_000, max_positions=8)
        result = bt.run([_trade("AAA"), _trade("AAA")])
        assert result.taken_trades == 1
        assert result.skipped_already_open == 1

    def test_empty_portfolio_is_unchanged(self):
        bt = PortfolioBacktester(initial_capital=100_000)
        result = bt.run([])
        assert result.final_equity == 100_000
        assert result.taken_trades == 0


class TestPortfolioAccounting:
    def test_equity_change_equals_sum_of_net_pnl(self):
        bt = PortfolioBacktester(initial_capital=100_000, max_positions=4, sector_cap_pct=100.0)
        result = bt.run([_trade("AAA"), _trade("BBB")])
        expected = 100_000 + sum(t["net_pnl"] for t in result.trades)
        assert abs(result.final_equity - expected) < 0.01
        assert result.total_return_pct > 0

    def test_costs_are_deducted(self):
        bt = PortfolioBacktester(initial_capital=100_000, max_positions=4, sector_cap_pct=100.0)
        result = bt.run([_trade("AAA")])
        trade = result.trades[0]
        assert trade["costs"] > 0
        assert trade["net_pnl"] < trade["gross_pnl"]

    def test_position_size_respects_risk_budget(self):
        bt = PortfolioBacktester(initial_capital=100_000, risk_per_trade_pct=0.75)
        result = bt.run([_trade("AAA")])
        trade = result.trades[0]
        risk = abs(trade["entry_price"] - 95.0) * trade["shares"]
        # 0.75% of 100k = 750, allow rounding to whole shares
        assert risk <= 750 * 1.01

    def test_metrics_are_finite(self):
        bt = PortfolioBacktester(initial_capital=100_000)
        result = bt.run([_trade("AAA"), _trade("BBB", start="2024-02-01 09:15", end="2024-02-05 15:15")])
        assert result.max_drawdown_pct >= 0
        assert result.profit_factor >= 0
        assert isinstance(result.equity_curve, list)


class TestConversion:
    def test_backtest_trades_convert_to_portfolio_candidates(self):
        fake_result = SimpleNamespace(
            trades=[
                SimpleNamespace(
                    direction="LONG",
                    entry_time=pd.Timestamp("2024-01-01"),
                    exit_time=pd.Timestamp("2024-01-02"),
                    entry_price=100.0,
                    exit_price=105.0,
                    stop_loss=98.0,
                    exit_reason="TARGET",
                )
            ]
        )
        candidates = portfolio_trades_from_backtest("RELIANCE", fake_result, sector="ENERGY")
        assert len(candidates) == 1
        assert candidates[0].symbol == "RELIANCE"
        assert candidates[0].sector == "ENERGY"
