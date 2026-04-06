"""
Unit tests for the Feature Engineering Agent — indicators module.
Pure pandas/numpy implementation — no external TA library.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from agents.feature_engine.indicators import calculate_indicators


def _make_synthetic_ohlcv(rows: int = 300, base_price: float = 100.0) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame with a gentle uptrend + noise."""
    np.random.seed(42)
    timestamps = pd.date_range("2023-01-01", periods=rows, freq="h")
    trend = np.linspace(0, 20, rows)
    noise = np.random.randn(rows) * 2

    close = base_price + trend + noise
    high = close + np.abs(np.random.randn(rows)) * 1.5
    low = close - np.abs(np.random.randn(rows)) * 1.5
    open_ = close + np.random.randn(rows) * 0.5
    volume = np.random.randint(1000, 50000, size=rows).astype(float)

    return pd.DataFrame({
        "timestamp": timestamps,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


class TestCalculateIndicators:
    """Tests for calculate_indicators()."""

    def test_returns_expected_columns(self):
        """All indicator columns should be present in the output."""
        df = _make_synthetic_ohlcv(300)
        result = calculate_indicators(df)

        expected_cols = ["RSI_14", "MACD", "MACD_signal", "MACD_hist",
                         "EMA_20", "EMA_50", "EMA_200", "ATR_14"]
        for col in expected_cols:
            assert col in result.columns, f"Missing column: {col}"

    def test_no_nan_after_warmup(self):
        """After the longest warmup window (EMA_200), values should not be NaN."""
        df = _make_synthetic_ohlcv(300)
        result = calculate_indicators(df)

        warmup = 200  # EMA_200 needs ~200 rows to stabilize
        tail = result.iloc[warmup:]

        for col in ["RSI_14", "MACD", "EMA_20", "EMA_50", "EMA_200", "ATR_14"]:
            nan_count = tail[col].isna().sum()
            assert nan_count == 0, f"{col} has {nan_count} NaN values after warmup"

    def test_rsi_bounded(self):
        """RSI values should be between 0 and 100."""
        df = _make_synthetic_ohlcv(300)
        result = calculate_indicators(df)
        rsi = result["RSI_14"].dropna()
        assert (rsi >= 0).all() and (rsi <= 100).all(), "RSI out of [0, 100] range"

    def test_ema_order_in_uptrend(self):
        """In an uptrend, EMA_20 should generally be above EMA_200 at the end."""
        df = _make_synthetic_ohlcv(300)
        result = calculate_indicators(df)
        last_row = result.iloc[-1]
        assert last_row["EMA_20"] > last_row["EMA_200"], \
            "In synthetic uptrend, EMA_20 should be above EMA_200"

    def test_empty_dataframe(self):
        """Empty input should return empty output without errors."""
        df = pd.DataFrame()
        result = calculate_indicators(df)
        assert result.empty

    def test_original_columns_preserved(self):
        """Original OHLCV columns should not be modified."""
        df = _make_synthetic_ohlcv(300)
        original_close = df["close"].copy()
        result = calculate_indicators(df)
        pd.testing.assert_series_equal(result["close"], original_close)
