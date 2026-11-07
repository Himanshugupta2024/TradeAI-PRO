"""
Vectorized Backtesting Engine.

Runs strategy signals through historical data and computes
performance metrics. Supports realistic next-bar execution (look-ahead bias protection),
conservative same-bar stop/target arbitration, transaction cost modeling (slippage,
brokerage, STT/taxes), and sample-size statistical qualification.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional

from config import settings
from agents.backtester.costs import CostModel, FlatCostModel, get_profile


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
    pnl: float  # Price points per unit
    pnl_pct: float  # Percentage gain/loss per share
    points_pnl: float = 0.0
    gross_pnl_inr: float = 0.0
    trade_cost_inr: float = 0.0
    net_pnl_inr: float = 0.0
    shares: int = 1
    exit_reason: str = "END_OF_DATA"  # "TARGET", "STOP_LOSS", "END_OF_DATA"


@dataclass
class BacktestResult:
    """Aggregated backtest performance metrics."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0  # Legacy points alias for backward compatibility
    total_pnl_pct: float = 0.0  # Legacy alias
    points_pnl: float = 0.0  # Explicit per-share price points
    gross_return_pct: float = 0.0
    estimated_costs_pct: float = 0.0
    net_return_pct: float = 0.0
    per_trade_cost_pct: float = 0.0
    cost_drag_pct: float = 0.0
    stats_available: bool = False
    min_trades_for_stats: int = 20
    net_pnl_inr: float = 0.0
    expectancy_pct: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_consecutive_losses: int = 0
    sample_status: str = "INSUFFICIENT_SAMPLE"  # "INSUFFICIENT_SAMPLE" (<30) or "VALID"
    sample_badge: str = "0 trades • Insufficient sample"
    cost_assumptions: dict = field(default_factory=dict)
    trades: list[TradeResult] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"\n{'=' * 50}\n"
            f"  BACKTEST RESULTS ({self.sample_badge})\n"
            f"{'=' * 50}\n"
            f"  Total Trades     : {self.total_trades}\n"
            f"  Win Rate         : {self.win_rate:.1f}%\n"
            f"  Points PnL       : {self.points_pnl:+.2f} pts\n"
            f"  Gross Return %   : {self.gross_return_pct:+.2f}%\n"
            f"  Est. Costs %     : {self.estimated_costs_pct:.2f}%\n"
            f"  Net Return %     : {self.net_return_pct:+.2f}%\n"
            f"  Net Cash PnL     : ₹{self.net_pnl_inr:+.2f}\n"
            f"  Expectancy %     : {self.expectancy_pct:+.2f}%\n"
            f"  Max Drawdown     : {self.max_drawdown:.2f} ({self.max_drawdown_pct:.1f}%)\n"
            f"  Profit Factor    : {self.profit_factor:.2f}\n"
            f"  Sharpe Ratio     : {self.sharpe_ratio:.2f}\n"
            f"  Avg Win %        : {self.avg_win:+.2f}%\n"
            f"  Avg Loss %       : {self.avg_loss:+.2f}%\n"
            f"  Max Consec Losses: {self.max_consecutive_losses}\n"
            f"{'=' * 50}\n"
        )


class Backtester:
    """
    Vectorized backtester with conservative execution, realistic transaction costs,
    and look-ahead bias prevention.

    Usage:
        bt = Backtester(initial_capital=100000, cost_profile="delivery")
        result = bt.run(df_with_signals)
    """

    def __init__(
        self,
        initial_capital: float = 100_000.0,
        slippage_pct: Optional[float] = None,
        cost_bps: Optional[float] = None,
        risk_per_trade_pct: float = 2.0,
        min_sample_size: int = 30,
        max_consecutive_losses: int = settings.MAX_CONSECUTIVE_LOSSES,
        daily_loss_limit_pct: float = settings.DAILY_LOSS_LIMIT_PCT,
        cost_profile: str = "delivery",
        cost_model: Optional[CostModel] = None,
    ):
        """
        Args:
            slippage_pct: adverse fill in percent per side (0.05 = 5 bps).
                ``None`` uses the cost profile's slippage.
            cost_bps: legacy flat round-trip charge in bps. When provided it
                overrides the profile (kept for callers that pass an explicit
                number). Prefer ``cost_profile``.
            cost_profile: ``"delivery"`` (~30 bps round trip) or ``"intraday"``
                (~8 bps round trip). See agents/backtester/costs.py.
            cost_model: fully custom CostModel; wins over everything else.
        """
        self.initial_capital = initial_capital

        profile = get_profile(cost_profile)
        if cost_model is not None:
            self.cost_model = cost_model
            profile_slip_pct = cost_model.slippage_pct * 100.0
        elif cost_bps is not None:
            self.cost_model = FlatCostModel(cost_bps)
            profile_slip_pct = 0.05
        else:
            self.cost_model = CostModel(profile)
            profile_slip_pct = profile.slippage_bps_per_side / 100.0

        slip_pct = profile_slip_pct if slippage_pct is None else slippage_pct
        self.slippage_pct = slip_pct / 100.0  # percent → decimal
        self.cost_bps = self.cost_model.profile.roundtrip_bps()
        self.cost_pct = self.cost_bps / 10_000.0
        self.risk_per_trade_pct = risk_per_trade_pct / 100.0
        self.min_sample_size = min_sample_size
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

        Execution Model:
            - Next-bar execution: If signal is generated at bar i close, order executes
              at bar i+1 OPEN price + slippage. Prevents look-ahead close bias.
            - Conservative same-bar TP/SL: If a bar's High touches TP and Low touches SL,
              STOP LOSS is assumed hit first.
            - Realistic costs: Slippage on entry & exit + exchange/brokerage/STT costs.
        """
        if df.empty or "signal" not in df.columns:
            return BacktestResult()

        trades: list[TradeResult] = []
        capital = self.initial_capital
        consecutive_losses = 0
        daily_pnl = 0.0
        current_date = None
        in_trade = False
        pending_signal = None  # (signal_type, sl, tp, signal_bar_idx)
        entry_price = 0.0
        sl = 0.0
        tp = 0.0
        direction = ""
        entry_time = None
        shares = 1

        n_rows = len(df)
        for i in range(n_rows):
            row = df.iloc[i]
            row_date = row["timestamp"].date() if hasattr(row["timestamp"], "date") else None

            # Reset daily PnL counter on new day
            if row_date != current_date:
                current_date = row_date
                daily_pnl = 0.0

            # ── If we have a pending entry from previous candle close, execute on current OPEN ──
            if pending_signal and not in_trade:
                sig_type, sig_sl, sig_tp = pending_signal
                pending_signal = None
                
                open_px = float(row.get("open", row["close"]))
                entry_price = open_px * (1 + self.slippage_pct if sig_type == "LONG" else 1 - self.slippage_pct)
                sl = sig_sl
                tp = sig_tp
                direction = sig_type
                entry_time = row["timestamp"]
                
                # Position sizing: risk 2% of capital, capped at 25% of capital per position
                risk_amt = capital * self.risk_per_trade_pct
                risk_per_share = max(abs(entry_price - sl), entry_price * 0.005)
                max_shares_capital = max(1, int((capital * 0.25) / max(entry_price, 1.0)))
                shares = min(max(1, int(risk_amt / risk_per_share)), max_shares_capital)
                in_trade = True

            # ── Check if we're in an active trade ──
            if in_trade:
                if direction == "LONG":
                    # Conservative check: If both SL and TP touched on same bar, assume SL hit first
                    sl_hit = (row["low"] <= sl)
                    tp_hit = (row["high"] >= tp)

                    if sl_hit:
                        exit_price = sl * (1 - self.slippage_pct)
                        pts = exit_price - entry_price
                        pnl_pct = (pts / entry_price) * 100.0
                        gross_inr = pts * shares
                        costs_inr = self.cost_model.trade_cost(entry_price * shares, exit_price * shares, direction)
                        net_inr = gross_inr - costs_inr
                        trades.append(TradeResult(
                            entry_time=entry_time, exit_time=row["timestamp"],
                            direction=direction, entry_price=entry_price,
                            exit_price=exit_price, stop_loss=sl, target=tp,
                            pnl=pts, pnl_pct=pnl_pct, points_pnl=pts,
                            gross_pnl_inr=gross_inr, trade_cost_inr=costs_inr,
                            net_pnl_inr=net_inr, shares=shares, exit_reason="STOP_LOSS",
                        ))
                        capital += net_inr
                        daily_pnl += net_inr
                        consecutive_losses += 1
                        in_trade = False
                        continue

                    if tp_hit:
                        exit_price = tp * (1 - self.slippage_pct)
                        pts = exit_price - entry_price
                        pnl_pct = (pts / entry_price) * 100.0
                        gross_inr = pts * shares
                        costs_inr = self.cost_model.trade_cost(entry_price * shares, exit_price * shares, direction)
                        net_inr = gross_inr - costs_inr
                        trades.append(TradeResult(
                            entry_time=entry_time, exit_time=row["timestamp"],
                            direction=direction, entry_price=entry_price,
                            exit_price=exit_price, stop_loss=sl, target=tp,
                            pnl=pts, pnl_pct=pnl_pct, points_pnl=pts,
                            gross_pnl_inr=gross_inr, trade_cost_inr=costs_inr,
                            net_pnl_inr=net_inr, shares=shares, exit_reason="TARGET",
                        ))
                        capital += net_inr
                        daily_pnl += net_inr
                        consecutive_losses = 0
                        in_trade = False
                        continue

                elif direction == "SHORT":
                    # Conservative check for SHORT: SL is above entry, TP is below
                    sl_hit = (row["high"] >= sl)
                    tp_hit = (row["low"] <= tp)

                    if sl_hit:
                        exit_price = sl * (1 + self.slippage_pct)
                        pts = entry_price - exit_price
                        pnl_pct = (pts / entry_price) * 100.0
                        gross_inr = pts * shares
                        costs_inr = self.cost_model.trade_cost(entry_price * shares, exit_price * shares, direction)
                        net_inr = gross_inr - costs_inr
                        trades.append(TradeResult(
                            entry_time=entry_time, exit_time=row["timestamp"],
                            direction=direction, entry_price=entry_price,
                            exit_price=exit_price, stop_loss=sl, target=tp,
                            pnl=pts, pnl_pct=pnl_pct, points_pnl=pts,
                            gross_pnl_inr=gross_inr, trade_cost_inr=costs_inr,
                            net_pnl_inr=net_inr, shares=shares, exit_reason="STOP_LOSS",
                        ))
                        capital += net_inr
                        daily_pnl += net_inr
                        consecutive_losses += 1
                        in_trade = False
                        continue

                    if tp_hit:
                        exit_price = tp * (1 + self.slippage_pct)
                        pts = entry_price - exit_price
                        pnl_pct = (pts / entry_price) * 100.0
                        gross_inr = pts * shares
                        costs_inr = self.cost_model.trade_cost(entry_price * shares, exit_price * shares, direction)
                        net_inr = gross_inr - costs_inr
                        trades.append(TradeResult(
                            entry_time=entry_time, exit_time=row["timestamp"],
                            direction=direction, entry_price=entry_price,
                            exit_price=exit_price, stop_loss=sl, target=tp,
                            pnl=pts, pnl_pct=pnl_pct, points_pnl=pts,
                            gross_pnl_inr=gross_inr, trade_cost_inr=costs_inr,
                            net_pnl_inr=net_inr, shares=shares, exit_reason="TARGET",
                        ))
                        capital += net_inr
                        daily_pnl += net_inr
                        consecutive_losses = 0
                        in_trade = False
                        continue

                # Still in trade, continue to next bar
                continue

            # ── Check for new signal at current candle close ──
            signal = row.get("signal")
            if signal not in ("LONG", "SHORT"):
                continue

            # Circuit breaker checks
            if consecutive_losses >= self.max_consecutive_losses:
                continue

            if abs(daily_pnl / max(capital, 1)) >= self.daily_loss_limit_pct:
                continue

            row_sl = row.get("stop_loss")
            row_tp = row.get("target")
            if pd.isna(row_sl) or pd.isna(row_tp):
                continue

            # Schedule entry for NEXT bar's OPEN (look-ahead bias protection)
            if i + 1 < n_rows:
                pending_signal = (signal, float(row_sl), float(row_tp))
            else:
                # If this is the absolute last bar in dataset, execute at close with slippage
                entry_price = row["close"] * (1 + self.slippage_pct if signal == "LONG" else 1 - self.slippage_pct)
                sl = float(row_sl)
                tp = float(row_tp)
                direction = signal
                entry_time = row["timestamp"]
                shares = 1
                in_trade = True

        # Close any open trade at end of data
        if in_trade:
            last_row = df.iloc[-1]
            exit_price = last_row["close"]
            if direction == "LONG":
                pts = exit_price - entry_price
            else:
                pts = entry_price - exit_price
            pnl_pct = (pts / entry_price) * 100.0
            gross_inr = pts * shares
            costs_inr = self.cost_model.trade_cost(entry_price * shares, exit_price * shares, direction)
            net_inr = gross_inr - costs_inr
            trades.append(TradeResult(
                entry_time=entry_time, exit_time=last_row["timestamp"],
                direction=direction, entry_price=entry_price,
                exit_price=exit_price, stop_loss=sl, target=tp,
                pnl=pts, pnl_pct=pnl_pct, points_pnl=pts,
                gross_pnl_inr=gross_inr, trade_cost_inr=costs_inr,
                net_pnl_inr=net_inr, shares=shares, exit_reason="END_OF_DATA",
            ))
            capital += net_inr

        return self._compute_metrics(trades, capital)

    def _compute_metrics(self, trades: list[TradeResult], final_capital: float) -> BacktestResult:
        """Compute all performance metrics from a list of trade results."""
        cost_info = dict(self.cost_model.summary())
        cost_info["slippage_pct"] = round(self.slippage_pct * 100.0, 3)
        cost_info["roundtrip_cost_pct"] = round(
            (self.cost_model.profile.roundtrip_bps() / 100.0) + (self.slippage_pct * 200.0),
            3,
        )
        
        result = BacktestResult(
            trades=trades,
            cost_assumptions=cost_info,
        )

        if not trades:
            result.sample_badge = "0 trades • Insufficient sample"
            return result

        result.total_trades = len(trades)
        pnls = [t.pnl for t in trades]
        pnl_pcts = [t.pnl_pct for t in trades]
        net_inrs = [t.net_pnl_inr for t in trades]
        gross_inrs = [t.gross_pnl_inr for t in trades]
        cost_inrs = [t.trade_cost_inr for t in trades]

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_pcts = [p for p in pnl_pcts if p > 0]
        loss_pcts = [p for p in pnl_pcts if p <= 0]

        result.winning_trades = len(wins)
        result.losing_trades = len(losses)
        result.win_rate = (result.winning_trades / result.total_trades) * 100.0
        
        # Explicit points vs INR cash vs percentages
        result.points_pnl = round(sum(pnls), 2)
        result.total_pnl = result.points_pnl  # Backward-compatible alias
        result.total_pnl_pct = round(sum(pnl_pcts), 2)
        
        result.net_pnl_inr = round(sum(net_inrs), 2)
        total_gross_inr = sum(gross_inrs)
        total_costs_inr = sum(cost_inrs)
        result.gross_return_pct = round((total_gross_inr / self.initial_capital) * 100.0, 2)
        result.estimated_costs_pct = round((total_costs_inr / self.initial_capital) * 100.0, 2)
        result.net_return_pct = round(result.gross_return_pct - result.estimated_costs_pct, 2)

        trade_count = max(result.total_trades, 1)
        result.per_trade_cost_pct = round(result.estimated_costs_pct / trade_count, 2)
        if result.gross_return_pct > 0:
            result.cost_drag_pct = round((result.estimated_costs_pct / abs(result.gross_return_pct)) * 100.0, 1)
        else:
            result.cost_drag_pct = 0.0

        result.stats_available = bool(result.total_trades >= 20)
        result.min_trades_for_stats = 20


        result.avg_win = float(np.mean(win_pcts)) if win_pcts else 0.0
        result.avg_loss = float(np.mean(loss_pcts)) if loss_pcts else 0.0

        # Expectancy % per trade = (Win Rate * Avg Win) - (Loss Rate * |Avg Loss|)
        win_prob = result.winning_trades / result.total_trades
        loss_prob = result.losing_trades / result.total_trades
        result.expectancy_pct = round((win_prob * result.avg_win) - (loss_prob * abs(result.avg_loss)), 2)

        # Profit Factor = gross profit / gross loss
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 1.0  # Avoid div/0
        result.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

        # Max Drawdown (from portfolio equity curve)
        equity = [self.initial_capital]
        for trade_net in net_inrs:
            equity.append(equity[-1] + trade_net)
        equity = np.array(equity)
        peak = np.maximum.accumulate(equity)
        drawdown = peak - equity
        result.max_drawdown = float(round(drawdown.max(), 2))
        result.max_drawdown_pct = float(round((drawdown / peak).max() * 100.0, 1)) if peak.max() > 0 else 0.0

        # Sharpe Ratio (annualized, assuming daily returns)
        if len(pnl_pcts) > 1:
            returns = np.array(pnl_pcts) / 100.0
            mean_ret = np.mean(returns)
            std_ret = np.std(returns, ddof=1)
            result.sharpe_ratio = round(float((mean_ret / std_ret) * np.sqrt(252)), 2) if std_ret > 0 else 0.0
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

        # Sample size qualification
        if result.total_trades < self.min_sample_size:
            result.sample_status = "INSUFFICIENT_SAMPLE"
            result.sample_badge = f"{result.total_trades} trades • Exploratory only"
        else:
            result.sample_status = "VALID"
            result.sample_badge = f"{result.total_trades} trades • Statistically significant"

        return result
