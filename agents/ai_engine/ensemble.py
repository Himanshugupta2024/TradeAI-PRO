"""
Advanced Multi-Model Ensemble Predictor for Crypto/Stock Trading.

Combines four complementary model types + a sentiment layer:

  Model                    | Type       | Always Active | Base Weight
  ─────────────────────────┼────────────┼───────────────┼────────────
  Statistical (3-window)   | Rule-based | YES           | 0.25
  XGBoost                  | ML/tabular | When trained  | 0.30
  LightGBM                 | ML/tabular | When trained  | 0.25
  LSTM                     | DL/seq     | When trained  | 0.20
  ─────────────────────────┴────────────┴───────────────┴────────────
  Sentiment adjustment (Fear & Greed Index)              | ±15 pts

Why four model types?
  - Statistical catches classic momentum/divergence patterns
  - XGBoost learns non-linear feature interactions (no sequences needed)
  - LightGBM: faster, often more accurate than XGBoost on tabular data
  - LSTM captures temporal patterns (price memory across 60 candles)
  - Sentiment adds market psychology — the contrarian layer

Dynamic weight adjustment:
  Each model's weight is adjusted based on its rolling 20-prediction accuracy.
  Better-performing models get up to 1.5× their base weight.
  Worse-performing models get down to 0.5× their base weight.
  This allows the ensemble to adapt to changing market conditions.

Consensus boosting:
  All models agree → confidence ×1.20 (trust the agreement)
  Only 1/4 agrees  → confidence ×0.70 (penalise disagreement)

Usage:
    from agents.ai_engine.ensemble import predict_advanced, predict_batch_ensemble

    # Single prediction on latest bar
    result = predict_advanced(df, symbol="BTC/USDT")
    print(result["direction"], result["confidence"])

    # Batch predictions for all rows (used as ai_confidence in signal_generator)
    conf_series = predict_batch_ensemble(df, symbol="BTC/USDT")
"""

from __future__ import annotations

import logging
import collections
import sqlite3
from pathlib import Path
from typing import Deque

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Base model weights (renormalized if some models are unavailable)
_BASE_WEIGHTS = {
    "statistical": 0.25,
    "xgboost":     0.30,
    "lightgbm":    0.25,
    "lstm":        0.20,
}

_CRYPTO_SYMBOLS = {"BTC", "ETH", "SOL", "BNB", "XRP", "USDT", "ADA", "DOT", "AVAX", "MATIC"}

# Guardrails for dynamic ensemble weighting so one model cannot dominate instantly.
_MIN_ACTIVE_WEIGHT = 0.10
_MAX_ACTIVE_WEIGHT = 0.60
_MAX_WEIGHT_STEP = 0.20

# ── Dynamic Weight Tracker ─────────────────────────────────────────────────────

class _ModelPerformanceTracker:
    """
    Tracks rolling prediction accuracy for each model.

    Stores the last N (direction, actual_outcome) pairs per model.
    Computes a weight multiplier: 0.5 (poor) → 1.0 (average) → 1.5 (excellent).

    This allows the ensemble to automatically up-weight models that are
    performing well in the current market regime.
    """

    def __init__(self, window: int = 20):
        self.window = window
        self._history: dict[str, Deque] = {
            "statistical": collections.deque(maxlen=window),
            "xgboost":     collections.deque(maxlen=window),
            "lightgbm":    collections.deque(maxlen=window),
            "lstm":        collections.deque(maxlen=window),
        }

    def record(self, model_name: str, predicted_up: bool, actual_up: bool):
        """Record a prediction outcome for a model."""
        if model_name in self._history:
            correct = int(predicted_up == actual_up)
            self._history[model_name].append(correct)

    def get_weight_multiplier(self, model_name: str) -> float:
        """
        Return a weight multiplier based on recent accuracy.

        Accuracy → Multiplier:
          < 40%  → 0.50  (penalise poor model)
          40-50% → 0.75
          50-60% → 1.00  (neutral)
          60-70% → 1.25
          > 70%  → 1.50  (reward excellent model)
        """
        history = self._history.get(model_name, [])
        if len(history) < 5:
            return 1.0  # Not enough data — use base weight

        accuracy = sum(history) / len(history)

        if accuracy < 0.40:
            return 0.50
        elif accuracy < 0.50:
            return 0.75
        elif accuracy < 0.60:
            return 1.00
        elif accuracy < 0.70:
            return 1.25
        else:
            return 1.50

    def get_all_multipliers(self) -> dict[str, float]:
        return {name: self.get_weight_multiplier(name) for name in self._history}

    def get_accuracy_summary(self) -> dict[str, float]:
        result = {}
        for name, history in self._history.items():
            if len(history) >= 3:
                result[name] = round(sum(history) / len(history) * 100, 1)
            else:
                result[name] = None
        return result


# Module-level tracker (persists across calls within a process)
_tracker = _ModelPerformanceTracker(window=20)
_last_effective_weights: dict[str, float] = {}


def _guardrailed_normalize_weights(
    raw_weights: dict[str, float],
    previous_weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Normalize dynamic weights with min/max and per-update step guardrails."""
    active = {k: max(0.0, float(v)) for k, v in raw_weights.items() if float(v) > 0.0}
    if not active:
        return {k: 0.0 for k in raw_weights}

    if len(active) == 1:
        only = next(iter(active))
        out = {k: 0.0 for k in raw_weights}
        out[only] = 1.0
        return out

    total = sum(active.values()) or 1.0
    normalized = {k: v / total for k, v in active.items()}

    # Apply floor/cap per active model.
    bounded = {
        k: min(_MAX_ACTIVE_WEIGHT, max(_MIN_ACTIVE_WEIGHT, w))
        for k, w in normalized.items()
    }
    total_bounded = sum(bounded.values()) or 1.0
    bounded = {k: v / total_bounded for k, v in bounded.items()}

    # Limit abrupt shifts relative to the previous effective allocation.
    if previous_weights:
        stepped: dict[str, float] = {}
        for k, w in bounded.items():
            prev = float(previous_weights.get(k, w))
            lo = max(0.0, prev - _MAX_WEIGHT_STEP)
            hi = min(1.0, prev + _MAX_WEIGHT_STEP)
            stepped[k] = min(hi, max(lo, w))
        stepped_total = sum(stepped.values()) or 1.0
        bounded = {k: v / stepped_total for k, v in stepped.items()}

    out = {k: 0.0 for k in raw_weights}
    out.update(bounded)
    return out


def _db_path() -> Path:
    return Path("trading.db")


def _ensure_model_performance_table() -> None:
    with sqlite3.connect(str(_db_path())) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_name TEXT NOT NULL,
                predicted TEXT NOT NULL,
                actual TEXT NOT NULL,
                symbol TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_model_perf_model_ts
            ON model_performance(model_name, timestamp)
            """
        )
        conn.commit()


def _persist_prediction_outcome(model_name: str, predicted_direction: str, actual_direction: str) -> None:
    try:
        with sqlite3.connect(str(_db_path())) as conn:
            conn.execute(
                """
                INSERT INTO model_performance(model_name, predicted, actual)
                VALUES (?, ?, ?)
                """,
                (model_name, predicted_direction, actual_direction),
            )
            conn.commit()
    except Exception as exc:
        log.debug("[Ensemble] model_performance persist failed: %s", exc)


def _warm_start_tracker() -> None:
    _ensure_model_performance_table()
    try:
        with sqlite3.connect(str(_db_path())) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT model_name, predicted, actual
                FROM model_performance
                ORDER BY id DESC
                LIMIT 400
                """
            ).fetchall()

        for row in reversed(rows):
            model_name = str(row["model_name"])
            predicted = str(row["predicted"])
            actual = str(row["actual"])
            if model_name not in _tracker._history:
                continue
            if predicted not in ("UP", "DOWN") or actual not in ("UP", "DOWN"):
                continue
            _tracker.record(model_name, predicted == "UP", actual == "UP")
    except Exception as exc:
        log.debug("[Ensemble] warm-start load failed: %s", exc)


_warm_start_tracker()


def record_prediction_outcome(model_name: str, predicted_direction: str, actual_direction: str):
    """
    Record a prediction outcome for dynamic weight adjustment.

    Call this after each bar closes with the actual price direction.

    Args:
        model_name:          "statistical" | "xgboost" | "lightgbm" | "lstm"
        predicted_direction: "UP" | "DOWN" | "NEUTRAL"
        actual_direction:    "UP" | "DOWN"
    """
    if predicted_direction == "NEUTRAL":
        return  # Don't penalise neutral predictions
    predicted_up = predicted_direction == "UP"
    actual_up    = actual_direction    == "UP"
    _tracker.record(model_name, predicted_up, actual_up)
    _persist_prediction_outcome(model_name, predicted_direction, actual_direction)


def get_dynamic_weight_state() -> dict:
    """Return current dynamic-weight runtime snapshot for observability."""
    return {
        "effective_weights": {k: round(float(v), 4) for k, v in _last_effective_weights.items()},
        "multipliers": {k: round(float(v), 4) for k, v in _tracker.get_all_multipliers().items()},
        "accuracy": _tracker.get_accuracy_summary(),
        "guardrails": {
            "min_active_weight": _MIN_ACTIVE_WEIGHT,
            "max_active_weight": _MAX_ACTIVE_WEIGHT,
            "max_weight_step": _MAX_WEIGHT_STEP,
        },
    }


# ── Main Ensemble Functions ────────────────────────────────────────────────────

def predict_advanced(df: pd.DataFrame, symbol: str = "") -> dict:
    """
    Run the full 4-model ensemble on the latest bar of df.

    Args:
        df:     OHLCV + indicators DataFrame (≥60 rows recommended)
        symbol: e.g. "BTC/USDT"

    Returns:
        dict with keys:
          direction          "UP" | "DOWN" | "NEUTRAL"
          confidence         float 0-100
          prob_up            float 0-100
          prob_down          float 0-100
          method             "ensemble_advanced"
          model_votes        {model_name: {direction, confidence}}
          sentiment          SentimentResult dict
          ensemble_agreement int (0-4)
          dynamic_weights    {model_name: effective_weight}
          model_accuracy     {model_name: rolling_accuracy_%}
    """
    if df.empty or len(df) < 2:
        return _neutral("empty_df")

    # ── 1. Statistical predictor ───────────────────────────────────────────
    stat_result = {"direction": "NEUTRAL", "confidence": 0.0}
    try:
        from agents.ai_engine.fallback_predictor import predict_ensemble
        stat_result = predict_ensemble(df)
    except Exception as exc:
        log.debug("[Ensemble] Statistical failed: %s", exc)

    # ── 2. XGBoost predictor ───────────────────────────────────────────────
    xgb_result = {"direction": "NEUTRAL", "confidence": 0.0}
    xgb_fitted = False
    try:
        from agents.ai_engine.xgboost_model import get_xgb_predictor
        xgb = get_xgb_predictor()
        if xgb.is_fitted:
            xgb_result = xgb.predict_row(df)
            xgb_fitted = True
    except Exception as exc:
        log.debug("[Ensemble] XGBoost failed: %s", exc)

    # ── 3. LightGBM predictor ──────────────────────────────────────────────
    lgbm_result = {"direction": "NEUTRAL", "confidence": 0.0}
    lgbm_fitted = False
    try:
        from agents.ai_engine.lightgbm_model import get_lgbm_predictor
        lgbm = get_lgbm_predictor()
        if lgbm.is_fitted:
            lgbm_result = lgbm.predict_row(df)
            lgbm_fitted = True
    except Exception as exc:
        log.debug("[Ensemble] LightGBM failed: %s", exc)

    # ── 4. LSTM predictor ──────────────────────────────────────────────────
    lstm_result = {"direction": "NEUTRAL", "confidence": 0.0}
    lstm_fitted = False
    try:
        import torch
        from agents.ai_engine.lstm_model import TradingLSTM
        from agents.ai_engine.trainer import FEATURE_COLS
        model_path = "models/best_model.pt"
        if os.path.exists(model_path):
            seq_length = 60
            available_cols = [c for c in FEATURE_COLS if c in df.columns]
            if len(available_cols) >= 5 and len(df) >= seq_length:
                feat_mat = df[available_cols].dropna().values.astype("float64")
                if len(feat_mat) >= seq_length:
                    # z-score normalise using last seq_length rows
                    window = feat_mat[-seq_length:]
                    mean = feat_mat.mean(axis=0)
                    std  = feat_mat.std(axis=0)
                    std[std == 0] = 1.0
                    norm = (window - mean) / std
                    # Shape: (1, seq_length, n_features)
                    x_tensor = torch.FloatTensor(norm[np.newaxis])
                    model = TradingLSTM(input_size=len(available_cols))
                    state = torch.load(model_path, map_location="cpu", weights_only=True)
                    model.load_state_dict(state)
                    model.eval()
                    lstm_result = model.predict(x_tensor)
                    lstm_fitted = True
    except Exception as exc:
        log.debug("[Ensemble] LSTM failed: %s", exc)

    # ── 5. Sentiment adjustment ────────────────────────────────────────────
    sentiment_result = {}
    sentiment_adj = 0.0
    is_crypto = _is_crypto_symbol(symbol)
    if is_crypto or not symbol:
        try:
            from agents.ai_engine.crypto_sentiment import (
                get_fear_greed_index, sentiment_to_confidence_adjustment
            )
            fg = get_fear_greed_index()
            sentiment_adj = sentiment_to_confidence_adjustment(fg)
            sentiment_result = fg if isinstance(fg, dict) else {"value": fg}
        except Exception:
            pass

    # ── 6. Compute dynamic weights ─────────────────────────────────────────
    multipliers = _tracker.get_all_multipliers()

    raw_weights = {}
    raw_weights["statistical"] = _BASE_WEIGHTS["statistical"] * multipliers["statistical"]
    raw_weights["xgboost"]     = (_BASE_WEIGHTS["xgboost"]  * multipliers["xgboost"])  if xgb_fitted  else 0.0
    raw_weights["lightgbm"]    = (_BASE_WEIGHTS["lightgbm"] * multipliers["lightgbm"]) if lgbm_fitted else 0.0
    raw_weights["lstm"]        = (_BASE_WEIGHTS["lstm"]      * multipliers["lstm"])     if lstm_fitted else 0.0

    global _last_effective_weights
    weights = _guardrailed_normalize_weights(raw_weights, previous_weights=_last_effective_weights)
    _last_effective_weights = dict(weights)

    # ── 7. Signed confidence scores ────────────────────────────────────────
    def _signed(result: dict) -> float:
        """Convert direction+confidence to signed score: UP=+conf, DOWN=-conf."""
        d = result.get("direction", "NEUTRAL")
        c = float(result.get("confidence", 0.0))
        if d == "UP":
            return c
        elif d == "DOWN":
            return -c
        return 0.0

    signed_scores = {
        "statistical": _signed(stat_result),
        "xgboost":     _signed(xgb_result),
        "lightgbm":    _signed(lgbm_result),
        "lstm":        _signed(lstm_result),
    }

    # Weighted sum
    weighted_score = sum(signed_scores[k] * weights[k] for k in weights)

    # Apply sentiment adjustment
    weighted_score += sentiment_adj

    # ── 8. Consensus boosting ──────────────────────────────────────────────
    active_results = [stat_result]
    if xgb_fitted:
        active_results.append(xgb_result)
    if lgbm_fitted:
        active_results.append(lgbm_result)
    if lstm_fitted:
        active_results.append(lstm_result)

    n_active = len(active_results)
    directions = [r.get("direction", "NEUTRAL") for r in active_results]
    up_votes   = directions.count("UP")
    down_votes = directions.count("DOWN")
    agreement  = max(up_votes, down_votes)

    # Consensus multiplier
    if n_active >= 3 and agreement == n_active:
        consensus_mult = 1.20   # Full agreement — boost confidence
    elif n_active >= 2 and agreement >= n_active - 1:
        consensus_mult = 1.10   # Near-agreement
    elif agreement <= 1 and n_active >= 3:
        consensus_mult = 0.70   # Strong disagreement — reduce confidence
    else:
        consensus_mult = 1.00

    final_confidence = min(abs(weighted_score) * consensus_mult, 100.0)

    # ── 9. Final direction ─────────────────────────────────────────────────
    if weighted_score > 5:
        direction = "UP"
        prob_up   = 50 + final_confidence / 2
        prob_down = 50 - final_confidence / 2
    elif weighted_score < -5:
        direction = "DOWN"
        prob_down = 50 + final_confidence / 2
        prob_up   = 50 - final_confidence / 2
    else:
        direction = "NEUTRAL"
        prob_up = prob_down = 50.0
        final_confidence = 0.0

    model_votes = {
        "statistical": stat_result,
        "xgboost":     xgb_result  if xgb_fitted  else None,
        "lightgbm":    lgbm_result if lgbm_fitted  else None,
        "lstm":        lstm_result if lstm_fitted  else None,
    }

    log.debug(
        "[Ensemble] %s conf=%.1f (stat=%.1f xgb=%.1f lgbm=%.1f lstm=%.1f) "
        "agreement=%d/%d consensus=%.2f sentiment_adj=%.1f",
        direction, final_confidence,
        signed_scores["statistical"], signed_scores["xgboost"],
        signed_scores["lightgbm"],    signed_scores["lstm"],
        agreement, n_active, consensus_mult, sentiment_adj,
    )

    return {
        "direction":          direction,
        "confidence":         round(final_confidence, 1),
        "prob_up":            round(max(0, min(100, prob_up)), 1),
        "prob_down":          round(max(0, min(100, prob_down)), 1),
        "method":             "ensemble_advanced",
        "model_votes":        model_votes,
        "sentiment":          sentiment_result,
        "ensemble_agreement": agreement,
        "n_active_models":    n_active,
        "dynamic_weights":    {k: round(v, 3) for k, v in weights.items()},
        "signed_scores":      {k: round(v, 1) for k, v in signed_scores.items()},
        "sentiment_adj":      round(sentiment_adj, 1),
        "model_accuracy":     _tracker.get_accuracy_summary(),
    }


def predict_batch_ensemble(df: pd.DataFrame, symbol: str = "") -> pd.Series:
    """
    Generate ensemble confidence scores for all rows in df.

    Returns a Series of confidence scores (0-100):
      >50 = bullish bias
      <50 = bearish bias
      ~50 = neutral / no conviction

    Used as ai_confidence in signal_generator.
    Runs XGBoost + LightGBM batch (fast O(n)) + statistical batch.
    LSTM is skipped in batch mode (too slow for large datasets).
    """
    n = len(df)
    if n == 0:
        return pd.Series(dtype=float)

    # ── Statistical batch ──────────────────────────────────────────────────
    try:
        from agents.ai_engine.fallback_predictor import predict_batch
        stat_conf = predict_batch(df).values.astype(float)
    except Exception:
        stat_conf = np.full(n, 50.0)

    # ── XGBoost batch ──────────────────────────────────────────────────────
    xgb_conf   = np.full(n, 50.0)
    xgb_fitted = False
    try:
        from agents.ai_engine.xgboost_model import get_xgb_predictor
        xgb = get_xgb_predictor()
        if xgb.is_fitted:
            xgb_conf   = xgb.predict_batch(df).values.astype(float)
            xgb_fitted = True
    except Exception as exc:
        log.debug("[Ensemble batch] XGBoost failed: %s", exc)

    # ── LightGBM batch ─────────────────────────────────────────────────────
    lgbm_conf   = np.full(n, 50.0)
    lgbm_fitted = False
    try:
        from agents.ai_engine.lightgbm_model import get_lgbm_predictor
        lgbm = get_lgbm_predictor()
        if lgbm.is_fitted:
            lgbm_conf   = lgbm.predict_batch(df).values.astype(float)
            lgbm_fitted = True
    except Exception as exc:
        log.debug("[Ensemble batch] LightGBM failed: %s", exc)

    # ── Dynamic weights for batch ──────────────────────────────────────────
    multipliers = _tracker.get_all_multipliers()

    stat_w = _BASE_WEIGHTS["statistical"] * multipliers["statistical"]
    xgb_w  = (_BASE_WEIGHTS["xgboost"]  * multipliers["xgboost"])  if xgb_fitted  else 0.0
    lgbm_w = (_BASE_WEIGHTS["lightgbm"] * multipliers["lightgbm"]) if lgbm_fitted else 0.0

    global _last_effective_weights
    weight_map = _guardrailed_normalize_weights(
        {
            "statistical": stat_w,
            "xgboost": xgb_w,
            "lightgbm": lgbm_w,
            "lstm": 0.0,
        },
        previous_weights=_last_effective_weights,
    )
    _last_effective_weights = dict(weight_map)

    # Weighted blend
    blended = (
        stat_conf * weight_map["statistical"] +
        xgb_conf  * weight_map["xgboost"] +
        lgbm_conf * weight_map["lightgbm"]
    )

    # ── Sentiment flat-shift ───────────────────────────────────────────────
    is_crypto = _is_crypto_symbol(symbol)
    if is_crypto or not symbol:
        try:
            from agents.ai_engine.crypto_sentiment import (
                get_fear_greed_index, sentiment_to_confidence_adjustment
            )
            fg  = get_fear_greed_index()
            adj = sentiment_to_confidence_adjustment(fg)
            blended = blended + adj
        except Exception:
            pass

    blended = np.clip(blended, 5.0, 95.0)
    return pd.Series(blended.round(1), index=df.index, name="ensemble_confidence")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_crypto_symbol(symbol: str) -> bool:
    """Return True if symbol looks like a crypto trading pair."""
    upper = symbol.upper()
    return any(tok in upper for tok in _CRYPTO_SYMBOLS)


def _neutral(reason: str) -> dict:
    return {
        "direction":          "NEUTRAL",
        "confidence":         0.0,
        "prob_up":            50.0,
        "prob_down":          50.0,
        "method":             f"ensemble_advanced_{reason}",
        "model_votes":        {},
        "sentiment":          {},
        "ensemble_agreement": 0,
        "n_active_models":    0,
        "dynamic_weights":    {},
        "signed_scores":      {},
        "sentiment_adj":      0.0,
        "model_accuracy":     {},
    }


# ── Lazy import for os (used in LSTM path check) ───────────────────────────────
import os
