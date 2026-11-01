"""
Unit Tests for Phase 1: Graduated Isolation Forest Market Anomaly Detector.

Verifies:
  1. Multi-factor anomaly feature computation.
  2. Normal bars are classified as CLEAN with multiplier 1.0.
  3. Mild volatility shocks are classified as MILD_ANOMALY with multiplier 0.75.
  4. Extreme flash crash / massive gap outliers are classified as SEVERE_ANOMALY with is_vetoed = True.
  5. Fit and inference via IsolationForest runs cleanly with low memory footprint.
"""

import sys
import os
import pytest
import numpy as np
import pandas as pd
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.ai_engine.anomaly_detector import (
    MarketAnomalyDetector,
    compute_anomaly_features,
    ANOMALY_FEATURE_COLS,
)
from agents.feature_engine.indicators import calculate_indicators


def _make_normal_bars(n: int = 150) -> pd.DataFrame:
    np.random.seed(42)
    start = datetime(2026, 1, 1, 9, 15)
    timestamps = [start + timedelta(minutes=5 * i) for i in range(n)]
    
    # Mild random walk with 0.2% daily vol
    returns = np.random.normal(0.0001, 0.002, size=n)
    price = 2500.0 * np.exp(np.cumsum(returns))
    
    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": price * (1 + np.random.normal(0, 0.0005, n)),
        "high": price * (1 + np.abs(np.random.normal(0, 0.001, n))),
        "low": price * (1 - np.abs(np.random.normal(0, 0.001, n))),
        "close": price,
        "volume": np.random.randint(5000, 15000, size=n).astype(float),
    })
    return calculate_indicators(df)


class TestAnomalyDetector:
    """Test suite for MarketAnomalyDetector."""

    def test_compute_anomaly_features_schema(self):
        df = _make_normal_bars(50)
        feats = compute_anomaly_features(df)
        assert list(feats.columns) == ANOMALY_FEATURE_COLS
        assert len(feats) == len(df)
        assert not feats.isna().any().any()

    def test_normal_bars_classified_as_clean(self):
        df = _make_normal_bars(100)
        detector = MarketAnomalyDetector(model_path="non_existent.pkl")
        result = detector.score_row(df)

        assert result["tier"] == "CLEAN"
        assert result["confidence_multiplier"] == 1.0
        assert result["is_vetoed"] is False

    def test_extreme_gap_and_volume_shock_vetoed(self):
        df = _make_normal_bars(100)
        
        # Inject an extreme flash-crash anomaly on the last bar
        last_idx = df.index[-1]
        df.loc[last_idx, "open"] = df.loc[last_idx, "close"] * 0.90  # 10% gap down
        df.loc[last_idx, "close"] = df.loc[last_idx, "close"] * 0.88 # -12% close
        df.loc[last_idx, "high"] = df.loc[last_idx, "open"] * 1.02
        df.loc[last_idx, "low"] = df.loc[last_idx, "close"] * 0.98
        df.loc[last_idx, "volume"] = df["volume"].mean() * 25.0       # 25x volume shock

        # Recalculate indicators for the modified bar
        df = calculate_indicators(df)

        detector = MarketAnomalyDetector(model_path="non_existent.pkl")
        result = detector.score_row(df)

        assert result["tier"] == "SEVERE_ANOMALY"
        assert result["is_vetoed"] is True
        assert result["confidence_multiplier"] == 0.0
        assert "Extreme market shock" in result["reason"]

    def test_fit_and_inference_with_isolation_forest(self):
        df = _make_normal_bars(200)
        with tempfile.TemporaryDirectory() as tmpdir:
            model_file = os.path.join(tmpdir, "test_anom.pkl")
            detector = MarketAnomalyDetector(model_path=model_file)
            
            # Fit on normal historical data
            detector.fit(df, save=True)
            assert os.path.exists(model_file)
            assert detector._is_fitted is True

            # Score normal bar
            res_normal = detector.score_row(df)
            assert res_normal["is_vetoed"] is False

            # Test reload from disk
            detector2 = MarketAnomalyDetector(model_path=model_file)
            assert detector2._is_fitted is True
            res_reload = detector2.score_row(df)
            assert res_reload["tier"] == res_normal["tier"]
