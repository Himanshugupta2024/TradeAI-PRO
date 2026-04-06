"""
Tests for Multi-Timeframe Scorer.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from agents.feature_engine.mtf_scorer import (
    _score_timeframe,
    calculate_mtf_score,
    mtf_to_confidence,
)
from agents.feature_engine.indicators import calculate_indicators


def _make_bullish_df(rows=200):
    """Create bullish data with more natural RSI behavior."""
    np.random.seed(42)
    ts = pd.date_range("2023-01-01", periods=rows, freq="h")
    # Moderate uptrend with pullbacks so RSI stays in normal range
    noise = np.random.randn(rows) * 0.8
    trend = np.linspace(0, 20, rows)
    # Add pullbacks via sine wave
    pullbacks = np.sin(np.linspace(0, 6 * np.pi, rows)) * 3
    close = 100 + trend + pullbacks + noise

    df = pd.DataFrame({
        "timestamp": ts,
        "open": close - np.random.uniform(0, 0.3, rows),
        "high": close + np.abs(np.random.randn(rows)) * 0.5,
        "low": close - np.abs(np.random.randn(rows)) * 0.5,
        "close": close,
        "volume": np.random.randint(1000, 50000, size=rows).astype(float),
    })
    return calculate_indicators(df)


def _make_bearish_df(rows=200):
    """Create bearish data with natural RSI behavior."""
    np.random.seed(42)
    ts = pd.date_range("2023-01-01", periods=rows, freq="h")
    noise = np.random.randn(rows) * 0.8
    trend = np.linspace(0, -20, rows)
    pullbacks = np.sin(np.linspace(0, 6 * np.pi, rows)) * 3
    close = 200 + trend + pullbacks + noise

    df = pd.DataFrame({
        "timestamp": ts,
        "open": close + np.random.uniform(0, 0.3, rows),
        "high": close + np.abs(np.random.randn(rows)) * 0.5,
        "low": close - np.abs(np.random.randn(rows)) * 0.5,
        "close": close,
        "volume": np.random.randint(1000, 50000, size=rows).astype(float),
    })
    return calculate_indicators(df)


class TestScoreTimeframe:
    def test_score_bounded(self):
        df = _make_bullish_df()
        score = _score_timeframe(df)
        assert -100 <= score <= 100

    def test_empty_returns_zero(self):
        score = _score_timeframe(pd.DataFrame())
        assert score == 0.0

    def test_single_row_returns_zero(self):
        df = _make_bullish_df().head(1)
        score = _score_timeframe(df)
        assert score == 0.0

    def test_bullish_and_bearish_differ(self):
        bull_score = _score_timeframe(_make_bullish_df())
        bear_score = _score_timeframe(_make_bearish_df())
        # Bullish should score higher than bearish
        assert bull_score > bear_score


class TestCalculateMtfScore:
    def test_consistent_direction(self):
        """All TFs same direction should give non-zero score."""
        df = _make_bullish_df()
        data_store = {"1d": df, "1h": df, "15m": df, "5m": df}
        score = calculate_mtf_score(data_store)
        assert score != 0  # Should have a directional opinion

    def test_score_bounded(self):
        df = _make_bullish_df()
        data_store = {"1d": df, "1h": df}
        score = calculate_mtf_score(data_store)
        assert -100 <= score <= 100

    def test_empty_store(self):
        score = calculate_mtf_score({})
        assert score == 0.0

    def test_partial_timeframes(self):
        """Should work even with missing timeframes."""
        df = _make_bullish_df()
        score = calculate_mtf_score({"1d": df})
        assert -100 <= score <= 100

    def test_opposing_directions(self):
        """Bullish and bearish TFs should partially cancel out."""
        bull = _make_bullish_df()
        bear = _make_bearish_df()
        mixed_score = calculate_mtf_score({"1d": bull, "1h": bear})
        pure_score = calculate_mtf_score({"1d": bull, "1h": bull})
        # Mixed should be closer to zero than pure
        assert abs(mixed_score) <= abs(pure_score) + 10  # Some tolerance


class TestMtfToConfidence:
    def test_strong_bull(self):
        conf = mtf_to_confidence(100)
        assert conf == 100.0

    def test_strong_bear(self):
        conf = mtf_to_confidence(-100)
        assert conf == 0.0

    def test_neutral(self):
        conf = mtf_to_confidence(0)
        assert conf == 50.0
