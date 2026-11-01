"""
Phase 5: Paper-Trading Shadow Mode Evaluator.

Allows TradeAI Ensemble V2 to run concurrently alongside V1 in paper trading:
  - V1 generates and executes real/paper trades as primary.
  - V2 runs silently in shadow mode, recording its predictions, calibrated win
    probabilities, two-axis regimes, and anomaly vetos to SQLite 'shadow_signals'.
  - Evaluates actual outcomes (1R:2R) over time to track Brier score, expectancy,
    and the quality of anomaly vetoes before promoting V2 to primary.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "trading.db"


def init_shadow_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Initialize the shadow_signals table in SQLite if not present."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shadow_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    v1_direction TEXT,
                    v1_confidence REAL,
                    v2_direction TEXT,
                    v2_calibrated_win_prob REAL,
                    v2_raw_score REAL,
                    trend_regime TEXT,
                    volatility_regime TEXT,
                    anomaly_tier TEXT,
                    is_vetoed INTEGER DEFAULT 0,
                    actual_outcome INTEGER DEFAULT NULL,
                    resolved_at TEXT DEFAULT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_symbol ON shadow_signals(symbol);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_time ON shadow_signals(timestamp);")
            conn.commit()
    except Exception as exc:
        log.warning("[ShadowEvaluator] Failed to initialize DB table: %s", exc)


class ShadowEvaluator:
    """Logs and tracks shadow evaluation metrics between V1 and V2."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, conn: sqlite3.Connection | None = None):
        self.db_path = db_path
        self._conn = conn
        if self._conn is None and db_path == ":memory:":
            self._conn = sqlite3.connect(":memory:")

        if self._conn is not None:
            self._init_conn(self._conn)
        else:
            init_shadow_db(db_path)

    def _init_conn(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                v1_direction TEXT,
                v1_confidence REAL,
                v2_direction TEXT,
                v2_calibrated_win_prob REAL,
                v2_raw_score REAL,
                trend_regime TEXT,
                volatility_regime TEXT,
                anomaly_tier TEXT,
                is_vetoed INTEGER DEFAULT 0,
                actual_outcome INTEGER DEFAULT NULL,
                resolved_at TEXT DEFAULT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_symbol ON shadow_signals(symbol);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_time ON shadow_signals(timestamp);")
        conn.commit()

    def record_shadow_signal(
        self,
        symbol: str,
        v1_result: dict[str, Any],
        v2_result: dict[str, Any],
        timestamp: str | None = None,
    ) -> int:
        """Log a parallel signal from both V1 and V2."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        query = """
            INSERT INTO shadow_signals (
                timestamp, symbol, v1_direction, v1_confidence,
                v2_direction, v2_calibrated_win_prob, v2_raw_score,
                trend_regime, volatility_regime, anomaly_tier, is_vetoed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            ts,
            symbol,
            v1_result.get("direction", "NEUTRAL"),
            float(v1_result.get("confidence", 0.0)),
            v2_result.get("direction", "NEUTRAL"),
            float(v2_result.get("calibrated_trade_win_probability", 50.0)),
            float(v2_result.get("raw_ensemble_score", 50.0)),
            v2_result.get("trend_regime", "RANGING"),
            v2_result.get("volatility_regime", "NORMAL"),
            v2_result.get("anomaly_tier", "CLEAN"),
            1 if v2_result.get("is_vetoed", False) else 0,
        )
        try:
            if self._conn is not None:
                cursor = self._conn.cursor()
                cursor.execute(query, params)
                self._conn.commit()
                return cursor.lastrowid or 0
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                conn.commit()
                return cursor.lastrowid or 0
        except Exception as exc:
            log.debug("[ShadowEvaluator] Record signal failed: %s", exc)
            return 0

    def resolve_outcome(self, signal_id: int, outcome: int) -> None:
        """
        Record actual trade outcome (1 = WIN, 0 = LOSS, -1 = TIMEOUT).
        """
        now = datetime.now(timezone.utc).isoformat()
        query = """
            UPDATE shadow_signals
            SET actual_outcome = ?, resolved_at = ?
            WHERE id = ?
        """
        try:
            if self._conn is not None:
                self._conn.execute(query, (outcome, now, signal_id))
                self._conn.commit()
                return
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(query, (outcome, now, signal_id))
                conn.commit()
        except Exception as exc:
            log.debug("[ShadowEvaluator] Resolve outcome failed: %s", exc)

    def get_summary_metrics(self) -> dict[str, Any]:
        """Compute parallel comparison metrics between V1 and V2."""
        try:
            if self._conn is not None:
                conn = self._conn
                return self._compute_metrics_from_conn(conn)
            with sqlite3.connect(self.db_path) as conn:
                return self._compute_metrics_from_conn(conn)
        except Exception as exc:
            log.debug("[ShadowEvaluator] Metric summary failed: %s", exc)
            return {"error": str(exc)}

    def _compute_metrics_from_conn(self, conn: sqlite3.Connection) -> dict[str, Any]:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM shadow_signals")
        total_logged = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM shadow_signals WHERE actual_outcome IS NOT NULL")
        total_resolved = cursor.fetchone()[0]

        if total_resolved < 5:
            return {
                "total_logged": total_logged,
                "total_resolved": total_resolved,
                "status": "accumulating_samples",
                "min_required": 50,
            }

        cursor.execute("""
            SELECT v2_calibrated_win_prob, actual_outcome, is_vetoed
            FROM shadow_signals
            WHERE actual_outcome IN (0, 1)
        """)
        rows = cursor.fetchall()

        if not rows:
            return {"total_logged": total_logged, "total_resolved": total_resolved}

        probs = np.array([r[0] / 100.0 for r in rows])
        outcomes = np.array([r[1] for r in rows])
        vetoes = np.array([r[2] for r in rows])

        # Brier score of V2 calibrated probabilities
        brier_score = float(np.mean((probs - outcomes) ** 2))

        # Quality of veto: what fraction of vetoed trades actually failed (0)
        veto_mask = vetoes == 1
        if np.any(veto_mask):
            veto_success_rate = float(np.mean(outcomes[veto_mask] == 0)) * 100.0
        else:
            veto_success_rate = 100.0

        return {
            "total_logged": total_logged,
            "total_resolved": total_resolved,
            "v2_brier_score": round(brier_score, 4),
            "veto_quality_pct": round(veto_success_rate, 1),
            "status": "ready_for_review" if total_resolved >= 50 else "evaluating",
        }


_SHADOW_EVALUATOR: ShadowEvaluator | None = None


def get_shadow_evaluator() -> ShadowEvaluator:
    global _SHADOW_EVALUATOR
    if _SHADOW_EVALUATOR is None:
        _SHADOW_EVALUATOR = ShadowEvaluator()
    return _SHADOW_EVALUATOR
