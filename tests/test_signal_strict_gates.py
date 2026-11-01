import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.support_resistance import calculate_support_resistance
from agents.feature_engine.trend import classify_trend
from agents.signal_engine.signal_generator import generate_signals
from config import settings


def _make_df(rows: int = 320, seed: int = 101) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-01", periods=rows, freq="h")
    close = 100 + np.cumsum(rng.normal(loc=0.04, scale=0.45, size=rows))
    base_vol = np.full(rows, 10_000.0)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close + rng.normal(0, 0.12, size=rows),
            "high": close + np.abs(rng.normal(0, 0.7, size=rows)),
            "low": close - np.abs(rng.normal(0, 0.7, size=rows)),
            "close": close,
            "volume": base_vol,
        }
    )


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    df = calculate_indicators(df)
    df = calculate_support_resistance(df)
    df = classify_trend(df)
    return df


def test_strict_gate_emits_alignment_columns(monkeypatch):
    monkeypatch.setattr(settings, "SIGNAL_CONFLUENCE_GATE_ENABLED", True)
    monkeypatch.setattr(settings, "SIGNAL_CONFLUENCE_MIN_VOTES", 3)
    monkeypatch.setattr(settings, "MTF_SIGNAL_GATE_ENABLED", True)
    monkeypatch.setattr(settings, "MTF_SIGNAL_MIN_VOTES", 2)

    out = generate_signals(_enrich(_make_df()))

    assert "confluence_votes_buy" in out.columns
    assert "confluence_votes_sell" in out.columns
    assert "timeframe_alignment_score" in out.columns
    assert "auto_exec_eligible_mtf" in out.columns
    assert "high_conviction_mtf" in out.columns


def test_volume_gate_blocks_directional_signals_on_dead_volume(monkeypatch):
    monkeypatch.setattr(settings, "SIGNAL_REQUIRE_VOLUME_GATE", True)
    monkeypatch.setattr(settings, "SIGNAL_CONFLUENCE_GATE_ENABLED", False)
    monkeypatch.setattr(settings, "MTF_SIGNAL_GATE_ENABLED", False)

    df = _make_df()
    # Flat volume forces volume_ok False because it cannot exceed 1.2x rolling average.
    df["volume"] = 10_000.0

    out = generate_signals(_enrich(df))
    directional = out["signal"].isin(["LONG", "SHORT"]).sum()
    assert directional == 0
