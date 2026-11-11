"""
Self-Learning Feedback Loop for TradeAI PRO.

Architecture
───────────
1. TradeOutcomeLogger  — writes one row per closed trade to `trade_outcomes_pg`
   table in PostgreSQL (persistent across Render restarts).
2. FeedbackAnalyser    — every N trades, queries the table and computes:
     - Win rate by signal_type    (RSI / MACD / BREAKOUT / EMA_CROSS / DIVERGENCE / OTHER)
     - Win rate by market_regime  (TRENDING / RANGING / HIGH_VOLATILITY)
     - Optimal RSI zone           (< 30 / 30-50 / 50-70 / > 70)
     - Volume impact              (above_avg vs below_avg)
3. WeightAdapter       — updates `config/learned_weights.json` based on above analysis.
   The signal_generator reads this file at startup (or every N minutes) to re-weight
   individual signal components without code changes.

Statistical Integrity:
- Only genuine PAPER_FORWARD trades are eligible for learning.
- Synthetic / mock / test trades are strictly quarantined.
- Trades with missing metadata are marked LEGACY_INCOMPLETE and excluded from learning.
- Sample < 100 trades is constrained to conservative bounds [0.75, 1.25].
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from utils.atomic_io import atomic_write_json
from typing import Optional, Any
from config.settings import PAPER_TRADING_UNLOCK_MIN_TRADES

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# ── Paths ─────────────────────────────────────────────────────────────────────
_WEIGHTS_PATH = Path("config/learned_weights.json")

# ── Feedback trigger: analyse after every N closed trades ─────────────────────
ANALYSE_EVERY_N_TRADES = int(os.getenv("ANALYSE_EVERY_N_TRADES", str(PAPER_TRADING_UNLOCK_MIN_TRADES)))

# ── Learning stability knobs ───────────────────────────────────────────────────
RECENCY_HALF_LIFE_TRADES = 120
WEIGHT_PRIOR_STRENGTH = 12.0
WEIGHT_MIN_SAMPLE_TRADES = 5
WEIGHT_MAX_STEP_PER_UPDATE = 0.15

# ── Default signal weights (match signal_generator.py component labels) ───────
DEFAULT_WEIGHTS: dict[str, float] = {
    "RSI_BOUNCE":    1.0,   # RSI oversold/overbought bounce
    "MACD_CROSS":    1.0,   # MACD histogram crossover
    "BREAKOUT":      1.0,   # Price breakout from consolidation
    "EMA_CROSS":     1.0,   # EMA 20/50 crossover
    "DIVERGENCE":    1.0,   # RSI divergence (bullish / bearish)
    "VOLUME_SPIKE":  1.0,   # Above-average volume
    "COMBINED":      1.0,   # Multiple signals in agreement
}

# ── 2D Regime Prior Weights (Multi-Regime Baseline Matrix) ─────────────────────
DEFAULT_REGIME_WEIGHTS_2D: dict[str, dict[str, float]] = {
    "TRENDING": {
        "BREAKOUT":     1.30,
        "EMA_CROSS":    1.20,
        "MACD_CROSS":   1.15,
        "RSI_BOUNCE":   0.85,
        "DIVERGENCE":   1.10,
        "VOLUME_SPIKE": 1.20,
        "COMBINED":     1.10,
    },
    "RANGING": {
        "BREAKOUT":     0.70,
        "EMA_CROSS":    0.75,
        "MACD_CROSS":   0.80,
        "RSI_BOUNCE":   1.30,
        "DIVERGENCE":   1.20,
        "VOLUME_SPIKE": 0.90,
        "COMBINED":     1.00,
    },
    "HIGH_VOLATILITY": {
        "BREAKOUT":     0.90,
        "EMA_CROSS":    0.85,
        "MACD_CROSS":   1.05,
        "RSI_BOUNCE":   1.15,
        "DIVERGENCE":   1.15,
        "VOLUME_SPIKE": 1.15,
        "COMBINED":     1.05,
    },
    "CHOPPY": {
        "BREAKOUT":     0.60,
        "EMA_CROSS":    0.70,
        "MACD_CROSS":   0.75,
        "RSI_BOUNCE":   1.25,
        "DIVERGENCE":   1.15,
        "VOLUME_SPIKE": 0.85,
        "COMBINED":     0.95,
    },
    "UNKNOWN": {k: 1.0 for k in DEFAULT_WEIGHTS},
}


def get_ist_session_phase(dt: Optional[Union[datetime, str]] = None) -> str:
    """
    Map a timestamp to its IST Market Session Phase.
      - OPENING_MOMENTUM:  09:15 – 10:15 IST (Opening institutional volatility/expansion)
      - MORNING_EXPANSION: 10:15 – 11:30 IST (Initial trend continuation)
      - MIDDAY_CHOP:       11:30 – 13:30 IST (Low-volume midday lull, high fakeout rate)
      - AFTERNOON_SETUP:   13:30 – 14:15 IST (European open alignment & pre-closing setup)
      - CLOSING_TREND:     14:15 – 15:30 IST (Square-off & institutional closing momentum)
      - OFF_HOURS:         Outside 09:15–15:30 IST
    """
    if dt is None:
        parsed_dt = datetime.now(tz=_IST)
    elif isinstance(dt, datetime):
        if dt.tzinfo is None:
            parsed_dt = dt.replace(tzinfo=_IST)
        else:
            parsed_dt = dt.astimezone(_IST)
    elif isinstance(dt, str):
        clean_str = dt.strip().replace("Z", "+00:00")
        try:
            parsed_dt = datetime.fromisoformat(clean_str)
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=_IST)
            else:
                parsed_dt = parsed_dt.astimezone(_IST)
        except Exception:
            return "OFF_HOURS"
    else:
        return "OFF_HOURS"

    minutes = parsed_dt.hour * 60 + parsed_dt.minute
    if 555 <= minutes < 615:
        return "OPENING_MOMENTUM"
    elif 615 <= minutes < 690:
        return "MORNING_EXPANSION"
    elif 690 <= minutes < 810:
        return "MIDDAY_CHOP"
    elif 810 <= minutes < 855:
        return "AFTERNOON_SETUP"
    elif 855 <= minutes <= 930:
        return "CLOSING_TREND"
    else:
        return "OFF_HOURS"



def _get_pg_session():
    """Return a new SQLAlchemy session bound to the main database engine."""
    from db.connection import SessionLocal
    return SessionLocal()


def _is_using_postgres() -> bool:
    """Check if the database is PostgreSQL (vs. local SQLite dev)."""
    from config.settings import DATABASE_URL
    url = DATABASE_URL or ""
    return "postgres" in url.lower()


# ─────────────────────────────────────────────────────────────────────────────
# TradeOutcomeLogger  — called by paper_trading._close_trade()
# ─────────────────────────────────────────────────────────────────────────────

class TradeOutcomeLogger:
    """
    Logs each closed paper trade to the `trade_outcomes_pg` PostgreSQL table.
    Ensures strict idempotency via unique trade_id.
    """

    def __init__(self):
        self._trade_count = self._get_logged_count()
        log.info("TradeOutcomeLogger ready — %d genuine outcomes logged", self._trade_count)

    def _get_logged_count(self) -> int:
        try:
            session = _get_pg_session()
            try:
                from db.models import TradeOutcome
                count = session.query(TradeOutcome).filter(
                    TradeOutcome.environment == "PAPER_FORWARD",
                    ~TradeOutcome.symbol.like("TEST%"),
                    ~TradeOutcome.symbol.like("MOCK%"),
                    ~TradeOutcome.symbol.like("DEMO%"),
                    ~TradeOutcome.symbol.like("SIM_%"),
                ).count()
                return count
            finally:
                session.close()
        except Exception:
            return 0

    def log_closed_trade(
        self,
        *,
        symbol: str,
        direction: str,
        signal_type: str = "COMBINED",
        regime: str = "UNKNOWN",
        rsi_at_entry: Optional[float] = None,
        volume_above_avg: bool = False,
        entry_price: float,
        exit_price: float,
        pnl_pct: float,
        exit_reason: str = "",
        confidence: float = 0.0,
        trade_id: Optional[str] = None,
        asset_class: Optional[str] = None,
        entry_time: Optional[str] = None,
        exit_time: Optional[str] = None,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
        pnl: Optional[float] = None,
        quantity: Optional[float] = None,
        environment: str = "PAPER_FORWARD",
        mfe_r: Optional[float] = None,
        mae_r: Optional[float] = None,
    ) -> Optional[int]:
        """
        Log one closed trade outcome to PostgreSQL trade_outcomes_pg table.
        Returns outcome id or None if skipped/quarantined.
        """
        result = "WIN" if pnl_pct > 0 else "LOSS"
        closed_at = datetime.now(tz=_IST).isoformat()

        # Normalise signal_type
        signal_type = _normalise_signal_type(signal_type)

        # Quarantine test / mock / demo trades from adaptive learning
        sym_upper = (symbol or "").upper()
        if sym_upper.startswith(("TEST", "MOCK", "DEMO", "SIM_")):
            log.debug("Quarantined test trade %s from production trade_outcomes", symbol)
            return None

        # Detect missing critical metadata
        if regime in ("UNKNOWN", "", None) or confidence <= 0.0:
            environment = "LEGACY_INCOMPLETE"
            log.warning("Trade %s has incomplete metadata (regime=%s, conf=%.1f) -> marked %s",
                        symbol, regime, confidence, environment)

        # Auto-detect asset class if not provided
        if not asset_class:
            if "/" in symbol:
                asset_class = "CRYPTO"
            else:
                asset_class = "EQUITY"

        try:
            from db.models import TradeOutcome
            session = _get_pg_session()
            try:
                # Idempotency check on trade_id
                if trade_id:
                    existing = session.query(TradeOutcome).filter(
                        TradeOutcome.trade_id == trade_id
                    ).first()
                    if existing:
                        log.debug("Outcome for trade_id=%s already exists (id=%s) — skipping duplicate",
                                  trade_id, existing.id)
                        return existing.id

                outcome = TradeOutcome(
                    trade_id=trade_id,
                    symbol=symbol,
                    direction=direction,
                    signal_type=signal_type,
                    regime=regime,
                    rsi_at_entry=rsi_at_entry,
                    volume_above_avg=int(volume_above_avg),
                    entry_price=entry_price,
                    exit_price=exit_price,
                    pnl_pct=pnl_pct,
                    result=result,
                    closed_at=closed_at,
                    confidence=confidence,
                    exit_reason=exit_reason,
                    asset_class=asset_class,
                    environment=environment,
                    entry_time=entry_time,
                    exit_time=exit_time,
                    stop_loss=stop_loss,
                    target=target,
                    pnl=pnl,
                    quantity=quantity,
                )
                session.add(outcome)
                session.commit()
                out_id = outcome.id

                if environment == "PAPER_FORWARD":
                    self._trade_count += 1
                    log.info(
                        "📝 Feedback logged (PG #%d): %s %s %s | %s | RSI=%.1f | %.2f%% | %s",
                        out_id, result, direction, symbol,
                        signal_type, rsi_at_entry or 0.0, pnl_pct, regime,
                    )

                    # Auto-trigger analysis every N genuine trades
                    if self._trade_count >= ANALYSE_EVERY_N_TRADES and (self._trade_count % ANALYSE_EVERY_N_TRADES == 0):
                        log.info(
                            "🧠 %d trades logged — running learning analysis...",
                            self._trade_count,
                        )
                        analyser = FeedbackAnalyser()
                        insights = analyser.run()
                        adapter = WeightAdapter()
                        adapter.update_weights(insights)
                else:
                    log.info("📝 Logged %s outcome #%d for %s (excluded from active learning)",
                             environment, out_id, symbol)

                return out_id

            except Exception as exc:
                session.rollback()
                if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                    log.debug("Duplicate outcome insert ignored for trade_id=%s", trade_id)
                else:
                    log.error("TradeOutcomeLogger.log_closed_trade failed: %s", exc)
                return None
            finally:
                session.close()

        except Exception as exc:
            log.error("TradeOutcomeLogger.log_closed_trade failed: %s", exc)
            return None


# ─────────────────────────────────────────────────────────────────────────────
# FeedbackAnalyser  — queries ONLY PAPER_FORWARD outcomes
# ─────────────────────────────────────────────────────────────────────────────

class FeedbackAnalyser:
    """
    Queries trade_outcomes_pg and returns structured insights.
    Exclusively evaluates genuine 'PAPER_FORWARD' records.
    """

    def run(self, lookback: int = 500) -> dict:
        """
        Run analysis on the last `lookback` trades from PostgreSQL.
        Returns structured insights dict.
        """
        try:
            from db.models import TradeOutcome
            session = _get_pg_session()
            try:
                # Query ONLY PAPER_FORWARD records (excludes LEGACY_INCOMPLETE and TEST)
                rows = session.query(TradeOutcome).filter(
                    TradeOutcome.environment == "PAPER_FORWARD",
                    ~TradeOutcome.symbol.like("TEST%"),
                    ~TradeOutcome.symbol.like("MOCK%"),
                    ~TradeOutcome.symbol.like("DEMO%"),
                    ~TradeOutcome.symbol.like("SIM_%"),
                ).order_by(TradeOutcome.id.desc()).limit(lookback).all()

                if not rows:
                    return _empty_insights()

                trades = []
                for r in rows:
                    entry_t = getattr(r, "entry_time", None)
                    close_t = getattr(r, "closed_at", None)
                    session_p = get_ist_session_phase(entry_t or close_t)
                    trades.append({
                        "signal_type": r.signal_type,
                        "regime": r.regime,
                        "rsi_at_entry": r.rsi_at_entry,
                        "volume_above_avg": r.volume_above_avg,
                        "pnl_pct": r.pnl_pct,
                        "result": r.result,
                        "confidence": r.confidence,
                        "entry_time": entry_t,
                        "closed_at": close_t,
                        "session_phase": session_p,
                    })
            finally:
                session.close()

            _attach_recency_weights(trades, half_life=RECENCY_HALF_LIFE_TRADES)

            # ── 1. Signal type win rates ──────────────────────────────────────
            signal_stats = _group_win_rate(trades, "signal_type")

            # ── 2. Regime win rates ───────────────────────────────────────────
            regime_stats = _group_win_rate(trades, "regime")

            # ── 2b. Session phase win rates (IST Market Phase) ────────────────
            session_phase_stats = _group_win_rate(trades, "session_phase")

            # ── 2c. 2D Regime × Signal Type Matrix Stats ─────────────────────
            regime_matrix_stats: dict[str, dict[str, dict]] = {}
            for r_key in ["TRENDING", "RANGING", "HIGH_VOLATILITY", "CHOPPY", "UNKNOWN"]:
                regime_matrix_stats[r_key] = {}
                for sig_key in DEFAULT_WEIGHTS:
                    sub = [
                        t for t in trades
                        if str(t.get("regime", "")).upper() == r_key
                        and str(t.get("signal_type", "")).upper() == sig_key
                    ]
                    regime_matrix_stats[r_key][sig_key] = _stats_block(sub)

            # ── 3. RSI zone win rates ─────────────────────────────────────────
            rsi_zone_stats = _rsi_zone_analysis(trades)

            # ── 4. Volume impact ──────────────────────────────────────────────
            vol_above = [t for t in trades if t.get("volume_above_avg") == 1]
            vol_below = [t for t in trades if t.get("volume_above_avg") == 0]
            volume_stats = {
                "above_avg": _stats_block(vol_above),
                "below_avg": _stats_block(vol_below),
            }

            # ── 4b. Confidence calibration ───────────────────────────────────
            conf_band_stats = _confidence_band_analysis(trades)
            conf_calibration = _confidence_calibration_summary(conf_band_stats)

            # ── 5. Overall & Mathematical Expectancy (EV) ─────────────────────
            wins = [t for t in trades if t["result"] == "WIN"]
            total = len(trades)
            total_weight = sum(float(t.get("_w", 1.0)) for t in trades)
            weighted_wins = sum(float(t.get("_w", 1.0)) for t in trades if t["result"] == "WIN")
            weighted_pnl_sum = sum(float(t.get("pnl_pct", 0.0)) * float(t.get("_w", 1.0)) for t in trades)

            overall_win_rate = round(_safe_div(weighted_wins, total_weight) * 100, 1)
            overall_win_rate_unweighted = round(len(wins) / total * 100, 1) if total else 0
            avg_pnl = round(_safe_div(weighted_pnl_sum, total_weight), 2)
            avg_pnl_unweighted = round(sum(float(t.get("pnl_pct", 0.0)) for t in trades) / total, 2) if total else 0
            overall_ev = _calculate_expectancy_metrics(trades)

            # Model accuracy: win rate of the high-confidence subset only.
            # It is NOT overall model accuracy, so we ship the label and the
            # subset size alongside the number instead of letting the UI imply
            # the model is right that often on every trade.
            high_conf = [t for t in trades if (t.get("confidence") or 0) >= 50]
            high_conf_weight = sum(float(t.get("_w", 1.0)) for t in high_conf)
            hc_wins_weight = sum(float(t.get("_w", 1.0)) for t in high_conf if t["result"] == "WIN")
            model_accuracy = round(_safe_div(hc_wins_weight, high_conf_weight) * 100, 1)
            reliability_score = round(min(1.0, _safe_div(total_weight, 120.0)) * 100, 1)

            # ── 6. Pick best / worst ──────────────────────────────────────────
            # A group with 1-4 trades can win by luck. Reporting it as "best
            # signal type" is a fabricated edge, so below MIN_GROUP_SAMPLE we
            # return UNKNOWN plus the reason and let the UI label the gap.
            MIN_GROUP_SAMPLE = 5

            def _pick(stats: dict, *, best: bool) -> tuple[str, str]:
                eligible = {k: v for k, v in stats.items() if v["trades"] >= MIN_GROUP_SAMPLE}
                if not eligible:
                    return "UNKNOWN", "insufficient_sample"
                key_fn = max if best else min
                return key_fn(eligible, key=lambda k: eligible[k]["win_rate"]), "ok"

            best_signal, best_signal_conf = _pick(signal_stats, best=True)
            worst_signal, worst_signal_conf = _pick(signal_stats, best=False)
            best_regime, best_regime_conf = _pick(regime_stats, best=True)
            optimal_rsi, optimal_rsi_conf = _pick(rsi_zone_stats, best=True)

            insights = {
                "signal_type_stats":  signal_stats,
                "regime_stats":       regime_stats,
                "session_phase_stats": session_phase_stats,
                "regime_matrix_stats": regime_matrix_stats,
                "rsi_zone_stats":     rsi_zone_stats,
                "volume_stats":       volume_stats,
                "confidence_band_stats": conf_band_stats,
                "confidence_calibration": conf_calibration,
                "expectancy_metrics": overall_ev,
                "overall": {
                    "total_trades":    total,
                    "effective_trades": round(total_weight, 2),
                    "win_rate":        overall_win_rate,
                    "win_rate_unweighted": overall_win_rate_unweighted,
                    "avg_pnl_pct":     avg_pnl,
                    "avg_pnl_pct_unweighted": avg_pnl_unweighted,
                    "avg_win":         overall_ev["avg_win"],
                    "avg_loss":        overall_ev["avg_loss"],
                    "profit_factor":   overall_ev["profit_factor"],
                    "expectancy":      overall_ev["expectancy"],
                    "weighted_expectancy": overall_ev["weighted_expectancy"],
                    "model_accuracy":  model_accuracy,
                    "model_accuracy_label": "high-confidence subset win rate (confidence >= 50)",
                    "model_accuracy_basis_trades": len(high_conf),
                    "reliability_score": reliability_score,
                },
                "best_signal_type":  best_signal,
                "worst_signal_type": worst_signal,
                "best_regime":       best_regime,
                "optimal_rsi_zone":  optimal_rsi,
                "best_worst_min_sample": MIN_GROUP_SAMPLE,
                "best_signal_type_confidence": best_signal_conf,
                "worst_signal_type_confidence": worst_signal_conf,
                "best_regime_confidence": best_regime_conf,
                "optimal_rsi_zone_confidence": optimal_rsi_conf,
                "insights_at":       datetime.now(tz=_IST).isoformat(),
            }

            log.info(
                "🧠 Learning insights: best=%s worst=%s win_rate=%.1f%% accuracy=%.1f%%",
                best_signal, worst_signal, overall_win_rate, model_accuracy,
            )
            return insights

        except Exception as exc:
            log.error("FeedbackAnalyser.run failed: %s", exc)
            return _empty_insights()

    def run_daily_eod_review(self, target_date_ist: Optional[str] = None) -> dict:
        """
        Run EOD learning review for trades closed today (or target_date_ist).
        """
        if not target_date_ist:
            target_date_ist = datetime.now(tz=_IST).date().isoformat()

        insights = self.run(lookback=500)
        adapter = WeightAdapter()
        new_weights = adapter.update_weights(insights)

        today_trades = []
        try:
            from db.models import TradeOutcome
            session = _get_pg_session()
            try:
                rows = session.query(TradeOutcome).filter(
                    TradeOutcome.environment == "PAPER_FORWARD",
                    TradeOutcome.closed_at.like(f"{target_date_ist}%")
                ).order_by(TradeOutcome.id.asc()).all()
                today_trades = [
                    {
                        "id": r.id,
                        "symbol": r.symbol,
                        "direction": r.direction,
                        "signal_type": r.signal_type,
                        "regime": r.regime,
                        "entry_price": r.entry_price,
                        "exit_price": r.exit_price,
                        "pnl_pct": r.pnl_pct,
                        "result": r.result,
                        "exit_reason": r.exit_reason,
                        "closed_at": r.closed_at,
                    }
                    for r in rows
                ]
            finally:
                session.close()
        except Exception as e:
            log.warning("Could not query today trades for EOD review: %s", e)

        today_count = len(today_trades)
        today_wins = len([t for t in today_trades if t.get("result") == "WIN"])
        # No closed trades today = no win rate. Reporting 0.0% reads like a
        # total loss instead of "nothing happened yet".
        today_wr = round(today_wins / today_count * 100, 1) if today_count > 0 else None
        today_pnl = round(sum(float(t.get("pnl_pct", 0.0)) for t in today_trades), 2)

        review = {
            "review_date": target_date_ist,
            "today_trade_count": today_count,
            "today_wins": today_wins,
            "today_win_rate": today_wr,
            "today_win_rate_label": "no closed trades" if today_wr is None else None,
            "today_total_pnl_pct": today_pnl,
            "best_signal_type": insights.get("best_signal_type", "UNKNOWN"),
            "worst_signal_type": insights.get("worst_signal_type", "UNKNOWN"),
            "best_signal_type_confidence": insights.get("best_signal_type_confidence", "insufficient_sample"),
            "worst_signal_type_confidence": insights.get("worst_signal_type_confidence", "insufficient_sample"),
            "updated_weights": new_weights,
            "insights": insights,
        }
        log.info(
            "📊 EOD Review [%s]: %d trades | WinRate: %s | PnL: %+.2f%% | Best: %s",
            target_date_ist,
            today_count,
            f"{today_wr:.1f}%" if today_wr is not None else "n/a",
            today_pnl,
            review["best_signal_type"],
        )
        return review


# ─────────────────────────────────────────────────────────────────────────────
# WeightAdapter  — writes config/learned_weights.json with conservative bounds
# ─────────────────────────────────────────────────────────────────────────────

class WeightAdapter:
    """
    Reads insights from FeedbackAnalyser and updates signal component weights.

    Conservative Bayesian Shrinkage:
      - Threshold gate: < 30 trades -> weights remain strictly 1.0 (COLD_START).
      - Sample 30-99 trades: conservative bounds [0.75, 1.25].
      - Sample 100-249 trades: expanded bounds [0.50, 1.75].
      - Sample 250+ trades: full bounds [0.30, 2.50].
      - Max step per update capped at 0.15.
    """

    def update_weights(self, insights: dict) -> dict:
        """
        Update learned_weights.json based on insights.
        Returns the new weights dict.
        """
        stats = insights.get("signal_type_stats", {})
        total_sample = int(insights.get("overall", {}).get("total_trades", 0) or 0)
        existing = self._load()
        new_weights = {
            k: float(existing.get(k, v))
            for k, v in DEFAULT_WEIGHTS.items()
        }

        # Cold start gate: do NOT modify weights if sample < canonical requirement
        if total_sample < PAPER_TRADING_UNLOCK_MIN_TRADES:
            log.info("Cold start active (%d/%d trades) — weights held at prior",
                     total_sample, PAPER_TRADING_UNLOCK_MIN_TRADES)
            new_weights["regime_weights"] = dict(DEFAULT_REGIME_WEIGHTS_2D)
            new_weights["weights_2d"] = dict(DEFAULT_REGIME_WEIGHTS_2D)
            new_weights["_updated_at"] = datetime.now(tz=_IST).isoformat()
            new_weights["_total_trades"] = total_sample
            new_weights["_learning_state"] = "COLD_START"
            return new_weights

        # Determine conservative bounds by sample size
        if total_sample < 100:
            bound_lo, bound_hi = 0.75, 1.25
        elif total_sample < 250:
            bound_lo, bound_hi = 0.50, 1.75
        else:
            bound_lo, bound_hi = 0.30, 2.50

        for sig_type, data in stats.items():
            if sig_type not in new_weights:
                continue
            wr = float(data.get("win_rate", 50.0))
            nt = int(data.get("trades", 0))
            if nt < WEIGHT_MIN_SAMPLE_TRADES:
                continue

            weighted_nt = float(data.get("weighted_trades", nt))
            sample_strength = max(0.0, min(250.0, weighted_nt))

            # Bayesian shrinkage toward neutral 50%
            shrunk_wr = _safe_div(
                (wr * sample_strength) + (50.0 * WEIGHT_PRIOR_STRENGTH),
                sample_strength + WEIGHT_PRIOR_STRENGTH,
            )

            # Target computation
            x = (shrunk_wr - 50.0) / 18.0
            sigmoid = 1.0 / (1.0 + math.exp(-x))
            target = bound_lo + sigmoid * (bound_hi - bound_lo)

            old = float(new_weights[sig_type])
            alpha = min(0.25, 0.08 + min(1.0, sample_strength / 180.0) * 0.17)
            proposed = old * (1.0 - alpha) + target * alpha

            # Step cap
            hi = old + WEIGHT_MAX_STEP_PER_UPDATE
            lo = old - WEIGHT_MAX_STEP_PER_UPDATE
            proposed = min(hi, max(lo, proposed))

            # Hard clamp
            new_weights[sig_type] = round(min(bound_hi, max(bound_lo, proposed)), 3)

        # ── 2D Multi-Regime Weight Matrix Adaptation ────────────────────────
        existing_2d = existing.get("regime_weights") or existing.get("weights_2d") or {}
        regime_matrix_stats = insights.get("regime_matrix_stats", {})
        new_2d: dict[str, dict[str, float]] = {}

        for regime_name, default_sub in DEFAULT_REGIME_WEIGHTS_2D.items():
            new_2d[regime_name] = {}
            existing_sub = existing_2d.get(regime_name, {})
            for sig_type in DEFAULT_WEIGHTS:
                base_prior = default_sub.get(sig_type, 1.0)
                old_val = float(existing_sub.get(sig_type, base_prior))

                cell_stat = regime_matrix_stats.get(regime_name, {}).get(sig_type, {})
                nt = int(cell_stat.get("trades", 0))

                if nt < WEIGHT_MIN_SAMPLE_TRADES:
                    new_2d[regime_name][sig_type] = round(old_val, 3)
                    continue

                wr = float(cell_stat.get("win_rate", 50.0))
                weighted_nt = float(cell_stat.get("weighted_trades", nt))
                sample_str = max(0.0, min(250.0, weighted_nt))

                # Bayesian shrinkage toward 50%
                shrunk_wr = _safe_div(
                    (wr * sample_str) + (50.0 * WEIGHT_PRIOR_STRENGTH),
                    sample_str + WEIGHT_PRIOR_STRENGTH,
                )

                # Expectancy (EV) factor boost
                ev = float(cell_stat.get("expectancy", 0.0) or 0.0)
                ev_boost = min(1.5, max(-1.5, ev)) * 0.20

                x = (shrunk_wr - 50.0) / 18.0 + ev_boost
                sigmoid = 1.0 / (1.0 + math.exp(-x))
                target = bound_lo + sigmoid * (bound_hi - bound_lo)

                alpha = min(0.25, 0.08 + min(1.0, sample_str / 180.0) * 0.17)
                proposed = old_val * (1.0 - alpha) + target * alpha
                hi = old_val + WEIGHT_MAX_STEP_PER_UPDATE
                lo = old_val - WEIGHT_MAX_STEP_PER_UPDATE
                proposed = min(hi, max(lo, proposed))
                new_2d[regime_name][sig_type] = round(min(bound_hi, max(bound_lo, proposed)), 3)

        new_weights["regime_weights"] = new_2d
        new_weights["weights_2d"] = new_2d
        new_weights["_updated_at"] = datetime.now(tz=_IST).isoformat()
        new_weights["_total_trades"] = total_sample
        new_weights["_effective_trades"] = insights.get("overall", {}).get("effective_trades", 0)
        new_weights["_win_rate"] = insights.get("overall", {}).get("win_rate", 0)
        new_weights["_model_accuracy"] = insights.get("overall", {}).get("model_accuracy", 0)
        new_weights["_reliability_score"] = insights.get("overall", {}).get("reliability_score", 0)
        new_weights["_best_signal"] = insights.get("best_signal_type", "")
        new_weights["_worst_signal"] = insights.get("worst_signal_type", "")
        new_weights["_learning_version"] = 2
        new_weights["_bounds"] = [bound_lo, bound_hi]

        try:
            from db.runtime_state import set_runtime_state
            set_runtime_state("learned_weights:v1", new_weights)
            set_runtime_state("learned_weights_2d:v1", new_2d)
            log.info("Persisted learned weights & 2D matrix to DB runtime_state (bounds: [%.2f, %.2f])", bound_lo, bound_hi)
        except Exception as exc:
            log.debug("WeightAdapter DB backup failed: %s", exc)

        try:
            _WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_WEIGHTS_PATH, new_weights, indent=2)
            log.info("Learned weights updated -> %s", _WEIGHTS_PATH)
        except Exception as exc:
            log.error("WeightAdapter.update_weights write failed: %s", exc)

        return new_weights

    def get_regime_weights(self, regime: str = "UNKNOWN") -> dict[str, float]:
        """
        Return signal component weights conditioned on the market regime.
        Falls back gracefully to flat learned weights or DEFAULT_REGIME_WEIGHTS_2D.
        """
        regime_key = str(regime or "UNKNOWN").upper().strip()
        all_data = self._load()
        regime_matrix = all_data.get("regime_weights") or all_data.get("weights_2d")

        if regime_matrix and isinstance(regime_matrix, dict) and regime_key in regime_matrix:
            return dict(regime_matrix[regime_key])

        # If specific regime is in DEFAULT_REGIME_WEIGHTS_2D
        if regime_key in DEFAULT_REGIME_WEIGHTS_2D:
            base_regime = dict(DEFAULT_REGIME_WEIGHTS_2D[regime_key])
            # Blend lightly with flat weights if trained
            for k in DEFAULT_WEIGHTS:
                if k in all_data and isinstance(all_data[k], (int, float)):
                    flat_w = float(all_data[k])
                    base_regime[k] = round(base_regime[k] * 0.7 + flat_w * 0.3, 3)
            return base_regime

        # Fallback: flat weights
        return {k: float(all_data.get(k, 1.0)) for k in DEFAULT_WEIGHTS}

    def _load(self) -> dict:
        if _WEIGHTS_PATH != Path("config/learned_weights.json") and _WEIGHTS_PATH.exists():
            try:
                with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        try:
            from db.runtime_state import get_runtime_state
            db_weights = get_runtime_state("learned_weights:v1")
            if db_weights and isinstance(db_weights, dict) and len(db_weights) > 0:
                return db_weights
        except Exception:
            pass
        try:
            if _WEIGHTS_PATH.exists():
                with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return dict(DEFAULT_WEIGHTS)


# ─────────────────────────────────────────────────────────────────────────────
# Controlled Backfill Utility for Legacy Closed Paper Trades
# ─────────────────────────────────────────────────────────────────────────────

def backfill_closed_paper_trades(trades_list: list[dict[str, Any]]) -> dict[str, int]:
    """
    Safely backfill closed paper trades into TradeOutcome PostgreSQL table.
    Enforces strict metadata audit:
      - Valid metadata (regime != UNKNOWN, confidence > 0) -> PAPER_FORWARD
      - Incomplete metadata -> LEGACY_INCOMPLETE (excluded from learning)
      - Test/Mock symbols -> QUARANTINED (skipped)
      - Duplicate trade_id -> IDEMPOTENT (skipped)

    Returns audit counts dict.
    """
    logger = TradeOutcomeLogger()
    stats = {
        "total_evaluated": len(trades_list),
        "quarantined_test": 0,
        "legacy_incomplete": 0,
        "genuine_forward": 0,
        "already_exists": 0,
    }

    for td in trades_list:
        status = td.get("status") or td.get("exit_reason", "")
        if status == "OPEN":
            continue

        sym = td.get("symbol", "")
        if sym.upper().startswith(("TEST", "MOCK", "DEMO", "SIM_")):
            stats["quarantined_test"] += 1
            continue

        trade_id = f"pt-{td.get('id')}"
        regime = str(td.get("regime") or "UNKNOWN").upper()
        confidence = float(td.get("confidence") or 0.0)

        # Check if complete
        is_complete = (
            regime != "UNKNOWN"
            and confidence > 0.0
            and td.get("entry_price") is not None
            and td.get("exit_price") is not None
            and td.get("exit_time")
        )

        env = "PAPER_FORWARD" if is_complete else "LEGACY_INCOMPLETE"
        if env == "LEGACY_INCOMPLETE":
            stats["legacy_incomplete"] += 1
        else:
            stats["genuine_forward"] += 1

        out_id = logger.log_closed_trade(
            trade_id=trade_id,
            symbol=sym,
            direction=td.get("direction", "LONG"),
            signal_type=td.get("signal_type", "COMBINED"),
            regime=regime,
            rsi_at_entry=td.get("rsi_at_entry"),
            volume_above_avg=bool(td.get("volume_above_avg", False)),
            entry_price=float(td.get("entry_price", 0.0)),
            exit_price=float(td.get("exit_price", 0.0)),
            pnl_pct=float(td.get("pnl_pct", 0.0)),
            exit_reason=status,
            confidence=confidence,
            asset_class="CRYPTO" if "/" in sym else "EQUITY",
            entry_time=td.get("entry_time"),
            exit_time=td.get("exit_time"),
            stop_loss=td.get("stop_loss"),
            target=td.get("target"),
            pnl=td.get("total_pnl"),
            quantity=td.get("quantity"),
            environment=env,
        )

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Public API helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_learning_stats() -> dict:
    """
    Primary read API for /api/learning/stats FastAPI endpoint.
    Returns latest insights from PAPER_FORWARD trades, plus current weights.
    """
    analyser = FeedbackAnalyser()
    insights = analyser.run()
    adapter  = WeightAdapter()
    weights  = adapter._load()
    total_trades = int(insights.get("overall", {}).get("total_trades", 0) or 0)

    # Count breakdown from DB
    incomplete_count = 0
    quarantined_count = 0
    try:
        from db.models import TradeOutcome
        session = _get_pg_session()
        try:
            incomplete_count = session.query(TradeOutcome).filter(
                TradeOutcome.environment == "LEGACY_INCOMPLETE"
            ).count()
        finally:
            session.close()
    except Exception:
        pass

    if total_trades <= 0:
        to_next_recalibration = ANALYSE_EVERY_N_TRADES
    else:
        to_next_recalibration = (ANALYSE_EVERY_N_TRADES - (total_trades % ANALYSE_EVERY_N_TRADES)) % ANALYSE_EVERY_N_TRADES

    health = {
        "analyse_every_n_trades": ANALYSE_EVERY_N_TRADES,
        "trades_until_recalibration": to_next_recalibration,
        "state": _learning_state_label(total_trades),
        "canonical_sample_requirement": PAPER_TRADING_UNLOCK_MIN_TRADES,
        "genuine_forward_trades": total_trades,
        "legacy_incomplete_trades": incomplete_count,
        "bounds": [0.75, 1.25] if total_trades < 100 else [0.50, 1.75],
    }

    regime_weights = weights.get("regime_weights") or weights.get("weights_2d") or DEFAULT_REGIME_WEIGHTS_2D

    return {
        "insights":  insights,
        "weights":   weights,
        "weights_2d": regime_weights,
        "regime_matrix_stats": insights.get("regime_matrix_stats", {}),
        "session_phase_stats": insights.get("session_phase_stats", {}),
        "expectancy_metrics": insights.get("expectancy_metrics", {}),
        "health":    health,
        "storage":   "postgresql" if _is_using_postgres() else "sqlite",
    }


def get_recent_outcomes(limit: int = 100) -> list[dict]:
    """Return recent trade outcomes for the dashboard table."""
    try:
        from db.models import TradeOutcome
        session = _get_pg_session()
        try:
            rows = session.query(TradeOutcome).filter(
                ~TradeOutcome.symbol.like("TEST%"),
                ~TradeOutcome.symbol.like("MOCK%"),
                ~TradeOutcome.symbol.like("DEMO%"),
                ~TradeOutcome.symbol.like("SIM_%"),
            ).order_by(TradeOutcome.id.desc()).limit(limit).all()
            return [
                {
                    "id": r.id,
                    "trade_id": r.trade_id,
                    "symbol": r.symbol,
                    "direction": r.direction,
                    "signal_type": r.signal_type,
                    "regime": r.regime,
                    "rsi_at_entry": r.rsi_at_entry,
                    "volume_above_avg": r.volume_above_avg,
                    "pnl_pct": r.pnl_pct,
                    "result": r.result,
                    "closed_at": r.closed_at,
                    "confidence": r.confidence,
                    "exit_reason": r.exit_reason,
                    "environment": r.environment,
                    "asset_class": r.asset_class,
                }
                for r in rows
            ]
        finally:
            session.close()
    except Exception as exc:
        log.error("get_recent_outcomes failed: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_signal_type(raw: str) -> str:
    raw = (raw or "").upper().strip()
    mapping = {
        "RSI": "RSI_BOUNCE", "RSI_BOUNCE": "RSI_BOUNCE",
        "RSI_OVERSOLD": "RSI_BOUNCE", "RSI_OVERBOUGHT": "RSI_BOUNCE",
        "MACD": "MACD_CROSS", "MACD_CROSS": "MACD_CROSS",
        "MACD_HIST": "MACD_CROSS",
        "BREAKOUT": "BREAKOUT", "BREAK": "BREAKOUT",
        "EMA": "EMA_CROSS", "EMA_CROSS": "EMA_CROSS",
        "DIV": "DIVERGENCE", "DIVERGENCE": "DIVERGENCE",
        "BULLISH_DIV": "DIVERGENCE", "BEARISH_DIV": "DIVERGENCE",
        "VOLUME": "VOLUME_SPIKE", "VOLUME_SPIKE": "VOLUME_SPIKE",
        "AUTONOMOUS": "COMBINED",
    }
    return mapping.get(raw, "COMBINED")


def _calculate_expectancy_metrics(group: list[dict]) -> dict:
    """
    Calculates profit factor and mathematical expectancy (EV) for a group of trades.
    EV = (WinRate * AvgWin) - (LossRate * AvgLoss)
    """
    if not group:
        return {
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 1.0,
            "expectancy": 0.0,
            "weighted_expectancy": 0.0,
        }
    wins = [t for t in group if t.get("result") == "WIN"]
    losses = [t for t in group if t.get("result") == "LOSS"]
    total_count = len(group)

    win_pnls = [float(t.get("pnl_pct", 0.0)) for t in wins if float(t.get("pnl_pct", 0.0)) > 0]
    loss_pnls = [abs(float(t.get("pnl_pct", 0.0))) for t in losses if float(t.get("pnl_pct", 0.0)) < 0]

    avg_win = round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0.0
    avg_loss = round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0

    sum_wins = sum(win_pnls)
    sum_losses = sum(loss_pnls)
    if sum_losses > 0:
        profit_factor = round(sum_wins / sum_losses, 2)
    elif sum_wins > 0:
        profit_factor = round(sum_wins, 2)
    else:
        profit_factor = 1.0

    win_rate_dec = len(wins) / total_count if total_count else 0.0
    loss_rate_dec = len(losses) / total_count if total_count else 0.0
    expectancy = round((win_rate_dec * avg_win) - (loss_rate_dec * avg_loss), 2)

    total_weight = sum(float(t.get("_w", 1.0)) for t in group)
    weighted_wins = sum(float(t.get("_w", 1.0)) for t in wins)
    weighted_losses = max(0.0, total_weight - weighted_wins)
    weighted_wr_dec = _safe_div(weighted_wins, total_weight)
    weighted_loss_dec = _safe_div(weighted_losses, total_weight)
    weighted_expectancy = round((weighted_wr_dec * avg_win) - (weighted_loss_dec * avg_loss), 2)

    return {
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "weighted_expectancy": weighted_expectancy,
    }


def _group_win_rate(trades: list[dict], key: str) -> dict:
    groups: dict[str, list] = {}
    for t in trades:
        val = str(t.get(key) or "UNKNOWN").upper()
        groups.setdefault(val, []).append(t)

    result = {}
    for val, group in groups.items():
        wins = [t for t in group if t["result"] == "WIN"]
        total_weight = sum(float(t.get("_w", 1.0)) for t in group)
        weighted_wins = sum(float(t.get("_w", 1.0)) for t in wins)
        weighted_pnl = sum(float(t.get("pnl_pct", 0.0)) * float(t.get("_w", 1.0)) for t in group)
        ev_metrics = _calculate_expectancy_metrics(group)
        result[val] = {
            "trades":   len(group),
            "wins":     len(wins),
            "weighted_trades": round(total_weight, 2),
            "win_rate": round(_safe_div(weighted_wins, total_weight) * 100, 1),
            "win_rate_unweighted": round(len(wins) / len(group) * 100, 1) if group else 0,
            "avg_pnl":  round(_safe_div(weighted_pnl, total_weight), 2),
            "avg_pnl_unweighted": round(sum(float(t.get("pnl_pct", 0.0)) for t in group) / len(group), 2) if group else 0,
            "avg_win":  ev_metrics["avg_win"],
            "avg_loss": ev_metrics["avg_loss"],
            "profit_factor": ev_metrics["profit_factor"],
            "expectancy": ev_metrics["expectancy"],
            "weighted_expectancy": ev_metrics["weighted_expectancy"],
        }
    return result


def _rsi_zone_analysis(trades: list[dict]) -> dict:
    zones = {
        "oversold (<30)":    [],
        "mild_bull (30-50)": [],
        "mild_bear (50-70)": [],
        "overbought (>70)":  [],
        "unknown":           [],
    }
    for t in trades:
        rsi = t.get("rsi_at_entry")
        if rsi is None or rsi <= 0.0:
            zones["unknown"].append(t)
        elif rsi < 30:
            zones["oversold (<30)"].append(t)
        elif rsi < 50:
            zones["mild_bull (30-50)"].append(t)
        elif rsi < 70:
            zones["mild_bear (50-70)"].append(t)
        else:
            zones["overbought (>70)"].append(t)

    out = {}
    for zone, g in zones.items():
        total_weight = sum(float(t.get("_w", 1.0)) for t in g)
        weighted_wins = sum(float(t.get("_w", 1.0)) for t in g if t["result"] == "WIN")
        raw_win_rate = round(sum(1 for t in g if t["result"] == "WIN") / len(g) * 100, 1) if g else 0
        out[zone] = {
            "trades": len(g),
            "weighted_trades": round(total_weight, 2),
            "win_rate": round(_safe_div(weighted_wins, total_weight) * 100, 1),
            "win_rate_unweighted": raw_win_rate,
        }
    return out


def _stats_block(group: list[dict]) -> dict:
    if not group:
        return {
            "trades": 0, "wins": 0, "weighted_trades": 0, "win_rate": 0, "win_rate_unweighted": 0, "avg_pnl": 0,
            "avg_pnl_unweighted": 0, "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 1.0,
            "expectancy": 0.0, "weighted_expectancy": 0.0
        }
    wins = [t for t in group if t["result"] == "WIN"]
    total_weight = sum(float(t.get("_w", 1.0)) for t in group)
    weighted_wins = sum(float(t.get("_w", 1.0)) for t in wins)
    weighted_pnl = sum(float(t.get("pnl_pct", 0.0)) * float(t.get("_w", 1.0)) for t in group)
    ev_metrics = _calculate_expectancy_metrics(group)
    return {
        "trades":   len(group),
        "wins":     len(wins),
        "weighted_trades": round(total_weight, 2),
        "win_rate": round(_safe_div(weighted_wins, total_weight) * 100, 1),
        "win_rate_unweighted": round(len(wins) / len(group) * 100, 1),
        "avg_pnl":  round(_safe_div(weighted_pnl, total_weight), 2),
        "avg_pnl_unweighted": round(sum(float(t.get("pnl_pct", 0.0)) for t in group) / len(group), 2),
        "avg_win":  ev_metrics["avg_win"],
        "avg_loss": ev_metrics["avg_loss"],
        "profit_factor": ev_metrics["profit_factor"],
        "expectancy": ev_metrics["expectancy"],
        "weighted_expectancy": ev_metrics["weighted_expectancy"],
    }


def _empty_insights() -> dict:
    return {
        "signal_type_stats":  {},
        "regime_stats":       {},
        "rsi_zone_stats":     {},
        "volume_stats":       {"above_avg": {"trades": 0}, "below_avg": {"trades": 0}},
        "session_phase_stats": {},
        "regime_matrix_stats": {},
        "expectancy_metrics": {
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 1.0,
            "expectancy": 0.0,
            "weighted_expectancy": 0.0,
        },
        "confidence_band_stats": {},
        "confidence_calibration": {
            "is_monotonic": False,
            "high_minus_low": 0,
            "status": "NO_DATA",
        },
        "overall":            {
            "total_trades": 0,
            "effective_trades": 0,
            "win_rate": 0,
            "win_rate_unweighted": 0,
            "avg_pnl_pct": 0,
            "avg_pnl_pct_unweighted": 0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 1.0,
            "expectancy": 0.0,
            "weighted_expectancy": 0.0,
            "model_accuracy": 0,
            "reliability_score": 0,
        },
        "best_signal_type":   "—",
        "worst_signal_type":  "—",
        "best_regime":        "—",
        "optimal_rsi_zone":   "—",
        "insights_at":        datetime.now(tz=_IST).isoformat(),
    }


def _attach_recency_weights(trades: list[dict], half_life: int = RECENCY_HALF_LIFE_TRADES) -> None:
    hl = max(1, int(half_life))
    for idx, t in enumerate(trades):
        t["_w"] = float(0.5 ** (idx / hl))


def _safe_div(num: float, den: float) -> float:
    if den <= 0:
        return 0.0
    return float(num) / float(den)


def _learning_state_label(total_trades: int) -> str:
    if total_trades >= 200:
        return "MATURE"
    if total_trades >= 80:
        return "WARMING_UP"
    if total_trades >= PAPER_TRADING_UNLOCK_MIN_TRADES:
        return "EARLY"
    return "COLD_START"


def _confidence_band_analysis(trades: list[dict]) -> dict:
    bands = {
        "60-70": (60.0, 70.0),
        "70-80": (70.0, 80.0),
        "80-90": (80.0, 90.0),
        "90+": (90.0, None),
    }
    out: dict[str, dict] = {}
    for label, (lo, hi) in bands.items():
        if hi is None:
            group = [t for t in trades if float(t.get("confidence", 0.0) or 0.0) >= lo]
        else:
            group = [
                t for t in trades
                if lo <= float(t.get("confidence", 0.0) or 0.0) < hi
            ]
        wins = [t for t in group if t.get("result") == "WIN"]
        total_weight = sum(float(t.get("_w", 1.0)) for t in group)
        weighted_wins = sum(float(t.get("_w", 1.0)) for t in wins)
        out[label] = {
            "trades": len(group),
            "weighted_trades": round(total_weight, 2),
            "win_rate": round(_safe_div(weighted_wins, total_weight) * 100, 1),
            "win_rate_unweighted": round(_safe_div(len(wins), len(group)) * 100, 1),
        }
    return out


def _confidence_calibration_summary(bands: dict) -> dict:
    order = ["60-70", "70-80", "80-90", "90+"]
    series = [
        float((bands.get(k) or {}).get("win_rate", 0.0) or 0.0)
        for k in order
        if int((bands.get(k) or {}).get("trades", 0) or 0) > 0
    ]
    if len(series) < 2:
        return {
            "is_monotonic": False,
            "high_minus_low": 0,
            "status": "NO_DATA",
        }

    monotonic = all(series[i] >= series[i - 1] for i in range(1, len(series)))
    spread = round(series[-1] - series[0], 1)
    if monotonic and spread >= 5.0:
        status = "CALIBRATED"
    elif spread >= 0:
        status = "WEAK_CALIBRATION"
    else:
        status = "MISALIGNED"
    return {
        "is_monotonic": monotonic,
        "high_minus_low": spread,
        "status": status,
    }
