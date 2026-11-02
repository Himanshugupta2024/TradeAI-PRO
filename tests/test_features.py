"""
Tests for Support/Resistance detector and HTF Trend classifier.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from agents.feature_engine.support_resistance import calculate_support_resistance
from agents.feature_engine.trend import classify_trend, propagate_trend_to_ltf
from db.models import TrendDirection


def _make_ohlcv(rows: int = 300, base: float = 100.0) -> pd.DataFrame:
    """Synthetic uptrend OHLCV data."""
    np.random.seed(42)
    timestamps = pd.date_range("2023-01-01", periods=rows, freq="h")
    close = base + np.linspace(0, 30, rows) + np.random.randn(rows) * 2
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": close + np.random.randn(rows) * 0.3,
        "high": close + np.abs(np.random.randn(rows)),
        "low": close - np.abs(np.random.randn(rows)),
        "close": close,
        "volume": np.random.randint(1000, 50000, size=rows).astype(float),
    })


class TestSupportResistance:
    def test_columns_added(self):
        df = _make_ohlcv(300)
        result = calculate_support_resistance(df)
        assert "support" in result.columns
        assert "resistance" in result.columns

    def test_support_below_close(self):
        df = _make_ohlcv(300)
        result = calculate_support_resistance(df)
        valid = result.dropna(subset=["support"])
        assert (valid["support"] <= valid["close"]).all()

    def test_resistance_above_close(self):
        df = _make_ohlcv(300)
        result = calculate_support_resistance(df)
        valid = result.dropna(subset=["resistance"])
        assert (valid["resistance"] >= valid["close"]).all()


class TestTrendClassifier:
    def test_uptrend_detected(self):
        """Synthetic uptrend should classify as BULLISH at the end."""
        df = _make_ohlcv(300)
        result = classify_trend(df)
        assert "htf_trend" in result.columns
        # Last rows should be bullish given 30-point uptrend
        assert result["htf_trend"].iloc[-1] == TrendDirection.BULLISH

    def test_propagation(self):
        """HTF trend should propagate to LTF rows."""
        htf = _make_ohlcv(100)
        htf = classify_trend(htf)
        ltf = _make_ohlcv(500)
        ltf["timestamp"] = pd.date_range("2023-01-01", periods=500, freq="15min")
        result = propagate_trend_to_ltf(ltf, htf)
        assert "htf_trend" in result.columns
        assert result["htf_trend"].notna().all()

    def test_propagation_heterogeneous_datetime_resolutions(self):
        """LTF microsecond [us] and HTF second [s] precision must merge without MergeError."""
        htf = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=10, freq="1D").astype("datetime64[s]"),
            "htf_trend": [TrendDirection.BULLISH.value] * 10,
        })
        ltf = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01 10:00:00", periods=50, freq="5min").astype("datetime64[us]"),
            "close": [100.0] * 50,
        })
        result = propagate_trend_to_ltf(ltf, htf)
        assert "htf_trend" in result.columns
        assert result["htf_trend"].iloc[0] == TrendDirection.BULLISH.value
        assert len(result) == 50
