"""
Paper Trading Engine.

Simulates live trade execution without real money.
Records virtual trades, tracks PnL, and enforces
all risk management rules in real-time.

Upgrades:
  - Trailing stop-loss integration
  - Position sizing (risk-based)
  - Cooldown timer per symbol
"""

import json
import os
from datetime import datetime, date, timezone, timedelta
from dataclasses import dataclass, field, asdict

from config import settings

_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist_isoformat() -> str:
    """Return current time as ISO string in IST."""
    return datetime.now(tz=_IST).isoformat()


def _today_ist() -> str:
    """Return today's date string in IST (YYYY-MM-DD)."""
    return datetime.now(tz=_IST).date().isoformat()
from config.logger import get_logger
from agents.execution.trailing_sl import TrailingStopManager
from agents.execution.position_sizing import calculate_position_size

log = get_logger(__name__)


@dataclass
class PaperTrade:
    """A single paper trade."""
    id: int
    symbol: str
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    stop_loss: float
    target: float
    entry_time: str
    quantity: float = 1.0
    status: str = "OPEN"  # OPEN, CLOSED_TP, CLOSED_SL, CLOSED_TRAIL, CLOSED_MANUAL
    exit_price: float = 0.0
    exit_time: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0


class PaperTradingEngine:
    """
    Paper trading engine with full risk management.

    Features:
        - Virtual trade execution (BUY/SELL)
        - Position sizing based on risk percentage
        - Trailing stop-loss (ATR-based)
        - Cooldown timer per symbol
        - Real-time SL/TP monitoring against price updates
        - Consecutive loss circuit breaker
        - Daily loss limit
        - Trade history persistence (JSON file)
    """

    def __init__(
        self,
        initial_capital: float = 100_000.0,
        log_file: str = "paper_trades.json",
    ):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.log_file = log_file
        self.trades: list[PaperTrade] = []
        self.open_trades: list[PaperTrade] = []
        self._trade_counter = 0
        self._consecutive_losses = 0
        self._daily_pnl: dict[str, float] = {}  # date_str (IST) → cumulative PnL
        self._last_trade_date: str = _today_ist()  # Track day for loss counter reset

        # Trailing SL manager
        self.trailing_manager = TrailingStopManager()

        # Cooldown tracker: symbol → candles remaining
        self._cooldowns: dict[str, int] = {}

        # Equity history for performance tracking
        self.equity_history: list[dict] = []

        # Load existing history
        self._load()

    # ── Public API ───────────────────────────────────────

    def execute(
        self,
        symbol: str,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        atr: float = 0.0,
    ) -> PaperTrade | None:
        """
        Execute a paper trade if risk rules allow it.

        Returns:
            PaperTrade if executed, None if blocked by risk rules.
        """
        # Daily reset: clear consecutive losses at start of new day (IST)
        today = _today_ist()
        if self._last_trade_date and self._last_trade_date != today:
            if self._consecutive_losses > 0:
                log.info("New trading day — resetting consecutive loss counter (%d → 0)",
                         self._consecutive_losses)
                self._consecutive_losses = 0
        self._last_trade_date = today

        # Risk check: consecutive losses
        if self._consecutive_losses >= settings.MAX_CONSECUTIVE_LOSSES:
            log.warning("BLOCKED: %d consecutive losses — pausing", self._consecutive_losses)
            return None

        # Risk check: daily loss limit
        today = _today_ist()
        daily = self._daily_pnl.get(today, 0.0)
        if abs(daily) / max(self.capital, 1) >= settings.DAILY_LOSS_LIMIT_PCT / 100:
            log.warning("BLOCKED: Daily loss limit hit (%.2f)", daily)
            return None

        # Risk check: cooldown timer
        if symbol in self._cooldowns and self._cooldowns[symbol] > 0:
            log.warning("BLOCKED: %s on cooldown (%d candles remaining)",
                        symbol, self._cooldowns[symbol])
            return None

        # Risk check: minimum RR
        if direction in ("LONG", "BUY"):
            risk = entry - sl
            reward = tp - entry
        else:
            risk = sl - entry
            reward = entry - tp

        if risk <= 0:
            log.warning("BLOCKED: Invalid SL (risk=%.2f)", risk)
            return None

        rr = reward / risk
        if rr < settings.MIN_RISK_REWARD:
            log.warning("BLOCKED: RR %.1f below minimum %.1f", rr, settings.MIN_RISK_REWARD)
            return None

        # Calculate position size
        qty = calculate_position_size(
            capital=self.capital,
            risk_pct=settings.RISK_PER_TRADE_PCT,
            entry=entry,
            stop_loss=sl,
        )
        if qty <= 0:
            qty = 1.0  # Fallback to 1 unit

        # Execute
        self._trade_counter += 1
        trade = PaperTrade(
            id=self._trade_counter,
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            target=tp,
            quantity=qty,
            entry_time=_now_ist_isoformat(),
        )

        self.trades.append(trade)
        self.open_trades.append(trade)

        # Register with trailing SL manager
        if atr > 0:
            self.trailing_manager.register(
                trade_id=trade.id,
                direction=direction,
                entry=entry,
                sl=sl,
                atr=atr,
            )

        self._save()

        log.info(
            "✅ #%d %s %s @ %.2f | SL: %.2f | TP: %.2f | RR: %.1f | Qty: %.4f",
            trade.id, direction, symbol, entry, sl, tp, rr, qty,
        )
        return trade

    def update_prices(self, prices: dict[str, dict], atr_values: dict[str, float] = None) -> list[PaperTrade]:
        """
        Check open trades against current prices and close if SL/TP hit.

        Args:
            prices: {"BTC/USDT": {"high": ..., "low": ..., "close": ...}}
            atr_values: {"BTC/USDT": 500.0} — current ATR for trailing SL updates

        Returns:
            List of trades that were closed in this update.
        """
        closed = []
        atr_values = atr_values or {}

        for trade in list(self.open_trades):
            price = prices.get(trade.symbol)
            if not price:
                continue

            high = price.get("high", 0)
            low = price.get("low", 0)

            # Update trailing SL if ATR is available
            atr = atr_values.get(trade.symbol, 0)
            if atr > 0:
                new_sl = self.trailing_manager.update(
                    trade_id=trade.id,
                    direction=trade.direction,
                    current_high=high,
                    current_low=low,
                    atr=atr,
                )
                if new_sl is not None:
                    trade.stop_loss = new_sl

            if trade.direction in ("LONG", "BUY"):
                # Check SL
                if low <= trade.stop_loss:
                    exit_reason = "CLOSED_TRAIL" if atr > 0 else "CLOSED_SL"
                    self._close_trade(trade, trade.stop_loss, exit_reason)
                    closed.append(trade)
                # Check TP
                elif high >= trade.target:
                    self._close_trade(trade, trade.target, "CLOSED_TP")
                    closed.append(trade)

            elif trade.direction in ("SHORT", "SELL"):
                # Short: SL is above
                if high >= trade.stop_loss:
                    exit_reason = "CLOSED_TRAIL" if atr > 0 else "CLOSED_SL"
                    self._close_trade(trade, trade.stop_loss, exit_reason)
                    closed.append(trade)
                elif low <= trade.target:
                    self._close_trade(trade, trade.target, "CLOSED_TP")
                    closed.append(trade)

        if closed:
            self._save()

        return closed

    def tick_cooldowns(self):
        """Decrement all cooldown timers by 1. Call once per candle."""
        for symbol in list(self._cooldowns):
            self._cooldowns[symbol] -= 1
            if self._cooldowns[symbol] <= 0:
                del self._cooldowns[symbol]

    def close_manual(self, trade_id: int, exit_price: float) -> PaperTrade | None:
        """Manually close a trade at a specified price."""
        for trade in self.open_trades:
            if trade.id == trade_id:
                self._close_trade(trade, exit_price, "CLOSED_MANUAL")
                self._save()
                return trade
        return None

    def get_open_trades(self) -> list[dict]:
        """Get all open trades as dicts."""
        return [asdict(t) for t in self.open_trades]

    def get_history(self, limit: int = 50) -> list[dict]:
        """Get closed trade history."""
        closed = [t for t in self.trades if t.status != "OPEN"]
        return [asdict(t) for t in closed[-limit:]]

    def get_equity_curve(self) -> list[dict]:
        """Get equity curve data points."""
        if not self.equity_history:
            # Rebuild from trades
            equity = self.initial_capital
            curve = [{"timestamp": "", "equity": equity}]
            for t in self.trades:
                if t.status != "OPEN":
                    equity += t.pnl * t.quantity
                    curve.append({"timestamp": t.exit_time, "equity": round(equity, 2)})
            return curve
        return self.equity_history

    def get_stats(self) -> dict:
        """Get performance stats."""
        closed = [t for t in self.trades if t.status != "OPEN"]
        if not closed:
            return {
                "total_trades": 0, "capital": self.capital,
                "total_pnl": 0, "win_rate": 0, "consecutive_losses": 0,
                "sharpe_ratio": 0, "max_drawdown": 0, "max_drawdown_pct": 0,
                "profit_factor": 0, "avg_win": 0, "avg_loss": 0,
            }

        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]
        pnls = [t.pnl * t.quantity for t in closed]
        pnl_pcts = [t.pnl_pct for t in closed]

        # Equity curve for drawdown
        equity = [self.initial_capital]
        for pnl in pnls:
            equity.append(equity[-1] + pnl)

        import numpy as np
        equity_arr = np.array(equity)
        peak = np.maximum.accumulate(equity_arr)
        drawdown = peak - equity_arr
        max_dd = float(drawdown.max())
        max_dd_pct = float((drawdown / peak).max() * 100) if peak.max() > 0 else 0

        # Profit factor
        gross_profit = sum(p for p in pnls if p > 0) or 0
        gross_loss = abs(sum(p for p in pnls if p <= 0)) or 1
        profit_factor = gross_profit / gross_loss

        # Sharpe ratio (annualized)
        if len(pnl_pcts) > 1:
            returns = np.array(pnl_pcts) / 100.0
            mean_ret = np.mean(returns)
            std_ret = np.std(returns, ddof=1)
            sharpe = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0
        else:
            sharpe = 0.0

        return {
            "total_trades": len(closed),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "capital": round(self.capital, 2),
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "avg_win": round(np.mean([t.pnl for t in wins]), 2) if wins else 0,
            "avg_loss": round(np.mean([t.pnl for t in losses]), 2) if losses else 0,
            "best_trade": round(max(pnls), 2),
            "worst_trade": round(min(pnls), 2),
            "consecutive_losses": self._consecutive_losses,
            "daily_pnl": round(self._daily_pnl.get(_today_ist(), 0), 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 1),
            "profit_factor": round(profit_factor, 2),
        }

    # ── Internal ─────────────────────────────────────────

    def _close_trade(self, trade: PaperTrade, exit_price: float, status: str):
        """Close a trade and update metrics."""
        trade.exit_price = exit_price
        trade.exit_time = _now_ist_isoformat()
        trade.status = status

        if trade.direction in ("LONG", "BUY"):
            trade.pnl = exit_price - trade.entry_price
        else:
            trade.pnl = trade.entry_price - exit_price

        trade.pnl_pct = round((trade.pnl / trade.entry_price) * 100, 3)

        # PnL weighted by quantity
        weighted_pnl = trade.pnl * trade.quantity
        self.capital += weighted_pnl
        self.open_trades.remove(trade)

        # Remove from trailing manager
        self.trailing_manager.remove(trade.id)

        # Start cooldown for this symbol
        self._cooldowns[trade.symbol] = settings.COOLDOWN_CANDLES

        # Track daily PnL (keyed by IST date)
        today = _today_ist()
        self._daily_pnl[today] = self._daily_pnl.get(today, 0.0) + weighted_pnl

        # Track equity history
        self.equity_history.append({
            "timestamp": trade.exit_time,
            "equity": round(self.capital, 2),
        })

        # Track consecutive losses
        if trade.pnl <= 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        if trade.pnl > 0:
            log.info("🎯 #%d %s @ %.2f | PnL: %+.2f (%+.2f%%) | Qty: %.4f",
                     trade.id, status, exit_price, trade.pnl, trade.pnl_pct, trade.quantity)
        else:
            log.warning("❌ #%d %s @ %.2f | PnL: %+.2f (%+.2f%%) | Qty: %.4f",
                        trade.id, status, exit_price, trade.pnl, trade.pnl_pct, trade.quantity)

    def _save(self):
        """Persist trades to JSON file."""
        data = {
            "capital": self.capital,
            "initial_capital": self.initial_capital,
            "consecutive_losses": self._consecutive_losses,
            "last_trade_date": self._last_trade_date,
            "daily_pnl": self._daily_pnl,
            "cooldowns": self._cooldowns,
            "equity_history": self.equity_history[-500:],  # Keep last 500 points
            "trades": [asdict(t) for t in self.trades],
        }
        with open(self.log_file, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _load(self):
        """Load trades from JSON file if it exists."""
        if not os.path.exists(self.log_file):
            return

        try:
            with open(self.log_file) as f:
                data = json.load(f)

            self.capital = data.get("capital", self.initial_capital)
            self._consecutive_losses = data.get("consecutive_losses", 0)
            self._last_trade_date = data.get("last_trade_date", _today_ist())
            self._daily_pnl = data.get("daily_pnl", {})
            self._cooldowns = data.get("cooldowns", {})
            self.equity_history = data.get("equity_history", [])

            for td in data.get("trades", []):
                # Handle older trade format without quantity
                if "quantity" not in td:
                    td["quantity"] = 1.0
                trade = PaperTrade(**td)
                self.trades.append(trade)
                if trade.status == "OPEN":
                    self.open_trades.append(trade)

            self._trade_counter = max((t.id for t in self.trades), default=0)
            log.debug("Loaded %d trades from %s", len(self.trades), self.log_file)
        except (json.JSONDecodeError, TypeError):
            log.error("Could not load trade history from %s — starting fresh", self.log_file)
