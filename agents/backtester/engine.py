"""
Vectorized Backtesting Engine.

Runs strategy signals through historical data and computes
performance metrics. Supports slippage modeling and configurable
risk parameters.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional

from config import settings


@dataclass
class TradeResult:
    """A single completed trade."""
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    exit_price: float
    stop_loss: float
    target: float
    pnl: float
    pnl_pct: float
    exit_reason: str  # "TARGET", "STOP_LOSS", "END_OF_DATA"


@dataclass
class BacktestResult:
    """Aggregated backtest performance metrics."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_consecutive_losses: int = 0
    trades: list[TradeResult] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"\n{'=' * 50}\n"
            f"  BACKTEST RESULTS\n"
            f"{'=' * 50}\n"
            f"  Total Trades     : {self.total_trades}\n"
            f"  Win Rate         : {self.win_rate:.1f}%\n"
            f"  Total PnL        : {self.total_pnl:+.2f}\n"
            f"  Total PnL %      : {self.total_pnl_pct:+.2f}%\n"
            f"  Max Drawdown     : {self.max_drawdown:.2f} ({self.max_drawdown_pct:.1f}%)\n"
            f"  Profit Factor    : {self.profit_factor:.2f}\n"
            f"  Sharpe Ratio     : {self.sharpe_ratio:.2f}\n"
            f"  Avg Win          : {self.avg_win:+.2f}\n"
            f"  Avg Loss         : {self.avg_loss:+.2f}\n"
            f"  Max Consec Losses: {self.max_consecutive_losses}\n"
            f"{'=' * 50}\n"
        )


class Backtester:
    """
    Vectorized backtester with slippage and risk management.

    Usage:
        bt = Backtester(initial_capital=100000, slippage_pct=0.05)
        result = bt.run(df_with_signals)
    """

    def __init__(
        self,
        initial_capital: float = 100_000.0,
        slippage_pct: float = 0.05,
        risk_per_trade_pct: float = 2.0,
        max_consecutive_losses: int = settings.MAX_CONSECUTIVE_LOSSES,
        daily_loss_limit_pct: float = settings.DAILY_LOSS_LIMIT_PCT,
    ):
        self.initial_capital = initial_capital
        self.slippage_pct = slippage_pct / 100.0  # Convert to decimal
        self.risk_per_trade_pct = risk_per_trade_pct / 100.0
        self.max_consecutive_losses = max_consecutive_losses
        self.daily_loss_limit_pct = daily_loss_limit_pct / 100.0

    def run(self, df: pd.DataFrame) -> BacktestResult:
        """
        Run backtest on a DataFrame that has signal columns.

        Expected columns:
            - timestamp, open, high, low, close, volume
            - signal: "LONG", "SHORT", or "HOLD" / NaN
            - stop_loss: price level
            - target: price level

        Returns:
            BacktestResult with all metrics and trade log.
        """
        if df.empty or "signal" not in df.columns:
            return BacktestResult()

        trades: list[TradeResult] = []
        capital = self.initial_capital
        consecutive_losses = 0
        daily_pnl = 0.0
        current_date = None
        in_trade = False
        entry_price = 0.0
        sl = 0.0
        tp = 0.0
        direction = ""
        entry_time = None

        for i in range(len(df)):
            row = df.iloc[i]
            row_date = row["timestamp"].date() if hasattr(row["timestamp"], "date") else None

            # Reset daily PnL counter on new day
            if row_date != current_date:
                current_date = row_date
                daily_pnl = 0.0

            # ── Check if we're in an active trade ──
            if in_trade:
                if direction == "LONG":
                    # Check stop loss hit (low touches SL)
                    if row["low"] <= sl:
                        exit_price = sl * (1 - self.slippage_pct)  # Slippage on exit
                        pnl = exit_price - entry_price
                        pnl_pct = (pnl / entry_price) * 100
                        trades.append(TradeResult(
                            entry_time=entry_time, exit_time=row["timestamp"],
                            direction=direction, entry_price=entry_price,
                            exit_price=exit_price, stop_loss=sl, target=tp,
                            pnl=pnl, pnl_pct=pnl_pct, exit_reason="STOP_LOSS",
                        ))
                        capital += pnl
                        daily_pnl += pnl
                        consecutive_losses += 1
                        in_trade = False
                        continue

                    # Check target hit (high touches TP)
                    if row["high"] >= tp:
                        exit_price = tp * (1 - self.slippage_pct)
                        pnl = exit_price - entry_price
                        pnl_pct = (pnl / entry_price) * 100
                        trades.append(TradeResult(
                            entry_time=entry_time, exit_time=row["timestamp"],
                            direction=direction, entry_price=entry_price,
                            exit_price=exit_price, stop_loss=sl, target=tp,
                            pnl=pnl, pnl_pct=pnl_pct, exit_reason="TARGET",
                        ))
                        capital += pnl
                        daily_pnl += pnl
                        consecutive_losses = 0
                        in_trade = False
                        continue

                elif direction == "SHORT":
                    # Short: SL is above entry, TP is below
                    if row["high"] >= sl:
                        exit_price = sl * (1 + self.slippage_pct)
                        pnl = entry_price - exit_price
                        pnl_pct = (pnl / entry_price) * 100
                        trades.append(TradeResult(
                            entry_time=entry_time, exit_time=row["timestamp"],
                            direction=direction, entry_price=entry_price,
                            exit_price=exit_price, stop_loss=sl, target=tp,
                            pnl=pnl, pnl_pct=pnl_pct, exit_reason="STOP_LOSS",
                        ))
                        capital += pnl
                        daily_pnl += pnl
                        consecutive_losses += 1
                        in_trade = False
                        continue

                    if row["low"] <= tp:
                        exit_price = tp * (1 + self.slippage_pct)
                        pnl = entry_price - exit_price
                        pnl_pct = (pnl / entry_price) * 100
                        trades.append(TradeResult(
                            entry_time=entry_time, exit_time=row["timestamp"],
                            direction=direction, entry_price=entry_price,
                            exit_price=exit_price, stop_loss=sl, target=tp,
                            pnl=pnl, pnl_pct=pnl_pct, exit_reason="TARGET",
                        ))
                        capital += pnl
                        daily_pnl += pnl
                        consecutive_losses = 0
                        in_trade = False
                        continue

                # Still in trade, continue
                continue

            # ── Check for new signal (not in trade) ──
            signal = row.get("signal")
            if signal not in ("LONG", "SHORT"):
                continue

            # ── Circuit breaker checks ──
            if consecutive_losses >= self.max_consecutive_losses:
                continue  # Paused — too many losses in a row

            if abs(daily_pnl / max(capital, 1)) >= self.daily_loss_limit_pct:
                continue  # Daily loss limit hit

            # ── Enter trade ──
            row_sl = row.get("stop_loss")
            row_tp = row.get("target")
            if pd.isna(row_sl) or pd.isna(row_tp):
                continue

            entry_price = row["close"] * (1 + self.slippage_pct if signal == "LONG" else 1 - self.slippage_pct)
            sl = row_sl
            tp = row_tp
            direction = signal
            entry_time = row["timestamp"]
            in_trade = True

        # Close any open trade at end of data
        if in_trade:
            last_row = df.iloc[-1]
            exit_price = last_row["close"]
            if direction == "LONG":
                pnl = exit_price - entry_price
            else:
                pnl = entry_price - exit_price
            pnl_pct = (pnl / entry_price) * 100
            trades.append(TradeResult(
                entry_time=entry_time, exit_time=last_row["timestamp"],
                direction=direction, entry_price=entry_price,
                exit_price=exit_price, stop_loss=sl, target=tp,
                pnl=pnl, pnl_pct=pnl_pct, exit_reason="END_OF_DATA",
            ))

        return self._compute_metrics(trades)

    def _compute_metrics(self, trades: list[TradeResult]) -> BacktestResult:
        """Compute all performance metrics from a list of trade results."""
        result = BacktestResult(trades=trades)

        if not trades:
            return result

        result.total_trades = len(trades)
        pnls = [t.pnl for t in trades]
        pnl_pcts = [t.pnl_pct for t in trades]

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        result.winning_trades = len(wins)
        result.losing_trades = len(losses)
        result.win_rate = (result.winning_trades / result.total_trades) * 100
        result.total_pnl = sum(pnls)
        result.total_pnl_pct = sum(pnl_pcts)
        result.avg_win = np.mean(wins) if wins else 0.0
        result.avg_loss = np.mean(losses) if losses else 0.0

        # Profit Factor = gross profit / gross loss
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 1.0  # Avoid div/0
        result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Max Drawdown (from equity curve)
        equity = [self.initial_capital]
        for pnl in pnls:
            equity.append(equity[-1] + pnl)
        equity = np.array(equity)
        peak = np.maximum.accumulate(equity)
        drawdown = peak - equity
        result.max_drawdown = float(drawdown.max())
        result.max_drawdown_pct = float((drawdown / peak).max() * 100) if peak.max() > 0 else 0.0

        # Sharpe Ratio (annualized, assuming daily returns)
        if len(pnl_pcts) > 1:
            returns = np.array(pnl_pcts) / 100.0
            mean_ret = np.mean(returns)
            std_ret = np.std(returns, ddof=1)
            result.sharpe_ratio = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0
        else:
            result.sharpe_ratio = 0.0

        # Max consecutive losses
        max_consec = 0
        current_consec = 0
        for pnl in pnls:
            if pnl <= 0:
                current_consec += 1
                max_consec = max(max_consec, current_consec)
            else:
                current_consec = 0
        result.max_consecutive_losses = max_consec

        return result
