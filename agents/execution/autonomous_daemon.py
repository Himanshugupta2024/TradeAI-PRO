"""
TradeAI-PRO — Autonomous Execution Daemon & Position Manager.

Handles:
  1. 100% Autonomous Zero-Click Paper Trade Execution during active market windows.
  2. Multi-Asset concurrency control (NSE Equities + Top Crypto majors, capped at 5).
  3. Dynamic trade management:
     - Breakeven trailing stop at +1R.
     - 50% partial profit booking at +1.5R.
     - EOD auto-square-off at 15:15 IST for intraday equities.
     - 24/7 ATR trailing stop for crypto assets.
  4. Decision feed logging & Telegram broadcast.
  5. Daily 15:45 IST Post-Market Self-Learning EOD Review.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from config import settings
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(tz=_IST)


def _today_ist() -> str:
    return _now_ist().date().isoformat()


class AutonomousExecutionDaemon:
    """
    Autonomous Execution Daemon for TradeAI-PRO.
    Manages autonomous entry, live exit rules, learning triggers, and decision feeds.
    """

    def __init__(
        self,
        max_positions: int = 5,
        min_qs_score: float = 60.0,
        min_ai_confidence: float = 50.0,
        max_portfolio_heat: float = 0.15,
    ) -> None:
        self.enabled: bool = os.getenv("AUTONOMOUS_TRADING_ENABLED", "true").lower() in ("1", "true", "yes")
        self.max_positions: int = int(os.getenv("AUTONOMOUS_MAX_POSITIONS", str(max_positions)))
        self.min_qs_score: float = float(os.getenv("AUTONOMOUS_MIN_QS_SCORE", str(min_qs_score)))
        self.min_ai_confidence: float = float(os.getenv("AUTONOMOUS_MIN_AI_CONFIDENCE", str(min_ai_confidence)))
        self.max_portfolio_heat: float = max_portfolio_heat
        self.decision_log: list[dict[str, Any]] = []
        self._max_log_entries: int = 100
        self.last_eod_review_date: Optional[str] = None
        self._last_scan_time: Optional[str] = None

    def record_decision(
        self,
        action: str,
        symbol: str,
        direction: str,
        price: float,
        reason: str,
        qs_score: float = 0.0,
        ai_confidence: float = 0.0,
        pnl_pct: Optional[float] = None,
        trade_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Record an autonomous action in the decision feed."""
        entry = {
            "id": f"dec-{int(_now_ist().timestamp() * 1000)}",
            "timestamp": _now_ist().isoformat(),
            "action": action,
            "symbol": symbol,
            "direction": direction,
            "price": round(price, 2),
            "qs_score": round(qs_score, 1),
            "ai_confidence": round(ai_confidence, 1),
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "trade_id": trade_id,
            "reason": reason,
            "asset_class": "CRYPTO" if "/" in symbol else "EQUITY",
        }
        self.decision_log.insert(0, entry)
        if len(self.decision_log) > self._max_log_entries:
            self.decision_log = self.decision_log[: self._max_log_entries]
        return entry

    def evaluate_signals(
        self,
        signals: list[dict[str, Any]],
        current_prices: dict[str, dict[str, Any]],
        engine: PaperTradingEngine,
    ) -> list[PaperTrade]:
        """
        Evaluate candidate signals and auto-execute top qualified setups.
        Enforces:
          - Max 5 concurrent positions across equity & crypto
          - QS Score >= 60.0
          - AI Model Confidence >= 50.0%
          - Cooldown & correlation checks
        """
        self._last_scan_time = _now_ist().isoformat()
        if not self.enabled:
            return []

        open_trades = engine.open_trades
        remaining_slots = self.max_positions - len(open_trades)
        if remaining_slots <= 0:
            log.debug("[Autonomous] Max concurrent positions reached (%d/%d)", len(open_trades), self.max_positions)
            return []

        open_symbols = {engine._normalize_symbol(t.symbol) for t in open_trades}
        candidates = []

        for sig in signals:
            if not isinstance(sig, dict):
                continue

            symbol = sig.get("symbol")
            if not symbol:
                continue

            norm_sym = engine._normalize_symbol(symbol)
            if norm_sym in open_symbols:
                continue

            direction = str(sig.get("signal", sig.get("direction", sig.get("action", "HOLD")))).upper()
            if direction not in ("LONG", "SHORT", "BUY", "SELL"):
                continue

            # Standardize direction
            norm_dir = "LONG" if direction in ("LONG", "BUY") else "SHORT"

            qs_score = float(sig.get("quant_score", sig.get("quality_score", sig.get("qs_score", sig.get("score", 0.0)))))
            ai_conf = float(sig.get("confidence", sig.get("ai_confidence", 0.0)))

            # Threshold validation
            if qs_score > 0 and qs_score < self.min_qs_score:
                continue
            if ai_conf < self.min_ai_confidence:
                continue

            # Cooldown check
            if engine._cooldowns.get(norm_sym, 0) > 0:
                continue

            # Correlation check
            if engine._has_correlated_open_position(symbol):
                continue

            # Check prices
            price_info = current_prices.get(symbol) or {}
            entry = float(sig.get("entry", sig.get("entry_price", price_info.get("close", 0.0))))
            if not math.isfinite(entry) or entry <= 0:
                continue

            sl = float(sig.get("stop_loss", sig.get("sl", 0.0)))
            tp = float(sig.get("target", sig.get("tp", 0.0)))

            # Ensure valid geometry
            if norm_dir == "LONG" and not (sl < entry < tp):
                continue
            if norm_dir == "SHORT" and not (tp < entry < sl):
                continue

            candidates.append({
                "symbol": symbol,
                "direction": norm_dir,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "qs_score": qs_score,
                "ai_confidence": ai_conf,
                "atr": float(sig.get("atr", price_info.get("atr", 0.0))),
                "strategy": sig.get("strategy", sig.get("signal_type", "AUTONOMOUS")),
                "regime": sig.get("regime", "UNKNOWN"),
                "rsi": sig.get("rsi_at_entry", sig.get("rsi", 50.0)),
                "volume_above_avg": bool(sig.get("volume_above_avg", False)),
            })

        # Rank candidates by QS Score descending, then AI confidence
        candidates.sort(key=lambda c: (c["qs_score"], c["ai_confidence"]), reverse=True)
        selected = candidates[:remaining_slots]
        executed_trades = []

        for cand in selected:
            try:
                trade = engine.execute(
                    symbol=cand["symbol"],
                    direction=cand["direction"],
                    entry=cand["entry"],
                    sl=cand["sl"],
                    tp=cand["tp"],
                    atr=cand["atr"],
                    signal_type=cand["strategy"],
                    regime=cand["regime"],
                    rsi_at_entry=cand["rsi"],
                    volume_above_avg=cand["volume_above_avg"],
                    confidence=cand["ai_confidence"],
                )
                if trade:
                    executed_trades.append(trade)
                    open_symbols.add(engine._normalize_symbol(cand["symbol"]))
                    self.record_decision(
                        action="AUTO_ENTER",
                        symbol=cand["symbol"],
                        direction=cand["direction"],
                        price=cand["entry"],
                        reason=f"Top Setup (QS: {cand['qs_score']:.1f}, AI: {cand['ai_confidence']:.1f}%) passed all risk gates",
                        qs_score=cand["qs_score"],
                        ai_confidence=cand["ai_confidence"],
                        trade_id=trade.id,
                    )
                    log.info(
                        "🚀 [Autonomous] Auto-Entered %s %s #%d @ %.2f (QS: %.1f, AI: %.1f%%)",
                        cand["direction"], cand["symbol"], trade.id, cand["entry"], cand["qs_score"], cand["ai_confidence"]
                    )
                    # Broadcast telegram alert if configured
                    try:
                        from agents.alerts.telegram_bot import send_signal_alert
                        send_signal_alert(
                            symbol=cand["symbol"],
                            direction=cand["direction"],
                            entry=cand["entry"],
                            stop_loss=cand["sl"],
                            target=cand["tp"],
                            confidence=cand["ai_confidence"],
                            trend=cand["regime"],
                            rsi=float(cand["rsi"] or 50.0),
                            regime=cand["regime"],
                        )
                    except Exception as tg_err:
                        log.debug("Telegram alert skipped for %s: %s", cand["symbol"], tg_err)

            except Exception as exc:
                log.error("[Autonomous] Execution failed for %s: %s", cand["symbol"], exc)

        return executed_trades

    def manage_open_trades(
        self,
        engine: PaperTradingEngine,
        current_prices: dict[str, dict[str, Any]],
        atr_values: Optional[dict[str, float]] = None,
    ) -> list[PaperTrade]:
        """
        Monitor open trades for:
          1. Dynamic SL/TP and ATR trailing stop updates.
          2. Partial profit bookings (50% at +1.5R).
          3. Breakeven adjustments (+1R).
          4. Intraday equity 15:15 IST auto-square-off.
        """
        atr_values = atr_values or {}
        closed_trades: list[PaperTrade] = []

        # 1. Update prices against SL/TP & trailing stops
        closed = engine.update_prices(current_prices, atr_values)
        if closed:
            closed_trades.extend(closed)
            for t in closed:
                action_name = "TARGET_HIT" if "TP" in t.status else ("TRAILING_STOP" if "TRAIL" in t.status else "STOP_LOSS")
                self.record_decision(
                    action=action_name,
                    symbol=t.symbol,
                    direction=t.direction,
                    price=t.exit_price or 0.0,
                    reason=f"Position closed via {t.status} (PnL: {t.pnl_pct:+.2f}%)",
                    pnl_pct=t.pnl_pct,
                    trade_id=t.id,
                )

        # 2. Check 15:15 IST Intraday EOD squareoff for equities
        eod_closed = engine.check_eod_squareoff(current_prices)
        if eod_closed:
            closed_trades.extend(eod_closed)
            for t in eod_closed:
                self.record_decision(
                    action="EOD_SQUAREOFF",
                    symbol=t.symbol,
                    direction=t.direction,
                    price=t.exit_price or 0.0,
                    reason=f"15:15 IST Market Close Auto-Squareoff (PnL: {t.pnl_pct:+.2f}%)",
                    pnl_pct=t.pnl_pct,
                    trade_id=t.id,
                )

        return closed_trades

    def check_daily_eod_review(self) -> Optional[dict[str, Any]]:
        """
        Run EOD Self-Learning Review at 15:45 IST on weekdays.
        Calculates today's win rate, adapts Bayesian weights, and pushes a summary.
        """
        now = _now_ist()
        is_weekday = now.weekday() < 5
        ist_minutes = now.hour * 60 + now.minute
        today = _today_ist()

        # Target window: 15:45 to 16:30 IST
        if not (is_weekday and (15 * 60 + 45) <= ist_minutes <= (16 * 60 + 30)):
            return None

        if self.last_eod_review_date == today:
            return None  # Already run today

        try:
            from agents.analytics.feedback_loop import FeedbackAnalyser
            analyser = FeedbackAnalyser()
            review = analyser.run_daily_eod_review(today)
            self.last_eod_review_date = today

            self.record_decision(
                action="EOD_LEARNING_REVIEW",
                symbol="ALL",
                direction="NEUTRAL",
                price=0.0,
                reason=f"EOD Review completed: {review.get('today_trade_count', 0)} trades today, WinRate {review.get('today_win_rate', 0)}%, Weights Adapted",
                pnl_pct=review.get("today_total_pnl_pct", 0.0),
            )

            # Broadcast summary to Telegram if available
            try:
                from agents.alerts.telegram_bot import send_daily_summary
                summary_stats = {
                    "total_trades": review.get("today_trade_count", 0),
                    "win_rate": review.get("today_win_rate", 0.0),
                    "total_pnl": review.get("today_total_pnl_pct", 0.0),
                    "best_strategy": review.get("best_signal_type", "NONE"),
                }
                send_daily_summary(summary_stats)
            except Exception as tg_err:
                log.debug("EOD telegram summary skipped: %s", tg_err)

            log.info("🧠 [Autonomous] Daily EOD Self-Learning Review completed for %s", today)
            return review

        except Exception as exc:
            log.error("[Autonomous] EOD Review failed: %s", exc)
            return None

    def get_status(self, engine: Optional[PaperTradingEngine] = None) -> dict[str, Any]:
        """Return the current autonomous daemon operational status."""
        open_count = len(engine.open_trades) if engine else 0
        return {
            "enabled": self.enabled,
            "mode": "100% Autonomous Zero-Click",
            "max_positions": self.max_positions,
            "open_positions": open_count,
            "min_qs_score": self.min_qs_score,
            "min_ai_confidence": self.min_ai_confidence,
            "last_scan_time": self._last_scan_time,
            "last_eod_review": self.last_eod_review_date,
            "recent_decisions_count": len(self.decision_log),
        }

    def get_decision_feed(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return recent decision feed items."""
        return self.decision_log[:limit]


# Global singleton instance
_daemon: Optional[AutonomousExecutionDaemon] = None


def get_autonomous_daemon() -> AutonomousExecutionDaemon:
    """Get or create the global AutonomousExecutionDaemon singleton."""
    global _daemon
    if _daemon is None:
        _daemon = AutonomousExecutionDaemon()
    return _daemon
