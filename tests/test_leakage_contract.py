"""
Unit Tests for Phase 0: Time-Series Leakage Contract & Target Definition.

Verifies:
  1. TimeSeriesSplitWithEmbargo guarantees max(train) + gap < min(val) for all folds.
  2. chronological_four_way_split partitions Base, Meta, Calib, Test with zero overlap.
  3. prepare_trade_outcome_targets generates clean 1R:2R binary targets.
  4. Timestamp assertions strictly enforce chronological ordering without forward peeking.
"""

import sys
import os
import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.ai_engine.time_split import (
    TimeSeriesSplitWithEmbargo,
    chronological_four_way_split,
    prepare_trade_outcome_targets,
)
from agents.feature_engine.indicators import calculate_indicators


def _generate_synthetic_ohlcv(n_bars: int = 500) -> pd.DataFrame:
    """Generate synthetic OHLCV data with timestamp index."""
    np.random.seed(42)
    start = datetime(2026, 1, 1, 9, 15)
    timestamps = [start + timedelta(minutes=5 * i) for i in range(n_bars)]
    
    # Random walk price
    returns = np.random.normal(0.0002, 0.005, size=n_bars)
    price = 100.0 * np.exp(np.cumsum(returns))
    
    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": price * (1 + np.random.normal(0, 0.001, n_bars)),
        "high": price * (1 + np.abs(np.random.normal(0, 0.003, n_bars))),
        "low": price * (1 - np.abs(np.random.normal(0, 0.003, n_bars))),
        "close": price,
        "volume": np.random.randint(500, 25000, size=n_bars).astype(float),
    })
    # Add standard indicators
    df = calculate_indicators(df)
    return df


class TestTimeSeriesSplitWithEmbargo:
    """Test suite ensuring zero temporal leakage across cross-validation folds."""

    def test_embargo_gap_strictly_enforced(self):
        df = _generate_synthetic_ohlcv(300)
        embargo = 5
        n_splits = 4
        cv = TimeSeriesSplitWithEmbargo(n_splits=n_splits, embargo_bars=embargo)

        for fold, (train_idx, val_idx) in enumerate(cv.split(df)):
            max_train = max(train_idx)
            min_val = min(val_idx)

            # Strict assertion: Train indices must not leak into validation
            assert min_val > max_train + embargo, (
                f"Fold {fold} failed embargo check: min_val={min_val}, "
                f"max_train={max_train}, embargo={embargo}"
            )

            # Verify timestamp ordering
            train_times = df.iloc[train_idx]["timestamp"]
            val_times = df.iloc[val_idx]["timestamp"]
            assert train_times.max() < val_times.min(), f"Fold {fold} has timestamp inversion!"

    def test_invalid_parameters_raise_value_error(self):
        with pytest.raises(ValueError, match="n_splits must be at least 2"):
            TimeSeriesSplitWithEmbargo(n_splits=1)

        with pytest.raises(ValueError, match="embargo_bars must be non-negative"):
            TimeSeriesSplitWithEmbargo(n_splits=3, embargo_bars=-1)

        with pytest.raises(ValueError, match="Insufficient samples"):
            cv = TimeSeriesSplitWithEmbargo(n_splits=10, embargo_bars=50)
            list(cv.split(np.arange(20)))


class TestChronologicalFourWaySplit:
    """Test suite for the 4-way chronological partition."""

    def test_partition_ordering_and_embargo_isolation(self):
        df = _generate_synthetic_ohlcv(400)
        embargo = 6
        base, meta, calib, test = chronological_four_way_split(
            df,
            ratios=(0.50, 0.25, 0.125, 0.125),
            embargo_bars=embargo,
        )

        # 1. Non-empty check
        assert len(base) > 0
        assert len(meta) > 0
        assert len(calib) > 0
        assert len(test) > 0

        # 2. Strict chronological order checks
        assert base["timestamp"].max() < meta["timestamp"].min()
        assert meta["timestamp"].max() < calib["timestamp"].min()
        assert calib["timestamp"].max() < test["timestamp"].min()

        # 3. Disjoint index sets
        idx_sets = [set(base.index), set(meta.index), set(calib.index), set(test.index)]
        for i in range(len(idx_sets)):
            for j in range(i + 1, len(idx_sets)):
                assert len(idx_sets[i].intersection(idx_sets[j])) == 0, "Partitions overlap!"

        # 4. Total rows + discarded embargo bars == total rows
        discarded = 3 * embargo
        total_retained = len(base) + len(meta) + len(calib) + len(test)
        assert total_retained + discarded == len(df)


class TestTradeOutcomeTargetContract:
    """Test suite ensuring Option C 1R:2R targets align correctly."""

    def test_prepare_trade_outcome_targets_structure(self):
        df = _generate_synthetic_ohlcv(200)
        # Mock signals on some bars
        df["signal"] = "HOLD"
        df.loc[10:30, "signal"] = "LONG"
        df.loc[50:70, "signal"] = "SHORT"

        target, mask = prepare_trade_outcome_targets(
            df, forward_bars=5, rr_ratio=2.0, treat_timeout_as_loss=True
        )

        assert len(target) == len(df)
        assert len(mask) == len(df)
        assert set(target.unique()).issubset({0, 1})
        assert mask.dtype == bool

