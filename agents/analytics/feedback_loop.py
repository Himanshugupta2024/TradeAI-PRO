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
                    trades.append({
                        "signal_type": r.signal_type,
                        "regime": r.regime,
                        "rsi_at_entry": r.rsi_at_entry,
                        "volume_above_avg": r.volume_above_avg,
                        "pnl_pct": r.pnl_pct,
                        "result": r.result,
                        "confidence": r.confidence,
                    })
            finally:
                session.close()

            _attach_recency_weights(trades, half_life=RECENCY_HALF_LIFE_TRADES)

            # ── 1. Signal type win rates ──────────────────────────────────────
            signal_stats = _group_win_rate(trades, "signal_type")

            # ── 2. Regime win rates ───────────────────────────────────────────
            regime_stats = _group_win_rate(trades, "regime")

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

            # ── 5. Overall ────────────────────────────────────────────────────
            wins = [t for t in trades if t["result"] == "WIN"]
            total = len(trades)
            total_weight = sum(float(t.get("_w", 1.0)) for t in trades)
            weighted_wins = sum(float(t.get("_w", 1.0)) for t in trades if t["result"] == "WIN")
            weighted_pnl_sum = sum(float(t.get("pnl_pct", 0.0)) * float(t.get("_w", 1.0)) for t in trades)

            overall_win_rate = round(_safe_div(weighted_wins, total_weight) * 100, 1)
            overall_win_rate_unweighted = round(len(wins) / total * 100, 1) if total else 0
            avg_pnl = round(_safe_div(weighted_pnl_sum, total_weight), 2)
            avg_pnl_unweighted = round(sum(float(t.get("pnl_pct", 0.0)) for t in trades) / total, 2) if total else 0

            # Model accuracy: % trades where confidence > 50 that WON
            high_conf = [t for t in trades if (t.get("confidence") or 0) >= 50]
            high_conf_weight = sum(float(t.get("_w", 1.0)) for t in high_conf)
            hc_wins_weight = sum(float(t.get("_w", 1.0)) for t in high_conf if t["result"] == "WIN")
            model_accuracy = round(_safe_div(hc_wins_weight, high_conf_weight) * 100, 1)
            reliability_score = round(min(1.0, _safe_div(total_weight, 120.0)) * 100, 1)

            # ── 6. Pick best / worst ──────────────────────────────────────────
            def _best(stats: dict) -> str:
                eligible = {k: v for k, v in stats.items() if v["trades"] >= 5}
                if not eligible:
                    eligible = stats
                return max(eligible, key=lambda k: eligible[k]["win_rate"], default="UNKNOWN")

            def _worst(stats: dict) -> str:
                eligible = {k: v for k, v in stats.items() if v["trades"] >= 5}
                if not eligible:
                    eligible = stats
                return min(eligible, key=lambda k: eligible[k]["win_rate"], default="UNKNOWN")

            best_signal  = _best(signal_stats)
            worst_signal = _worst(signal_stats)
            best_regime  = _best(regime_stats)
            optimal_rsi  = _best(rsi_zone_stats)

            insights = {
                "signal_type_stats":  signal_stats,
                "regime_stats":       regime_stats,
                "rsi_zone_stats":     rsi_zone_stats,
                "volume_stats":       volume_stats,
                "confidence_band_stats": conf_band_stats,
                "confidence_calibration": conf_calibration,
                "overall": {
                    "total_trades":    total,
                    "effective_trades": round(total_weight, 2),
                    "win_rate":        overall_win_rate,
                    "win_rate_unweighted": overall_win_rate_unweighted,
                    "avg_pnl_pct":     avg_pnl,
                    "avg_pnl_pct_unweighted": avg_pnl_unweighted,
                    "model_accuracy":  model_accuracy,
                    "reliability_score": reliability_score,
                },
                "best_signal_type":  best_signal,
                "worst_signal_type": worst_signal,
                "best_regime":       best_regime,
                "optimal_rsi_zone":  optimal_rsi,
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
        today_wr = round(today_wins / today_count * 100, 1) if today_count > 0 else 0.0
        today_pnl = round(sum(float(t.get("pnl_pct", 0.0)) for t in today_trades), 2)

        review = {
            "review_date": target_date_ist,
            "today_trade_count": today_count,
            "today_wins": today_wins,
            "today_win_rate": today_wr,
            "today_total_pnl_pct": today_pnl,
            "best_signal_type": insights.get("best_signal_type", "NONE"),
            "worst_signal_type": insights.get("worst_signal_type", "NONE"),
            "updated_weights": new_weights,
            "insights": insights,
        }
        log.info(
            "📊 EOD Review [%s]: %d trades | WinRate: %.1f%% | PnL: %+.2f%% | Best: %s",
            target_date_ist, today_count, today_wr, today_pnl, review["best_signal_type"]
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
            log.info("Persisted learned weights to DB runtime_state (bounds: [%.2f, %.2f])", bound_lo, bound_hi)
        except Exception as exc:
            log.debug("WeightAdapter DB backup failed: %s", exc)

        try:
            _WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_WEIGHTS_PATH, new_weights, indent=2)
            log.info("Learned weights updated -> %s", _WEIGHTS_PATH)
        except Exception as exc:
            log.error("WeightAdapter.update_weights write failed: %s", exc)

        return new_weights

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

    return {
        "insights":  insights,
        "weights":   weights,
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
        result[val] = {
            "trades":   len(group),
            "wins":     len(wins),
            "weighted_trades": round(total_weight, 2),
            "win_rate": round(_safe_div(weighted_wins, total_weight) * 100, 1),
            "win_rate_unweighted": round(len(wins) / len(group) * 100, 1) if group else 0,
            "avg_pnl":  round(_safe_div(weighted_pnl, total_weight), 2),
            "avg_pnl_unweighted": round(sum(float(t.get("pnl_pct", 0.0)) for t in group) / len(group), 2) if group else 0,
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
        return {"trades": 0, "weighted_trades": 0, "win_rate": 0, "avg_pnl": 0}
    wins = [t for t in group if t["result"] == "WIN"]
    total_weight = sum(float(t.get("_w", 1.0)) for t in group)
    weighted_wins = sum(float(t.get("_w", 1.0)) for t in wins)
    weighted_pnl = sum(float(t.get("pnl_pct", 0.0)) * float(t.get("_w", 1.0)) for t in group)
    return {
        "trades":   len(group),
        "wins":     len(wins),
        "weighted_trades": round(total_weight, 2),
        "win_rate": round(_safe_div(weighted_wins, total_weight) * 100, 1),
        "win_rate_unweighted": round(len(wins) / len(group) * 100, 1),
        "avg_pnl":  round(_safe_div(weighted_pnl, total_weight), 2),
        "avg_pnl_unweighted": round(sum(float(t.get("pnl_pct", 0.0)) for t in group) / len(group), 2),
    }


def _empty_insights() -> dict:
    return {
        "signal_type_stats":  {},
        "regime_stats":       {},
        "rsi_zone_stats":     {},
        "volume_stats":       {"above_avg": {"trades": 0}, "below_avg": {"trades": 0}},
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
