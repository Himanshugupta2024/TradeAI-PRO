"""Regression locks for the dashboard defect sweep.

Each test here pins one honesty fix that used to produce a misleading UI:

* swing quality scores came from a field the scanner never emits,
* setup percentiles were derived from list position,
* the learning panel hid itself below the sample threshold,
* crypto signals leaked legacy ``BUY``/``SELL`` labels,
* a zero-trade day reported ``0.0%`` win rate.

They are deliberately pure: no DB, no network, no live API.
"""

from __future__ import annotations

import pytest

from agents.ai_engine.meme_predictor import MemePredictor
from agents.analytics import feedback_loop
from api import service as api_service


# ── swing quality score ──────────────────────────────────────────────────────

def test_swing_quality_score_falls_back_to_strength():
    # The swing scanner only emits ``strength``; ``confluence_score`` is a
    # ranked-payload field. Reading it first used to score every row 0.
    assert api_service._swing_quality_score({"strength": 82}) == (82, "strength")


def test_swing_quality_score_prefers_confluence_when_present():
    setup = {"confluence_score": 91.4, "strength": 40}
    assert api_service._swing_quality_score(setup) == (91, "confluence_score")


def test_swing_quality_score_is_zero_and_labelled_when_missing():
    # No candidate field must not become a fabricated 75/"A" grade.
    assert api_service._swing_quality_score({}) == (0, "missing")
    assert api_service._swing_quality_score({"strength": None, "score": "n/a"}) == (
        0,
        "missing",
    )


def test_swing_quality_score_clamps_and_ignores_non_positive():
    assert api_service._swing_quality_score({"strength": 140}) == (100, "strength")
    assert api_service._swing_quality_score({"strength": 0, "score": 55}) == (55, "score")


# ── setup percentiles ───────────────────────────────────────────────────────

def test_setup_percentiles_follow_score_not_list_position():
    setups = [
        {"symbol": "WEAK", "composite_rank": 10.0},
        {"symbol": "BEST", "composite_rank": 90.0},
        {"symbol": "MID", "composite_rank": 50.0},
    ]
    api_service._assign_setup_percentiles(setups)

    by_symbol = {s["symbol"]: s["setup_percentile"] for s in setups}
    assert by_symbol == {"WEAK": 0.0, "MID": 50.0, "BEST": 100.0}


def test_setup_percentiles_give_ties_the_same_value():
    setups = [
        {"symbol": "A", "composite_rank": 50.0},
        {"symbol": "B", "composite_rank": 50.0},
        {"symbol": "C", "composite_rank": 10.0},
    ]
    api_service._assign_setup_percentiles(setups)

    by_symbol = {s["symbol"]: s["setup_percentile"] for s in setups}
    assert by_symbol["A"] == by_symbol["B"]
    assert by_symbol["C"] == 0.0


def test_setup_percentiles_handle_empty_and_single():
    api_service._assign_setup_percentiles([])  # no crash, no rows

    single = [{"symbol": "ONLY", "composite_rank": 12.0}]
    api_service._assign_setup_percentiles(single)
    assert single[0]["setup_percentile"] == 100.0


# ── learning overlay ────────────────────────────────────────────────────────

def _learning_state(total: int, **overall) -> dict:
    stats = {"total_trades": total, "win_rate": 34.2}
    stats.update(overall)
    return {
        "insights": {
            "overall": stats,
            "best_signal_type": "COMBINED",
            "best_regime": "TRENDING",
        },
        "generated_at": "2026-11-13T10:00:00+05:30",
    }


def test_learning_overlay_flags_small_sample_instead_of_hiding():
    overlay = api_service._build_learning_overlay(_learning_state(3))

    assert overlay is not None
    assert overlay["small_sample"] is True
    assert overlay["sample_size"] == 3
    assert overlay["min_sample"] == api_service._LEARNING_MIN_SAMPLE
    assert "Only 3 closed trades" in overlay["advisory"]


def test_learning_overlay_marks_large_sample_as_trustworthy():
    overlay = api_service._build_learning_overlay(
        _learning_state(40, model_accuracy=61.0)
    )

    assert overlay["small_sample"] is False
    assert overlay["advisory"].startswith("Based on 40 closed trades")


def test_learning_overlay_labels_model_accuracy_even_when_absent():
    overlay = api_service._build_learning_overlay(
        _learning_state(12, model_accuracy=None)
    )

    # The number can be missing; the label must not be, or the UI implies
    # "model accuracy" in the plain sense.
    assert overlay["model_accuracy"] is None
    assert overlay["model_accuracy_label"] == "high-confidence subset win rate"


def test_learning_overlay_is_none_without_closed_trades():
    assert api_service._build_learning_overlay(_learning_state(0)) is None
    assert api_service._build_learning_overlay({}) is None


# ── crypto signal vocabulary ────────────────────────────────────────────────

@pytest.fixture()
def predictor() -> MemePredictor:
    return MemePredictor()


def test_aggregate_signal_emits_long(predictor):
    signal, confidence = predictor._aggregate_signal(
        {"momentum": 0.8}, {"momentum": 1.0}, {}
    )
    assert signal == "LONG"
    assert 0.5 <= confidence <= 0.95


def test_aggregate_signal_emits_short(predictor):
    signal, confidence = predictor._aggregate_signal(
        {"momentum": -0.8}, {"momentum": 1.0}, {}
    )
    assert signal == "SHORT"
    assert 0.5 <= confidence <= 0.95


def test_aggregate_signal_deadband_is_hold(predictor):
    signal, _ = predictor._aggregate_signal({"momentum": 0.1}, {"momentum": 1.0}, {})
    assert signal == "HOLD"


def test_aggregate_signal_damps_pump_chase_without_leaking_buy(predictor):
    features = {"pump_from_recent_low": 30.0, "volatility": 10.0}
    signal, confidence = predictor._aggregate_signal(
        {"momentum": 0.8}, {"momentum": 1.0}, features
    )
    assert signal in ("LONG", "HOLD")
    assert signal != "BUY"
    assert confidence < 0.82


# ── zero-trade EOD review ───────────────────────────────────────────────────

def test_eod_review_reports_no_win_rate_when_nothing_closed(monkeypatch, tmp_path):
    def _no_db():
        raise RuntimeError("no database in unit test")

    monkeypatch.setattr(feedback_loop, "_get_pg_session", _no_db)
    monkeypatch.setattr(feedback_loop, "_WEIGHTS_PATH", tmp_path / "weights.json")
    monkeypatch.setattr(
        feedback_loop.FeedbackAnalyser, "run", lambda self, lookback=500: {}
    )

    review = feedback_loop.FeedbackAnalyser().run_daily_eod_review("2026-11-13")

    assert review["today_trade_count"] == 0
    assert review["today_win_rate"] is None
    assert review["today_win_rate_label"] == "no closed trades"
