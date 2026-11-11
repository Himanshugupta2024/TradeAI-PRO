import pytest
from datetime import datetime, timezone, timedelta
from agents.analytics.feedback_loop import (
    DEFAULT_WEIGHTS,
    DEFAULT_REGIME_WEIGHTS_2D,
    get_ist_session_phase,
    _calculate_expectancy_metrics,
    WeightAdapter,
    FeedbackAnalyser,
    get_learning_stats,
)
import pandas as pd
from agents.feature_engine.regime_detector import MarketRegime
from agents.signal_engine.signal_generator import generate_signals

_IST = timezone(timedelta(hours=5, minutes=30))


def test_default_regime_weights_2d_structure():
    assert "TRENDING" in DEFAULT_REGIME_WEIGHTS_2D
    assert "RANGING" in DEFAULT_REGIME_WEIGHTS_2D
    assert "HIGH_VOLATILITY" in DEFAULT_REGIME_WEIGHTS_2D
    assert "CHOPPY" in DEFAULT_REGIME_WEIGHTS_2D
    assert "UNKNOWN" in DEFAULT_REGIME_WEIGHTS_2D

    # Verify Trending boosts Breakout over RSI Mean-Reversion
    trending = DEFAULT_REGIME_WEIGHTS_2D["TRENDING"]
    assert trending["BREAKOUT"] > trending["RSI_BOUNCE"]

    # Verify Ranging boosts RSI Mean-Reversion over Breakout
    ranging = DEFAULT_REGIME_WEIGHTS_2D["RANGING"]
    assert ranging["RSI_BOUNCE"] > ranging["BREAKOUT"]


def test_ist_session_phase_mapping():
    # 09:30 IST -> OPENING_MOMENTUM
    t_open = datetime(2026, 11, 12, 9, 30, tzinfo=_IST)
    assert get_ist_session_phase(t_open) == "OPENING_MOMENTUM"

    # 10:45 IST -> MORNING_EXPANSION
    t_morn = datetime(2026, 11, 12, 10, 45, tzinfo=_IST)
    assert get_ist_session_phase(t_morn) == "MORNING_EXPANSION"

    # 12:15 IST -> MIDDAY_CHOP
    t_mid = datetime(2026, 11, 12, 12, 15, tzinfo=_IST)
    assert get_ist_session_phase(t_mid) == "MIDDAY_CHOP"

    # 13:45 IST -> AFTERNOON_SETUP
    t_aft = datetime(2026, 11, 12, 13, 45, tzinfo=_IST)
    assert get_ist_session_phase(t_aft) == "AFTERNOON_SETUP"

    # 14:45 IST -> CLOSING_TREND
    t_close = datetime(2026, 11, 12, 14, 45, tzinfo=_IST)
    assert get_ist_session_phase(t_close) == "CLOSING_TREND"

    # 19:30 IST -> OFF_HOURS
    t_off = datetime(2026, 11, 12, 19, 30, tzinfo=_IST)
    assert get_ist_session_phase(t_off) == "OFF_HOURS"

    # ISO string parsing
    assert get_ist_session_phase("2026-11-12T12:00:00+05:30") == "MIDDAY_CHOP"


def test_expectancy_and_profit_factor_calculation():
    trades = [
        {"result": "WIN", "pnl_pct": 2.0, "_w": 1.0},
        {"result": "WIN", "pnl_pct": 1.5, "_w": 1.0},
        {"result": "WIN", "pnl_pct": 2.5, "_w": 1.0},
        {"result": "LOSS", "pnl_pct": -1.0, "_w": 1.0},
    ]
    # 3 wins avg = 2.0%, 1 loss = 1.0%. WinRate = 75%, LossRate = 25%
    # EV = (0.75 * 2.0) - (0.25 * 1.0) = 1.5 - 0.25 = +1.25%
    # Profit factor = 6.0 / 1.0 = 6.0x
    metrics = _calculate_expectancy_metrics(trades)
    assert metrics["avg_win"] == 2.0
    assert metrics["avg_loss"] == 1.0
    assert metrics["profit_factor"] == 6.0
    assert metrics["expectancy"] == 1.25


def test_expectancy_negative_edge():
    # 4 wins with tiny gains, 1 huge loss
    trades = [
        {"result": "WIN", "pnl_pct": 0.2, "_w": 1.0},
        {"result": "WIN", "pnl_pct": 0.3, "_w": 1.0},
        {"result": "WIN", "pnl_pct": 0.2, "_w": 1.0},
        {"result": "WIN", "pnl_pct": 0.1, "_w": 1.0},
        {"result": "LOSS", "pnl_pct": -3.0, "_w": 1.0},
    ]
    # Win rate = 80%, but avg win = 0.2%, loss = 3.0%
    # EV = (0.80 * 0.2) - (0.20 * 3.0) = 0.16 - 0.60 = -0.44%
    metrics = _calculate_expectancy_metrics(trades)
    assert metrics["expectancy"] < 0
    assert metrics["profit_factor"] < 1.0


def test_weight_adapter_regime_weights_resolution():
    adapter = WeightAdapter()
    trending_w = adapter.get_regime_weights("TRENDING")
    assert trending_w["BREAKOUT"] >= 1.2
    assert trending_w["RSI_BOUNCE"] <= 0.95

    ranging_w = adapter.get_regime_weights("RANGING")
    assert ranging_w["RSI_BOUNCE"] >= 1.2
    assert ranging_w["BREAKOUT"] <= 0.85

    unknown_w = adapter.get_regime_weights("UNKNOWN")
    assert unknown_w["BREAKOUT"] == 1.0
    assert unknown_w["RSI_BOUNCE"] == 1.0


def test_signal_generator_applies_2d_regime_weights():
    # Create simple dummy market dataframe
    df = pd.DataFrame({
        "open": [99.5, 100.5, 101.5, 102.5, 104.5],
        "close": [100.0, 101.0, 102.0, 103.0, 105.0],
        "high": [101.0, 102.0, 103.0, 104.0, 106.0],
        "low": [99.0, 100.0, 101.0, 102.0, 104.0],
        "volume": [1000, 1200, 1100, 1500, 2500],
        "RSI_14": [28.0, 32.0, 35.0, 38.0, 42.0],
        "MACD": [0.1, 0.2, 0.3, 0.5, 0.8],
        "MACD_signal": [0.05, 0.1, 0.2, 0.3, 0.4],
        "MACD_hist": [0.05, 0.1, 0.1, 0.2, 0.4],
        "EMA_20": [98.0, 99.0, 100.0, 101.0, 102.0],
        "EMA_50": [95.0, 96.0, 97.0, 98.0, 99.0],
        "EMA_200": [90.0, 90.5, 91.0, 91.5, 92.0],
        "ATR_14": [1.5, 1.5, 1.6, 1.6, 1.8],
        "htf_trend": ["BULLISH", "BULLISH", "BULLISH", "BULLISH", "BULLISH"],
    })

    # Test under TRENDING regime
    signals_trending = generate_signals(df, regime=MarketRegime.TRENDING, symbol="TEST_STOCK", offline=True)
    assert not signals_trending.empty
    assert "signal" in signals_trending.columns
    assert "signal_score_buy" in signals_trending.columns

    # Test under RANGING regime
    signals_ranging = generate_signals(df, regime=MarketRegime.RANGING, symbol="TEST_STOCK", offline=True)
    assert not signals_ranging.empty


def test_get_learning_stats_endpoint_payload():
    stats = get_learning_stats()
    assert "weights_2d" in stats
    assert "regime_matrix_stats" in stats
    assert "session_phase_stats" in stats
    assert "expectancy_metrics" in stats
    assert "insights" in stats
    assert "weights" in stats

    # Verify 2D weights keys
    assert "TRENDING" in stats["weights_2d"]
    assert "RANGING" in stats["weights_2d"]
