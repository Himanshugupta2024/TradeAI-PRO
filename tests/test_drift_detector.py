"""Tests for signal feature drift detector."""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.signal_engine.signal_generator import _compute_feature_drift


def test_feature_drift_not_detected_on_stable_series():
    rows = 220
    rng = np.random.default_rng(42)
    base = 100 + rng.normal(0, 0.3, rows)

    df = pd.DataFrame(
        {
            "close": base,
            "volume": 1000 + rng.normal(0, 5, rows),
            "RSI_14": 50 + rng.normal(0, 1.0, rows),
            "MACD_hist": rng.normal(0, 0.05, rows),
            "ATR_14": 2 + rng.normal(0, 0.05, rows),
        }
    )

    score, detected, feature = _compute_feature_drift(df, lookback_bars=120, recent_bars=20)

    assert score >= 0.0
    assert detected is False
    assert isinstance(feature, str)


def test_feature_drift_detected_on_recent_shift(monkeypatch):
    rows = 240
    rng = np.random.default_rng(7)
    close = 100 + rng.normal(0, 0.25, rows)
    close[-20:] += 8.0

    df = pd.DataFrame(
        {
            "close": close,
            "volume": 1000 + rng.normal(0, 10, rows),
            "RSI_14": 50 + rng.normal(0, 1.5, rows),
            "MACD_hist": rng.normal(0, 0.05, rows),
            "ATR_14": 2 + rng.normal(0, 0.04, rows),
        }
    )

    monkeypatch.setenv("DUMMY", "1")
    score, detected, feature = _compute_feature_drift(df, lookback_bars=120, recent_bars=20)

    assert score > 3.0
    assert detected is True
    assert feature in {"close", "RSI_14", "MACD_hist", "ATR_14", "volume"}
