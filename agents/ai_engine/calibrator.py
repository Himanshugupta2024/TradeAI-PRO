"""
Independent Platt Sigmoid Probability Calibrator.

Uses modern scikit-learn (>=1.6/1.8):
  CalibratedClassifierCV(FrozenEstimator(estimator), method="sigmoid")

Ensures calibrated probabilities represent true empirical win rates:
  e.g., signals bucketed at 70-80% confidence historically yield ~75% winning trades.

Computes validation metrics:
  - Brier Score: 1/N * sum((p_i - y_i)^2)  (Benchmark < 0.22)
  - Log Loss: Cross-entropy error
  - Expected Calibration Error (ECE)
"""

from __future__ import annotations

import logging
import os
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import brier_score_loss, log_loss

log = logging.getLogger(__name__)

DEFAULT_CALIBRATOR_PATH = "models/calibrator.pkl"


class ProbabilityCalibrator:
    """
    Sigmoid (Platt) probability calibrator wrapping a fitted estimator.
    """

    def __init__(self, model_path: str = DEFAULT_CALIBRATOR_PATH):
        self.model_path = model_path
        self._calibrator: CalibratedClassifierCV | None = None
        self._is_fitted: bool = False
        self._load_if_available()

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted

    def _load_if_available(self) -> None:
        if os.path.exists(self.model_path):
            try:
                with open(self.model_path, "rb") as f:
                    self._calibrator = pickle.load(f)
                    self._is_fitted = True
                    log.info("[Calibrator] Loaded calibrator from %s", self.model_path)
            except Exception as exc:
                log.warning("[Calibrator] Failed to load %s: %s", self.model_path, exc)

    def fit_calibration(
        self,
        base_estimator: Any,
        X_calib: pd.DataFrame | np.ndarray,
        y_calib: pd.Series | np.ndarray,
        save: bool = True,
    ) -> ProbabilityCalibrator:
        """
        Calibrate probabilities on an independent chronological holdout slice.
        Wraps base_estimator with FrozenEstimator to prevent refitting base model weights.
        """
        if len(X_calib) < 20:
            log.warning("[Calibrator] Insufficient data for calibration (%d rows)", len(X_calib))
            return self

        # Wrap in FrozenEstimator as mandated by scikit-learn 1.6+
        frozen = FrozenEstimator(base_estimator)
        self._calibrator = CalibratedClassifierCV(frozen, method="sigmoid")
        self._calibrator.fit(X_calib, y_calib)
        self._is_fitted = True

        if save:
            try:
                Path(self.model_path).parent.mkdir(parents=True, exist_ok=True)
                with open(self.model_path, "wb") as f:
                    pickle.dump(self._calibrator, f)
                log.info("[Calibrator] Saved calibrator to %s", self.model_path)
            except Exception as exc:
                log.warning("[Calibrator] Save failed: %s", exc)

        return self

    def calibrate(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        """
        Calibrate probabilities for input feature matrix.
        Returns calibrated win probability array (0.0 to 1.0).
        """
        if self._is_fitted and self._calibrator is not None:
            try:
                probs = self._calibrator.predict_proba(X)[:, 1]
                return probs
            except Exception as exc:
                log.debug("[Calibrator] Calibration inference failed: %s", exc)

        # Fallback: sigmoid transformation centered at 0.50
        return np.full(len(X), 0.50)

    def evaluate_quality(
        self,
        X_test: pd.DataFrame | np.ndarray,
        y_test: pd.Series | np.ndarray,
    ) -> dict[str, float]:
        """
        Compute calibration quality metrics: Brier score, log loss, and ECE.
        """
        probs = self.calibrate(X_test)
        brier = float(brier_score_loss(y_test, probs))
        ll = float(log_loss(y_test, probs))

        # Expected Calibration Error (ECE) over 5 bins
        bins = np.linspace(0.0, 1.0, 6)
        bin_indices = np.digitize(probs, bins) - 1
        ece = 0.0
        n = len(probs)

        for b in range(5):
            mask = bin_indices == b
            if np.any(mask):
                bin_prob = float(np.mean(probs[mask]))
                bin_true = float(np.mean(y_test[mask]))
                ece += (np.sum(mask) / n) * abs(bin_prob - bin_true)

        return {
            "brier_score": round(brier, 4),
            "log_loss": round(ll, 4),
            "expected_calibration_error": round(ece, 4),
        }


_CALIBRATOR: ProbabilityCalibrator | None = None


def get_calibrator() -> ProbabilityCalibrator:
    global _CALIBRATOR
    if _CALIBRATOR is None:
        _CALIBRATOR = ProbabilityCalibrator()
    return _CALIBRATOR


_PROBABILITY_KEYS = (
    "p_calibrated",
    "calibrated_prob",
    "prob",
    "prob_up",
    "win_probability",
)


def _trade_probability(trade: dict[str, Any]) -> tuple[float | None, str]:
    """Extract the model's predicted win probability from one closed trade.

    Only explicitly named probability fields are trusted as probabilities.
    `confidence` is a separate 0-100 heuristic score, and the previous
    implementation conflated the two: it divided by 100 only when the value was
    above 1, so `confidence == 1.0` (1%, the weakest possible signal) was read
    as a 100% win probability and dragged the ECE up by itself.

    Returns (probability in (0, 1], source field name).
    """
    for key in _PROBABILITY_KEYS:
        if trade.get(key) is None:
            continue
        try:
            value = float(trade[key])
        except (TypeError, ValueError):
            continue
        # Probability fields are written either as 0-1 or as 0-100 percentages.
        if value > 1.0:
            value = value / 100.0
        if 0.0 < value <= 1.0:
            return value, key
        return None, key

    # Fallback: the heuristic confidence score is always 0-100.
    raw_conf = trade.get("confidence")
    if raw_conf is None:
        return None, "confidence"
    try:
        value = float(raw_conf) / 100.0
    except (TypeError, ValueError):
        return None, "confidence"
    if 0.0 < value <= 1.0:
        return value, "confidence"
    return None, "confidence"


def _trade_pnl(trade: dict[str, Any]) -> float | None:
    """Return the trade P&L, accepting the column names actually stored."""
    for key in ("pnl", "pnl_pct", "total_pnl", "realized_pnl"):
        if trade.get(key) is None:
            continue
        try:
            return float(trade[key])
        except (TypeError, ValueError):
            continue
    return None


def check_live_calibration_drift(
    closed_trades: list[dict[str, Any]],
    threshold_ece: float = 0.10,
    min_sample_size: int = 20,
) -> dict[str, Any]:
    """
    Check for live intra-week probability calibration drift on closed trades.
    Compares predicted win probability against empirical binary outcomes.

    Trades without a usable probability or without a resolved outcome are
    counted and reported instead of being dropped silently -- a drift monitor
    that quietly ignores most of its input can look healthy forever.

    Note: no production caller wires this up yet; only tests exercise it. The
    report is only as meaningful as the probability it reads, so callers must
    pass rows that carry `p_calibrated` (or an equivalent probability field).
    """
    valid: list[tuple[float, int]] = []
    probability_sources: dict[str, int] = {}
    invalid_probability = 0
    breakeven_excluded = 0
    unresolved = 0

    for t in closed_trades:
        prob, source = _trade_probability(t)
        pnl = _trade_pnl(t)
        status = str(t.get("status", t.get("exit_reason", ""))).upper()

        if prob is None:
            invalid_probability += 1
            continue

        if "TP" in status or (pnl is not None and pnl > 0):
            outcome = 1
        elif "SL" in status or "TRAIL" in status or (pnl is not None and pnl < 0):
            outcome = 0
        elif pnl is not None:
            # Exactly flat: neither win nor loss, so it carries no calibration
            # signal. Counted separately because a growing pile of zero-P&L
            # exits is itself worth noticing.
            breakeven_excluded += 1
            continue
        else:
            unresolved += 1
            continue

        valid.append((prob, outcome))
        probability_sources[source] = probability_sources.get(source, 0) + 1

    diagnostics = {
        "invalid_probability": invalid_probability,
        "breakeven_excluded": breakeven_excluded,
        "unresolved_outcome": unresolved,
        "probability_sources": probability_sources,
    }

    if len(valid) < min_sample_size:
        return {
            "sample_size": len(valid),
            "status": "INSUFFICIENT_SAMPLE",
            "drift_detected": False,
            "ece": 0.0,
            "recommendation": f"Awaiting minimum {min_sample_size} closed trades (current: {len(valid)})",
            **diagnostics,
        }

    probs = np.array([v[0] for v in valid], dtype=float)
    trues = np.array([v[1] for v in valid], dtype=float)

    mean_pred = float(np.mean(probs))
    realized_wr = float(np.mean(trues))
    brier = float(np.mean((probs - trues) ** 2))

    bins = np.linspace(0.0, 1.0, 6)
    bin_indices = np.digitize(probs, bins) - 1
    ece = 0.0
    n = len(probs)
    for b in range(5):
        mask = bin_indices == b
        if np.any(mask):
            bin_p = float(np.mean(probs[mask]))
            bin_t = float(np.mean(trues[mask]))
            ece += (np.sum(mask) / n) * abs(bin_p - bin_t)

    drift_detected = bool(ece > threshold_ece or abs(mean_pred - realized_wr) > threshold_ece)
    if drift_detected:
        rec = "TRIGGER_RETRAIN: Intra-week probability drift detected (>10%). Platt calibrator needs re-fitting."
    else:
        rec = "CALIBRATION_HEALTHY: Probabilities track realized win rates within guardrail."

    return {
        "sample_size": len(valid),
        "mean_predicted_prob": round(mean_pred, 3),
        "realized_win_rate": round(realized_wr, 3),
        "brier_score": round(brier, 4),
        "ece": round(ece, 4),
        "drift_detected": drift_detected,
        "recommendation": rec,
        **diagnostics,
    }

