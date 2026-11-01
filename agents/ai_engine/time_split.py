"""
TimeSeries Splitter with Embargo and Chronological Partitioning.

Ensures absolute zero-leakage contracts for outcome-based trade prediction:
  1. TimeSeriesSplitWithEmbargo: enforces a strict gap (embargo_bars >= forward_bars)
     between training folds and validation folds to prevent label autocorrelation leakage.
  2. chronological_four_way_split: chronologically partitions datasets into:
     Base Train -> Meta OOF Train -> Calibration -> Untouched Test.
  3. prepare_trade_outcome_targets: extracts binary 1R:2R win/loss targets aligning
     with label_1r2r_outcome.
"""

from __future__ import annotations

import logging
from typing import Generator

import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit

from agents.feature_engine.label_generator import label_1r2r_outcome

log = logging.getLogger(__name__)


class TimeSeriesSplitWithEmbargo:
    """
    Time-series cross-validator with an embargo gap between train and test splits.

    In financial time series where labels look ahead H bars (e.g. 1R:2R target within
    forward_bars=5), standard cross-validation or zero-gap time splits leak information
    from the training set into the test set because labels overlap temporally.

    This validator guarantees:
        max(train_idx) + embargo_bars < min(test_idx)
    """

    def __init__(
        self,
        n_splits: int = 5,
        embargo_bars: int = 5,
        max_train_size: int | None = None,
        test_size: int | None = None,
    ):
        if n_splits < 2:
            raise ValueError("n_splits must be at least 2")
        if embargo_bars < 0:
            raise ValueError("embargo_bars must be non-negative")

        self.n_splits = n_splits
        self.embargo_bars = embargo_bars
        self.max_train_size = max_train_size
        self.test_size = test_size

    def split(
        self,
        X: np.ndarray | pd.DataFrame,
        y: np.ndarray | pd.Series | None = None,
        groups: np.ndarray | None = None,
    ) -> Generator[tuple[np.ndarray, np.ndarray], None, None]:
        """
        Generate indices to split data into training and test sets.

        Yields:
            (train_indices, test_indices)
        """
        n_samples = len(X)
        if n_samples < (self.n_splits + 1) + self.embargo_bars:
            raise ValueError(
                f"Insufficient samples ({n_samples}) for {self.n_splits} splits "
                f"with embargo_bars={self.embargo_bars}"
            )

        # Use scikit-learn's native TimeSeriesSplit with gap parameter if applicable
        tscv = TimeSeriesSplit(
            n_splits=self.n_splits,
            max_train_size=self.max_train_size,
            test_size=self.test_size,
            gap=self.embargo_bars,
        )

        for train_idx, test_idx in tscv.split(X, y, groups):
            # Enforce that max train index strictly respects the embargo gap
            if len(train_idx) > 0 and len(test_idx) > 0:
                assert max(train_idx) + self.embargo_bars < min(test_idx), (
                    f"Leakage detected: max train index {max(train_idx)} with embargo "
                    f"{self.embargo_bars} overlaps test index {min(test_idx)}"
                )
            yield train_idx, test_idx

    def get_n_splits(
        self,
        X: np.ndarray | pd.DataFrame | None = None,
        y: np.ndarray | pd.Series | None = None,
        groups: np.ndarray | None = None,
    ) -> int:
        return self.n_splits


def chronological_four_way_split(
    df: pd.DataFrame,
    ratios: tuple[float, float, float, float] = (0.50, 0.25, 0.125, 0.125),
    embargo_bars: int = 5,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Chronologically partition df into 4 sequential non-overlapping datasets
    separated by an embargo gap:
      1. Base Model Training Data
      2. Meta-Learner (OOF) Training Data
      3. Calibration Data
      4. Untouched Out-of-Time Test Data

    Args:
        df: Input DataFrame (must be chronologically sorted).
        ratios: Proportions for (base, meta, calibration, test). Sum must equal 1.0.
        embargo_bars: Number of candles discarded between segments to prevent lookahead leakage.

    Returns:
        (base_df, meta_df, calib_df, test_df)
    """
    if df.empty:
        raise ValueError("DataFrame cannot be empty")
    if not np.isclose(sum(ratios), 1.0):
        raise ValueError(f"Split ratios must sum to 1.0, got {sum(ratios)}")

    n = len(df)
    min_required = 4 * embargo_bars + 20
    if n < min_required:
        raise ValueError(f"Insufficient data ({n} rows) for four-way split with embargo {embargo_bars}")

    # Compute raw slice boundaries
    n_base = int(n * ratios[0])
    n_meta = int(n * ratios[1])
    n_calib = int(n * ratios[2])

    idx0 = 0
    idx1 = n_base
    idx2 = idx1 + embargo_bars
    idx3 = idx2 + n_meta
    idx4 = idx3 + embargo_bars
    idx5 = idx4 + n_calib
    idx6 = idx5 + embargo_bars
    idx7 = n

    if not (idx0 < idx1 <= idx2 < idx3 <= idx4 < idx5 <= idx6 < idx7):
        raise ValueError("Embargo gaps exceed data size. Reduce ratios or embargo_bars.")

    base_df = df.iloc[idx0:idx1].copy()
    meta_df = df.iloc[idx2:idx3].copy()
    calib_df = df.iloc[idx4:idx5].copy()
    test_df = df.iloc[idx6:idx7].copy()

    log.debug(
        "[TimeSplit] 4-Way Chronological Split: Base=%d, Meta=%d, Calib=%d, Test=%d (Embargo=%d bars)",
        len(base_df), len(meta_df), len(calib_df), len(test_df), embargo_bars,
    )

    return base_df, meta_df, calib_df, test_df


def prepare_trade_outcome_targets(
    df: pd.DataFrame,
    forward_bars: int = 5,
    rr_ratio: float = 2.0,
    treat_timeout_as_loss: bool = True,
) -> tuple[pd.Series, pd.Series]:
    """
    Extract aligned 1R:2R binary win/loss targets from label_1r2r_outcome.

    Args:
        df: DataFrame with OHLCV and technical indicators.
        forward_bars: Number of candles forward to evaluate TP/SL.
        rr_ratio: Risk to reward ratio (default 2.0).
        treat_timeout_as_loss: If True, timeouts (outcome=0) are mapped to 0 (did not win).
                               If False, timeout rows are excluded from training mask.

    Returns:
        (target_series, valid_mask)
        target_series: int8 binary series (1 = Win, 0 = Loss)
        valid_mask: boolean series of rows eligible for model training
    """
    raw_labels = label_1r2r_outcome(df, forward_bars=forward_bars, rr_ratio=rr_ratio)

    # 1 is WIN, -1 is LOSS, 0 is TIMEOUT
    if treat_timeout_as_loss:
        # Trade failed to hit 1:2 TP -> treat as non-win (0)
        target = (raw_labels == 1).astype("int8")
        # Valid rows are any row where a signal was evaluated
        valid_mask = raw_labels.isin([1, -1, 0])
    else:
        # Only evaluate definite resolved outcomes
        target = (raw_labels == 1).astype("int8")
        valid_mask = raw_labels.isin([1, -1])

    target.name = "trade_outcome_win"
    valid_mask.name = "trade_outcome_mask"

    return target, valid_mask

