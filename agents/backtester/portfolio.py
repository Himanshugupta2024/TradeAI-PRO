"""
Portfolio-level backtesting.

The per-symbol :class:`~agents.backtester.engine.Backtester` answers "does this
signal have an edge on one chart?". It assumes the full initial capital is
available for every symbol independently, so the equity curve it produces is
not the equity curve a real account would have seen.

This module takes the trades produced by one or more per-symbol backtests and
replays them chronologically through a single account:

    - one cash balance, shared by every symbol
    - risk-based position sizing (risk_per_trade_pct of current equity)
    - max concurrent positions, per-position notional cap, sector cap
    - commission/STT/exchange/stamp costs from CostModel
    - equity curve, drawdown, CAGR, Sharpe, exposure

Slippage is expected to be already embedded in the entry/exit prices (the
per-symbol engine applies it). This layer adds charges only — no second
slippage hit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from agents.backtester.costs import CostModel, get_profile


@dataclass
class PortfolioTrade:
    """A candidate round trip, usually produced by a per-symbol backtest."""

    symbol: str
    direction: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    stop_loss: float
    sector: str = "UNKNOWN"
    exit_reason: str = ""

    @property
    def is_long(self) -> bool:
        return str(self.direction).upper() == "LONG"


@dataclass
class PortfolioResult:
    initial_capital: float = 0.0
    final_equity: float = 0.0
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    total_candidates: int = 0
    taken_trades: int = 0
    skipped_max_positions: int = 0
    skipped_sector_cap: int = 0
    skipped_capital: int = 0
    skipped_already_open: int = 0
    avg_exposure_pct: float = 0.0
    equity_curve: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    cost_model: dict = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"\n{'=' * 56}\n"
            f"  PORTFOLIO BACKTEST\n"
            f"{'=' * 56}\n"
            f"  Candidates       : {self.total_candidates}\n"
            f"  Taken / skipped  : {self.taken_trades} / "
            f"{self.skipped_max_positions + self.skipped_sector_cap + self.skipped_capital + self.skipped_already_open}\n"
            f"  Final equity     : ₹{self.final_equity:,.0f}\n"
            f"  Total return     : {self.total_return_pct:+.2f}%\n"
            f"  CAGR             : {self.cagr_pct:+.2f}%\n"
            f"  Max drawdown     : {self.max_drawdown_pct:.1f}%\n"
            f"  Sharpe           : {self.sharpe:.2f}\n"
            f"  Win rate         : {self.win_rate:.1f}%\n"
            f"  Profit factor    : {self.profit_factor:.2f}\n"
            f"  Avg exposure     : {self.avg_exposure_pct:.1f}%\n"
            f"{'=' * 56}\n"
        )


class PortfolioBacktester:
    """Replay candidate trades through one shared account."""

    def __init__(
        self,
        initial_capital: float = 100_000.0,
        max_positions: int = 8,
        risk_per_trade_pct: float = 0.75,
        max_position_pct: float = 15.0,
        sector_cap_pct: float = 30.0,
        cost_model: Optional[CostModel] = None,
        cost_profile: str = "delivery",
    ):
        self.initial_capital = float(initial_capital)
        self.max_positions = int(max_positions)
        self.risk_per_trade_pct = float(risk_per_trade_pct) / 100.0
        self.max_position_pct = float(max_position_pct) / 100.0
        self.sector_cap_pct = float(sector_cap_pct) / 100.0
        self.cost_model = cost_model or CostModel(get_profile(cost_profile))

    # ── public API ────────────────────────────────────────────────────────
    def run(self, trades: Iterable[PortfolioTrade]) -> PortfolioResult:
        result = PortfolioResult(
            initial_capital=self.initial_capital,
            final_equity=self.initial_capital,
            cost_model=dict(self.cost_model.summary()),
        )

        valid = [t for t in trades if self._is_valid(t)]
        result.total_candidates = len(valid)
        if not valid:
            return result

        events = []
        for idx, trade in enumerate(valid):
            events.append((pd.Timestamp(trade.entry_time), 1, idx))
            events.append((pd.Timestamp(trade.exit_time), 0, idx))
        # At the same timestamp, process exits before entries so freed cash can
        # be redeployed on the same bar.
        events.sort(key=lambda item: (item[0], item[1]))

        cash = self.initial_capital
        open_positions: dict[str, dict] = {}
        equity_points: list[tuple[pd.Timestamp, float]] = [(events[0][0], self.initial_capital)]
        exposure_samples: list[float] = []
        closed_net: list[float] = []
        wins = 0

        def reserved() -> float:
            return sum(p["notional"] for p in open_positions.values())

        def equity() -> float:
            return cash + reserved()

        for timestamp, kind, idx in events:
            trade = valid[idx]

            if kind == 0:  # exit
                position = open_positions.pop(trade.symbol, None)
                if position is None:
                    continue
                entry_notional = position["notional"]
                exit_notional = abs(float(trade.exit_price)) * position["shares"]
                gross = (
                    exit_notional - entry_notional
                    if trade.is_long
                    else entry_notional - exit_notional
                )
                cost = self.cost_model.trade_cost(
                    entry_notional, exit_notional, trade.direction
                )
                net = gross - cost

                if trade.is_long:
                    cash += exit_notional
                else:
                    cash += (2.0 * entry_notional) - exit_notional
                cash -= cost

                closed_net.append(net)
                if net > 0:
                    wins += 1

                result.trades.append(
                    {
                        "symbol": trade.symbol,
                        "direction": trade.direction,
                        "sector": trade.sector,
                        "entry_time": trade.entry_time,
                        "exit_time": trade.exit_time,
                        "shares": position["shares"],
                        "entry_price": float(trade.entry_price),
                        "exit_price": float(trade.exit_price),
                        "gross_pnl": round(gross, 2),
                        "costs": round(cost, 2),
                        "net_pnl": round(net, 2),
                        "exit_reason": trade.exit_reason,
                    }
                )
                equity_points.append((timestamp, equity()))
                exposure_samples.append(0.0 if equity() <= 0 else reserved() / equity())
                continue

            # entry
            if trade.symbol in open_positions:
                result.skipped_already_open += 1
                continue
            if len(open_positions) >= self.max_positions:
                result.skipped_max_positions += 1
                continue

            entry_price = float(trade.entry_price)
            stop = float(trade.stop_loss)
            if entry_price <= 0 or not np.isfinite(entry_price):
                result.skipped_capital += 1
                continue

            eq = equity()
            risk_per_share = abs(entry_price - stop)
            if risk_per_share <= 0:
                risk_per_share = entry_price * 0.005

            shares = int(
                min(
                    (eq * self.risk_per_trade_pct) / risk_per_share,
                    (eq * self.max_position_pct) / entry_price,
                    cash / entry_price,
                )
            )
            if shares < 1:
                result.skipped_capital += 1
                continue

            notional = shares * entry_price
            sector_notional = sum(
                p["notional"] for p in open_positions.values() if p["sector"] == trade.sector
            )
            if sector_notional + notional > eq * self.sector_cap_pct:
                result.skipped_sector_cap += 1
                continue

            cash -= notional
            open_positions[trade.symbol] = {
                "shares": shares,
                "notional": notional,
                "sector": trade.sector,
                "direction": trade.direction,
            }
            result.taken_trades += 1
            exposure_samples.append(0.0 if eq <= 0 else reserved() / eq)

        result.final_equity = round(equity(), 2)
        result.total_return_pct = round(
            ((result.final_equity - self.initial_capital) / self.initial_capital) * 100.0, 2
        )
        result.equity_curve = [(ts.isoformat(), round(value, 2)) for ts, value in equity_points]

        self._compute_metrics(result, closed_net, wins, equity_points, exposure_samples)
        return result

    # ── internals ─────────────────────────────────────────────────────────
    @staticmethod
    def _is_valid(trade: PortfolioTrade) -> bool:
        try:
            if pd.isna(trade.entry_time) or pd.isna(trade.exit_time):
                return False
            if pd.Timestamp(trade.exit_time) < pd.Timestamp(trade.entry_time):
                return False
            if not np.isfinite(float(trade.entry_price)) or float(trade.entry_price) <= 0:
                return False
            if not np.isfinite(float(trade.exit_price)) or float(trade.exit_price) <= 0:
                return False
            return True
        except (TypeError, ValueError):
            return False

    def _compute_metrics(
        self,
        result: PortfolioResult,
        closed_net: list[float],
        wins: int,
        equity_points: list[tuple[pd.Timestamp, float]],
        exposure_samples: list[float],
    ) -> None:
        if closed_net:
            result.win_rate = round((wins / len(closed_net)) * 100.0, 1)

        gross_profit = sum(p for p in closed_net if p > 0)
        gross_loss = abs(sum(p for p in closed_net if p <= 0))
        result.profit_factor = (
            round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf") if gross_profit else 0.0
        )

        if exposure_samples:
            result.avg_exposure_pct = round(float(np.mean(exposure_samples)) * 100.0, 1)

        if len(equity_points) >= 2:
            values = np.array([v for _, v in equity_points], dtype=float)
            peak = np.maximum.accumulate(values)
            drawdown = (peak - values) / np.where(peak > 0, peak, 1.0)
            result.max_drawdown_pct = round(float(drawdown.max()) * 100.0, 1)

            series = pd.Series(
                values,
                index=pd.DatetimeIndex([ts for ts, _ in equity_points]),
            )
            daily = series.resample("1D").last().dropna()
            if len(daily) > 2:
                returns = daily.pct_change().dropna()
                std = returns.std(ddof=1)
                if std and std > 0:
                    result.sharpe = round(float(returns.mean() / std * np.sqrt(252)), 2)

        if equity_points:
            start = equity_points[0][0]
            end = equity_points[-1][0]
            days = max((end - start).days, 1)
            years = max(days / 365.25, 1.0 / 365.25)
            if self.initial_capital > 0 and result.final_equity > 0:
                result.cagr_pct = round(
                    ((result.final_equity / self.initial_capital) ** (1.0 / years) - 1.0) * 100.0,
                    2,
                )


def portfolio_trades_from_backtest(
    symbol: str,
    backtest_result,
    sector: str = "UNKNOWN",
) -> list[PortfolioTrade]:
    """Convert one symbol's ``BacktestResult`` into portfolio candidates."""
    trades = []
    for trade in getattr(backtest_result, "trades", []) or []:
        trades.append(
            PortfolioTrade(
                symbol=symbol,
                direction=trade.direction,
                entry_time=trade.entry_time,
                exit_time=trade.exit_time,
                entry_price=float(trade.entry_price),
                exit_price=float(trade.exit_price),
                stop_loss=float(trade.stop_loss),
                sector=sector,
                exit_reason=trade.exit_reason,
            )
        )
    return trades
