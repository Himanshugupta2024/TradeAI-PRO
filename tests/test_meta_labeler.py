"""Tests for meta-labelling: features, dataset, model, metrics, end-to-end."""

import json
import io
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from agents.ai_engine.meta_labeler import (
    BASELINE_FEATURES,
    KRONOS_FEATURES,
    LogisticModel,
    build_meta_dataset,
    calibration_table,
    compute_features,
    log_loss,
    primary_signal,
    roc_auc,
    save_result,
    select_threshold,
    time_split,
    train_meta_model,
    _merge_kronos,
)


def _panel(n=260, symbols=("AAA", "BBB"), seed=7):
    rng = np.random.default_rng(seed)
    rows = []
    for symbol in symbols:
        # Up-trend then down-trend so both LONG and SHORT signals occur
        drift = np.concatenate([np.full(n // 2, 0.003), np.full(n - n // 2, -0.003)])
        prices = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.008, n) + drift))
        timestamps = pd.date_range("2024-01-01", periods=n, freq="D")
        for i in range(n):
            close = float(prices[i])
            rows.append(
                {
                    "timestamp": timestamps[i],
                    "symbol": symbol,
                    "open": close * (1 + rng.normal(0, 0.001)),
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": float(1000 + rng.integers(0, 500)),
                }
            )
    return pd.DataFrame(rows)


class TestFeatures:
    def test_compute_features_columns(self):
        features = compute_features(_panel(n=120, symbols=("AAA",)))
        for column in BASELINE_FEATURES + ["ATR"]:
            assert column in features.columns
        tail = features.iloc[60:]
        assert (tail["ATR"] > 0).all()
        assert tail["rsi_14"].between(0, 100).all()

    def test_primary_signal_has_both_sides(self):
        frame = compute_features(_panel(n=260, symbols=("AAA",)))
        signal = primary_signal(frame)
        assert (signal == "LONG").any()
        assert (signal == "SHORT").any()


class TestDataset:
    def test_build_dataset_labels_and_costs(self):
        data = build_meta_dataset(_panel(n=260, symbols=("AAA", "BBB")), cost_pct=0.39)
        assert len(data) > 0
        assert set(data["meta_label"].unique()).issubset({0, 1})
        assert set(data["tb_label"].unique()).issubset({1.0, -1.0})
        diff = (data["net_return_pct"] - (data["tb_return_pct"] - 0.39)).abs().max()
        assert diff < 1e-9
        assert "ret_5" in data.attrs["feature_columns"]

    def test_short_panel_raises(self):
        try:
            build_meta_dataset(_panel(n=40, symbols=("AAA",)))
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_merge_kronos_flags_missing_rows(self):
        data = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2024-01-01", "2024-01-02"]),
                "symbol": ["AAA", "AAA"],
            }
        )
        kronos = pd.DataFrame(
            {
                "symbol": ["AAA"],
                "as_of": ["2024-01-01"],
                "kronos_expected_return_pct": [1.5],
                "kronos_prob_up": [0.6],
            }
        )
        merged = _merge_kronos(data, kronos)
        assert merged.loc[0, "kronos_available"] == 1.0
        assert merged.loc[1, "kronos_available"] == 0.0
        assert merged.loc[1, "kronos_prob_up"] == 0.0
        for column in KRONOS_FEATURES:
            assert column in merged.columns


class TestSplit:
    def test_chronological_order(self):
        data = build_meta_dataset(_panel(n=300, symbols=("AAA",)))
        split = time_split(data)
        assert split.train["timestamp"].max() < split.validation["timestamp"].min()
        assert split.validation["timestamp"].max() < split.test["timestamp"].min()


class TestMetrics:
    def test_auc_perfect_and_reversed(self):
        y = np.array([0, 0, 1, 1])
        assert roc_auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
        assert roc_auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0

    def test_auc_single_class_is_nan(self):
        assert np.isnan(roc_auc(np.zeros(5), np.linspace(0, 1, 5)))

    def test_log_loss_bounds(self):
        y = np.array([0, 1])
        assert log_loss(y, np.array([0.01, 0.99])) < 0.05
        assert log_loss(y, np.array([0.5, 0.5])) > 0.5

    def test_calibration_table_shape(self):
        rng = np.random.default_rng(0)
        y = rng.integers(0, 2, 200)
        p = rng.uniform(0, 1, 200)
        table = calibration_table(y, p, bins=5)
        assert 1 <= len(table) <= 5
        assert {"bin", "n", "predicted", "actual"} <= set(table[0])

    def test_select_threshold_prefers_profitable_bucket(self):
        probs = np.array([0.1, 0.2, 0.8, 0.9, 0.85, 0.95])
        y = np.array([0, 0, 1, 1, 1, 1])
        returns = np.array([-1, -1, 2, 2, 2, 2])
        threshold = select_threshold(y, probs, returns, min_selected=2)
        assert threshold >= 0.8


class TestLogisticModel:
    def test_separable_data(self):
        rng = np.random.default_rng(1)
        n = 300
        x = rng.normal(size=(n, 2))
        y = (x[:, 0] + 0.5 * x[:, 1] > 0).astype(int)
        model = LogisticModel(l2=0.1, epochs=600).fit(x, y)
        probs = model.predict_proba(x)
        assert roc_auc(y, probs) > 0.95
        assert ((probs >= 0) & (probs <= 1)).all()

    def test_single_class_raises(self):
        try:
            LogisticModel().fit(np.zeros((10, 2)), np.ones(10))
            assert False, "expected ValueError"
        except ValueError:
            pass


class TestEndToEnd:
    def _tabular(self, n=700, seed=3):
        rng = np.random.default_rng(seed)
        X = rng.normal(size=(n, len(BASELINE_FEATURES)))
        logit = 1.4 * X[:, 1] - 1.0 * X[:, 2]
        probability = 1.0 / (1.0 + np.exp(-logit))
        y = rng.binomial(1, probability)

        frame = pd.DataFrame(X, columns=BASELINE_FEATURES)
        frame["timestamp"] = pd.date_range("2024-01-01", periods=n, freq="D")
        frame["meta_label"] = y
        frame["net_return_pct"] = np.where(y == 1, 2.0, -1.0) + rng.normal(0, 0.3, n)
        frame.attrs["feature_columns"] = list(BASELINE_FEATURES)
        return frame

    def test_train_and_save(self):
        data = self._tabular()
        result = train_meta_model(data, l2=0.5)

        assert result.metrics["auc"] > 0.6
        assert result.threshold >= 0
        assert isinstance(result.calibration, list)
        assert result.verdict in {"EDGE: candidate for shadow mode",
                                  "NO EDGE: do not size positions with this model yet"}
        assert len(result.model.weights) == len(result.features)

        class _Buffer(io.StringIO):
            def close(self):
                pass

        written = {}

        def _fake_open(path, mode="r", encoding=None):
            buffer = _Buffer()
            written[str(path)] = buffer
            return buffer

        with patch("builtins.open", _fake_open):
            save_result(result, "meta_model.json", "meta_metrics.json")

        payload = json.loads(written["meta_model.json"].getvalue())
        assert payload["model"]["type"] == "logistic"
        assert "metrics" in json.loads(written["meta_metrics.json"].getvalue())
