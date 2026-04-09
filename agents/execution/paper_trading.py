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
from db.runtime_state import get_runtime_state, set_runtime_state

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

# ── Singleton outcome logger (lazy-initialised) ───────────────────────────────
_outcome_logger = None

def _get_outcome_logger():
    global _outcome_logger
    if _outcome_logger is None:
        try:
            from agents.analytics.feedback_loop import TradeOutcomeLogger
            _outcome_logger = TradeOutcomeLogger()
        except Exception as e:
            log.warning("TradeOutcomeLogger unavailable: %s — self-learning disabled", e)
            _outcome_logger = _NoopLogger()
    return _outcome_logger


class _NoopLogger:
    """Fallback when feedback_loop is unavailable — silently does nothing."""
    def log_closed_trade(self, **_kwargs): pass



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
    # ── Self-Learning metadata (captured at entry, stored with trade) ──────
    signal_type: str = "COMBINED"   # RSI_BOUNCE / MACD_CROSS / BREAKOUT / EMA_CROSS / DIVERGENCE
    regime: str = "UNKNOWN"         # TRENDING / RANGING / HIGH_VOLATILITY
    rsi_at_entry: float = 0.0       # RSI value at signal bar
    volume_above_avg: bool = False   # was volume > 1.5x avg at entry?
    confidence: float = 0.0         # AI confidence score at signal time


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
        self._state_key = f"paper_trading_state:{os.path.abspath(self.log_file)}"
        self.trades: list[PaperTrade] = []
        self.open_trades: list[PaperTrade] = []
        self._trade_counter = 0
        self._consecutive_losses = 0
        self._daily_pnl: dict[str, float] = {}  # date_str (IST) → cumulative PnL
        self._last_trade_date: str = _today_ist()  # Track day for loss counter reset
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""

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
        # ── Self-learning context (optional but recommended) ────────────────
        signal_type: str = "COMBINED",
        regime: str = "UNKNOWN",
        rsi_at_entry: float = 0.0,
        volume_above_avg: bool = False,
        confidence: float = 0.0,
    ) -> PaperTrade | None:
        """
        Execute a paper trade if risk rules allow it.

        Extra keyword params (signal_type, regime, rsi_at_entry, volume_above_avg,
        confidence) are stored on the trade and later forwarded to the feedback
        logger when the trade closes.

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

        # Risk check: circuit breaker drawdown gate (latched until reset)
        self._update_circuit_breaker_state()
        if self._circuit_breaker_active:
            log.warning("BLOCKED: Circuit breaker active (%s)", self._circuit_breaker_reason or "risk breach")
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
        qty = self._apply_correlation_size_reduction(symbol, qty)
        if qty <= 0:
            qty = 1.0  # Fallback to 1 unit

        # Risk check: portfolio heat (projected risk after this trade)
        projected_heat = self._projected_portfolio_heat_pct(entry, sl, qty)
        if projected_heat > settings.MAX_PORTFOLIO_HEAT_PCT:
            log.warning(
                "BLOCKED: Portfolio heat %.2f%% exceeds max %.2f%%",
                projected_heat * 100,
                settings.MAX_PORTFOLIO_HEAT_PCT * 100,
            )
            return None

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
            signal_type=signal_type,
            regime=regime,
            rsi_at_entry=rsi_at_entry,
            volume_above_avg=volume_above_avg,
            confidence=confidence,
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

    def get_portfolio_heat_pct(self) -> float:
        """Current portfolio heat = open risk / capital."""
        if self.capital <= 0:
            return 0.0
        open_risk = 0.0
        for trade in self.open_trades:
            risk_per_unit = abs(trade.entry_price - trade.stop_loss)
            open_risk += risk_per_unit * max(trade.quantity, 0.0)
        return max(open_risk / self.capital, 0.0)

    def get_risk_state(self) -> dict:
        """Expose live risk guard state for API consumers."""
        self._update_circuit_breaker_state()
        daily = self._daily_pnl.get(_today_ist(), 0.0)
        drawdown_pct = 0.0
        if self.initial_capital > 0:
            drawdown_pct = max((self.initial_capital - self.capital) / self.initial_capital, 0.0)
        daily_loss_pct = abs(daily) / max(self.capital, 1.0)
        return {
            "circuit_breaker_active": self._circuit_breaker_active,
            "circuit_breaker_reason": self._circuit_breaker_reason,
            "drawdown_pct": round(drawdown_pct, 6),
            "drawdown_threshold": float(settings.CIRCUIT_BREAKER_THRESHOLD),
            "portfolio_heat_pct": round(self.get_portfolio_heat_pct(), 6),
            "portfolio_heat_max": float(settings.MAX_PORTFOLIO_HEAT_PCT),
            "consecutive_losses": int(self._consecutive_losses),
            "max_consecutive_losses": int(settings.MAX_CONSECUTIVE_LOSSES),
            "daily_loss_pct": round(daily_loss_pct, 6),
            "daily_loss_limit_pct": float(settings.DAILY_LOSS_LIMIT_PCT / 100.0),
        }

    def reset_circuit_breaker(self, rebase_capital: bool = False) -> dict:
        """Manual risk reset; can optionally rebase initial capital to current capital."""
        if rebase_capital and self.capital > 0:
            self.initial_capital = self.capital

        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""
        self._consecutive_losses = 0
        self._save()
        return self.get_risk_state()

    # ── Internal ─────────────────────────────────────────

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        s = (symbol or "").upper().replace("/", "")
        aliases = {
            "RELIANCE": "RELIANCE.NS",
            "ONGC": "ONGC.NS",
            "HDFCBANK": "HDFCBANK.NS",
            "ICICIBANK": "ICICIBANK.NS",
            "KOTAKBANK": "KOTAKBANK.NS",
        }
        return aliases.get(s, s)

    def _has_correlated_open_position(self, symbol: str) -> bool:
        candidate = self._normalize_symbol(symbol)
        for group in settings.CORRELATION_GROUPS:
            normalized_group = {self._normalize_symbol(item) for item in group}
            if candidate not in normalized_group:
                continue
            for trade in self.open_trades:
                open_symbol = self._normalize_symbol(trade.symbol)
                if open_symbol in normalized_group and open_symbol != candidate:
                    return True
        return False

    def _apply_correlation_size_reduction(self, symbol: str, qty: float) -> float:
        if qty <= 0:
            return 0.0
        if self._has_correlated_open_position(symbol):
            return round(qty * 0.5, 6)
        return qty

    def _projected_portfolio_heat_pct(self, entry: float, stop_loss: float, qty: float) -> float:
        if self.capital <= 0:
            return 1.0
        existing_risk = self.get_portfolio_heat_pct() * self.capital
        new_risk = abs(entry - stop_loss) * max(qty, 0.0)
        return max((existing_risk + new_risk) / self.capital, 0.0)

    def _update_circuit_breaker_state(self) -> None:
        if self.initial_capital <= 0:
            return
        drawdown = max((self.initial_capital - self.capital) / self.initial_capital, 0.0)
        if drawdown >= settings.CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_breaker_active = True
            self._circuit_breaker_reason = (
                f"drawdown {drawdown * 100:.2f}% >= {settings.CIRCUIT_BREAKER_THRESHOLD * 100:.2f}%"
            )

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

        # ── Self-learning: log outcome to SQLite ──────────────────────────
        try:
            from agents.analytics.feedback_loop import TradeOutcomeLogger
            logger = _get_outcome_logger()
            logger.log_closed_trade(
                symbol=trade.symbol,
                direction=trade.direction,
                signal_type=getattr(trade, "signal_type", "COMBINED"),
                regime=getattr(trade, "regime", "UNKNOWN"),
                rsi_at_entry=getattr(trade, "rsi_at_entry", None),
                volume_above_avg=bool(getattr(trade, "volume_above_avg", False)),
                entry_price=trade.entry_price,
                exit_price=exit_price,
                pnl_pct=trade.pnl_pct,
                exit_reason=status,
                confidence=getattr(trade, "confidence", 0.0),
            )
        except Exception as _fb_err:
            log.debug("Feedback logger skipped: %s", _fb_err)

    def _save(self):
        """Persist paper trading state to DB runtime state."""
        data = {
            "capital": self.capital,
            "initial_capital": self.initial_capital,
            "consecutive_losses": self._consecutive_losses,
            "last_trade_date": self._last_trade_date,
            "daily_pnl": self._daily_pnl,
            "cooldowns": self._cooldowns,
            "equity_history": self.equity_history[-500:],  # Keep last 500 points
            "trades": [asdict(t) for t in self.trades],
            "circuit_breaker_active": self._circuit_breaker_active,
            "circuit_breaker_reason": self._circuit_breaker_reason,
        }
        set_runtime_state(self._state_key, data)

    def _load_from_payload(self, data: dict) -> None:
        self.capital = data.get("capital", self.initial_capital)
        self.initial_capital = data.get("initial_capital", self.initial_capital)
        self._consecutive_losses = data.get("consecutive_losses", 0)
        self._last_trade_date = data.get("last_trade_date", _today_ist())
        self._daily_pnl = data.get("daily_pnl", {})
        self._cooldowns = data.get("cooldowns", {})
        self._circuit_breaker_active = bool(data.get("circuit_breaker_active", False))
        self._circuit_breaker_reason = str(data.get("circuit_breaker_reason", ""))
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

    def _load(self):
        """Load state from DB runtime state, then fallback legacy JSON once."""
        data = get_runtime_state(self._state_key)
        if isinstance(data, dict):
            try:
                self._load_from_payload(data)
                log.debug("Loaded %d trades from DB runtime state", len(self.trades))
                return
            except Exception as exc:
                log.error("Could not load DB paper trading state: %s", exc)

        if not os.path.exists(self.log_file):
            return

        try:
            with open(self.log_file) as f:
                data = json.load(f)

            self._load_from_payload(data)
            set_runtime_state(self._state_key, data)
            log.debug("Loaded %d trades from %s", len(self.trades), self.log_file)
        except (json.JSONDecodeError, TypeError):
            log.error("Could not load trade history from %s — starting fresh", self.log_file)
