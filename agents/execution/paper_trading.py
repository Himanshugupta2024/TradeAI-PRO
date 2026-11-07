import sys
import tempfile
import uuid
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
from pathlib import Path
from datetime import datetime, date, timezone, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict

from config import settings
import math
from db.runtime_state import get_runtime_state, set_runtime_state, DatabaseStateError

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
    total_pnl: float = 0.0
    initial_quantity: float = 0.0
    original_target: float = 0.0
    target_multiplier: float = 1.0
    partial_target: float = 0.0
    partial_taken: bool = False
    partial_exit_price: float = 0.0
    partial_exit_time: str = ""
    partial_closed_qty: float = 0.0
    realized_pnl: float = 0.0
    # ── Self-Learning metadata (captured at entry, stored with trade) ──────
    signal_type: str = "COMBINED"   # RSI_BOUNCE / MACD_CROSS / BREAKOUT / EMA_CROSS / DIVERGENCE
    regime: str = "UNKNOWN"         # TRENDING / RANGING / HIGH_VOLATILITY
    rsi_at_entry: float = 0.0       # RSI value at signal bar
    volume_above_avg: bool = False   # was volume > 1.5x avg at entry?
    confidence: float = 0.0         # AI confidence score at signal time
    # ── Raw vs Final Geometry Audit ───────────────────────────────────────
    entry_raw: float = 0.0
    stop_raw: float = 0.0
    target_raw: float = 0.0
    # ── Multi-Currency Accounting metadata ────────────────────────────────
    asset_class: str = "EQUITY"
    quote_currency: str = "INR"
    base_currency: str = "INR"
    fx_rate_entry: float = 1.0
    fx_rate_exit: float = 1.0
    fx_rate_source: str = "CANONICAL"
    fx_rate_timestamp: str = ""
    total_pnl_native: float = 0.0
    pnl_native: float = 0.0
    realized_pnl_native: float = 0.0
    fx_status: str = "ACTIVE" 
    # ── Cost-aware accounting (gross stays in total_pnl) ──────────────────
    estimated_costs: float = 0.0
    cost_profile: str = ""
    net_pnl: float = 0.0


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
        state_key: str | None = None,
    ):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.log_file = log_file
        default_state_paths = {
            os.path.abspath("paper_trades.json"),
            os.path.abspath("logs/paper_trades.json"),
            os.path.abspath(str(getattr(settings, "LOG_DIR", Path("logs")) / "paper_trades.json")),
        }
        if state_key is not None:
            self._state_key = state_key
        elif log_file in ("paper_trades.json", "logs/paper_trades.json") or os.path.abspath(log_file) in default_state_paths:
            self._state_key = os.getenv("PAPER_TRADING_STATE_KEY", "paper_trading_state:v1")
        else:
            self._state_key = f"paper_trading_state:{os.path.abspath(log_file)}"
        self.trades: list[PaperTrade] = []
        self.open_trades: list[PaperTrade] = []
        self._trade_counter = 0
        self._consecutive_losses = 0
        self._daily_pnl: dict[str, float] = {}  # date_str (IST) → cumulative PnL
        self._last_trade_date: str = _today_ist()  # Track day for loss counter reset
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""
        self.last_rejection_reason: str = ""

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
        min_rr: float | None = None,
        allow_duplicate_symbol: bool = False,
    ) -> PaperTrade | None:
        """
        Execute a paper trade if risk rules allow it.

        Extra keyword params (signal_type, regime, rsi_at_entry, volume_above_avg,
        confidence) are stored on the trade and later forwarded to the feedback
        logger when the trade closes.

        Returns:
            PaperTrade if executed, None if blocked by risk rules.
        """
        self.last_rejection_reason = ""

        # Daily reset: clear consecutive losses at start of new day (IST)
        today = _today_ist()
        if self._last_trade_date and self._last_trade_date != today:
            if self._consecutive_losses > 0:
                log.info("New trading day — resetting consecutive loss counter (%d → 0)",
                         self._consecutive_losses)
                self._consecutive_losses = 0
        self._last_trade_date = today

        # Safeguard: prevent trading with non-positive capital
        if self.capital <= 0:
            self.last_rejection_reason = f"Insufficient capital (current capital is {self.capital:.2f})"
            log.warning("BLOCKED: %s", self.last_rejection_reason)
            return None

        # Risk check: consecutive losses
        if self._consecutive_losses >= settings.MAX_CONSECUTIVE_LOSSES:
            self.last_rejection_reason = (
                f"Max consecutive losses reached ({self._consecutive_losses}/{settings.MAX_CONSECUTIVE_LOSSES}) - trading paused for today"
            )
            log.warning("BLOCKED: %d consecutive losses — pausing", self._consecutive_losses)
            return None

        # Risk check: circuit breaker drawdown gate (latched until reset)
        self._update_circuit_breaker_state()
        if self._circuit_breaker_active:
            self.last_rejection_reason = (
                f"Circuit breaker active ({self._circuit_breaker_reason or 'risk breach'})"
            )
            log.warning("BLOCKED: Circuit breaker active (%s)", self._circuit_breaker_reason or "risk breach")
            return None

        # Risk check: daily loss limit (only block on losses, not profitable days)
        today = _today_ist()
        daily = self._daily_pnl.get(today, 0.0)
        if daily < 0 and abs(daily) / max(self.capital, 1) >= settings.DAILY_LOSS_LIMIT_PCT / 100:
            self.last_rejection_reason = (
                f"Daily loss limit hit ({daily:.2f} PnL, max {settings.DAILY_LOSS_LIMIT_PCT}%)"
            )
            log.warning("BLOCKED: Daily loss limit hit (%.2f)", daily)
            return None

        # Risk check: max open positions
        max_open = int(getattr(settings, "PAPER_TRADING_MAX_OPEN", 5))
        if len(self.open_trades) >= max_open:
            self.last_rejection_reason = (
                f"Maximum open positions limit ({len(self.open_trades)}/{max_open}) reached"
            )
            log.warning("BLOCKED: %s", self.last_rejection_reason)
            return None

        # Risk check: duplicate open trade on the same symbol
        norm_sym = self._normalize_symbol(symbol)
        if not allow_duplicate_symbol and any(self._normalize_symbol(t.symbol) == norm_sym for t in self.open_trades):
            self.last_rejection_reason = f"An open position already exists for {symbol}"
            log.warning("BLOCKED: %s", self.last_rejection_reason)
            return None

        # Risk check: cooldown timer (normalized key check)
        norm_sym = self._normalize_symbol(symbol)
        if norm_sym in self._cooldowns and self._cooldowns[norm_sym] > 0:
            self.last_rejection_reason = (
                f"{symbol} on cooldown ({self._cooldowns[norm_sym]} candles remaining)"
            )
            log.warning("BLOCKED: %s on cooldown (%d candles remaining)",
                        symbol, self._cooldowns[norm_sym])
            return None

        # Macro Sector Risk Gate Check (Crude / Yield / Global headwinds)
        try:
            from agents.market_intelligence.global_macro_fetcher import get_global_macro_fetcher
            macro_ok, macro_reason = get_global_macro_fetcher().evaluate_macro_sector_gate(symbol, direction)
            if not macro_ok:
                self.last_rejection_reason = macro_reason or "Macro Sector Risk Gate triggered"
                log.warning("BLOCKED: %s", self.last_rejection_reason)
                return None
        except Exception as m_err:
            log.debug("Macro sector gate check skipped: %s", m_err)

        # Normalize and validate direction
        if direction in ("LONG", "BUY"):
            direction = "LONG"
        elif direction in ("SHORT", "SELL"):
            direction = "SHORT"
        else:
            self.last_rejection_reason = f"Invalid direction '{direction}' for {symbol}"
            log.warning("BLOCKED: Invalid direction '%s' for %s", direction, symbol)
            return None

        from agents.intraday_engine.types import normalize_price_to_tick, format_price_by_magnitude

        entry_raw, sl_raw, tp_raw = float(entry), float(sl), float(tp)

        # Normalize execution prices strictly to instrument/exchange tick rules
        if direction == "LONG":
            entry = normalize_price_to_tick(symbol, entry_raw, "ROUND")
            sl = normalize_price_to_tick(symbol, sl_raw, "FLOOR")
            tp = normalize_price_to_tick(symbol, tp_raw, "CEIL")
            if not (sl < entry < tp):
                self.last_rejection_reason = (
                    f"SKIP_INVALID_GEOMETRY: Stop Loss ({sl}) < Entry ({entry}) < Target ({tp}) required for LONG on {symbol}"
                )
                log.warning("BLOCKED: %s", self.last_rejection_reason)
                return None
            risk = entry - sl
            reward = tp - entry
        else:  # SHORT
            entry = normalize_price_to_tick(symbol, entry_raw, "ROUND")
            sl = normalize_price_to_tick(symbol, sl_raw, "CEIL")
            tp = normalize_price_to_tick(symbol, tp_raw, "FLOOR")
            if not (tp < entry < sl):
                self.last_rejection_reason = (
                    f"SKIP_INVALID_GEOMETRY: Target ({tp}) < Entry ({entry}) < Stop Loss ({sl}) required for SHORT on {symbol}"
                )
                log.warning("BLOCKED: %s", self.last_rejection_reason)
                return None
            risk = sl - entry
            reward = entry - tp

        if risk <= 0:
            self.last_rejection_reason = f"Invalid SL (risk={risk:.2f} must be positive)"
            log.warning("BLOCKED: Invalid SL (risk=%.2f)", risk)
            return None

        rr = reward / risk
        if min_rr is not None and min_rr > 0:
            required_rr = float(min_rr)
        elif "SWING" in str(signal_type or "").upper():
            required_rr = float(getattr(settings, "SWING_MIN_RISK_REWARD", 1.5))
        elif any(k in str(signal_type or "").upper() for k in ("INTRADAY", "ORB", "VWAP", "GAP", "CPR", "SUPERTREND")):
            required_rr = float(getattr(settings, "INTRADAY_MIN_RISK_REWARD", 1.5))
        else:
            required_rr = float(getattr(settings, "MIN_RISK_REWARD", 2.0))

        if rr < required_rr:
            self.last_rejection_reason = (
                f"RR {rr:.2f} below minimum required {required_rr:.1f} for {signal_type}"
            )
            log.warning("BLOCKED: RR %.2f below minimum %.1f", rr, required_rr)
            return None

        # Regime-aware target scaling for better exit fit across market states.
        target_multiplier = self._target_multiplier_for_regime(regime=regime, direction=direction)
        adjusted_tp = self._scale_target(entry=entry, target=tp, direction=direction, multiplier=target_multiplier)
        adjusted_tp = normalize_price_to_tick(symbol, adjusted_tp, "CEIL" if direction == "LONG" else "FLOOR")

        # Recompute and validate RR after regime target scaling (never allow target to shrink below required RR)
        if direction == "LONG":
            adjusted_reward = adjusted_tp - entry
        else:
            adjusted_reward = entry - adjusted_tp

        adjusted_rr = adjusted_reward / risk
        if adjusted_rr < required_rr:
            self.last_rejection_reason = (
                f"Adjusted RR {adjusted_rr:.2f} below minimum {required_rr:.2f} after regime scaling"
            )
            log.warning("BLOCKED: %s", self.last_rejection_reason)
            return None

        partial_target = self._partial_target(entry=entry, target=adjusted_tp, direction=direction)

        # Safeguard: prevent trading with non-positive capital
        if self.capital <= 0:
            self.last_rejection_reason = f"Insufficient capital (current capital is {self.capital:.2f})"
            log.warning("BLOCKED: %s", self.last_rejection_reason)
            return None

        # Safeguard: check for NaN / Inf
        for val_name, val_val in [("entry", entry), ("sl", sl), ("tp", tp), ("atr", atr)]:
            if math.isnan(val_val) or math.isinf(val_val):
                self.last_rejection_reason = f"Non-finite value in {val_name}"
                log.warning("BLOCKED: %s", self.last_rejection_reason)
                return None

        # Safeguard: strictly validate trade geometry
        fmt = ".6f" if entry < 1.0 else (".4f" if entry < 10.0 else ".2f")
        if direction == "LONG" and not (sl < entry < tp):
            self.last_rejection_reason = (
                f"SKIP_INVALID_GEOMETRY: Invalid trade geometry for LONG — SL ({sl:{fmt}}) < entry ({entry:{fmt}}) < TP ({tp:{fmt}}) required"
            )
            log.warning("BLOCKED: %s", self.last_rejection_reason)
            return None
        elif direction == "SHORT" and not (tp < entry < sl):
            self.last_rejection_reason = (
                f"SKIP_INVALID_GEOMETRY: Invalid trade geometry for SHORT — TP ({tp:{fmt}}) < entry ({entry:{fmt}}) < SL ({sl:{fmt}}) required"
            )
            log.warning("BLOCKED: %s", self.last_rejection_reason)
            return None

        # Resolve instrument currency and FX rate
        from agents.execution.currency import (
            resolve_instrument_identity,
            FXRateService,
            MissingFXRateError,
            to_decimal,
            quantize_inr,
        )
        asset_class, quote_curr, base_curr = resolve_instrument_identity(symbol)
        try:
            fx_rec = FXRateService.get_instance().get_fx_rate(quote_curr, base_curr)
            fx_rate = float(fx_rec.rate)
            fx_source = fx_rec.source
            fx_ts = fx_rec.timestamp
        except MissingFXRateError as fx_err:
            self.last_rejection_reason = f"SKIP_MISSING_FX: Cannot resolve FX rate for {quote_curr}->{base_curr}: {fx_err}"
            log.warning("BLOCKED: %s", self.last_rejection_reason)
            return None

        # Convert base capital to quote currency for position sizing
        capital_in_quote = float(to_decimal(self.capital) / to_decimal(fx_rate))

        # Calculate position size in native quote units
        qty = calculate_position_size(
            capital=capital_in_quote,
            risk_pct=settings.RISK_PER_TRADE_PCT,
            entry=entry,
            stop_loss=sl,
            max_leverage=getattr(settings, "MAX_POSITION_LEVERAGE", 1.0),
        )
        qty = self._apply_correlation_size_reduction(symbol, qty)
        if qty <= 0:
            max_lev = getattr(settings, "MAX_POSITION_LEVERAGE", 1.0)
            if self.capital * max_lev >= entry:
                qty = 1.0
            else:
                self.last_rejection_reason = f"Insufficient purchasing power for minimum position (capital={self.capital:.2f}, entry={entry:.2f})"
                log.warning("BLOCKED: %s", self.last_rejection_reason)
                return None

        # Risk check: portfolio-wide gross exposure limit (converted to base currency INR)
        current_exposure = sum(float(to_decimal(t.entry_price * t.quantity) * to_decimal(getattr(t, "fx_rate_entry", 1.0))) for t in self.open_trades)
        max_exposure = self.capital * float(getattr(settings, "MAX_PORTFOLIO_LEVERAGE", 1.0))
        new_exposure = float(to_decimal(abs(entry * qty)) * to_decimal(fx_rate))
        if current_exposure + new_exposure > max_exposure:
            remaining_exposure = max(max_exposure - current_exposure, 0.0)
            if remaining_exposure >= entry:
                qty = math.floor(remaining_exposure / entry) if entry >= 10 else round(remaining_exposure / entry, 4)
            else:
                self.last_rejection_reason = (
                    f"Portfolio leverage limit exceeded (current={current_exposure:.2f}, new={new_exposure:.2f}, max={max_exposure:.2f})"
                )
                log.warning("BLOCKED: %s", self.last_rejection_reason)
                return None

        # Risk check: portfolio heat (projected risk after this trade)
        projected_heat = self._projected_portfolio_heat_pct(entry, sl, qty, fx_rate=fx_rate)
        if projected_heat > settings.MAX_PORTFOLIO_HEAT_PCT:
            self.last_rejection_reason = (
                f"Portfolio heat {projected_heat * 100:.2f}% exceeds max {settings.MAX_PORTFOLIO_HEAT_PCT * 100:.2f}%"
            )
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
            target=adjusted_tp,
            quantity=qty,
            initial_quantity=qty,
            original_target=tp,
            target_multiplier=target_multiplier,
            partial_target=partial_target,
            entry_time=_now_ist_isoformat(),
            signal_type=signal_type,
            regime=regime,
            rsi_at_entry=rsi_at_entry,
            volume_above_avg=volume_above_avg,
            confidence=confidence,
            entry_raw=entry_raw,
            stop_raw=sl_raw,
            target_raw=tp_raw,
            asset_class=asset_class,
            quote_currency=quote_curr,
            base_currency=base_curr,
            fx_rate_entry=fx_rate,
            fx_rate_exit=fx_rate,
            fx_rate_source=fx_source,
            fx_rate_timestamp=fx_ts,
            total_pnl_native=0.0,
            pnl_native=0.0,
            realized_pnl_native=0.0,
            fx_status="ACTIVE",
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
            "✅ #%d %s %s @ %.2f | SL: %.2f | TP: %.2f (x%.2f) | RR: %.1f | Qty: %.4f",
            trade.id, direction, symbol, entry, sl, adjusted_tp, target_multiplier, rr, qty,
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
        state_changed = False
        atr_values = atr_values or {}

        for trade in list(self.open_trades):
            price = prices.get(trade.symbol)
            if not price:
                continue

            raw_high = price.get("high")
            raw_low = price.get("low")

            try:
                high = float(raw_high)
                low = float(raw_low)
            except (TypeError, ValueError):
                log.warning("Skipping invalid price bar for %s: high=%s low=%s", trade.symbol, raw_high, raw_low)
                continue

            if (
                not math.isfinite(high)
                or not math.isfinite(low)
                or high <= 0
                or low <= 0
                or low > high
            ):
                log.warning(
                    "Skipping malformed price bar for %s: high=%s low=%s",
                    trade.symbol,
                    high,
                    low,
                )
                continue

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
                if new_sl is not None and new_sl != trade.stop_loss:
                    trade.stop_loss = new_sl
                    state_changed = True

            if trade.direction in ("LONG", "BUY"):
                # Check SL
                if low <= trade.stop_loss:
                    exit_reason = "CLOSED_TRAIL" if atr > 0 else "CLOSED_SL"
                    self._close_trade(trade, trade.stop_loss, exit_reason)
                    closed.append(trade)
                else:
                    # Optional scale-out: book partial at mid-target and trail remainder.
                    partial_changed = self._maybe_take_partial_profit(trade, high=high, low=low, atr=atr)
                    if partial_changed:
                        state_changed = True
                    # Check TP
                    if high >= trade.target:
                        self._close_trade(trade, trade.target, "CLOSED_TP")
                        closed.append(trade)

            elif trade.direction in ("SHORT", "SELL"):
                # Short: SL is above
                if high >= trade.stop_loss:
                    exit_reason = "CLOSED_TRAIL" if atr > 0 else "CLOSED_SL"
                    self._close_trade(trade, trade.stop_loss, exit_reason)
                    closed.append(trade)
                else:
                    partial_changed = self._maybe_take_partial_profit(trade, high=high, low=low, atr=atr)
                    if partial_changed:
                        state_changed = True
                    if low <= trade.target:
                        self._close_trade(trade, trade.target, "CLOSED_TP")
                        closed.append(trade)

        if closed or state_changed:
            self._save()

        return closed

    def check_eod_squareoff(self, prices: dict[str, dict] = None, dt: Optional[datetime] = None) -> list[PaperTrade]:
        """
        Auto-square-off open intraday equity trades at or after 15:15 IST (Mon-Fri).
        Prevents overnight gap-down risk for intraday setups.
        Crypto pairs (containing '/') are exempt and run 24/7 with ATR trailing stops.
        """
        prices = prices or {}
        closed = []
        now_ist = dt or datetime.now(tz=_IST)
        ist_minutes = now_ist.hour * 60 + now_ist.minute
        is_weekday = now_ist.weekday() < 5

        # Check if current IST time is >= 15:15 and <= 16:30 on weekdays
        if not (is_weekday and (15 * 60 + 15) <= ist_minutes <= (16 * 60 + 30)):
            return closed

        for trade in list(self.open_trades):
            # Crypto and forex pairs skip NSE 15:15 IST equity square-off
            sym_u = trade.symbol.upper()
            if "/" in sym_u or sym_u.endswith("USDT") or sym_u in ("BTC", "ETH", "SOL", "DOGE", "XRP", "BNB"):
                continue

            # Swing trades held deliberately across days skip intraday squareoff
            if str(getattr(trade, "signal_type", "")).upper() == "SWING":
                continue

            # Determine exit price from current bar, or fallback to entry price
            p_info = prices.get(trade.symbol) or {}
            exit_price = float(p_info.get("close") or trade.entry_price)
            if not math.isfinite(exit_price) or exit_price <= 0:
                exit_price = trade.entry_price

            log.info(
                "⏰ EOD Square-Off: Closing %s %s #%d @ %.2f (IST %02d:%02d)",
                trade.direction,
                trade.symbol,
                trade.id,
                exit_price,
                now_ist.hour,
                now_ist.minute,
            )
            self._close_trade(trade, exit_price, "CLOSED_EOD_SQUAREOFF")
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
        try:
            exit_price = float(exit_price)
        except (TypeError, ValueError):
            log.warning("Invalid manual exit price '%s' for trade #%s", exit_price, trade_id)
            return None

        if not math.isfinite(exit_price) or exit_price <= 0:
            log.warning("Non-positive or non-finite manual exit price %s for trade #%s", exit_price, trade_id)
            return None

        for trade in self.open_trades:
            if trade.id == trade_id:
                self._close_trade(trade, exit_price, "CLOSED_MANUAL")
                self._save()
                return trade
        return None

    def get_open_trades(self, current_prices: dict = None) -> list[dict]:
        """Get all open trades as dicts with optional live pricing & unrealized PnL."""
        current_prices = current_prices or {}
        results = []
        for trade in self.open_trades:
            td = asdict(trade)
            price_info = current_prices.get(trade.symbol) or {}
            if isinstance(price_info, dict):
                cur_price = float(price_info.get("close", trade.entry_price))
            elif isinstance(price_info, (int, float)):
                cur_price = float(price_info)
            else:
                cur_price = trade.entry_price

            is_long = trade.direction in ("LONG", "BUY")
            qty = trade.quantity
            mult = 1.0 if is_long else -1.0
            unrealized = (cur_price - trade.entry_price) * qty * mult
            unrealized_pct = ((cur_price - trade.entry_price) / trade.entry_price * 100.0 * mult) if trade.entry_price > 0 else 0.0

            td["current_price"] = round(cur_price, 2)
            td["unrealized_pnl"] = round(unrealized, 2)
            td["unrealized_pnl_pct"] = round(unrealized_pct, 2)
            results.append(td)
        return results

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
                    equity += self._trade_total_pnl(t)
                    curve.append({"timestamp": t.exit_time, "equity": round(equity, 2)})
            return curve
        return self.equity_history

    def get_portfolio_equity(self, current_prices: dict = None) -> dict:
        """Calculate live equity including floating mark-to-market (MTM) of open trades."""
        current_prices = current_prices or {}
        unrealized_pnl = 0.0
        deployed_margin = 0.0

        for trade in self.open_trades:
            price_info = current_prices.get(trade.symbol) or {}
            if isinstance(price_info, dict):
                cur_price = float(price_info.get("close", trade.entry_price))
            elif isinstance(price_info, (int, float)):
                cur_price = float(price_info)
            else:
                cur_price = trade.entry_price

            from agents.execution.currency import FXRateService, to_decimal, quantize_inr
            qty = trade.quantity
            if trade.direction in ("LONG", "BUY"):
                mtm_native = (cur_price - trade.entry_price) * qty
            else:
                mtm_native = (trade.entry_price - cur_price) * qty

            try:
                fx_rec = FXRateService.get_instance().get_fx_rate(getattr(trade, "quote_currency", "INR"), "INR")
                fx_rate = float(fx_rec.rate)
            except Exception:
                fx_rate = float(getattr(trade, "fx_rate_entry", 1.0) or 1.0)

            mtm_base = float(quantize_inr(to_decimal(mtm_native) * to_decimal(fx_rate)))
            margin_base = float(quantize_inr(to_decimal(trade.entry_price * qty) * to_decimal(fx_rate)))

            unrealized_pnl += mtm_base
            deployed_margin += margin_base

        total_equity = self.capital + unrealized_pnl
        closed = [t for t in self.trades if t.status != "OPEN"]
        inr_closed = [
            t for t in closed
            if not (getattr(t, "fx_status", "ACTIVE") == "LEGACY_UNCONVERTED" and getattr(t, "quote_currency", "INR") != "INR")
        ]
        realized_pnl = sum(self._trade_total_pnl(t) for t in inr_closed)

        return {
            "initial_capital": round(self.initial_capital, 2),
            "cash_capital": round(self.capital, 2),
            "total_equity": round(total_equity, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "realized_pnl": round(realized_pnl, 2),
            "deployed_margin": round(deployed_margin, 2),
            "free_cash": round(max(0.0, self.capital - deployed_margin), 2),
            "open_positions": len(self.open_trades),
        }

    def reset_account(self, new_capital: float = 100000.0, clear_trades: bool = False) -> dict:
        """Reset or reconfigure self-learning paper capital."""
        if new_capital > 0:
            self.initial_capital = float(new_capital)
            self.capital = float(new_capital)
        if clear_trades:
            self.trades.clear()
            self._daily_pnl.clear()
            self.equity_history = [{"timestamp": _now_ist_isoformat(), "equity": self.capital}]
        self._circuit_breaker_active = False
        self._circuit_breaker_reason = ""
        self._consecutive_losses = 0
        self._cooldowns.clear()
        self._save()
        return self.get_stats()

    def get_stats(self, current_prices: dict = None) -> dict:
        """Get performance stats."""
        equity_info = self.get_portfolio_equity(current_prices)
        closed = [t for t in self.trades if t.status != "OPEN"]
        if not closed:
            return {
                "total_trades": 0, "sample_size": 0, "capital": self.capital,
                "total_equity": equity_info["total_equity"],
                "unrealized_pnl": equity_info["unrealized_pnl"],
                "initial_capital": equity_info["initial_capital"],
                "deployed_margin": equity_info["deployed_margin"],
                "free_cash": equity_info["free_cash"],
                "total_pnl": 0, "win_rate": None, "consecutive_losses": 0,
                "sharpe_ratio": 0, "max_drawdown": 0, "max_drawdown_pct": 0,
                "profit_factor": 0, "avg_win": 0, "avg_loss": 0,
                "open_trades": len([t for t in self.trades if t.status == "OPEN"]),
                "breakeven_trades": 0,
                "unconverted_legacy_trades": [],
            }

        # Quarantine unconverted non-INR legacy trades from INR totals (F05)
        inr_closed = [
            t for t in closed
            if not (getattr(t, "fx_status", "ACTIVE") == "LEGACY_UNCONVERTED" and getattr(t, "quote_currency", "INR") != "INR")
        ]
        unconverted_legacy = [
            t for t in closed
            if getattr(t, "fx_status", "ACTIVE") == "LEGACY_UNCONVERTED" and getattr(t, "quote_currency", "INR") != "INR"
        ]

        pnls = [self._trade_total_pnl(t) for t in inr_closed]
        wins = [t for t in inr_closed if self._trade_total_pnl(t) > 1e-6]
        losses = [t for t in inr_closed if self._trade_total_pnl(t) < -1e-6]
        breakevens = [t for t in inr_closed if abs(self._trade_total_pnl(t)) <= 1e-6]
        pnl_pcts = [t.pnl_pct for t in inr_closed]

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

        # Profit factor: undefined/None if gross_loss is 0
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

        # Sharpe & Sortino ratio
        # Annualized with sqrt(252) ONLY if >= 2 calendar daily returns exist
        daily_pnls = [v for v in self._daily_pnl.values() if v != 0]
        if len(daily_pnls) > 1:
            base_cap = max(self.initial_capital, 1.0)
            daily_returns = np.array(daily_pnls) / base_cap
            mean_ret = np.mean(daily_returns)
            std_ret = np.std(daily_returns, ddof=1)
            sharpe = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0
            downside = daily_returns[daily_returns < 0]
            downside_std = np.std(downside, ddof=1) if len(downside) > 1 else std_ret
            sortino = (mean_ret / downside_std) * np.sqrt(252) if downside_std > 0 else 0.0
        elif len(pnl_pcts) > 1:
            # Per-trade returns != daily returns: do NOT apply sqrt(252) annualization factor
            returns = np.array(pnl_pcts) / 100.0
            mean_ret = np.mean(returns)
            std_ret = np.std(returns, ddof=1)
            sharpe = round(mean_ret / std_ret, 2) if std_ret > 0 else 0.0
            downside = returns[returns < 0]
            downside_std = np.std(downside, ddof=1) if len(downside) > 1 else std_ret
            sortino = round(mean_ret / downside_std, 2) if downside_std > 0 else 0.0
        else:
            sharpe = None
            sortino = None

        return {
            "total_trades": len(closed),
            "sample_size": len(closed),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "breakeven_trades": len(breakevens),
            "win_rate": round(len(wins) / len(inr_closed) * 100, 1) if inr_closed else None,
            "capital": round(self.capital, 2),
            "total_equity": equity_info["total_equity"],
            "unrealized_pnl": equity_info["unrealized_pnl"],
            "initial_capital": equity_info["initial_capital"],
            "deployed_margin": equity_info["deployed_margin"],
            "free_cash": equity_info["free_cash"],
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
            "avg_win": round(np.mean([self._trade_total_pnl(t) for t in wins]), 2) if wins else 0.0,
            "avg_loss": round(np.mean([self._trade_total_pnl(t) for t in losses]), 2) if losses else 0.0,
            "best_trade": round(max(pnls), 2) if pnls else 0.0,
            "worst_trade": round(min(pnls), 2) if pnls else 0.0,
            "consecutive_losses": self._consecutive_losses,
            "daily_pnl": round(self._daily_pnl.get(_today_ist(), 0), 2),
            "sharpe_ratio": round(sharpe, 2) if sharpe is not None else None,
            "sortino_ratio": round(sortino, 2) if sortino is not None else None,
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 1),
            "profit_factor": profit_factor,
            "open_trades": len([t for t in self.trades if t.status == "OPEN"]),
            "unconverted_legacy_trades": [
                {
                    "id": t.id,
                    "symbol": t.symbol,
                    "quote_currency": getattr(t, "quote_currency", "USDT"),
                    "total_pnl_native": getattr(t, "total_pnl_native", t.total_pnl),
                    "fx_status": getattr(t, "fx_status", "LEGACY_UNCONVERTED"),
                }
                for t in unconverted_legacy
            ],
        }

    def get_portfolio_heat_pct(self) -> float:
        """Current portfolio heat = open risk (converted to base currency) / capital."""
        if self.capital <= 0:
            return 0.0
        open_risk_base = 0.0
        for trade in self.open_trades:
            if trade.direction in ("LONG", "BUY"):
                risk_per_unit = max(trade.entry_price - trade.stop_loss, 0.0)
            else:
                risk_per_unit = max(trade.stop_loss - trade.entry_price, 0.0)
            native_risk = risk_per_unit * max(trade.quantity, 0.0)
            fx = float(getattr(trade, "fx_rate_entry", 1.0) or 1.0)
            open_risk_base += native_risk * fx
        return max(open_risk_base / self.capital, 0.0)

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

    def _target_multiplier_for_regime(self, regime: str, direction: str) -> float:
        """Return regime-aware target scaling multiplier."""
        if not getattr(settings, "REGIME_TARGET_SCALING_ENABLED", True):
            return 1.0
        rv = str(regime or "UNKNOWN").upper()
        direction = str(direction or "").upper()

        if rv in ("STRONG_TREND_UP", "STRONG_TREND_DOWN"):
            aligned = (rv.endswith("UP") and direction == "LONG") or (rv.endswith("DOWN") and direction == "SHORT")
            if aligned:
                return float(getattr(settings, "REGIME_TARGET_STRONG_TREND_MULT", 1.5))
            return float(getattr(settings, "REGIME_TARGET_WEAK_TREND_MULT", 0.7))

        if rv in ("TRENDING",):
            return float(getattr(settings, "REGIME_TARGET_STRONG_TREND_MULT", 1.5))
        if rv in ("WEAK_TREND",):
            return float(getattr(settings, "REGIME_TARGET_WEAK_TREND_MULT", 0.7))
        if rv in ("RANGING", "RANGE", "CHOP", "CHOPPY"):
            return float(getattr(settings, "REGIME_TARGET_RANGE_MULT", 0.7))
        if rv in ("HIGH_VOLATILITY", "HIGH_VOL", "VOLATILE"):
            return float(getattr(settings, "REGIME_TARGET_HIGH_VOL_MULT", 0.7))
        return 1.0

    @staticmethod
    def _scale_target(entry: float, target: float, direction: str, multiplier: float) -> float:
        """Scale target distance from entry by multiplier while preserving side."""
        m = max(0.1, float(multiplier))
        if direction in ("LONG", "BUY"):
            dist = max(0.0, float(target) - float(entry))
            return float(entry) + dist * m
        dist = max(0.0, float(entry) - float(target))
        return float(entry) - dist * m

    @staticmethod
    def _partial_target(entry: float, target: float, direction: str) -> float:
        """Compute partial-take-profit trigger between entry and final target."""
        frac = float(getattr(settings, "PARTIAL_TP_TRIGGER_FRACTION", 0.5))
        frac = min(0.9, max(0.1, frac))
        if direction in ("LONG", "BUY"):
            return float(entry) + (float(target) - float(entry)) * frac
        return float(entry) - (float(entry) - float(target)) * frac

    # ── Internal ─────────────────────────────────────────

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        s = (symbol or "").strip().upper().replace("/", "").replace("-", "").replace(" ", "")
        if s.endswith(".NS"):
            s = s[:-3]
        elif s.endswith(".BO"):
            s = s[:-3]
        return s

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

    def _projected_portfolio_heat_pct(self, entry: float, stop_loss: float, qty: float, fx_rate: float = 1.0) -> float:
        if self.capital <= 0:
            return 1.0
        existing_risk_base = self.get_portfolio_heat_pct() * self.capital
        new_risk_native = abs(entry - stop_loss) * max(qty, 0.0)
        new_risk_base = new_risk_native * fx_rate
        return max((existing_risk_base + new_risk_base) / self.capital, 0.0)

    @staticmethod
    def _trade_total_pnl(trade: PaperTrade) -> float:
        total = float(getattr(trade, "total_pnl", 0.0) or 0.0)
        if abs(total) > 1e-12:
            return total
        return float(getattr(trade, "pnl", 0.0) or 0.0) * float(getattr(trade, "quantity", 0.0) or 0.0)

    def _maybe_take_partial_profit(self, trade: PaperTrade, *, high: float, low: float, atr: float) -> bool:
        """Take partial profit once and keep remainder running with tighter protection. Returns True if taken."""
        if not getattr(settings, "PARTIAL_TP_ENABLED", True):
            return False
        if trade.partial_taken or trade.status != "OPEN":
            return False
        if trade.quantity <= 0:
            return False

        trigger = float(trade.partial_target or 0.0)
        if trigger <= 0:
            return False

        if trade.direction in ("LONG", "BUY"):
            hit = high >= trigger
            pnl_per_unit = trigger - trade.entry_price
        else:
            hit = low <= trigger
            pnl_per_unit = trade.entry_price - trigger
        if not hit:
            return False

        frac = float(getattr(settings, "PARTIAL_TP_CLOSE_FRACTION", 0.5))
        frac = min(0.9, max(0.1, frac))
        close_qty = max(0.0, trade.quantity * frac)
        if close_qty <= 0:
            return False

        realized_native = pnl_per_unit * close_qty
        trade.partial_taken = True
        trade.partial_exit_price = trigger
        trade.partial_exit_time = _now_ist_isoformat()
        trade.partial_closed_qty = close_qty
        trade.realized_pnl_native = float(getattr(trade, "realized_pnl_native", 0.0) or 0.0) + realized_native
        trade.total_pnl_native = trade.realized_pnl_native

        from agents.execution.currency import FXRateService, to_decimal, quantize_inr
        try:
            fx_rec = FXRateService.get_instance().get_fx_rate(
                getattr(trade, "quote_currency", "INR"),
                getattr(trade, "base_currency", "INR"),
            )
            fx_rate_partial = float(fx_rec.rate)
        except Exception:
            fx_rate_partial = float(getattr(trade, "fx_rate_entry", 1.0) or 1.0)

        realized_base = float(quantize_inr(to_decimal(realized_native) * to_decimal(fx_rate_partial)))
        trade.realized_pnl = float(getattr(trade, "realized_pnl", 0.0) or 0.0) + realized_base
        trade.total_pnl = trade.realized_pnl
        trade.quantity = max(0.0, trade.quantity - close_qty)

        # Capital/equity updates in base currency INR
        self.capital += realized_base
        today = _today_ist()
        self._daily_pnl[today] = self._daily_pnl.get(today, 0.0) + realized_base
        self.equity_history.append({
            "timestamp": trade.partial_exit_time,
            "equity": round(self.capital, 2),
        })

        # Move stop to breakeven for the runner and tighten trailing state.
        if getattr(settings, "PARTIAL_TP_BREAKEVEN_ON_TRIGGER", True):
            if trade.direction in ("LONG", "BUY"):
                trade.stop_loss = max(trade.stop_loss, trade.entry_price)
            else:
                trade.stop_loss = min(trade.stop_loss, trade.entry_price)
            try:
                self.trailing_manager.tighten_to(trade.id, trade.stop_loss)
            except Exception:
                pass

        log.info(
            "📌 #%d partial booked @ %.2f | qty=%.4f | realized=%+.2f | remaining=%.4f",
            trade.id,
            trigger,
            close_qty,
            realized_base,
            trade.quantity,
        )
        return True

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
        if trade.status in ("CLOSED_TP", "CLOSED_SL", "CLOSED_TRAIL", "CLOSED_MANUAL"):
            log.warning("Trade #%s is already closed (%s). Skipping duplicate close.", trade.id, trade.status)
            return

        trade.exit_price = exit_price
        trade.exit_time = _now_ist_isoformat()
        trade.status = status

        if trade.direction in ("LONG", "BUY"):
            leg_pnl_per_unit = exit_price - trade.entry_price
        else:
            leg_pnl_per_unit = trade.entry_price - exit_price

        remaining_qty = max(float(trade.quantity), 0.0)
        remaining_leg_pnl = leg_pnl_per_unit * remaining_qty
        trade.total_pnl = float(getattr(trade, "realized_pnl", 0.0) or 0.0) + remaining_leg_pnl

        from agents.execution.currency import FXRateService, to_decimal, quantize_inr
        try:
            fx_rec = FXRateService.get_instance().get_fx_rate(
                getattr(trade, "quote_currency", "INR"),
                getattr(trade, "base_currency", "INR"),
            )
            fx_rate_exit = float(fx_rec.rate)
            fx_source = fx_rec.source
            fx_ts = fx_rec.timestamp
        except Exception:
            fx_rate_exit = float(getattr(trade, "fx_rate_entry", 1.0) or 1.0)
            fx_source = "ENTRY_RATE_FALLBACK"
            fx_ts = trade.exit_time

        trade.fx_rate_exit = fx_rate_exit
        trade.fx_rate_source = fx_source
        trade.fx_rate_timestamp = fx_ts

        total_pnl_native = float(getattr(trade, "realized_pnl_native", 0.0) or 0.0) + remaining_leg_pnl
        trade.total_pnl_native = round(total_pnl_native, 6)

        # Base currency INR amounts using Decimal quantization
        total_pnl_base = float(quantize_inr(to_decimal(total_pnl_native) * to_decimal(fx_rate_exit)))
        remaining_leg_pnl_base = float(quantize_inr(to_decimal(remaining_leg_pnl) * to_decimal(fx_rate_exit)))
        trade.total_pnl = total_pnl_base

        initial_qty = max(float(getattr(trade, "initial_quantity", 0.0) or 0.0), 1e-9)
        trade.pnl_native = trade.total_pnl_native / initial_qty
        trade.pnl = trade.total_pnl / initial_qty
        trade.pnl_pct = round((total_pnl_native / (trade.entry_price * initial_qty)) * 100, 3)

        # Cost-aware accounting: total_pnl stays gross so existing capital and
        # equity maths do not move; net_pnl is the honest, tradeable number.
        try:
            from agents.execution.trade_costs import estimate_trade_costs

            cost_est = estimate_trade_costs(
                entry_price=float(trade.entry_price),
                exit_price=float(exit_price),
                quantity=float(getattr(trade, "initial_quantity", 0.0) or remaining_qty or 1.0),
                symbol=str(trade.symbol),
                signal_type=str(getattr(trade, "signal_type", "") or ""),
            )
            trade.estimated_costs = round(float(cost_est["total_cost"]), 2)
            trade.cost_profile = str(cost_est["profile"])
            trade.net_pnl = round(float(trade.total_pnl) - trade.estimated_costs, 2)
        except Exception as cost_err:
            log.debug("Cost estimate failed for trade #%s: %s", trade.id, cost_err)
            trade.estimated_costs = 0.0
            trade.cost_profile = "UNAVAILABLE"
            trade.net_pnl = float(trade.total_pnl)

        # Capital & Daily PnL updated in base currency INR
        self.capital += remaining_leg_pnl_base
        if trade in self.open_trades:
            self.open_trades.remove(trade)

        # Remove from trailing manager
        self.trailing_manager.remove(trade.id)

        # Start cooldown for this symbol (key normalized)
        self._cooldowns[self._normalize_symbol(trade.symbol)] = settings.COOLDOWN_CANDLES

        # Track daily PnL (keyed by IST date)
        today = _today_ist()
        self._daily_pnl[today] = self._daily_pnl.get(today, 0.0) + remaining_leg_pnl_base

        # Track equity history
        self.equity_history.append({
            "timestamp": trade.exit_time,
            "equity": round(self.capital, 2),
        })

        # Track consecutive losses (strict loss: pnl < -1e-6; breakeven doesn't count as loss)
        if trade.total_pnl < -1e-6:
            self._consecutive_losses += 1
        elif trade.total_pnl > 1e-6:
            self._consecutive_losses = 0

        if trade.total_pnl > 1e-6:
            log.info("🎯 #%d %s @ %.2f | Total PnL: %+.2f (%+.2f%%) | Qty: %.4f",
                     trade.id, status, exit_price, trade.total_pnl, trade.pnl_pct, initial_qty)
        elif trade.total_pnl < -1e-6:
            log.warning("❌ #%d %s @ %.2f | Total PnL: %+.2f (%+.2f%%) | Qty: %.4f",
                        trade.id, status, exit_price, trade.total_pnl, trade.pnl_pct, initial_qty)
        else:
            log.info("⚪ #%d %s @ %.2f | Breakeven PnL: +0.00 (+0.00%%) | Qty: %.4f",
                     trade.id, status, exit_price, initial_qty)

        # ── Self-learning: log outcome to PostgreSQL (trade_outcomes_pg) ──
        try:
            from agents.analytics.feedback_loop import TradeOutcomeLogger
            logger = _get_outcome_logger()
            asset_class = "CRYPTO" if "/" in trade.symbol else ("FOREX" if any(fx in trade.symbol for fx in ("INR", "USD", "EUR", "GBP", "JPY")) and "/" in trade.symbol else "EQUITY")
            logger.log_closed_trade(
                trade_id=f"pt-{trade.id}",
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
                asset_class=asset_class,
                entry_time=trade.entry_time,
                exit_time=trade.exit_time,
                stop_loss=trade.stop_loss,
                target=trade.target,
                pnl=trade.total_pnl,
                quantity=getattr(trade, "initial_quantity", trade.quantity),
                environment="PAPER_FORWARD",
            )
        except Exception as _fb_err:
            log.debug("Feedback logger skipped: %s", _fb_err)

    def _save(self):
        """Persist paper trading state to DB runtime state atomically with local backup."""
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
        if self.log_file:
            try:
                import tempfile
                dir_name = os.path.dirname(os.path.abspath(self.log_file)) or "."
                os.makedirs(dir_name, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                    json.dump(data, tf, indent=2, default=str)
                    temp_path = tf.name
                os.replace(temp_path, self.log_file)
            except Exception as f_err:
                log.debug("Atomic file backup skipped: %s", f_err)

    def _load_from_payload(self, data: dict) -> None:
        self.capital = data.get("capital", self.initial_capital)
        self.initial_capital = data.get("initial_capital", self.initial_capital)
        self._consecutive_losses = data.get("consecutive_losses", 0)
        self._last_trade_date = data.get("last_trade_date", _today_ist())
        self._daily_pnl = data.get("daily_pnl", {})
        raw_cooldowns = data.get("cooldowns", {})
        self._cooldowns = {self._normalize_symbol(k): int(v) for k, v in raw_cooldowns.items()}
        self._circuit_breaker_active = bool(data.get("circuit_breaker_active", False))
        self._circuit_breaker_reason = str(data.get("circuit_breaker_reason", ""))
        self.equity_history = data.get("equity_history", [])
        self.trades = []
        self.open_trades = []

        for td in data.get("trades", []):
            # Handle older trade format without quantity
            if "quantity" not in td:
                td["quantity"] = 1.0
            if "initial_quantity" not in td:
                td["initial_quantity"] = td.get("quantity", 1.0)
            if "original_target" not in td:
                td["original_target"] = td.get("target", 0.0)
            if "target_multiplier" not in td:
                td["target_multiplier"] = 1.0
            td.setdefault("direction", "LONG")
            if not td.get("entry_time"):
                td["entry_time"] = datetime.now().isoformat()
            td.setdefault("entry_price", 0.0)
            td.setdefault("stop_loss", 0.0)
            td.setdefault("target", 0.0)
            if "partial_target" not in td:
                try:
                    td["partial_target"] = self._partial_target(
                        float(td.get("entry_price", 0.0)),
                        float(td.get("target", 0.0)),
                        str(td.get("direction", "LONG")),
                    )
                except Exception:
                    td["partial_target"] = 0.0
            td.setdefault("direction", "LONG")
            td.setdefault("entry_time", datetime.now().isoformat())
            td.setdefault("partial_taken", False)
            td.setdefault("partial_exit_price", 0.0)
            td.setdefault("partial_exit_time", "")
            td.setdefault("partial_closed_qty", 0.0)
            td.setdefault("realized_pnl", 0.0)
            td.setdefault("total_pnl", 0.0)
            # Multi-currency legacy hydration
            if "quote_currency" not in td:
                from agents.execution.currency import resolve_instrument_identity
                ac, qc, bc = resolve_instrument_identity(td.get("symbol", ""))
                td["asset_class"] = ac
                td["quote_currency"] = qc
                td["base_currency"] = bc
                td["fx_rate_entry"] = 1.0
                td["fx_rate_exit"] = 1.0
                td["fx_rate_source"] = "LEGACY_PRE_MIGRATION"
                td["fx_rate_timestamp"] = ""
                td["total_pnl_native"] = td.get("total_pnl", 0.0)
                td["pnl_native"] = td.get("pnl", 0.0)
                td["realized_pnl_native"] = td.get("realized_pnl", 0.0)
                td["fx_status"] = "LEGACY_UNCONVERTED" if qc != "INR" else "ACTIVE"
            else:
                td.setdefault("asset_class", "EQUITY")
                td.setdefault("quote_currency", "INR")
                td.setdefault("base_currency", "INR")
                td.setdefault("fx_rate_entry", 1.0)
                td.setdefault("fx_rate_exit", 1.0)
                td.setdefault("fx_rate_source", "CANONICAL")
                td.setdefault("fx_rate_timestamp", "")
                td.setdefault("total_pnl_native", td.get("total_pnl", 0.0))
                td.setdefault("pnl_native", td.get("pnl", 0.0))
                td.setdefault("realized_pnl_native", td.get("realized_pnl", 0.0))
                td.setdefault("fx_status", "ACTIVE")
            from dataclasses import fields as _dc_fields
            _valid_trade_fields = {f.name for f in _dc_fields(PaperTrade)}
            td_clean = {k: v for k, v in td.items() if k in _valid_trade_fields}
            trade = PaperTrade(**td_clean)
            self.trades.append(trade)
            if trade.status == "OPEN":
                self.open_trades.append(trade)

        self._trade_counter = max((t.id for t in self.trades), default=0)

    def _load(self):
        """Load state from DB runtime state, with automatic legacy key migration and fail-loud DB safety."""
        try:
            data = get_runtime_state(self._state_key)
        except DatabaseStateError as dse:
            log.critical("Failed to load paper trading state due to database connectivity error: %s", dse)
            raise RuntimeError(f"Database error while loading paper trading state: {dse}") from dse

        if data is None:
            if self.log_file:
                legacy_key = f"paper_trading_state:{os.path.abspath(self.log_file)}"
                try:
                    legacy_data = get_runtime_state(legacy_key)
                except DatabaseStateError as dse:
                    log.critical("Failed to read legacy paper trading state due to DB error: %s", dse)
                    raise RuntimeError(f"Database error while reading legacy paper trading state: {dse}") from dse

                if isinstance(legacy_data, dict):
                    data = legacy_data
                    try:
                        set_runtime_state(self._state_key, data)
                        log.info("Migrated paper trading state to logical key: %s", self._state_key)
                    except Exception as m_err:
                        log.warning("Could not persist migrated state: %s", m_err)

        if isinstance(data, dict):
            try:
                self._load_from_payload(data)
                log.debug("Loaded %d trades from DB runtime state", len(self.trades))
                return
            except Exception as exc:
                log.error("Could not load DB paper trading state: %s", exc)
                raise RuntimeError(f"Corrupt paper trading state in database: {exc}") from exc

        if not self.log_file or not os.path.exists(self.log_file):
            return

        try:
            with open(self.log_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._load_from_payload(data)
            set_runtime_state(self._state_key, data)
            log.debug("Loaded %d trades from %s", len(self.trades), self.log_file)
        except (json.JSONDecodeError, TypeError) as err:
            log.error("Could not load trade history from %s: %s", self.log_file, err)
