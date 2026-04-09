"""
Trade Journal — records every LONG/SHORT signal and its outcome,
then computes a multi-dimensional scorecard.

Persistence: trade_journal.json (same pattern as tradeflow_history.json)

Outcome values:
   1  WIN     — price hit TP within OUTCOME_FORWARD_BARS
  -1  LOSS    — price hit SL before TP
   0  TIMEOUT — neither hit (time-out / SKIP label)
  -2  PENDING — outcome not yet determined

False signal reason codes (set on LOSS / TIMEOUT outcomes):
  AGAINST_TREND      — signal direction opposed higher timeframe trend
  LOW_VOLUME         — volume confirmation was weak
  REGIME_MISMATCH    — regime did not suit the signal type
  POOR_SR_POSITION   — entered far from support/resistance
  WEAK_MOMENTUM      — RSI / MACD / ROC were not aligned
  NO_FOLLOW_THROUGH  — setup looked ok but price didn't move (timeout)
  BAD_SESSION        — signal fired outside optimal session hours
  UNKNOWN            — outcome not yet analysed
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional
from utils.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_JOURNAL_FILE = os.path.join(
    os.path.dirname(__file__), "..", "..", "trade_journal.json"
)

# Pending outcome marker
_PENDING = -2


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SignalOutcome:
    symbol:         str
    timestamp:      str            # ISO datetime string from signal bar
    signal:         str            # "LONG" | "SHORT"
    quality_score:  int            # 0-100
    quality_grade:  str            # A/B/C/D
    regime:         str            # TRENDING / RANGING / HIGH_VOLATILITY
    hour_ist:       int            # 9, 10, 11 ... (IST hour, 0-23)
    confidence:     float          # ai_confidence at signal bar
    data_quality:   float          # DataQualityReport.quality_pct (0-100)
    outcome:        int   = _PENDING   # 1/-1/0/-2
    false_reason:   str   = "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────

class TradeJournal:
    """
    Records signals and outcomes, computes multi-dimensional scorecard.
    Thread-safe for use inside async FastAPI server (single-process).
    """

    def __init__(self) -> None:
        self._records: List[SignalOutcome] = []
        self._loaded = False

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()
            self._loaded = True

    def _load(self) -> None:
        path = os.path.abspath(_JOURNAL_FILE)
        if not os.path.exists(path):
            self._records = []
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._records = [SignalOutcome(**r) for r in raw]
            logger.info("Trade journal loaded: %d records from %s", len(self._records), path)
        except Exception as exc:
            logger.warning("Could not load trade journal: %s", exc)
            self._records = []

    def _save(self) -> None:
        path = os.path.abspath(_JOURNAL_FILE)
        try:
            atomic_write_json(path, [asdict(r) for r in self._records], ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("Could not save trade journal: %s", exc)

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_signal(self, signal_data: dict, data_quality: float = 100.0) -> None:
        """
        Record a new LONG or SHORT signal.

        signal_data keys (from _get_signal_for_symbol / server):
          symbol, signal, timestamp, confidence, regime,
          quality_score, quality_grade
        """
        self._ensure_loaded()

        sig = signal_data.get("signal", "HOLD")
        if sig not in ("LONG", "SHORT"):
            return   # Only record actionable signals

        # Derive IST hour from timestamp
        try:
            ts = datetime.fromisoformat(signal_data.get("timestamp", ""))
            # Convert to IST (UTC+5:30)
            from datetime import timezone, timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
            ts_ist = ts.astimezone(IST) if ts.tzinfo else ts
            hour_ist = ts_ist.hour
        except Exception:
            hour_ist = 0

        record = SignalOutcome(
            symbol       = str(signal_data.get("symbol", "")),
            timestamp    = str(signal_data.get("timestamp", "")),
            signal       = sig,
            quality_score= int(signal_data.get("quality_score", 0)),
            quality_grade= str(signal_data.get("quality_grade", "D")),
            regime       = str(signal_data.get("regime", "RANGING")),
            hour_ist     = hour_ist,
            confidence   = float(signal_data.get("confidence", 0)),
            data_quality = float(data_quality),
            outcome      = _PENDING,
            false_reason = "UNKNOWN",
        )
        self._records.append(record)
        self._save()

    def record_outcome(
        self,
        symbol: str,
        timestamp: str,
        outcome: int,
        false_reason: str = "UNKNOWN",
    ) -> bool:
        """
        Update the outcome of a previously recorded signal.
        Returns True if the record was found and updated.
        """
        self._ensure_loaded()
        for rec in reversed(self._records):
            if rec.symbol == symbol and rec.timestamp == timestamp and rec.outcome == _PENDING:
                rec.outcome      = outcome
                rec.false_reason = false_reason if outcome != 1 else ""
                self._save()
                return True
        return False

    def resolve_pending_outcomes(self, df: pd.DataFrame, symbol: str) -> int:
        """
        Attempt to resolve PENDING outcomes for `symbol` using the latest df.
        Uses label_1r2r_outcome internally.
        Returns count of resolved outcomes.
        """
        self._ensure_loaded()
        import pandas as pd
        from agents.feature_engine.label_generator import label_1r2r_outcome
        from config import settings

        pending = [r for r in self._records if r.symbol == symbol and r.outcome == _PENDING]
        if not pending:
            return 0

        labels = label_1r2r_outcome(df)
        resolved = 0

        for rec in pending:
            try:
                # Find the bar in df matching the signal timestamp
                if "timestamp" in df.columns:
                    ts_series = pd.to_datetime(df["timestamp"])
                elif isinstance(df.index, pd.DatetimeIndex):
                    ts_series = df.index.to_series()
                else:
                    continue

                signal_ts = pd.Timestamp(rec.timestamp)
                # Nearest match within 1 minute
                diffs = (ts_series - signal_ts).abs()
                nearest_pos = diffs.idxmin()
                if diffs[nearest_pos] > pd.Timedelta(minutes=2):
                    continue

                iloc_pos = df.index.get_loc(nearest_pos)
                label = int(labels.iloc[iloc_pos])

                if label == _PENDING:
                    continue   # Not yet resolvable

                reason = "UNKNOWN"
                if label == -1:
                    reason = _infer_false_reason(df.iloc[iloc_pos], rec.signal)
                elif label == 0:
                    reason = "NO_FOLLOW_THROUGH"

                rec.outcome      = label
                rec.false_reason = reason if label != 1 else ""
                resolved += 1

            except Exception as exc:
                logger.debug("Could not resolve outcome for %s@%s: %s", symbol, rec.timestamp, exc)

        if resolved:
            self._save()
            logger.info("Resolved %d pending outcomes for %s", resolved, symbol)

        return resolved

    # ── Scorecard ─────────────────────────────────────────────────────────────

    def get_scorecard(self) -> dict:
        """
        Compute multi-dimensional win-rate scorecard from all resolved records.
        Returns dict ready to JSON-serialise for /api/analytics/scorecard.
        """
        self._ensure_loaded()

        resolved = [r for r in self._records if r.outcome in (1, -1, 0)]
        total    = len(resolved)

        if total == 0:
            return _empty_scorecard()

        # ── By regime ──
        by_regime = _group_winrate(resolved, lambda r: r.regime)

        # ── By symbol (top 15 by volume) ──
        by_symbol = _group_winrate(resolved, lambda r: r.symbol)
        # Sort by signal count descending, keep top 15
        by_symbol = dict(
            sorted(by_symbol.items(), key=lambda kv: kv[1]["total"], reverse=True)[:15]
        )

        # ── By hour (IST) ──
        by_hour = _group_winrate(resolved, lambda r: str(r.hour_ist))

        # ── By confidence bucket ──
        def conf_bucket(r: SignalOutcome) -> str:
            c = r.confidence
            if c < 30:   return "0-30"
            if c < 50:   return "30-50"
            if c < 70:   return "50-70"
            return "70-100"
        by_confidence = _group_winrate(resolved, conf_bucket)

        # ── By quality grade ──
        by_quality_grade = _group_winrate(resolved, lambda r: r.quality_grade)

        # ── False signal reasons (only LOSS/TIMEOUT records) ──
        false_reasons: Dict[str, int] = defaultdict(int)
        for r in resolved:
            if r.outcome in (-1, 0) and r.false_reason:
                false_reasons[r.false_reason] += 1
        false_reasons_sorted = dict(
            sorted(false_reasons.items(), key=lambda kv: kv[1], reverse=True)
        )

        # ── Overall stats ──
        wins   = sum(1 for r in resolved if r.outcome == 1)
        losses = sum(1 for r in resolved if r.outcome == -1)

        return {
            "total_signals":       total,
            "overall_win_rate":    round(wins / total, 4) if total else 0.0,
            "wins":                wins,
            "losses":              losses,
            "timeouts":            total - wins - losses,
            "by_regime":           by_regime,
            "by_symbol":           by_symbol,
            "by_hour":             by_hour,
            "by_confidence":       by_confidence,
            "by_quality_grade":    by_quality_grade,
            "false_signal_reasons": false_reasons_sorted,
            "pending_count":       sum(1 for r in self._records if r.outcome == _PENDING),
            "generated_at":        datetime.now().isoformat(),
        }

    # ── Adaptive Thresholds ───────────────────────────────────────────────────

    def get_adaptive_thresholds(self, min_signals: int = 20) -> dict[str, int]:
        """
        Compute quality threshold adjustments based on historical win rates.

        Analyses resolved outcomes per (regime, direction) tuple:
          - Win rate < 40% over last 30 signals → raise quality_threshold by +10
          - Win rate > 65% → lower quality_threshold by -5 (allow more signals)
          - Otherwise → no adjustment (0)

        Returns dict like:
          {"TRENDING_LONG": -5, "RANGING_SHORT": 10, "HIGH_VOLATILITY_LONG": 0, ...}
        """
        self._ensure_loaded()

        resolved = [r for r in self._records if r.outcome in (1, -1, 0)]
        if len(resolved) < min_signals:
            return {}   # Not enough data to adapt

        adjustments: dict[str, int] = {}

        # Group by (regime, direction)
        groups: dict[str, list] = defaultdict(list)
        for r in resolved:
            key = f"{r.regime}_{r.signal}"
            groups[key].append(r)

        for key, records in groups.items():
            # Use only the last 30 signals for recency
            recent = records[-30:]
            if len(recent) < 10:
                continue   # Not enough data for this key

            wins = sum(1 for r in recent if r.outcome == 1)
            win_rate = wins / len(recent)

            if win_rate < 0.40:
                adjustments[key] = 10    # Tighten: too many losers
            elif win_rate > 0.65:
                adjustments[key] = -5    # Loosen: missing good trades
            else:
                adjustments[key] = 0

        return adjustments

    # ── Utility ───────────────────────────────────────────────────────────────

    def get_recent_signals(self, limit: int = 50) -> list[dict]:
        """Return the most recent `limit` recorded signals as dicts."""
        self._ensure_loaded()
        return [asdict(r) for r in self._records[-limit:]]

    def total_count(self) -> int:
        self._ensure_loaded()
        return len(self._records)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton — one journal for the whole server process
# ─────────────────────────────────────────────────────────────────────────────

_journal: TradeJournal | None = None


def get_journal() -> TradeJournal:
    global _journal
    if _journal is None:
        _journal = TradeJournal()
    return _journal


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _group_winrate(
    records: list[SignalOutcome],
    key_fn,
) -> dict:
    """Aggregate win/loss/total/rate by a key function."""
    groups: dict[str, dict] = defaultdict(lambda: {"win": 0, "loss": 0, "timeout": 0, "total": 0})
    for r in records:
        k = key_fn(r)
        groups[k]["total"] += 1
        if r.outcome == 1:
            groups[k]["win"]     += 1
        elif r.outcome == -1:
            groups[k]["loss"]    += 1
        else:
            groups[k]["timeout"] += 1

    out = {}
    for k, v in groups.items():
        t = v["total"]
        out[k] = {
            "win":      v["win"],
            "loss":     v["loss"],
            "timeout":  v["timeout"],
            "total":    t,
            "win_rate": round(v["win"] / t, 4) if t else 0.0,
        }
    return out


def _infer_false_reason(row, signal: str) -> str:
    """Heuristic: guess the primary reason a signal failed."""
    htf = str(row.get("htf_trend", "")).upper()
    is_long = signal == "LONG"

    if is_long and "BEAR" in htf:
        return "AGAINST_TREND"
    if not is_long and "BULL" in htf:
        return "AGAINST_TREND"

    vol_ratio = row.get("volume_ratio_20")
    if vol_ratio is not None and float(vol_ratio or 0) < 0.5:
        return "LOW_VOLUME"

    session_q = row.get("session_quality")
    if session_q is not None and float(session_q or 1) < 0.5:
        return "BAD_SESSION"

    return "NO_FOLLOW_THROUGH"


def _empty_scorecard() -> dict:
    return {
        "total_signals":        0,
        "overall_win_rate":     0.0,
        "wins":                 0,
        "losses":               0,
        "timeouts":             0,
        "by_regime":            {},
        "by_symbol":            {},
        "by_hour":              {},
        "by_confidence":        {},
        "by_quality_grade":     {},
        "false_signal_reasons": {},
        "pending_count":        0,
        "generated_at":         datetime.now().isoformat(),
        "message":              "No resolved signals yet. Scorecard builds as trades complete.",
    }


# Needed inside the class methods
import pandas as pd  # noqa: E402 (placed after class to avoid circular import issues)
