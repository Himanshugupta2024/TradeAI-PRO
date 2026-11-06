"""Tests for triple-barrier labelling and meta-labels."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd

from agents.feature_engine.label_generator import (
    add_triple_barrier_labels,
    meta_labels_from_triple_barrier,
    triple_barrier_labels,
    triple_barrier_stats,
)


def _frame(prices, atr=1.0, signal="HOLD"):
    closes = [float(p) for p in prices]
    n = len(closes)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [1000] * n,
            "signal": [signal] * n,
            "ATR": [float(atr)] * n,
        }
    )


class TestTripleBarrier:
    def test_long_target_hit(self):
        df = _frame([100] * 6, signal="HOLD")
        df.loc[0, "signal"] = "LONG"
        df.loc[2, "high"] = 103.0  # target = 100 + 2*1 = 102

        tb = triple_barrier_labels(df, atr_col="ATR", max_bars=5)
        row = tb.loc[0]
        assert row["tb_label"] == 1
        assert row["tb_barrier"] == "target"
        assert row["tb_bars_held"] == 2
        assert row["tb_side"] == "LONG"

    def test_long_stop_hit(self):
        df = _frame([100] * 6, signal="HOLD")
        df.loc[0, "signal"] = "LONG"
        df.loc[1, "low"] = 98.0  # stop = 99

        tb = triple_barrier_labels(df, atr_col="ATR", max_bars=5)
        row = tb.loc[0]
        assert row["tb_label"] == -1
        assert row["tb_barrier"] == "stop"
        assert row["tb_bars_held"] == 1

    def test_same_bar_both_barriers_resolves_to_stop(self):
        df = _frame([100] * 6, signal="HOLD")
        df.loc[0, "signal"] = "LONG"
        df.loc[1, "high"] = 103.0  # would hit target
        df.loc[1, "low"] = 97.0    # also hits stop

        tb = triple_barrier_labels(df, atr_col="ATR", max_bars=5)
        assert tb.loc[0, "tb_label"] == -1
        assert tb.loc[0, "tb_barrier"] == "stop"

    def test_vertical_barrier_timeout(self):
        df = _frame([100] * 6, signal="HOLD")
        df.loc[0, "signal"] = "LONG"

        tb = triple_barrier_labels(df, atr_col="ATR", max_bars=3)
        row = tb.loc[0]
        assert row["tb_label"] == 0
        assert row["tb_barrier"] == "vertical"
        assert row["tb_bars_held"] == 3

    def test_short_target_hit(self):
        df = _frame([100] * 6, signal="HOLD")
        df.loc[0, "signal"] = "SHORT"
        df.loc[2, "low"] = 97.0  # target = 98

        tb = triple_barrier_labels(df, atr_col="ATR", max_bars=5)
        row = tb.loc[0]
        assert row["tb_label"] == 1
        assert row["tb_side"] == "SHORT"
        assert row["tb_return_pct"] > 0

    def test_non_signal_rows_are_nan(self):
        df = _frame([100] * 4, signal="HOLD")
        tb = triple_barrier_labels(df, atr_col="ATR")
        assert tb["tb_label"].isna().all()

    def test_missing_atr_returns_all_nan(self):
        df = _frame([100] * 4).drop(columns=["ATR"])
        tb = triple_barrier_labels(df, atr_col="ATR")
        assert tb["tb_label"].isna().all()
        assert tb["tb_barrier"].isna().all()

    def test_add_helper_keeps_original_columns(self):
        df = _frame([100] * 4, signal="LONG")
        out = add_triple_barrier_labels(df, atr_col="ATR")
        assert "tb_label" in out.columns
        assert "close" in out.columns

    def test_stats_counts(self):
        df = _frame([100] * 8, signal="HOLD")
        df.loc[0, "signal"] = "LONG"
        df.loc[2, "high"] = 103.0
        df.loc[3, "signal"] = "LONG"
        df.loc[4, "low"] = 97.0
        tb = triple_barrier_labels(df, atr_col="ATR", max_bars=3)
        stats = triple_barrier_stats(tb)
        assert stats["targets"] >= 1
        assert stats["stops"] >= 1
        assert stats["labelled"] == stats["targets"] + stats["stops"] + stats["timeouts"]


class TestMetaLabels:
    def test_win_loss_and_unlabelled(self):
        tb = pd.DataFrame({"tb_label": [1.0, -1.0, 0.0]})
        primary = pd.Series(["LONG", "LONG", "LONG"])
        meta = meta_labels_from_triple_barrier(tb, primary)
        assert meta.iloc[0] == 1.0
        assert meta.iloc[1] == 0.0
        assert np.isnan(meta.iloc[2])

    def test_hold_rows_are_nan(self):
        tb = pd.DataFrame({"tb_label": [1.0, 1.0]})
        primary = pd.Series(["LONG", "HOLD"])
        meta = meta_labels_from_triple_barrier(tb, primary)
        assert meta.iloc[0] == 1.0
        assert np.isnan(meta.iloc[1])
