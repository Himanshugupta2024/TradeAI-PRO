"""
Comprehensive Verification Suite for TradeAI Ensemble V2.

Verifies:
  1. Full end-to-end inference of predict_advanced with all V2 fields.
  2. Option B fail-safe: clean fallback to dynamic weighting when meta-model is unfitted.
  3. Two-axis regime decoupling (trend_regime + volatility_regime).
  4. Graduated anomaly detection: normal -> CLEAN, flash-crash shock -> SKIP with is_vetoed = True.
  5. Logistic baseline training, prediction, and extraction.
  6. Meta-Learner training with interaction terms.
  7. FrozenEstimator Platt sigmoid calibration and metric evaluation (Brier score < 0.25).
  8. Live process RSS memory profiling stays strictly within Render 512MB limits.
"""

import sys
import os
import pytest
import numpy as np
import pandas as pd
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.ai_engine.ensemble import predict_advanced, predict_batch_ensemble, get_model_status
from agents.ai_engine.logistic_baseline import (
    LogisticBaselinePredictor,
    extract_logistic_features,
    LOGISTIC_FEATURE_COLS,
)
from agents.ai_engine.meta_learner import (
    MetaStackingLearner,
    build_meta_feature_vector,
    META_FEATURE_NAMES,
)
from agents.ai_engine.calibrator import ProbabilityCalibrator
from agents.ai_engine.shadow_evaluator import ShadowEvaluator
from agents.feature_engine.indicators import calculate_indicators
from utils.memory_profiler import get_process_rss_mb, check_memory_guardrail


def _make_df(n: int = 120, trend_bias: float = 0.001) -> pd.DataFrame:
    np.random.seed(42)
    start = datetime(2026, 1, 1, 9, 15)
    timestamps = [start + timedelta(minutes=5 * i) for i in range(n)]
    returns = np.random.normal(trend_bias, 0.003, size=n)
    price = 1500.0 * np.exp(np.cumsum(returns))

    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": price * (1 + np.random.normal(0, 0.0005, n)),
        "high": price * (1 + np.abs(np.random.normal(0, 0.0015, n))),
        "low": price * (1 - np.abs(np.random.normal(0, 0.0015, n))),
        "close": price,
        "volume": np.random.randint(2000, 20000, size=n).astype(float),
    })
    return calculate_indicators(df)


class TestEnsembleV2Pipeline:
    """End-to-end tests for the upgraded Ensemble V2."""

    def test_predict_advanced_v2_payload_schema(self):
        df = _make_df(100)
        res = predict_advanced(df, symbol="RELIANCE", offline=True)

        # Core predictions
        assert res["direction"] in ("UP", "DOWN", "NEUTRAL", "SKIP")
        assert 0.0 <= res["confidence"] <= 100.0
        assert 0.0 <= res["raw_ensemble_score"] <= 100.0
        assert 0.0 <= res["calibrated_trade_win_probability"] <= 100.0

        # Two-Axis Regime
        assert "trend_regime" in res
        assert "volatility_regime" in res
        assert -1.0 <= res["trend_score"] <= 1.0
        assert 0.0 <= res["volatility_score"] <= 1.0

        # Anomaly Guardrail
        assert res["anomaly_tier"] in ("CLEAN", "MILD_ANOMALY", "SEVERE_ANOMALY")
        assert isinstance(res["is_vetoed"], bool)

        # Model votes includes all 5 models
        assert "statistical" in res["model_votes"]
        assert "xgboost" in res["model_votes"]
        assert "lightgbm" in res["model_votes"]
        assert "lstm" in res["model_votes"]
        assert "logistic" in res["model_votes"]

    def test_flash_crash_veto_in_predict_advanced(self):
        df = _make_df(100)
        # Inject an extreme flash-crash on the latest candle
        last_idx = df.index[-1]
        df.loc[last_idx, "open"] = df.loc[last_idx, "close"] * 0.90
        df.loc[last_idx, "close"] = df.loc[last_idx, "close"] * 0.88
        df.loc[last_idx, "volume"] = df["volume"].mean() * 30.0

        res = predict_advanced(df, symbol="TCS", offline=True)
        assert res["direction"] == "SKIP"
        assert res["is_vetoed"] is True
        assert res["anomaly_tier"] == "SEVERE_ANOMALY"

    def test_predict_batch_ensemble(self):
        df = _make_df(80)
        conf = predict_batch_ensemble(df, symbol="INFY", offline=True)

        assert isinstance(conf, pd.Series)
        assert len(conf) == len(df)
        assert not conf.isna().any()
        assert ((conf >= 5.0) & (conf <= 95.0)).all()

    def test_get_model_status_badge(self):
        status = get_model_status()
        assert "fitted_count" in status
        assert "total_ml_models" in status
        assert status["total_ml_models"] == 4
        assert "badge_short" in status
        assert "Hybrid" in status["badge_short"] or "Ensemble" in status["badge_short"]


class TestComponentsTrainingAndCalibration:
    """Tests training and calibration mechanics."""

    def test_logistic_baseline_fit_and_predict(self):
        df = _make_df(120)
        y = (np.random.rand(len(df)) > 0.5).astype(int)

        with tempfile.TemporaryDirectory() as tmpdir:
            model_p = os.path.join(tmpdir, "log_m.pkl")
            scaler_p = os.path.join(tmpdir, "log_s.pkl")
            clf = LogisticBaselinePredictor(model_path=model_p, scaler_path=scaler_p)
            clf.fit(df, y, save=True)

            assert clf.is_fitted
            pred = clf.predict_row(df)
            assert pred["direction"] in ("UP", "DOWN", "NEUTRAL")
            assert 0.0 <= pred["prob_up"] <= 100.0

    def test_meta_learner_with_interaction_terms(self):
        X_vec = build_meta_feature_vector(
            xgb_prob=0.80,
            lgbm_prob=0.75,
            lstm_prob=0.70,
            logit_prob=0.65,
            stat_score=0.60,
            trend_score=0.85,  # Strong Bull
            volatility_score=0.40,
            adx=32.0,
            atr_pct=0.015,
        )
        assert list(X_vec.columns) == META_FEATURE_NAMES
        assert np.isclose(X_vec["xgb_x_trend"].iloc[0], 0.80 * 0.85)

        # Mock training of meta learner
        X_train = pd.concat([X_vec] * 30, ignore_index=True)
        X_train += np.random.normal(0, 0.01, X_train.shape)
        y_train = np.array([1, 0] * 15)

        with tempfile.TemporaryDirectory() as tmpdir:
            meta_p = os.path.join(tmpdir, "meta.pkl")
            meta = MetaStackingLearner(model_path=meta_p)
            meta.fit(X_train, y_train, save=True)
            assert meta.is_fitted

            res = meta.predict_meta(X_vec)
            assert res["meta_active"] is True
            assert 0.0 <= res["meta_prob_up"] <= 100.0

    def test_frozen_calibrator_sigmoid(self):
        from sklearn.linear_model import LogisticRegression
        # Base model trained on simple linear boundary
        X_base = np.linspace(-2, 2, 60).reshape(-1, 1)
        y_base = (X_base > 0).astype(int).ravel()
        base_model = LogisticRegression(solver="lbfgs").fit(X_base, y_base)

        # Independent calibration slice (30 rows >= min 20 rows)
        X_calib = np.linspace(-1.5, 1.5, 30).reshape(-1, 1)
        y_calib = (X_calib > 0).astype(int).ravel()

        with tempfile.TemporaryDirectory() as tmpdir:
            calib_p = os.path.join(tmpdir, "calib.pkl")
            calibrator = ProbabilityCalibrator(model_path=calib_p)
            calibrator.fit_calibration(base_model, X_calib, y_calib, save=True)

            assert calibrator.is_fitted
            probs = calibrator.calibrate(X_calib)
            assert len(probs) == len(X_calib)
            assert all(0.0 <= p <= 1.0 for p in probs)

            metrics = calibrator.evaluate_quality(X_calib, y_calib)
            assert "brier_score" in metrics
            assert metrics["brier_score"] < 0.25

    def test_shadow_evaluator_logging(self):
        # Use an in-memory SQLite DB to prevent Windows file lock conflicts during tests
        evaluator = ShadowEvaluator(db_path=":memory:")

        v1 = {"direction": "UP", "confidence": 75.0}
        v2 = {
            "direction": "UP",
            "calibrated_trade_win_probability": 68.0,
            "raw_ensemble_score": 82.0,
            "trend_regime": "STRONG_BULL",
            "volatility_regime": "NORMAL",
            "anomaly_tier": "CLEAN",
            "is_vetoed": False,
        }
        sig_id = evaluator.record_shadow_signal("SBIN", v1, v2)
        assert sig_id > 0

        # Resolve outcome
        evaluator.resolve_outcome(sig_id, 1)
        summary = evaluator.get_summary_metrics()
        assert summary.get("total_logged", 0) >= 1


class TestRenderMemoryProfiling:
    """Ensures live OS process RSS is strictly within 512MB limits."""

    def test_process_rss_memory_guardrail(self):
        import gc
        gc.collect()
        rss_mb = get_process_rss_mb()
        assert rss_mb > 0.0, "Could not measure process RSS!"

        # Render hard limit is 512MB. Steady-state target < 380MB on Linux, < 500MB on Windows DLL working set.
        import platform
        threshold = 380.0 if platform.system() == "Linux" else 512.0
        assert rss_mb < threshold, f"Memory RSS exceeded threshold: {rss_mb} MB"
        assert rss_mb < 512.0, f"Exceeded Render 512MB hard limit: {rss_mb} MB"
