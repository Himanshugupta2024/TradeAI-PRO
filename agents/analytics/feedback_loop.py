"""
Self-Learning Feedback Loop for TradeAI PRO.

Architecture
───────────
1. TradeOutcomeLogger  — writes one row per closed trade to `trade_outcomes` table in SQLite.
2. FeedbackAnalyser    — every 50 trades, queries the table and computes:
     - Win rate by signal_type    (RSI / MACD / BREAKOUT / EMA_CROSS / DIVERGENCE / OTHER)
     - Win rate by market_regime  (TRENDING / RANGING / HIGH_VOLATILITY)
     - Optimal RSI zone           (< 30 / 30-50 / 50-70 / > 70)
     - Volume impact              (above_avg vs below_avg)
3. WeightAdapter       — updates `config/learned_weights.json` based on above analysis.
   The signal_generator reads this file at startup (or every N minutes) to re-weight
   individual signal components without code changes.

DB schema (trade_outcomes):
  id              INTEGER PRIMARY KEY
  symbol          TEXT
  direction       TEXT            -- LONG / SHORT
  signal_type     TEXT            -- RSI_BOUNCE / MACD_CROSS / BREAKOUT / EMA_CROSS / DIVERGENCE / COMBINED
  regime          TEXT            -- TRENDING / RANGING / HIGH_VOLATILITY / UNKNOWN
  rsi_at_entry    REAL
  volume_above_avg INTEGER        -- 1 = yes, 0 = no
  entry_price     REAL
  exit_price      REAL
  pnl_pct         REAL
  result          TEXT            -- WIN / LOSS
  closed_at       TEXT            -- ISO timestamp (IST)
  confidence      REAL            -- model confidence 0-100 at signal time
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# ── Paths ─────────────────────────────────────────────────────────────────────
_DB_PATH = Path("trading.db")
_WEIGHTS_PATH = Path("config/learned_weights.json")

# ── Feedback trigger: analyse after every N closed trades ─────────────────────
ANALYSE_EVERY_N_TRADES = 50

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


# ─────────────────────────────────────────────────────────────────────────────
# DB bootstrapper
# ─────────────────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def bootstrap_db():
    """Create `trade_outcomes` table if it doesn't exist. Safe to call at startup."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_outcomes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                direction       TEXT    NOT NULL,
                signal_type     TEXT    NOT NULL DEFAULT 'COMBINED',
                regime          TEXT    NOT NULL DEFAULT 'UNKNOWN',
                rsi_at_entry    REAL,
                volume_above_avg INTEGER NOT NULL DEFAULT 0,
                entry_price     REAL    NOT NULL,
                exit_price      REAL    NOT NULL,
                pnl_pct         REAL    NOT NULL,
                result          TEXT    NOT NULL,   -- WIN or LOSS
                closed_at       TEXT    NOT NULL,
                confidence      REAL    DEFAULT 0.0,
                exit_reason     TEXT    DEFAULT ''
            )
        """)
        # Index for fast aggregation queries
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_to_signal_type ON trade_outcomes(signal_type)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_to_regime ON trade_outcomes(regime)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_to_result ON trade_outcomes(result)
        """)
        conn.commit()
    log.debug("trade_outcomes table bootstrapped")


# ─────────────────────────────────────────────────────────────────────────────
# TradeOutcomeLogger  — called by paper_trading._close_trade()
# ─────────────────────────────────────────────────────────────────────────────

class TradeOutcomeLogger:
    """
    Logs each closed paper trade to the `trade_outcomes` SQLite table.

    Call `log_closed_trade()` from PaperTradingEngine._close_trade().
    After every ANALYSE_EVERY_N_TRADES trade, auto-triggers FeedbackAnalyser.
    """

    def __init__(self):
        bootstrap_db()
        self._trade_count = self._get_logged_count()
        log.info("TradeOutcomeLogger ready — %d trades already logged", self._trade_count)

    def _get_logged_count(self) -> int:
        try:
            with _get_conn() as conn:
                row = conn.execute("SELECT COUNT(*) as c FROM trade_outcomes").fetchone()
                return int(row["c"])
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
    ) -> None:
        """
        Log one closed trade outcome to SQLite.

        Parameters
        ----------
        symbol           : e.g. "RELIANCE.NS" or "BTC/USDT"
        direction        : "LONG" or "SHORT"
        signal_type      : Which pattern triggered — "RSI_BOUNCE", "MACD_CROSS",
                           "BREAKOUT", "EMA_CROSS", "DIVERGENCE", "COMBINED"
        regime           : "TRENDING", "RANGING", "HIGH_VOLATILITY", "UNKNOWN"
        rsi_at_entry     : RSI value (0-100) at the bar that generated the signal
        volume_above_avg : True if volume > 1.5x average at entry
        entry_price      : Fill price
        exit_price       : Exit price (TP / SL / manual)
        pnl_pct          : % P&L (positive = win)
        exit_reason      : "CLOSED_TP" / "CLOSED_SL" / "CLOSED_TRAIL" / "CLOSED_MANUAL"
        confidence       : AI confidence score at signal time (0-100)
        """
        result = "WIN" if pnl_pct > 0 else "LOSS"
        closed_at = datetime.now(tz=_IST).isoformat()

        # Normalise signal_type
        signal_type = _normalise_signal_type(signal_type)

        try:
            with _get_conn() as conn:
                conn.execute("""
                    INSERT INTO trade_outcomes
                        (symbol, direction, signal_type, regime, rsi_at_entry,
                         volume_above_avg, entry_price, exit_price, pnl_pct,
                         result, closed_at, confidence, exit_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    symbol, direction, signal_type, regime,
                    rsi_at_entry, int(volume_above_avg),
                    entry_price, exit_price, pnl_pct,
                    result, closed_at, confidence, exit_reason,
                ))
                conn.commit()

            self._trade_count += 1
            log.info(
                "📝 Feedback logged: %s %s %s | %s | RSI=%.1f | %.2f%% | %s",
                result, direction, symbol,
                signal_type, rsi_at_entry or 0.0, pnl_pct, regime,
            )

            # Auto-trigger analysis every N trades
            if self._trade_count % ANALYSE_EVERY_N_TRADES == 0:
                log.info(
                    "🧠 %d trades logged — running learning analysis...",
                    self._trade_count,
                )
                analyser = FeedbackAnalyser()
                insights = analyser.run()
                adapter = WeightAdapter()
                adapter.update_weights(insights)

        except Exception as exc:
            log.error("TradeOutcomeLogger.log_closed_trade failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# FeedbackAnalyser  — runs every 50 trades
# ─────────────────────────────────────────────────────────────────────────────

class FeedbackAnalyser:
    """
    Queries trade_outcomes and returns structured insights.

    Returns a dict with:
      signal_type_stats  : {type → {win_rate, trades, wins, avg_pnl}}
      regime_stats       : {regime → {win_rate, trades, wins, avg_pnl}}
      rsi_zone_stats     : {zone → {win_rate, trades}}
      volume_stats       : {"above" → ..., "below" → ...}
      overall            : {total_trades, win_rate, avg_pnl, model_accuracy}
      best_signal_type   : str
      worst_signal_type  : str
      best_regime        : str
      optimal_rsi_zone   : str
      insights_at        : ISO timestamp
    """

    def run(self, lookback: int = 500) -> dict:
        """
        Run analysis on the last `lookback` trades.
        Returns structured insights dict.
        """
        try:
            with _get_conn() as conn:
                rows = conn.execute("""
                    SELECT signal_type, regime, rsi_at_entry, volume_above_avg,
                           pnl_pct, result, confidence
                    FROM trade_outcomes
                    ORDER BY id DESC
                    LIMIT ?
                """, (lookback,)).fetchall()

            if not rows:
                return _empty_insights()

            trades = [dict(r) for r in rows]

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

            # ── 5. Overall ────────────────────────────────────────────────────
            wins = [t for t in trades if t["result"] == "WIN"]
            total = len(trades)
            overall_win_rate = round(len(wins) / total * 100, 1) if total else 0
            avg_pnl = round(sum(t["pnl_pct"] for t in trades) / total, 2) if total else 0

            # Model accuracy: % trades where confidence > 50 that WON (higher conf = better accuracy)
            high_conf = [t for t in trades if (t.get("confidence") or 0) >= 50]
            hc_wins   = [t for t in high_conf if t["result"] == "WIN"]
            model_accuracy = round(len(hc_wins) / len(high_conf) * 100, 1) if high_conf else 0

            # ── 6. Pick best / worst ──────────────────────────────────────────
            def _best(stats: dict) -> str:
                """Return key with highest win_rate (min 5 trades to qualify)."""
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
                "overall": {
                    "total_trades":    total,
                    "win_rate":        overall_win_rate,
                    "avg_pnl_pct":     avg_pnl,
                    "model_accuracy":  model_accuracy,
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


# ─────────────────────────────────────────────────────────────────────────────
# WeightAdapter  — writes config/learned_weights.json
# ─────────────────────────────────────────────────────────────────────────────

class WeightAdapter:
    """
    Reads insights from FeedbackAnalyser and updates signal component weights
    in config/learned_weights.json.

    Weight adjustment logic (simple but auditable):
      - Each signal type gets weight = sigmoid(win_rate / 50 - 1) * 2
        → win_rate 50% → weight 1.0 (neutral)
        → win_rate 70% → weight ~1.46 (boost)
        → win_rate 30% → weight ~0.54 (reduce)
      - Weights are clipped to [0.3, 2.5] to avoid extremes.
    """

    def update_weights(self, insights: dict) -> dict:
        """
        Update learned_weights.json based on insights.
        Returns the new weights dict.
        """
        import math

        stats = insights.get("signal_type_stats", {})
        new_weights = dict(DEFAULT_WEIGHTS)

        for sig_type, data in stats.items():
            if sig_type not in new_weights:
                continue
            wr = data.get("win_rate", 50.0)
            nt = data.get("trades", 0)
            if nt < 5:
                # Too few samples — don't adjust
                continue

            # Sigmoid-like adjustment centred at 50% win rate
            # x = (win_rate - 50) / 20  →  ranges from -2.5 to +2.5
            x = (wr - 50.0) / 20.0
            weight = 1.0 / (1.0 + math.exp(-x))   # standard sigmoid 0..1
            weight = 0.3 + weight * (2.5 - 0.3)    # scale to 0.3..2.5
            weight = round(weight, 3)

            old = new_weights[sig_type]
            # Smooth: don't jump too fast (70% old + 30% new per cycle)
            new_weights[sig_type] = round(old * 0.7 + weight * 0.3, 3)

        # Load existing weights to smooth against them
        existing = self._load()
        for k in existing:
            if k in new_weights:
                new_weights[k] = round(existing[k] * 0.6 + new_weights[k] * 0.4, 3)

        new_weights["_updated_at"] = datetime.now(tz=_IST).isoformat()
        new_weights["_total_trades"] = insights.get("overall", {}).get("total_trades", 0)
        new_weights["_win_rate"] = insights.get("overall", {}).get("win_rate", 0)
        new_weights["_model_accuracy"] = insights.get("overall", {}).get("model_accuracy", 0)
        new_weights["_best_signal"] = insights.get("best_signal_type", "")
        new_weights["_worst_signal"] = insights.get("worst_signal_type", "")

        try:
            _WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_WEIGHTS_PATH, "w") as f:
                json.dump(new_weights, f, indent=2)
            log.info("✅ Learned weights updated → %s", _WEIGHTS_PATH)
        except Exception as exc:
            log.error("WeightAdapter.update_weights write failed: %s", exc)

        return new_weights

    def _load(self) -> dict:
        try:
            if _WEIGHTS_PATH.exists():
                with open(_WEIGHTS_PATH) as f:
                    return json.load(f)
        except Exception:
            pass
        return dict(DEFAULT_WEIGHTS)


# ─────────────────────────────────────────────────────────────────────────────
# Public API helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_learning_stats() -> dict:
    """
    Primary read API for /api/learning/stats FastAPI endpoint.
    Returns latest insights (runs analysis live over last 500 trades),
    plus current learned weights.
    """
    analyser = FeedbackAnalyser()
    insights = analyser.run()
    adapter  = WeightAdapter()
    weights  = adapter._load()

    return {
        "insights":  insights,
        "weights":   weights,
        "db_path":   str(_DB_PATH.resolve()),
    }


def get_recent_outcomes(limit: int = 100) -> list[dict]:
    """Return recent trade outcomes for the dashboard table."""
    try:
        bootstrap_db()
        with _get_conn() as conn:
            rows = conn.execute("""
                SELECT id, symbol, direction, signal_type, regime,
                       rsi_at_entry, volume_above_avg, pnl_pct, result,
                       closed_at, confidence, exit_reason
                FROM trade_outcomes
                ORDER BY id DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]
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
    }
    return mapping.get(raw, "COMBINED")


def _group_win_rate(trades: list[dict], key: str) -> dict:
    """Group trades by a categorical key and compute win rate + avg pnl."""
    groups: dict[str, list] = {}
    for t in trades:
        val = str(t.get(key) or "UNKNOWN").upper()
        groups.setdefault(val, []).append(t)

    result = {}
    for val, group in groups.items():
        wins = [t for t in group if t["result"] == "WIN"]
        result[val] = {
            "trades":   len(group),
            "wins":     len(wins),
            "win_rate": round(len(wins) / len(group) * 100, 1) if group else 0,
            "avg_pnl":  round(sum(t["pnl_pct"] for t in group) / len(group), 2) if group else 0,
        }
    return result


def _rsi_zone_analysis(trades: list[dict]) -> dict:
    """Bucket trades by RSI zone and return win rates."""
    zones = {
        "oversold (<30)":    [],
        "mild_bull (30-50)": [],
        "mild_bear (50-70)": [],
        "overbought (>70)":  [],
        "unknown":           [],
    }
    for t in trades:
        rsi = t.get("rsi_at_entry")
        if rsi is None:
            zones["unknown"].append(t)
        elif rsi < 30:
            zones["oversold (<30)"].append(t)
        elif rsi < 50:
            zones["mild_bull (30-50)"].append(t)
        elif rsi < 70:
            zones["mild_bear (50-70)"].append(t)
        else:
            zones["overbought (>70)"].append(t)

    return {
        zone: {
            "trades":   len(g),
            "win_rate": round(sum(1 for t in g if t["result"] == "WIN") / len(g) * 100, 1) if g else 0,
        }
        for zone, g in zones.items()
    }


def _stats_block(group: list[dict]) -> dict:
    if not group:
        return {"trades": 0, "win_rate": 0, "avg_pnl": 0}
    wins = [t for t in group if t["result"] == "WIN"]
    return {
        "trades":   len(group),
        "wins":     len(wins),
        "win_rate": round(len(wins) / len(group) * 100, 1),
        "avg_pnl":  round(sum(t["pnl_pct"] for t in group) / len(group), 2),
    }


def _empty_insights() -> dict:
    return {
        "signal_type_stats":  {},
        "regime_stats":       {},
        "rsi_zone_stats":     {},
        "volume_stats":       {"above_avg": {"trades": 0}, "below_avg": {"trades": 0}},
        "overall":            {"total_trades": 0, "win_rate": 0, "avg_pnl_pct": 0, "model_accuracy": 0},
        "best_signal_type":   "—",
        "worst_signal_type":  "—",
        "best_regime":        "—",
        "optimal_rsi_zone":   "—",
        "insights_at":        datetime.now(tz=_IST).isoformat(),
    }
