"""Tests for the optional Kronos adapter (no torch / no weights required)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from agents.ai_engine.kronos_forecaster import (
    FEATURE_KEYS,
    KronosConfig,
    KronosForecaster,
    KronosUnavailable,
)


class FakePredictor:
    """Returns a preset close path per call, cycling through the list."""

    def __init__(self, paths):
        self.paths = paths
        self.calls = 0

    def predict(self, df, x_timestamp, y_timestamp, pred_len, T, top_p, sample_count):
        values = self.paths[self.calls % len(self.paths)]
        self.calls += 1
        return pd.DataFrame({"close": list(values)[:pred_len]})


def _frame(n=60, start=100.0, end=None):
    timestamps = pd.date_range("2024-01-01", periods=n, freq="D")
    close = np.linspace(start, start + 10.0 if end is None else end, n)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
        }
    )


class TestDeriveFeatures:
    def test_two_paths_produce_a_distribution(self):
        paths = np.array([[101.0, 102.0, 103.0], [99.0, 98.0, 97.0]])
        features = KronosForecaster.derive_features(paths, last_close=100.0)

        assert abs(features["kronos_expected_return_pct"] - 0.0) < 1e-9
        assert abs(features["kronos_prob_up"] - 0.5) < 1e-9
        assert abs(features["kronos_dispersion_pct"] - 3.0) < 1e-9
        assert abs(features["kronos_q05_return_pct"] - (-2.7)) < 1e-9
        assert abs(features["kronos_q95_return_pct"] - 2.7) < 1e-9
        assert set(features) == set(FEATURE_KEYS)

    def test_single_path_is_a_point_forecast(self):
        features = KronosForecaster.derive_features(np.array([[101.0, 102.0, 105.0]]), 100.0)
        assert features["kronos_prob_up"] == 1.0
        assert features["kronos_dispersion_pct"] == 0.0
        assert abs(features["kronos_expected_return_pct"] - 5.0) < 1e-9

    def test_invalid_last_close_raises(self):
        try:
            KronosForecaster.derive_features(np.array([[101.0]]), 0.0)
            assert False, "expected ValueError"
        except ValueError:
            pass


class TestForecast:
    def test_forecast_uses_paths_and_reports_metadata(self):
        predictor = FakePredictor([[101.0, 102.0, 103.0], [99.0, 98.0, 97.0]])
        forecaster = KronosForecaster(config=KronosConfig(), predictor=predictor)

        result = forecaster.forecast(_frame(end=100.0), symbol="RELIANCE", pred_len=3, n_paths=2)

        assert result.symbol == "RELIANCE"
        assert result.pred_len == 3
        assert result.n_paths == 2
        assert result.as_of == pd.Timestamp("2024-01-01") + pd.Timedelta(days=59)
        assert result.features["kronos_prob_up"] == 0.5
        assert result.paths.shape == (2, 3)

    def test_record_shape(self):
        predictor = FakePredictor([[101.0, 102.0, 103.0]])
        forecaster = KronosForecaster(config=KronosConfig(), predictor=predictor)
        record = forecaster.forecast(_frame(), symbol="TCS", pred_len=3).as_record()

        assert record["symbol"] == "TCS"
        assert record["n_paths"] == 1
        for key in FEATURE_KEYS:
            assert key in record

    def test_prepare_frame_fills_missing_columns(self):
        raw = _frame().drop(columns=["volume"])
        prepared = KronosForecaster._prepare_frame(raw)
        assert "volume" in prepared.columns
        assert "amount" in prepared.columns
        assert (prepared["volume"] == 0.0).all()

    def test_prepare_frame_sorts_and_drops_bad_rows(self):
        raw = _frame().iloc[::-1].copy()
        raw.loc[0, "close"] = np.nan
        prepared = KronosForecaster._prepare_frame(raw)
        assert prepared["timestamp"].is_monotonic_increasing
        assert prepared["close"].notna().all()

    def test_short_history_raises(self):
        predictor = FakePredictor([[101.0, 102.0, 103.0]])
        forecaster = KronosForecaster(config=KronosConfig(), predictor=predictor)
        try:
            forecaster.forecast(_frame(n=10), pred_len=3)
            assert False, "expected ValueError"
        except ValueError:
            pass


class TestAvailability:
    def test_is_available_false_for_missing_repo(self):
        assert KronosForecaster.is_available(repo_path="/definitely/not/here") is False

    def test_load_raises_when_repo_missing(self):
        forecaster = KronosForecaster(
            config=KronosConfig(repo_path="/definitely/not/here", model_id="x", tokenizer_id="y")
        )
        try:
            forecaster.load()
            assert False, "expected KronosUnavailable"
        except KronosUnavailable:
            pass

    def test_model_catalog_metadata(self):
        assert KronosConfig.for_model("mini").max_context == 2048
        assert KronosConfig.for_model("small").max_context == 512
        try:
            KronosConfig.for_model("gigantic")
            assert False, "expected ValueError"
        except ValueError:
            pass
