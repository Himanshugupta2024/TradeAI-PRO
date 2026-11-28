"""Unit tests for Priority 3 signal parity, backtester gap handling, and calibration activation."""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_backtester_gap_through_stop_never_produces_ghost_profit():
    from agents.backtester.engine import Backtester

    ts = pd.date_range("2026-11-02 09:15", periods=4, freq="D")
    # Signal LONG on bar 0 at close=100, sl=95, tp=110.
    # Bar 1 gaps down below stop loss (open=90, high=91, low=89, close=90.5).
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0, 90.0, 90.5, 91.0],
            "high": [101.0, 91.0, 91.5, 92.0],
            "low": [99.0, 89.0, 89.5, 90.0],
            "close": [100.0, 90.5, 91.0, 91.5],
            "volume": [10000.0] * 4,
            "signal": ["LONG", "HOLD", "HOLD", "HOLD"],
            "stop_loss": [95.0, np.nan, np.nan, np.nan],
            "target": [110.0, np.nan, np.nan, np.nan],
        }
    )

    res = Backtester(initial_capital=100_000.0, slippage_pct=0.05).run(df)
    assert res.total_trades == 1
    trade = res.trades[0]
    assert trade.exit_reason == "STOP_LOSS"
    assert trade.exit_price <= trade.entry_price
    assert trade.net_pnl_inr < 0


def test_signal_generator_mtf_score_only_rewards_matching_direction(monkeypatch):
    from agents.feature_engine.indicators import calculate_indicators
    from agents.feature_engine.support_resistance import calculate_support_resistance
    from agents.feature_engine.trend import classify_trend
    from agents.signal_engine.signal_generator import generate_signals

    rng = np.random.default_rng(7)
    n = 120
    close = 100.0 + np.cumsum(rng.normal(0.05, 0.5, n))
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="h"),
            "open": close - 0.1,
            "high": close + 0.6,
            "low": close - 0.6,
            "close": close,
            "volume": np.linspace(5000, 15000, n),
        }
    )
    df = classify_trend(calculate_support_resistance(calculate_indicators(df)))
    fixed_conf = pd.Series(50.0, index=df.index)

    # Strong bearish mtf_score (-100) must NOT boost LONG candidates.
    out_bear_mtf = generate_signals(df, ai_confidence=fixed_conf, mtf_score=-100.0, offline=True)
    out_zero_mtf = generate_signals(df, ai_confidence=fixed_conf, mtf_score=0.0, offline=True)

    long_rows = out_zero_mtf["signal"] == "LONG"
    if long_rows.any():
        assert (out_bear_mtf.loc[long_rows, "confidence"] <= out_zero_mtf.loc[long_rows, "confidence"]).all()

    hold_rows = (out_zero_mtf["signal"] == "HOLD") & (out_bear_mtf["signal"] == "HOLD")
    if hold_rows.any():
        assert (out_bear_mtf.loc[hold_rows, "confidence"] == out_zero_mtf.loc[hold_rows, "confidence"]).all()


def test_signal_generator_offline_deterministic_across_wall_clock_midday(monkeypatch):
    from agents.analytics import feedback_loop
    from agents.feature_engine.indicators import calculate_indicators
    from agents.feature_engine.support_resistance import calculate_support_resistance
    from agents.feature_engine.trend import classify_trend
    from agents.signal_engine.signal_generator import generate_signals

    n = 120
    close = 200.0 + np.sin(np.linspace(0, 6, n)) * 3.0
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="D"),
            "open": close - 0.5,
            "high": close + 1.5,
            "low": close - 1.5,
            "close": close,
            "volume": np.full(n, 10000.0),
        }
    )
    df = classify_trend(calculate_support_resistance(calculate_indicators(df)))

    orig_phase = feedback_loop.get_ist_session_phase

    def _fake_phase(dt=None):
        if dt is None:
            return "MIDDAY_CHOP"
        return orig_phase(dt)

    monkeypatch.setattr(feedback_loop, "get_ist_session_phase", _fake_phase)
    res_chop = generate_signals(df, offline=True)

    def _fake_phase_open(dt=None):
        if dt is None:
            return "OPENING_MOMENTUM"
        return orig_phase(dt)

    monkeypatch.setattr(feedback_loop, "get_ist_session_phase", _fake_phase_open)
    res_open = generate_signals(df, offline=True)

    assert (res_chop["signal"].values == res_open["signal"].values).all()
    assert (res_chop["signal_score_buy"].values == res_open["signal_score_buy"].values).all()


def test_ensemble_activates_meta_and_calibrator_without_lstm(monkeypatch):
    import agents.ai_engine.calibrator as cal_mod
    import agents.ai_engine.ensemble as ens_mod
    import agents.ai_engine.lightgbm_model as lgb_mod
    import agents.ai_engine.logistic_baseline as log_mod
    import agents.ai_engine.meta_learner as meta_mod
    import agents.ai_engine.xgboost_model as xgb_mod

    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "false")
    monkeypatch.setenv("ENABLE_PYTORCH_MODELS", "false")
    monkeypatch.delenv("ML_SIDECAR_URL", raising=False)

    class _DummyPred:
        is_fitted = True

        def predict_row(self, _df):
            return {"direction": "UP", "confidence": 68.0, "prob_up": 74.0, "prob_down": 26.0}

    class _DummyMeta:
        is_fitted = True

        def predict_meta(self, _x):
            return {"meta_active": True, "meta_prob_up": 76.0, "model_disagreement": 0.1}

    class _DummyCal:
        is_fitted = True

        def calibrate(self, _x):
            return np.array([0.71])

    monkeypatch.setattr(xgb_mod, "get_xgb_predictor", lambda: _DummyPred())
    monkeypatch.setattr(lgb_mod, "get_lgbm_predictor", lambda: _DummyPred())
    monkeypatch.setattr(log_mod, "get_logistic_predictor", lambda: _DummyPred())
    monkeypatch.setattr(meta_mod, "get_meta_learner", lambda: _DummyMeta())
    monkeypatch.setattr(cal_mod, "get_calibrator", lambda: _DummyCal())

    n = 60
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-11-01 09:15", periods=n, freq="5min"),
            "open": np.linspace(100, 105, n),
            "high": np.linspace(101, 106, n),
            "low": np.linspace(99, 104, n),
            "close": np.linspace(100.5, 105.5, n),
            "volume": np.full(n, 5000.0),
        }
    )

    res = ens_mod.predict_advanced(df, symbol="CALIB_NO_LSTM", offline=True, force_refresh=True)
    assert res["meta_active"] is True
    assert res["calibration_active"] is True
    assert res["calibrated_trade_win_probability"] == 71.0

    # For bearish direction, win prob aligns to 1 - P(UP) rather than returning raw P(UP)
    class _DummyBearMeta:
        is_fitted = True

        def predict_meta(self, _x):
            return {"meta_active": True, "meta_prob_up": 22.0, "model_disagreement": 0.1}

    class _DummyBearCal:
        is_fitted = True

        def calibrate(self, _x):
            return np.array([0.24])

    monkeypatch.setattr(meta_mod, "get_meta_learner", lambda: _DummyBearMeta())
    monkeypatch.setattr(cal_mod, "get_calibrator", lambda: _DummyBearCal())
    res_dn = ens_mod.predict_advanced(df, symbol="CALIB_NO_LSTM_BEAR", offline=True, force_refresh=True)
    assert res_dn["direction"] == "DOWN"
    assert res_dn["calibration_active"] is True
    assert res_dn["calibrated_trade_win_probability"] == 76.0


def test_train_stacking_and_calibrator_pipeline_runs_cleanly(monkeypatch, tmp_path):
    from agents.feature_engine.indicators import calculate_indicators
    from agents.feature_engine.support_resistance import calculate_support_resistance
    from agents.feature_engine.trend import classify_trend
    from agents.signal_engine.signal_generator import generate_signals
    import agents.ai_engine.logistic_baseline as log_mod
    import agents.ai_engine.meta_learner as meta_mod
    import agents.ai_engine.calibrator as cal_mod
    import scripts.train_and_publish as tap

    rng = np.random.default_rng(123)
    n = 320
    close = 500.0 + np.cumsum(rng.normal(0.1, 2.0, n))
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=n, freq="D"),
            "open": close - 0.5,
            "high": close + 2.5,
            "low": close - 2.5,
            "close": close,
            "volume": rng.integers(5000, 30000, size=n).astype(float),
        }
    )
    df = classify_trend(calculate_support_resistance(calculate_indicators(df)))
    df = generate_signals(df, offline=True)

    monkeypatch.setattr(log_mod, "DEFAULT_LOGISTIC_MODEL_PATH", str(tmp_path / "logistic_model.pkl"))
    monkeypatch.setattr(log_mod, "DEFAULT_LOGISTIC_SCALER_PATH", str(tmp_path / "logistic_scaler.pkl"))
    monkeypatch.setattr(meta_mod, "DEFAULT_META_MODEL_PATH", str(tmp_path / "meta_learner.pkl"))
    monkeypatch.setattr(cal_mod, "DEFAULT_CALIBRATOR_PATH", str(tmp_path / "calibrator.pkl"))

    # Patch constructors so train_stacking_and_calibrator writes into tmp_path
    orig_log = log_mod.LogisticBaselinePredictor
    orig_meta = meta_mod.MetaStackingLearner
    orig_cal = cal_mod.ProbabilityCalibrator
    monkeypatch.setattr(
        log_mod,
        "LogisticBaselinePredictor",
        lambda: orig_log(model_path=str(tmp_path / "logistic_model.pkl"), scaler_path=str(tmp_path / "logistic_scaler.pkl")),
    )
    monkeypatch.setattr(
        meta_mod,
        "MetaStackingLearner",
        lambda: orig_meta(model_path=str(tmp_path / "meta_learner.pkl")),
    )
    monkeypatch.setattr(
        cal_mod,
        "ProbabilityCalibrator",
        lambda: orig_cal(model_path=str(tmp_path / "calibrator.pkl")),
    )

    out = tap.train_stacking_and_calibrator([df], embargo_bars=5, round_trip_cost_pct=0.0025)
    assert out["logistic_fitted"] is True
    assert out["meta_learner_fitted"] is True
    assert out["calibrator_fitted"] is True
    assert (tmp_path / "calibrator.pkl").exists()
    assert 0.0 <= out["brier_score"] <= 1.0
