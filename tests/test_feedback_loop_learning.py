import json

from agents.analytics import feedback_loop


def _trade(signal_type: str, result: str, pnl: float) -> dict:
    return {
        "signal_type": signal_type,
        "regime": "TRENDING",
        "rsi_at_entry": 45.0,
        "volume_above_avg": 1,
        "pnl_pct": pnl,
        "result": result,
        "confidence": 65.0,
    }


def test_group_win_rate_is_recency_weighted():
    # Newest trades come first. Make newest block mostly losses.
    recent_losses = [_trade("COMBINED", "LOSS", -1.0) for _ in range(10)]
    older_wins = [_trade("COMBINED", "WIN", 1.2) for _ in range(30)]
    trades = recent_losses + older_wins

    feedback_loop._attach_recency_weights(trades, half_life=6)
    stats = feedback_loop._group_win_rate(trades, "signal_type")
    combined = stats["COMBINED"]

    assert combined["win_rate_unweighted"] == 75.0
    assert combined["win_rate"] < combined["win_rate_unweighted"]


def test_weight_adapter_ignores_low_sample_updates(tmp_path):
    weights_path = tmp_path / "learned_weights.json"
    existing = dict(feedback_loop.DEFAULT_WEIGHTS)
    existing["RSI_BOUNCE"] = 1.4
    weights_path.write_text(json.dumps(existing), encoding="utf-8")

    old_path = feedback_loop._WEIGHTS_PATH
    try:
        feedback_loop._WEIGHTS_PATH = weights_path
        adapter = feedback_loop.WeightAdapter()
        insights = {
            "signal_type_stats": {
                "RSI_BOUNCE": {"win_rate": 10.0, "trades": 4, "weighted_trades": 3.0}
            },
            "overall": {"total_trades": 4, "effective_trades": 3.0, "win_rate": 10.0, "model_accuracy": 0.0, "reliability_score": 3.0},
            "best_signal_type": "RSI_BOUNCE",
            "worst_signal_type": "RSI_BOUNCE",
        }
        out = adapter.update_weights(insights)
        assert out["RSI_BOUNCE"] == 1.4
    finally:
        feedback_loop._WEIGHTS_PATH = old_path


def test_weight_adapter_caps_single_update_step(tmp_path):
    weights_path = tmp_path / "learned_weights.json"
    weights_path.write_text(json.dumps(feedback_loop.DEFAULT_WEIGHTS), encoding="utf-8")

    old_path = feedback_loop._WEIGHTS_PATH
    try:
        feedback_loop._WEIGHTS_PATH = weights_path
        adapter = feedback_loop.WeightAdapter()
        insights = {
            "signal_type_stats": {
                "MACD_CROSS": {"win_rate": 90.0, "trades": 140, "weighted_trades": 120.0}
            },
            "overall": {"total_trades": 140, "effective_trades": 120.0, "win_rate": 90.0, "model_accuracy": 80.0, "reliability_score": 95.0},
            "best_signal_type": "MACD_CROSS",
            "worst_signal_type": "COMBINED",
        }
        out = adapter.update_weights(insights)
        assert 1.0 < out["MACD_CROSS"] <= 1.2
    finally:
        feedback_loop._WEIGHTS_PATH = old_path


def test_confidence_calibration_reports_misaligned_when_high_band_underperforms():
    trades = []
    # Lower confidence band wins more
    for _ in range(12):
        trades.append({"confidence": 65.0, "result": "WIN", "pnl_pct": 1.0, "_w": 1.0})
    for _ in range(3):
        trades.append({"confidence": 65.0, "result": "LOSS", "pnl_pct": -1.0, "_w": 1.0})
    # Higher confidence band loses more
    for _ in range(4):
        trades.append({"confidence": 92.0, "result": "WIN", "pnl_pct": 1.0, "_w": 1.0})
    for _ in range(10):
        trades.append({"confidence": 92.0, "result": "LOSS", "pnl_pct": -1.0, "_w": 1.0})

    bands = feedback_loop._confidence_band_analysis(trades)
    summary = feedback_loop._confidence_calibration_summary(bands)

    assert summary["status"] == "MISALIGNED"
    assert summary["high_minus_low"] < 0


def test_confidence_calibration_reports_calibrated_when_monotonic():
    trades = []
    # 60-70
    for _ in range(8):
        trades.append({"confidence": 66.0, "result": "WIN", "pnl_pct": 1.0, "_w": 1.0})
    for _ in range(8):
        trades.append({"confidence": 66.0, "result": "LOSS", "pnl_pct": -1.0, "_w": 1.0})
    # 70-80
    for _ in range(11):
        trades.append({"confidence": 75.0, "result": "WIN", "pnl_pct": 1.0, "_w": 1.0})
    for _ in range(5):
        trades.append({"confidence": 75.0, "result": "LOSS", "pnl_pct": -1.0, "_w": 1.0})
    # 80-90
    for _ in range(13):
        trades.append({"confidence": 85.0, "result": "WIN", "pnl_pct": 1.0, "_w": 1.0})
    for _ in range(3):
        trades.append({"confidence": 85.0, "result": "LOSS", "pnl_pct": -1.0, "_w": 1.0})
    # 90+
    for _ in range(15):
        trades.append({"confidence": 93.0, "result": "WIN", "pnl_pct": 1.0, "_w": 1.0})
    for _ in range(1):
        trades.append({"confidence": 93.0, "result": "LOSS", "pnl_pct": -1.0, "_w": 1.0})

    bands = feedback_loop._confidence_band_analysis(trades)
    summary = feedback_loop._confidence_calibration_summary(bands)

    assert summary["status"] == "CALIBRATED"
    assert summary["is_monotonic"] is True
