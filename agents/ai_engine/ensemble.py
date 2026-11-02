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
    "statistical": 0.20,
    "xgboost":     0.25,
    "lightgbm":    0.25,
    "lstm":        0.15,
    "logistic":    0.15,
}

_CRYPTO_SYMBOLS = {"BTC", "ETH", "SOL", "BNB", "XRP", "USDT", "ADA", "DOT", "AVAX", "MATIC"}

# Guardrails for dynamic ensemble weighting so one model cannot dominate instantly.
_MIN_ACTIVE_WEIGHT = 0.10
_MAX_ACTIVE_WEIGHT = 0.60
_MAX_WEIGHT_STEP = 0.20

import threading
_ENSEMBLE_PRED_CACHE: dict[str, dict] = {}
_ENSEMBLE_CACHE_LOCK = threading.Lock()
_ENSEMBLE_CACHE_MAX_ENTRIES = 200

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
            "logistic":    collections.deque(maxlen=window),
        }
        self._restore_history_from_db()

    def _restore_history_from_db(self):
        try:
            import sqlite3
            conn = sqlite3.connect("trading.db")
            c = conn.cursor()
            rows = c.execute("SELECT result FROM trade_outcomes ORDER BY id DESC LIMIT 50").fetchall()
            if rows:
                for r in reversed(rows):
                    is_win = 1 if r[0] == "WIN" else 0
                    for m in self._history:
                        self._history[m].append(is_win)
        except Exception as e:
            log.debug("Could not restore model performance history: %s", e)

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
    try:
        from db.connection import get_sqlite_path
        return Path(get_sqlite_path())
    except Exception:
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

def predict_advanced(df: pd.DataFrame, symbol: str = "", offline: bool = False, force_refresh: bool = False) -> dict:
    """
    Run the full 4-model ensemble on the latest bar of df.

    Args:
        df:     OHLCV + indicators DataFrame (≥60 rows recommended)
        symbol: e.g. "BTC/USDT"
        offline: if True, skips live external sentiment calls
        force_refresh: if True, bypasses in-memory prediction cache
    """
    if df.empty or len(df) < 2:
        return _neutral("empty_df")

    # Fast candle-keyed cache lookup to prevent redundant multi-model inference on identical bar
    cache_key = None
    if symbol and not force_refresh and not df.empty and "close" in df.columns:
        try:
            last_ts = str(df["timestamp"].iloc[-1]) if "timestamp" in df.columns else str(df.index[-1])
            c_val = round(float(df["close"].iloc[-1]), 4)
            cache_key = f"{symbol}:{last_ts}:{c_val}:ensemble-v2"
            with _ENSEMBLE_CACHE_LOCK:
                if cache_key in _ENSEMBLE_PRED_CACHE:
                    return dict(_ENSEMBLE_PRED_CACHE[cache_key])
        except Exception:
            cache_key = None

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
    lstm_result = {"direction": "NEUTRAL", "confidence": 0.0, "prob_up": 50.0}
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
                    from agents.ai_engine.lstm_model import get_lstm_model
                    model = get_lstm_model(input_size=len(available_cols), model_path=model_path)
                    if model is not None:
                        lstm_result = model.predict(x_tensor)
                        lstm_fitted = True
    except Exception as exc:
        log.debug("[Ensemble] LSTM failed: %s", exc)

    # ── 4b. Logistic Regression Baseline (Linear Anchor) ───────────────────
    logit_result = {"direction": "NEUTRAL", "confidence": 0.0, "prob_up": 50.0}
    logit_fitted = False
    try:
        from agents.ai_engine.logistic_baseline import get_logistic_predictor
        logit = get_logistic_predictor()
        logit_result = logit.predict_row(df)
        logit_fitted = logit.is_fitted
    except Exception as exc:
        log.debug("[Ensemble] Logistic baseline failed: %s", exc)

    # ── 5. Two-Axis Market Regime Classification ───────────────────────────
    try:
        from agents.feature_engine.regime_classifier import detect_two_axis_regime
        regime_res = detect_two_axis_regime(df)
    except Exception as exc:
        log.debug("[Ensemble] Two-Axis Regime failed: %s", exc)
        from agents.feature_engine.regime_classifier import (
            TwoAxisRegimeResult, TrendRegime, VolatilityRegime
        )
        from agents.feature_engine.regime_detector import MarketRegime
        regime_res = TwoAxisRegimeResult(
            trend_regime=TrendRegime.RANGING,
            trend_score=0.0,
            volatility_regime=VolatilityRegime.NORMAL,
            volatility_score=0.5,
            legacy_regime=MarketRegime.RANGING,
        )

    # ── 5b. Graduated Market Anomaly Detection ─────────────────────────────
    try:
        from agents.ai_engine.anomaly_detector import get_anomaly_detector
        anom_res = get_anomaly_detector().score_row(df)
    except Exception as exc:
        log.debug("[Ensemble] Anomaly detector failed: %s", exc)
        anom_res = {
            "tier": "CLEAN",
            "anomaly_score": 0.10,
            "confidence_multiplier": 1.0,
            "is_vetoed": False,
            "reason": "fallback_clean",
            "anomaly_features": {},
        }

    # ── 5c. Sentiment adjustment ───────────────────────────────────────────
    sentiment_result = {}
    sentiment_adj = 0.0
    is_crypto = _is_crypto_symbol(symbol)
    if is_crypto and symbol and not offline:
        try:
            from agents.ai_engine.crypto_sentiment import (
                get_fear_greed_index, sentiment_to_confidence_adjustment
            )
            fg = get_fear_greed_index()
            sentiment_adj = sentiment_to_confidence_adjustment(fg)
            sentiment_result = fg if isinstance(fg, dict) else {"value": fg}
        except Exception:
            pass

    # ── 6. Compute dynamic weights (Fallback Baseline) ─────────────────────
    multipliers = _tracker.get_all_multipliers()

    raw_weights = {}
    raw_weights["statistical"] = _BASE_WEIGHTS["statistical"] * multipliers.get("statistical", 1.0)
    raw_weights["xgboost"]     = (_BASE_WEIGHTS["xgboost"]  * multipliers.get("xgboost", 1.0))  if xgb_fitted  else 0.0
    raw_weights["lightgbm"]    = (_BASE_WEIGHTS["lightgbm"] * multipliers.get("lightgbm", 1.0)) if lgbm_fitted else 0.0
    raw_weights["lstm"]        = (_BASE_WEIGHTS["lstm"]      * multipliers.get("lstm", 1.0))     if lstm_fitted else 0.0
    raw_weights["logistic"]    = (_BASE_WEIGHTS["logistic"]  * multipliers.get("logistic", 1.0)) if logit_fitted else 0.0

    global _last_effective_weights
    weights = _guardrailed_normalize_weights(raw_weights, previous_weights=_last_effective_weights)
    _last_effective_weights = dict(weights)

    # ── 7. Signed confidence scores ────────────────────────────────────────
    def _signed(result: dict) -> float:
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
        "logistic":    _signed(logit_result),
    }

    # Fallback weighted score
    fallback_weighted_score = sum(signed_scores[k] * weights.get(k, 0.0) for k in weights)
    fallback_weighted_score += sentiment_adj

    # ── 8. Stacking Meta-Learner with Interaction Features ──────────────────
    meta_active = False
    calib_active = False
    meta_prob_up = 50.0

    # Option B Fail-Safe: Check if critical models are active
    if xgb_fitted and lgbm_fitted and lstm_fitted and logit_fitted:
        try:
            from agents.ai_engine.meta_learner import build_meta_feature_vector, get_meta_learner
            meta = get_meta_learner()
            if meta.is_fitted:
                stat_prob_up = float(stat_result.get("prob_up", 50.0))
                xgb_p = float(xgb_result.get("prob_up", 50.0)) / 100.0
                lgb_p = float(lgbm_result.get("prob_up", 50.0)) / 100.0
                lst_p = float(lstm_result.get("prob_up", 50.0)) / 100.0
                log_p = float(logit_result.get("prob_up", 50.0)) / 100.0
                sta_p = stat_prob_up / 100.0

                adx_val = float(df.iloc[-1].get(f"ADX_{settings.ADX_PERIOD}", 20.0)) if f"ADX_{settings.ADX_PERIOD}" in df.columns else 20.0
                atr_val = float(df.iloc[-1].get(f"ATR_{settings.ATR_PERIOD}", 1.0)) if f"ATR_{settings.ATR_PERIOD}" in df.columns else 1.0
                close_val = max(float(df.iloc[-1]["close"]), 1.0)
                atr_pct = atr_val / close_val

                X_meta = build_meta_feature_vector(
                    xgb_prob=xgb_p,
                    lgbm_prob=lgb_p,
                    lstm_prob=lst_p,
                    logit_prob=log_p,
                    stat_score=sta_p,
                    trend_score=regime_res.trend_score,
                    volatility_score=regime_res.volatility_score,
                    adx=adx_val,
                    atr_pct=atr_pct,
                )
                pred_meta = meta.predict_meta(X_meta)
                if pred_meta.get("meta_active", False):
                    meta_prob_up = pred_meta["meta_prob_up"]
                    meta_active = True

                    # Independent Sigmoid Calibration (Phase 4)
                    try:
                        from agents.ai_engine.calibrator import get_calibrator
                        calib = get_calibrator()
                        if calib.is_fitted:
                            calibrated_trade_win_prob = round(float(calib.calibrate(X_meta)[0]) * 100.0, 1)
                            calib_active = True
                    except Exception:
                        pass
        except Exception as _meta_exc:
            log.debug("[Ensemble] Meta-learner error, falling back to dynamic weights: %s", _meta_exc)

    # If meta-learner was active, derive raw_weighted_score from meta_prob_up
    if meta_active:
        raw_weighted_score = (meta_prob_up - 50.0) * 2.0
    else:
        raw_weighted_score = fallback_weighted_score

    # ── 9. Consensus boosting ──────────────────────────────────────────────
    active_results = [stat_result]
    if xgb_fitted:
        active_results.append(xgb_result)
    if lgbm_fitted:
        active_results.append(lgbm_result)
    if lstm_fitted:
        active_results.append(lstm_result)
    if logit_fitted:
        active_results.append(logit_result)

    n_active = len(active_results)
    directions = [r.get("direction", "NEUTRAL") for r in active_results]
    up_votes   = directions.count("UP")
    down_votes = directions.count("DOWN")
    agreement  = max(up_votes, down_votes)

    if n_active >= 4 and agreement == n_active:
        consensus_mult = 1.20
    elif n_active >= 3 and agreement >= n_active - 1:
        consensus_mult = 1.10
    elif agreement <= 1 and n_active >= 3:
        consensus_mult = 0.70
    else:
        consensus_mult = 1.00

    raw_ensemble_score = min(abs(raw_weighted_score) * consensus_mult, 100.0)

    # ── 10. Direction and Calibrated Trade Win Probability ──────────────────
    if raw_weighted_score > 5:
        direction = "UP"
        prob_up   = 50 + raw_ensemble_score / 2
        prob_down = 50 - raw_ensemble_score / 2
    elif raw_weighted_score < -5:
        direction = "DOWN"
        prob_down = 50 + raw_ensemble_score / 2
        prob_up   = 50 - raw_ensemble_score / 2
    else:
        direction = "NEUTRAL"
        prob_up = prob_down = 50.0
        raw_ensemble_score = 0.0

    # If calibrator wasn't fitted, apply realistic Platt Sigmoid compression
    if not calib_active:
        calibrated_trade_win_prob = round(50.0 + (raw_ensemble_score / 2.0) * 0.72, 1)

    # ── 11. Anomaly Tier Application (Phase 1) ─────────────────────────────
    is_vetoed = anom_res.get("is_vetoed", False)
    anomaly_tier = anom_res.get("tier", "CLEAN")
    confidence_multiplier = anom_res.get("confidence_multiplier", 1.0)

    if is_vetoed:
        direction = "SKIP"
        final_confidence = 0.0
        calibrated_trade_win_prob = 50.0
    else:
        final_confidence = min(raw_ensemble_score * confidence_multiplier, 100.0)

    model_votes = {
        "statistical": stat_result,
        "xgboost":     xgb_result  if xgb_fitted  else None,
        "lightgbm":    lgbm_result if lgbm_fitted  else None,
        "lstm":        lstm_result if lstm_fitted  else None,
        "logistic":    logit_result if logit_fitted else None,
    }

    result = {
        "direction":                        direction,
        "confidence":                       round(final_confidence, 1),
        "raw_ensemble_score":               round(raw_ensemble_score, 1),
        "calibrated_trade_win_probability": round(calibrated_trade_win_prob, 1),
        "prob_up":                          round(max(0, min(100, prob_up)), 1),
        "prob_down":                        round(max(0, min(100, prob_down)), 1),
        "method":                           "ensemble_v2" if meta_active else "ensemble_advanced_fallback",
        "trend_regime":                     regime_res.trend_regime.value,
        "trend_score":                      round(regime_res.trend_score, 3),
        "volatility_regime":                regime_res.volatility_regime.value,
        "volatility_score":                 round(regime_res.volatility_score, 3),
        "legacy_regime":                    regime_res.legacy_regime.value,
        "anomaly_tier":                     anomaly_tier,
        "is_vetoed":                        is_vetoed,
        "meta_active":                      meta_active,
        "calibration_active":               calib_active,
        "model_votes":                      model_votes,
        "sentiment":                        sentiment_result,
        "ensemble_agreement":               agreement,
        "n_active_models":                  n_active,
        "dynamic_weights":                  {k: round(v, 3) for k, v in weights.items()},
        "signed_scores":                    {k: round(v, 1) for k, v in signed_scores.items()},
        "sentiment_adj":                    round(sentiment_adj, 1),
        "model_accuracy":                   _tracker.get_accuracy_summary(),
    }

    # ── 12. Shadow Mode Logging (Phase 5) ──────────────────────────────────
    if symbol and not offline:
        try:
            from agents.ai_engine.shadow_evaluator import get_shadow_evaluator
            v1_mock = {"direction": "UP" if raw_weighted_score > 5 else ("DOWN" if raw_weighted_score < -5 else "NEUTRAL"), "confidence": raw_ensemble_score}
            get_shadow_evaluator().record_shadow_signal(symbol=symbol, v1_result=v1_mock, v2_result=result)
        except Exception:
            pass

    if cache_key is not None:
        with _ENSEMBLE_CACHE_LOCK:
            if len(_ENSEMBLE_PRED_CACHE) >= _ENSEMBLE_CACHE_MAX_ENTRIES:
                _ENSEMBLE_PRED_CACHE.pop(next(iter(_ENSEMBLE_PRED_CACHE)))
            _ENSEMBLE_PRED_CACHE[cache_key] = dict(result)

    return result


def predict_batch_ensemble(df: pd.DataFrame, symbol: str = "", offline: bool = False) -> pd.Series:
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

    # ── Logistic Baseline batch ───────────────────────────────────────────
    logit_conf   = np.full(n, 50.0)
    logit_fitted = False
    try:
        from agents.ai_engine.logistic_baseline import get_logistic_predictor
        logit = get_logistic_predictor()
        logit_conf = logit.predict_batch(df).values.astype(float)
        logit_fitted = logit.is_fitted
    except Exception as exc:
        log.debug("[Ensemble batch] Logistic baseline failed: %s", exc)

    # ── Dynamic weights for batch ──────────────────────────────────────────
    multipliers = _tracker.get_all_multipliers()

    stat_w  = _BASE_WEIGHTS["statistical"] * multipliers.get("statistical", 1.0)
    xgb_w   = (_BASE_WEIGHTS["xgboost"]  * multipliers.get("xgboost", 1.0))  if xgb_fitted  else 0.0
    lgbm_w  = (_BASE_WEIGHTS["lightgbm"] * multipliers.get("lightgbm", 1.0)) if lgbm_fitted else 0.0
    logit_w = (_BASE_WEIGHTS["logistic"] * multipliers.get("logistic", 1.0)) if logit_fitted else 0.0

    global _last_effective_weights
    weight_map = _guardrailed_normalize_weights(
        {
            "statistical": stat_w,
            "xgboost": xgb_w,
            "lightgbm": lgbm_w,
            "lstm": 0.0,
            "logistic": logit_w,
        },
        previous_weights=_last_effective_weights,
    )
    _last_effective_weights = dict(weight_map)

    # Weighted blend
    blended = (
        stat_conf * weight_map.get("statistical", 0.0) +
        xgb_conf  * weight_map.get("xgboost", 0.0) +
        lgbm_conf * weight_map.get("lightgbm", 0.0) +
        logit_conf * weight_map.get("logistic", 0.0)
    )

    # ── Sentiment flat-shift ───────────────────────────────────────────────
    is_crypto = _is_crypto_symbol(symbol)
    if is_crypto and symbol and not offline:
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


def get_model_status() -> dict:
    """Return status of all models in the ensemble."""
    xgb_fitted = False
    try:
        from agents.ai_engine.xgboost_model import get_xgb_predictor
        xgb = get_xgb_predictor()
        xgb_fitted = bool(getattr(xgb, "is_fitted", False))
    except Exception:
        pass

    lgbm_fitted = False
    try:
        from agents.ai_engine.lightgbm_model import get_lgbm_predictor
        lgbm = get_lgbm_predictor()
        lgbm_fitted = bool(getattr(lgbm, "is_fitted", False))
    except Exception:
        pass

    lstm_fitted = os.path.exists("models/best_model.pt")

    logit_fitted = False
    try:
        from agents.ai_engine.logistic_baseline import get_logistic_predictor
        logit = get_logistic_predictor()
        logit_fitted = bool(getattr(logit, "is_fitted", False))
    except Exception:
        pass

    meta_fitted = False
    try:
        from agents.ai_engine.meta_learner import get_meta_learner
        meta = get_meta_learner()
        meta_fitted = bool(getattr(meta, "is_fitted", False))
    except Exception:
        pass

    calib_fitted = False
    try:
        from agents.ai_engine.calibrator import get_calibrator
        calib = get_calibrator()
        calib_fitted = bool(getattr(calib, "is_fitted", False))
    except Exception:
        pass

    fitted_count = sum([xgb_fitted, lgbm_fitted, lstm_fitted, logit_fitted])
    total_ml = 4

    if fitted_count == total_ml and calib_fitted:
        mode = "Ensemble V2 (4/4 ML + Calibrated)"
        badge_short = "🧠 Ensemble 4/4 • Calibrated"
    elif fitted_count == total_ml:
        mode = "Ensemble V2 (4/4 ML Fitted)"
        badge_short = "🧠 Ensemble (4/4 ML)"
    elif fitted_count > 0:
        mode = f"Hybrid Ensemble ({fitted_count}/{total_ml} ML fitted)"
        badge_short = f"🧠 AI: Hybrid ({fitted_count}/{total_ml} ML)"
    else:
        mode = "Statistical Heuristic (0/4 models fitted)"
        badge_short = "🧠 AI: Heuristic (0/4 ML)"

    return {
        "mode": mode,
        "fitted_count": fitted_count,
        "total_ml_models": total_ml,
        "statistical_heuristic": True,
        "xgboost_fitted": xgb_fitted,
        "lightgbm_fitted": lgbm_fitted,
        "lstm_fitted": lstm_fitted,
        "logistic_fitted": logit_fitted,
        "meta_learner_fitted": meta_fitted,
        "calibrator_fitted": calib_fitted,
        "badge_text": f"AI Engine: {mode}",
        "badge_short": badge_short,
    }
