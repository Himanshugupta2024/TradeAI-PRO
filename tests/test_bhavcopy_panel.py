"""Tests for the bhavcopy panel, corporate-action adjustment and PIT universe."""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd

from data.bhavcopy_panel import (
    BhavcopyPanel,
    Membership,
    PointInTimeUniverse,
    adjust_for_corporate_actions,
)


def _daily(day, symbols=("ABC", "XYZ")):
    return pd.DataFrame(
        {
            "date": [pd.Timestamp(day)] * len(symbols),
            "symbol": list(symbols),
            "open": [100.0] * len(symbols),
            "high": [101.0] * len(symbols),
            "low": [99.0] * len(symbols),
            "close": [100.0] * len(symbols),
            "prev_close": [100.0] * len(symbols),
            "volume": [1000.0] * len(symbols),
            "turnover": [100000.0] * len(symbols),
        }
    )


class FakeProvider:
    def __init__(self, frames):
        self.frames = frames

    def fetch_daily_exact(self, target_date, allow_download=True):
        return self.frames.get(target_date, pd.DataFrame())


class TestPanelBuild:
    def test_loads_only_weekdays_with_data(self):
        frames = {
            date(2024, 1, 1): _daily("2024-01-01"),
            date(2024, 1, 2): _daily("2024-01-02"),
            date(2024, 1, 3): _daily("2024-01-03"),
        }
        panel = BhavcopyPanel(provider=FakeProvider(frames)).load(
            date(2024, 1, 1), date(2024, 1, 3), allow_download=False
        )
        assert len(panel) == 6
        assert set(panel["symbol"]) == {"ABC", "XYZ"}
        assert panel["date"].nunique() == 3

    def test_symbol_filter(self):
        frames = {date(2024, 1, 1): _daily("2024-01-01")}
        panel = BhavcopyPanel(provider=FakeProvider(frames)).load(
            date(2024, 1, 1), date(2024, 1, 1), symbols=["abc"], allow_download=False
        )
        assert len(panel) == 1
        assert panel.iloc[0]["symbol"] == "ABC"

    def test_weekend_range_returns_empty(self):
        panel = BhavcopyPanel(provider=FakeProvider({})).load(
            date(2024, 1, 6), date(2024, 1, 7), allow_download=False
        )
        assert panel.empty

    def test_to_wide(self):
        frames = {
            date(2024, 1, 1): _daily("2024-01-01"),
            date(2024, 1, 2): _daily("2024-01-02"),
        }
        panel = BhavcopyPanel(provider=FakeProvider(frames)).load(
            date(2024, 1, 1), date(2024, 1, 2), allow_download=False
        )
        wide = BhavcopyPanel.to_wide(panel, "close")
        assert wide.shape == (2, 2)
        assert list(wide.columns) == ["ABC", "XYZ"]


class TestCorporateActions:
    def _panel(self):
        rows = []
        for day in pd.date_range("2024-01-01", periods=5, freq="D"):
            rows.append(
                {
                    "date": day,
                    "symbol": "ABC",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "prev_close": 100.0,
                    "volume": 1000.0,
                    "turnover": 100000.0,
                }
            )
        return pd.DataFrame(rows)

    def test_split_back_adjusts_prices_before_ex_date(self):
        panel = self._panel()
        actions = pd.DataFrame(
            [{"symbol": "ABC", "ex_date": pd.Timestamp("2024-01-04"), "ratio": 2.0}]
        )
        adjusted = adjust_for_corporate_actions(panel, actions)

        before = adjusted[adjusted["date"] < pd.Timestamp("2024-01-04")]
        after = adjusted[adjusted["date"] >= pd.Timestamp("2024-01-04")]

        assert (before["close"] == 50.0).all()
        assert (before["volume"] == 2000.0).all()
        assert (after["close"] == 100.0).all()
        assert (after["volume"] == 1000.0).all()

    def test_action_without_matching_rows_is_noop(self):
        panel = self._panel()
        actions = pd.DataFrame(
            [{"symbol": "ZZZ", "ex_date": pd.Timestamp("2024-01-04"), "ratio": 2.0}]
        )
        adjusted = adjust_for_corporate_actions(panel, actions)
        pd.testing.assert_frame_equal(adjusted, panel)

    def test_invalid_ratio_is_ignored(self):
        panel = self._panel()
        actions = pd.DataFrame(
            [{"symbol": "ABC", "ex_date": pd.Timestamp("2024-01-04"), "ratio": 0.0}]
        )
        adjusted = adjust_for_corporate_actions(panel, actions)
        assert (adjusted["close"] == 100.0).all()

    def test_missing_columns_raise(self):
        panel = self._panel()
        actions = pd.DataFrame([{"symbol": "ABC"}])
        try:
            adjust_for_corporate_actions(panel, actions)
            assert False, "expected ValueError"
        except ValueError:
            pass


class TestPointInTimeUniverse:
    def _panel(self):
        rows = []
        for day in pd.date_range("2024-01-01", periods=5, freq="D"):
            for symbol in ("ABC", "XYZ"):
                rows.append(
                    {
                        "date": day,
                        "symbol": symbol,
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.0,
                        "prev_close": 100.0,
                        "volume": 1000.0,
                        "turnover": 100000.0,
                    }
                )
        return pd.DataFrame(rows)

    def test_symbols_on_respects_window(self):
        universe = PointInTimeUniverse(
            [
                Membership("ABC", date(2024, 1, 1), date(2024, 1, 3)),
                Membership("XYZ", date(2024, 1, 4), None),
            ]
        )
        assert universe.symbols_on(date(2024, 1, 2)) == {"ABC"}
        assert universe.symbols_on(date(2024, 1, 4)) == {"XYZ"}

    def test_filter_drops_rows_outside_window(self):
        panel = self._panel()
        universe = PointInTimeUniverse(
            [
                Membership("ABC", date(2024, 1, 1), date(2024, 1, 3)),
                Membership("XYZ", date(2024, 1, 4), None),
            ]
        )
        filtered = universe.filter(panel)
        assert len(filtered) == 5  # ABC on 1-3 + XYZ on 4-5
        assert not ((filtered["symbol"] == "ABC") & (filtered["date"] > pd.Timestamp("2024-01-03"))).any()

    def test_from_records(self):
        universe = PointInTimeUniverse.from_records(
            [{"symbol": "abc", "start": "2024-01-01", "end": "2024-02-01"}]
        )
        assert universe.symbols_on(date(2024, 1, 15)) == {"ABC"}
        assert universe.symbols_on(date(2024, 2, 2)) == set()

    def test_membership_validates_dates(self):
        try:
            Membership("ABC", date(2024, 2, 1), date(2024, 1, 1))
            assert False, "expected ValueError"
        except ValueError:
            pass
