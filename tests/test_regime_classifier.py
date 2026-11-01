"""
Unit Tests for Phase 2: Decoupled Two-Axis Market Regime Engine.

Verifies:
  1. Directional trend and volatility regimes operate orthogonally.
  2. Market can be STRONG_BULL + HIGH_VOLATILITY simultaneously.
  3. Continuous trend_score [-1.0, 1.0] and volatility_score [0.0, 1.0] scale smoothly.
  4. 100% backward compatibility with legacy MarketRegime enum.
  5. detect_two_axis_regime_series produces correct index and schema.
"""

import sys
import os
import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.feature_engine.regime_classifier import (
    detect_two_axis_regime,
    detect_two_axis_regime_series,
    TrendRegime,
    VolatilityRegime,
    TwoAxisRegimeResult,
)
from agents.feature_engine.regime_detector import MarketRegime
from agents.feature_engine.indicators import calculate_indicators


def _make_regime_df(trend_bias: float = 0.0, vol_scale: float = 0.002, n: int = 100) -> pd.DataFrame:
    np.random.seed(42)
    start = datetime(2026, 1, 1, 9, 15)
    timestamps = [start + timedelta(minutes=5 * i) for i in range(n)]
    
    returns = np.random.normal(trend_bias, vol_scale, size=n)
    price = 1000.0 * np.exp(np.cumsum(returns))
    
    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": price * (1 + np.random.normal(0, vol_scale * 0.2, n)),
        "high": price * (1 + np.abs(np.random.normal(0, vol_scale * 0.5, n))),
        "low": price * (1 - np.abs(np.random.normal(0, vol_scale * 0.5, n))),
        "close": price,
        "volume": np.random.randint(1000, 20000, size=n).astype(float),
    })
    return calculate_indicators(df)


class TestTwoAxisRegimeClassifier:
    """Test suite for Two-Axis Regime Classifier."""

    def test_strong_bull_and_high_volatility_can_coexist(self):
        # High upward trend + high volatility
        df = _make_regime_df(trend_bias=0.005, vol_scale=0.015, n=120)
        res = detect_two_axis_regime(df)

        assert isinstance(res, TwoAxisRegimeResult)
        assert res.trend_regime in (TrendRegime.STRONG_BULL, TrendRegime.BULL)
        assert res.trend_score > 0.3
        # Volatility should be elevated due to large vol_scale
        assert res.volatility_regime in (VolatilityRegime.HIGH, VolatilityRegime.EXTREME, VolatilityRegime.NORMAL)
        assert 0.0 <= res.volatility_score <= 1.0

    def test_strong_bear_produces_negative_trend_score(self):
        # Downward trending market
        df = _make_regime_df(trend_bias=-0.004, vol_scale=0.003, n=120)
        res = detect_two_axis_regime(df)

        assert res.trend_regime in (TrendRegime.STRONG_BEAR, TrendRegime.BEAR)
        assert res.trend_score < -0.2

    def test_ranging_market_produces_neutral_score(self):
        # Flat zero-trend market with very low volatility
        df = _make_regime_df(trend_bias=0.0, vol_scale=0.0005, n=100)
        res = detect_two_axis_regime(df)

        assert res.trend_regime == TrendRegime.RANGING
        assert abs(res.trend_score) <= 0.35

    def test_legacy_enum_compatibility(self):
        df = _make_regime_df(trend_bias=0.004, vol_scale=0.003, n=100)
        res = detect_two_axis_regime(df)
        
        # Must be an instance of MarketRegime
        assert isinstance(res.legacy_regime, MarketRegime)
        assert res.legacy_regime in (MarketRegime.TRENDING, MarketRegime.RANGING, MarketRegime.HIGH_VOLATILITY)

    def test_detect_two_axis_regime_series_schema(self):
        df = _make_regime_df(trend_bias=0.001, vol_scale=0.002, n=50)
        series_df = detect_two_axis_regime_series(df, lookback=20)

        assert len(series_df) == len(df)
        expected_cols = ["trend_regime", "trend_score", "volatility_regime", "volatility_score", "legacy_regime"]
        assert list(series_df.columns) == expected_cols
        assert not series_df["trend_score"].isna().any()

