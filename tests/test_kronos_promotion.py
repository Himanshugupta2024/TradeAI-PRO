"""Tests for the offline Kronos promotion evaluator."""

import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.kronos_promotion import (  # noqa: E402
    MIN_PRIOR_FOR_PROB,
    attach_walk_forward_probabilities,
    build_pairs,
    main,
)


def _linear_panel(rows: int = 300, symbol: str = "TEST") -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=rows)
    close = [100.0 + i for i in range(rows)]
    return pd.DataFrame({"symbol": symbol, "date": dates, "close": close})


def _record(panel: pd.DataFrame, index: int, expected: float = 1.0) -> dict:
    return {
        "symbol": "TEST",
        "as_of": panel["date"].iloc[index].isoformat(),
        "pred_len": 5,
        "last_close": float(panel["close"].iloc[index]),
        "kronos_expected_return_pct": expected,
    }


def test_build_pairs_uses_momentum_and_forward_return():
    panel = _linear_panel()
    pairs = build_pairs([_record(panel, 260)], panel)

    assert len(pairs) == 1
    row = pairs[0]
    assert row["symbol"] == "TEST"
    assert row["horizon"] == 5
    # 12-1 momentum: close[239] / close[8] - 1
    assert row["champion_score"] == 1.0
    # Forward 5 bars: close[265] / close[260] - 1, in percent
    assert row["forward_return_pct"] == pytest.approx(5.0 / 360.0 * 100.0)
    assert row["challenger_score"] == 1.0


def test_build_pairs_skips_symbols_without_enough_history():
    panel = _linear_panel(rows=100)
    pairs = build_pairs([_record(panel, 60)], panel)
    assert pairs == []


def test_build_pairs_skips_unknown_dates_and_symbols():
    panel = _linear_panel()
    records = [
        {"symbol": "NOPE", "as_of": panel["date"].iloc[260].isoformat(), "pred_len": 5,
         "kronos_expected_return_pct": 1.0},
        {"symbol": "TEST", "as_of": "1999-01-01", "pred_len": 5, "kronos_expected_return_pct": 1.0},
        {"symbol": "TEST", "as_of": panel["date"].iloc[260].isoformat(), "pred_len": 5},
    ]
    assert build_pairs(records, panel) == []


def test_build_pairs_leaves_unresolved_forward_returns_empty():
    panel = _linear_panel()
    pairs = build_pairs([_record(panel, len(panel) - 2)], panel)
    assert len(pairs) == 1
    assert pairs[0]["forward_return_pct"] is None


def test_walk_forward_probabilities_have_no_lookahead():
    pairs = [
        {
            "as_of": f"2025-01-{day:02d}",
            "champion_score": 1.0,
            "challenger_score": 1.0,
            "forward_return_pct": 1.0,
        }
        for day in range(1, MIN_PRIOR_FOR_PROB + 6)
    ]
    attach_walk_forward_probabilities(pairs)

    assert all("champion_prob_up" not in row for row in pairs[:MIN_PRIOR_FOR_PROB])
    later = pairs[MIN_PRIOR_FOR_PROB]
    assert later["champion_prob_up"] == pytest.approx(0.95)  # clipped after 20/20 hits
    assert later["challenger_prob_up"] == pytest.approx(0.95)


def test_cli_fails_closed_when_challenger_matches_champion(tmp_path):
    panel = _linear_panel()
    panel_path = tmp_path / "panel.csv"
    panel.to_csv(panel_path, index=False)

    records = [_record(panel, index) for index in range(250, 295)]
    forecast_path = tmp_path / "forecasts.json"
    forecast_path.write_text(json.dumps({"records": records}), encoding="utf-8")

    exit_code = main(["--forecasts", str(forecast_path), "--panel", str(panel_path)])
    assert exit_code == 1


def test_cli_rejects_bad_input(tmp_path):
    bad_panel = tmp_path / "panel.csv"
    bad_panel.write_text("symbol,date\nTEST,2024-01-01\n", encoding="utf-8")
    forecast = tmp_path / "forecasts.json"
    forecast.write_text(json.dumps({"records": []}), encoding="utf-8")
    assert main(["--forecasts", str(forecast), "--panel", str(bad_panel)]) == 2
