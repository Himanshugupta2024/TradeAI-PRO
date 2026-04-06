"""
Tests for Market Regime Detector.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from agents.feature_engine.regime_detector import detect_regime, MarketRegime
from agents.feature_engine.indicators import calculate_indicators


def _make_trending_data(rows=200, direction="up"):
    """Create strongly trending data."""
    np.random.seed(42)
    ts = pd.date_range("2023-01-01", periods=rows, freq="h")
    drift = 0.5 if direction == "up" else -0.5
    close = 100 + np.cumsum(np.random.randn(rows) * 0.3 + drift)
    df = pd.DataFrame({
        "timestamp": ts,
        "open": close + np.random.randn(rows) * 0.1,
        "high": close + np.abs(np.random.randn(rows)) * 0.5,
        "low": close - np.abs(np.random.randn(rows)) * 0.5,
        "close": close,
        "volume": np.random.randint(1000, 50000, size=rows).astype(float),
    })
    return calculate_indicators(df)


def _make_ranging_data(rows=200):
    """Create sideways / ranging data with very low directionality."""
    np.random.seed(123)
    ts = pd.date_range("2023-01-01", periods=rows, freq="h")
    # Purely random noise around 100 — no trend at all
    close = 100 + np.random.randn(rows) * 0.3
    df = pd.DataFrame({
        "timestamp": ts,
        "open": close + np.random.randn(rows) * 0.1,
        "high": close + np.abs(np.random.randn(rows)) * 0.3,
        "low": close - np.abs(np.random.randn(rows)) * 0.3,
        "close": close,
        "volume": np.random.randint(1000, 50000, size=rows).astype(float),
    })
    return calculate_indicators(df)


def _make_volatile_data(rows=200):
    """Create high-volatility data."""
    np.random.seed(456)
    ts = pd.date_range("2023-01-01", periods=rows, freq="h")
    close = 100 + np.cumsum(np.random.randn(rows) * 3.0)  # Very high volatility
    df = pd.DataFrame({
        "timestamp": ts,
        "open": close + np.random.randn(rows) * 2.0,
        "high": close + np.abs(np.random.randn(rows)) * 5.0,
        "low": close - np.abs(np.random.randn(rows)) * 5.0,
        "close": close,
        "volume": np.random.randint(1000, 50000, size=rows).astype(float),
    })
    return calculate_indicators(df)


class TestRegimeDetector:
    def test_returns_valid_enum(self):
        df = _make_trending_data()
        regime = detect_regime(df)
        assert isinstance(regime, MarketRegime)

    def test_trending_data(self):
        df = _make_trending_data(direction="up")
        regime = detect_regime(df)
        # Strong uptrend should be detected as TRENDING (ADX should be high)
        assert regime in (MarketRegime.TRENDING, MarketRegime.HIGH_VOLATILITY)

    def test_ranging_data(self):
        df = _make_ranging_data()
        regime = detect_regime(df)
        assert regime == MarketRegime.RANGING

    def test_empty_df(self):
        df = pd.DataFrame()
        regime = detect_regime(df)
        assert regime == MarketRegime.RANGING  # Default

    def test_short_data(self):
        df = _make_trending_data(rows=10)
        regime = detect_regime(df)
        assert regime == MarketRegime.RANGING  # Not enough data → default
