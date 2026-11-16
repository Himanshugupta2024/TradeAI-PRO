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


class TestWalkForwardFoldScaling:
    """`walk_forward_split(scale_per_fold=True)` must scale with train-only stats.

    The point of the flag is that a validation fold is never normalised with its
    own mean/std: that is the classic "scaled on the full series" leak, where the
    model implicitly sees where the future distribution sits.
    """

    def _ramp(self, n: int = 300, n_features: int = 3) -> np.ndarray:
        # Strictly increasing per column, so every validation fold sits at a
        # different level than its training fold. Scaling a fold with its own
        # statistics would re-centre it back to 0 and erase that difference.
        idx = np.arange(n, dtype=float)[:, None]
        return np.tile(idx, (1, n_features))

    def test_validation_fold_is_scaled_with_train_statistics(self):
        from agents.ai_engine.trainer import walk_forward_split

        X = self._ramp()
        y = (np.arange(len(X)) % 2).astype(np.int64)

        scaled = walk_forward_split(
            X, y, n_splits=3, train_ratio=0.7, embargo_bars=0, scale_per_fold=True
        )
        assert scaled, "walk-forward split produced no folds"

        for X_tr, _, X_va, _ in scaled:
            # Training fold is standardised to mean 0 / std 1 per feature.
            assert np.allclose(np.nanmean(X_tr, axis=0), 0.0, atol=1e-9)
            assert np.allclose(np.nanstd(X_tr, axis=0), 1.0, atol=1e-9)
            # Validation fold must not be centred on itself, otherwise the
            # level shift between folds disappears.
            assert np.nanmean(X_va, axis=0).min() > 1.0

    def test_validation_fold_matches_manual_train_statistics(self):
        from agents.ai_engine.trainer import walk_forward_split

        X = self._ramp()
        y = (np.arange(len(X)) % 2).astype(np.int64)

        unscaled = walk_forward_split(
            X, y, n_splits=3, train_ratio=0.7, embargo_bars=0, scale_per_fold=False
        )
        scaled = walk_forward_split(
            X, y, n_splits=3, train_ratio=0.7, embargo_bars=0, scale_per_fold=True
        )
        assert len(unscaled) == len(scaled) > 0

        for (raw_tr, _, raw_va, _), (sc_tr, _, sc_va, _) in zip(unscaled, scaled):
            # Flag off: the raw level is preserved (train mean is not 0).
            assert np.nanmean(raw_tr, axis=0).min() > 0.0
            assert np.nanmean(raw_va, axis=0).min() > 0.0
            mu = np.nanmean(raw_tr, axis=0)
            sd = np.nanstd(raw_tr, axis=0)
            sd = np.where(np.abs(sd) > 1e-12, sd, 1.0)
            assert np.allclose(sc_tr, (raw_tr - mu) / sd)
            assert np.allclose(sc_va, (raw_va - mu) / sd)

    def test_constant_feature_does_not_divide_by_zero(self):
        from agents.ai_engine.trainer import walk_forward_split

        X = self._ramp()
        X[:, -1] = 5.0  # zero variance everywhere
        y = (np.arange(len(X)) % 2).astype(np.int64)

        for _, _, X_va, _ in walk_forward_split(
            X, y, n_splits=3, train_ratio=0.7, embargo_bars=0, scale_per_fold=True
        ):
            assert np.all(np.isfinite(X_va)), "constant feature produced inf/nan"
            assert np.allclose(X_va[:, -1], 0.0)


class TestPointInTimeFeatureAvailability:
    """Features must only use information that existed at the bar they describe.

    Two surfaces are covered:
      1. `propagate_trend_to_ltf` -- the HTF trend was joined on the bar's own
         label, so an intraday row received a trend derived from the close of
         the session it belonged to.
      2. `compute_anomaly_features` -- two series were back-filled, which handed
         early rows values from later bars.
    """

    def test_same_session_htf_trend_is_not_visible_intraday(self):
        from agents.feature_engine.trend import propagate_trend_to_ltf

        htf = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06",
            ]),
            "htf_trend": ["BULLISH", "BEARISH", "BULLISH", "BEARISH"],
        })
        # Intraday rows for 2026-01-02 only: every one of them must still see the
        # trend of 2026-01-01, because 2026-01-02 has not closed yet.
        ltf = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-02 09:15", "2026-01-02 15:15", freq="5min"),
            "close": 100.0,
        })

        result = propagate_trend_to_ltf(ltf, htf)

        assert len(result) == len(ltf)
        assert set(result["htf_trend"]) == {"BULLISH"}

    def test_htf_trend_becomes_visible_at_the_next_bar_boundary(self):
        from agents.feature_engine.trend import propagate_trend_to_ltf

        htf = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05"]),
            "htf_trend": ["BULLISH", "BEARISH", "BULLISH"],
        })
        ltf = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2026-01-02 00:00",   # exactly when the 01-01 bar became known
                "2026-01-03 00:00",   # exactly when the 01-02 bar became known
                "2026-01-04 12:00",   # between bars, still 01-02's value
            ]),
            "close": 100.0,
        })

        result = propagate_trend_to_ltf(ltf, htf).set_index("timestamp")

        assert result.loc["2026-01-02 00:00", "htf_trend"] == "BULLISH"
        assert result.loc["2026-01-03 00:00", "htf_trend"] == "BEARISH"
        assert result.loc["2026-01-04 12:00", "htf_trend"] == "BEARISH"

    def _anomaly_frame(self, atr_values: list[float]) -> pd.DataFrame:
        n = len(atr_values)
        return pd.DataFrame({
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1000.0] * n,
            "ATR_14": atr_values,
        })

    def test_first_bar_gap_does_not_borrow_its_own_close(self):
        from agents.ai_engine.anomaly_detector import compute_anomaly_features

        # Bar 0 opens 10% above its own close. Without a previous close the gap
        # is unknowable, so the honest value is 0.0 -- the old back-fill turned
        # this into a synthetic -9.09% "overnight gap" made from bar 0's own
        # close, which only exists after the bar completes.
        df = self._anomaly_frame([2.0, 2.0, 2.0])
        df.loc[0, "open"] = 110.0
        df.loc[1, "open"] = 105.0   # +5% versus bar 0's close, a real gap

        feats = compute_anomaly_features(df)

        assert feats["overnight_gap_pct"].iloc[0] == 0.0
        # The second bar does have a previous close and must still report the gap.
        assert feats["overnight_gap_pct"].iloc[1] == pytest.approx(0.05)

    def test_zero_atr_row_uses_the_last_known_atr(self):
        from agents.ai_engine.anomaly_detector import compute_anomaly_features

        df = self._anomaly_frame([5.0, 5.0, 0.0, 50.0, 50.0])

        feats = compute_anomaly_features(df)

        # Bar 2 has a zero ATR. Forward-fill gives it bar 1's 5.0, so the ratio
        # is 2.0/5.0. Back-filling would have used bar 3's ATR of 50.0 and
        # printed 0.04 -- a value derived from a bar that had not happened yet.
        assert feats["range_to_atr"].iloc[2] == pytest.approx(2.0 / 5.0)


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


class TestPointInTimePublicationGuards:
    """Ensure EOD macro and delivery metrics cannot be used before their exchange publication time."""

    def test_fii_dii_publication_guard(self):
        from agents.data_ingestion.fii_dii_fetcher import compute_as_of_timestamp, is_published_as_of
        from zoneinfo import ZoneInfo
        from datetime import datetime

        ist = ZoneInfo("Asia/Kolkata")
        as_of_iso = compute_as_of_timestamp("12-Nov-2026", hour=18, minute=0)

        # Before 18:00 IST (e.g. 11:30 IST during trading hours)
        dt_intraday = datetime(2026, 11, 12, 11, 30, tzinfo=ist)
        assert is_published_as_of(as_of_iso, dt_intraday) is False

        # Exactly at 18:00 IST
        dt_published = datetime(2026, 11, 12, 18, 0, tzinfo=ist)
        assert is_published_as_of(as_of_iso, dt_published) is True

        # Next morning (e.g. 09:15 IST next day)
        dt_next_day = datetime(2026, 11, 13, 9, 15, tzinfo=ist)
        assert is_published_as_of(as_of_iso, dt_next_day) is True

    def test_delivery_breadth_publication_guard(self):
        from agents.data_ingestion.delivery_breadth import is_published_as_of
        from zoneinfo import ZoneInfo
        from datetime import datetime

        ist = ZoneInfo("Asia/Kolkata")
        as_of_iso = datetime(2026, 11, 12, 17, 30, tzinfo=ist).isoformat()

        # At 15:15 IST before market close
        assert is_published_as_of(as_of_iso, datetime(2026, 11, 12, 15, 15, tzinfo=ist)) is False

        # At 17:35 IST after EOD delivery report is published
        assert is_published_as_of(as_of_iso, datetime(2026, 11, 12, 17, 35, tzinfo=ist)) is True


class TestTabularModelAntiLeakageGuards:
    """Ensure tabular XGBoost and LightGBM models enforce embargo bars and cost hurdles."""

    def test_xgboost_train_embargo(self, tmp_path):
        from agents.ai_engine.xgboost_model import XGBoostPredictor

        df = _generate_synthetic_ohlcv(250)
        df["signal"] = "LONG"

        model_file = str(tmp_path / "test_xgb.pkl")
        pred = XGBoostPredictor(model_path=model_file)
        metrics = pred.train(df, use_optuna=False, test_size=0.2, embargo_bars=15)

        assert "error" not in metrics
        n_samples = metrics["n_samples"]
        expected_val = int(n_samples * 0.2)
        expected_train = max(0, (n_samples - expected_val) - 15)
        assert metrics["n_val"] == expected_val
        assert metrics["n_train"] == expected_train


class TestNightlyScanParallelExecution:
    """Ensure nightly swing scan supports concurrent worker threads."""

    def test_run_scan_parallel_dispatch(self, monkeypatch):
        from scripts import nightly_swing_scan

        # Mock symbols to 3 fast symbols
        monkeypatch.setattr(nightly_swing_scan, "_symbol_list", lambda universe: ["RELIANCE", "TCS", "INFY"])
        # Mock _fetch_and_prepare to return empty DataFrame quickly
        monkeypatch.setattr(nightly_swing_scan, "_fetch_and_prepare", lambda sym: None)

        results, payload = nightly_swing_scan.run_scan(universe="local", workers=2)
        assert isinstance(results, list)
        assert payload["symbols_scanned"] == 3
        assert payload["symbols_skipped"] == 3

    def test_lightgbm_train_embargo(self, tmp_path):
        from agents.ai_engine.lightgbm_model import LightGBMPredictor

        df = _generate_synthetic_ohlcv(250)
        df["signal"] = "LONG"

        model_file = str(tmp_path / "test_lgbm.pkl")
        pred = LightGBMPredictor(model_path=model_file)
        metrics = pred.train(df, use_optuna=False, test_size=0.2, embargo_bars=15)

        assert "error" not in metrics
        n_samples = metrics["n_samples"]
        expected_val = int(n_samples * 0.2)
        expected_train = max(0, (n_samples - expected_val) - 15)
        assert metrics["n_val"] == expected_val
        assert metrics["n_train"] == expected_train


class TestNightlyScanParallelExecution:
    """Ensure nightly swing scan supports concurrent worker threads."""

    def test_run_scan_parallel_dispatch(self, monkeypatch):
        from scripts import nightly_swing_scan

        # Mock symbols to 3 fast symbols
        monkeypatch.setattr(nightly_swing_scan, "_symbol_list", lambda universe: ["RELIANCE", "TCS", "INFY"])
        # Mock _fetch_and_prepare to return empty DataFrame quickly
        monkeypatch.setattr(nightly_swing_scan, "_fetch_and_prepare", lambda sym: None)

        results, payload = nightly_swing_scan.run_scan(universe="local", workers=2)
        assert isinstance(results, list)
        assert payload["symbols_scanned"] == 3
        assert payload["symbols_skipped"] == 3

