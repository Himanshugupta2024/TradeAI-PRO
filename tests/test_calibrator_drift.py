"""
Unit tests for Calibration Drift Monitor.
Verifies that:
  1. Insufficient sample sizes return status INSUFFICIENT_SAMPLE.
  2. Aligned probabilities return CALIBRATION_HEALTHY with drift_detected=False.
  3. Overconfident predictions (e.g. 80% confidence on 40% win rate) trigger drift_detected=True.
"""

import pytest
from agents.ai_engine.calibrator import check_live_calibration_drift


def test_insufficient_sample():
    trades = [
        {"confidence": 75.0, "status": "CLOSED_TP", "pnl": 100.0},
        {"confidence": 60.0, "status": "CLOSED_SL", "pnl": -50.0},
    ]
    res = check_live_calibration_drift(trades, min_sample_size=20)
    assert res["status"] == "INSUFFICIENT_SAMPLE"
    assert res["drift_detected"] is False


def test_healthy_calibration():
    # 20 trades predicted at 60% confidence with 12 wins and 8 losses (realized 60% win rate)
    trades = []
    for i in range(12):
        trades.append({"confidence": 60.0, "status": "CLOSED_TP", "pnl": 100.0})
    for i in range(8):
        trades.append({"confidence": 60.0, "status": "CLOSED_SL", "pnl": -80.0})

    res = check_live_calibration_drift(trades, threshold_ece=0.10, min_sample_size=20)
    assert res["sample_size"] == 20
    assert res["drift_detected"] is False
    assert "CALIBRATION_HEALTHY" in res["recommendation"]
    assert res["brier_score"] < 0.30


def test_overconfident_calibration_drift():
    # 20 trades where model predicts 85% win probability, but actual win rate is only 25% (5 wins, 15 losses)
    trades = []
    for i in range(5):
        trades.append({"confidence": 85.0, "status": "CLOSED_TP", "pnl": 100.0})
    for i in range(15):
        trades.append({"confidence": 85.0, "status": "CLOSED_SL", "pnl": -100.0})

    res = check_live_calibration_drift(trades, threshold_ece=0.10, min_sample_size=20)
    assert res["sample_size"] == 20
    assert res["drift_detected"] is True
    assert "TRIGGER_RETRAIN" in res["recommendation"]
    assert res["ece"] > 0.40  # Massive calibration error
