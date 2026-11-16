"""
Unit tests for Calibration Drift Monitor.
Verifies that:
  1. Insufficient sample sizes return status INSUFFICIENT_SAMPLE.
  2. Aligned probabilities return CALIBRATION_HEALTHY with drift_detected=False.
  3. Overconfident predictions (e.g. 85% probability on 25% win rate) trigger drift_detected=True.
  4. Rows carrying only the 0-100 heuristic `confidence` never look calibrated.
  5. `probability_from_signal` refuses the synthetic 50.0 sentinel and `confidence`.

The monitor's input is `p_calibrated`, a real 0-1 probability persisted at
entry. `confidence` is a separate heuristic and must not be promoted to a
probability by the monitor or by the entry points.
"""

import pytest
from agents.ai_engine.calibrator import (
    check_live_calibration_drift,
    probability_from_signal,
)


def test_insufficient_sample():
    trades = [
        {"p_calibrated": 0.75, "status": "CLOSED_TP", "pnl": 100.0},
        {"p_calibrated": 0.60, "status": "CLOSED_SL", "pnl": -50.0},
    ]
    res = check_live_calibration_drift(trades, min_sample_size=20)
    assert res["status"] == "INSUFFICIENT_SAMPLE"
    assert res["drift_detected"] is False
    assert res["sample_size"] == 2


def test_healthy_calibration():
    # 20 trades predicted at 60% win probability with 12 wins and 8 losses.
    trades = []
    for _ in range(12):
        trades.append({"p_calibrated": 0.60, "status": "CLOSED_TP", "pnl": 100.0})
    for _ in range(8):
        trades.append({"p_calibrated": 0.60, "status": "CLOSED_SL", "pnl": -80.0})

    res = check_live_calibration_drift(trades, threshold_ece=0.10, min_sample_size=20)
    assert res["sample_size"] == 20
    assert res["drift_detected"] is False
    assert "CALIBRATION_HEALTHY" in res["recommendation"]
    assert res["brier_score"] < 0.30
    assert res["heuristic_only"] == 0


def test_overconfident_calibration_drift():
    # Model predicts 85% win probability, realised win rate is 25% (5-15).
    trades = []
    for _ in range(5):
        trades.append({"p_calibrated": 0.85, "status": "CLOSED_TP", "pnl": 100.0})
    for _ in range(15):
        trades.append({"p_calibrated": 0.85, "status": "CLOSED_SL", "pnl": -100.0})

    res = check_live_calibration_drift(trades, threshold_ece=0.10, min_sample_size=20)
    assert res["sample_size"] == 20
    assert res["drift_detected"] is True
    assert "TRIGGER_RETRAIN" in res["recommendation"]
    assert res["ece"] > 0.40  # Massive calibration error


def test_heuristic_confidence_is_never_a_probability():
    """A monitor fed only heuristic rows must stay sample-starved, not healthy.

    This is the regression guard for the old bug: `confidence` was divided by
    100 and treated as a prediction, so a report could claim CALIBRATION_HEALTHY
    from data that never carried a probability.
    """
    trades = []
    for _ in range(12):
        trades.append({"confidence": 60.0, "status": "CLOSED_TP", "pnl": 100.0})
    for _ in range(8):
        trades.append({"confidence": 60.0, "status": "CLOSED_SL", "pnl": -80.0})

    res = check_live_calibration_drift(trades, min_sample_size=20)
    assert res["sample_size"] == 0
    assert res["status"] == "INSUFFICIENT_SAMPLE"
    assert res["heuristic_only"] == 20
    assert "CALIBRATION_HEALTHY" not in res["recommendation"]
    assert "heuristic confidence score" in res["recommendation"]


def test_probability_from_signal_prefers_p_calibrated():
    prob, source = probability_from_signal(
        {"p_calibrated": 0.63, "p_source": "ensemble_calibrator", "confidence": 91.0}
    )
    assert prob == pytest.approx(0.63)
    assert source == "ensemble_calibrator"


def test_probability_from_signal_accepts_percentage_field():
    prob, source = probability_from_signal(
        {"calibrated_trade_win_probability": 62.5, "calibration_active": True}
    )
    assert prob == pytest.approx(0.625)
    assert source == "calibrated_trade_win_probability"


def test_probability_from_signal_rejects_inactive_sentinel():
    """`calibration_active: False` means the 50.0 is synthetic compression."""
    prob, source = probability_from_signal(
        {"calibrated_trade_win_probability": 50.0, "calibration_active": False}
    )
    assert prob is None
    assert source.endswith("_inactive")


def test_probability_from_signal_ignores_heuristic_confidence():
    prob, source = probability_from_signal({"confidence": 88.0})
    assert prob is None
    assert source == "unavailable"


def test_probability_from_signal_handles_missing_payload():
    assert probability_from_signal(None) == (None, "unavailable")
    assert probability_from_signal({}) == (None, "unavailable")


def test_breakeven_trades_included_and_brier_skill_score():
    """Verify breakeven trades (pnl=0) are accounted for as outcome=0 and brier skill score is computed."""
    trades = []
    # 10 wins at 0.60
    for _ in range(10):
        trades.append({"p_calibrated": 0.60, "status": "CLOSED_TP", "pnl": 150.0})
    # 10 breakeven exits at 0.60
    for _ in range(10):
        trades.append({"p_calibrated": 0.60, "status": "CLOSED_BE", "pnl": 0.0})

    res = check_live_calibration_drift(trades, min_sample_size=20, include_breakeven=True)
    assert res["sample_size"] == 20
    assert res["status"] in ("CALIBRATION_HEALTHY", "DRIFT_DETECTED")
    assert res["breakeven_included"] == 10
    assert "brier_benchmark" in res
    assert "brier_skill_score" in res
    assert res["realized_win_rate"] == pytest.approx(0.50)

