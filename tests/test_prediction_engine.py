"""
Tests for TradeAI PRO 6-Layer Advanced Stock Price Prediction Engine.
====================================================================
Covers:
  - 3-class probability distribution summing to 100.0%
  - Range forecaster invariant: lower_range < reference_price < upper_range
  - Supertrend & Volume Profile calculation
  - Walk-forward backtest embargo non-leakage verification
  - Indian transaction cost calculation (STT, GST, brokerage, slippage)
  - API endpoint integration: GET /api/prediction/{symbol}, POST /api/prediction/batch, status
"""

import math
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from agents.ai_engine.prediction_engine import (
    calculate_supertrend,
    calculate_volume_profile,
    compute_3class_probabilities,
    predict_stock_price,
)
from agents.ai_engine.range_forecaster import calculate_price_range
from agents.ai_engine.backtest_validator import (
    calculate_indian_transaction_costs,
    compute_multiclass_brier_score,
    label_3class_swing,
    run_walk_forward_backtest,
)
from server import app


def test_probability_distribution_sums_to_100():
    """Verify P(UP) + P(DOWN) + P(SIDEWAYS) == 100.0% across diverse market regimes."""
    test_cases = [
        # (close, ema20, ema50, ema200, rsi, macd_hist, supertrend_dir, adx, vol_ratio, rs_20d, vix)
        (260.0, 255.0, 250.0, 230.0, 65.0, 1.5, 1, 32.0, 1.5, 4.2, 14.0),   # Strong Bullish
        (200.0, 205.0, 215.0, 240.0, 32.0, -2.0, -1, 35.0, 1.8, -5.0, 19.5), # Strong Bearish
        (150.0, 150.5, 149.8, 148.0, 50.0, 0.05, 1, 15.0, 0.9, 0.1, 11.5),  # Low Vol Compression
        (180.0, 178.0, 182.0, 175.0, 48.0, -0.2, -1, 18.0, 1.1, -1.2, 26.0), # Panic High VIX
        (300.0, 290.0, 280.0, 250.0, 85.0, 3.5, 1, 45.0, 2.5, 10.5, 15.0),  # Extreme Overbought
    ]

    for tc in test_cases:
        p_up, p_down, p_side, conf = compute_3class_probabilities(*tc)
        total = round(p_up + p_down + p_side, 1)
        assert total == 100.0, f"Probabilities do not sum to 100.0%: {p_up} + {p_down} + {p_side} = {total}"
        assert 0.0 <= p_up <= 100.0
        assert 0.0 <= p_down <= 100.0
        assert 0.0 <= p_side <= 100.0
        assert 45.0 <= conf <= 95.0


def test_range_forecaster_invariants():
    """Verify lower_range < reference_price < upper_range and target/SL logic."""
    ref = 263.88
    atr = 5.2

    # Bullish scenario
    bull_res = calculate_price_range(
        reference_price=ref,
        atr14=atr,
        prob_up=62.0,
        prob_down=23.0,
        prob_sideways=15.0,
        supports=[255.0, 250.0],
        resistances=[275.0, 285.0],
        vix_level=14.5,
    )

    assert bull_res["lower_range"] < ref < bull_res["upper_range"]
    assert bull_res["scenario"] == "BULLISH"
    assert bull_res["expected_target"] > ref
    assert bull_res["stop_loss"] < ref
    assert bull_res["range_width_pct"] > 0

    # Bearish scenario
    bear_res = calculate_price_range(
        reference_price=ref,
        atr14=atr,
        prob_up=18.0,
        prob_down=65.0,
        prob_sideways=17.0,
        supports=[250.0],
        resistances=[270.0],
        vix_level=20.0,
    )

    assert bear_res["lower_range"] < ref < bear_res["upper_range"]
    assert bear_res["scenario"] == "BEARISH"
    assert bear_res["expected_target"] < ref
    assert bear_res["stop_loss"] > ref


def test_supertrend_calculation():
    """Verify Supertrend values and direction values (+1 / -1)."""
    np.random.seed(42)
    n = 60
    close = np.linspace(100, 150, n) + np.random.normal(0, 1, n)
    high = close + 1.5
    low = close - 1.5
    df = pd.DataFrame({"high": high, "low": low, "close": close})

    st, st_dir = calculate_supertrend(df, period=10, multiplier=3.0)
    assert len(st) == n
    assert len(st_dir) == n
    # Direction must be strictly +1 or -1
    valid_dirs = set(st_dir.dropna().unique())
    assert valid_dirs.issubset({-1, 1})


def test_volume_profile_calculation():
    """Verify Volume Profile POC and Value Area bounds (VAL <= POC <= VAH)."""
    np.random.seed(42)
    n = 60
    close = np.linspace(200, 210, n)
    high = close + 2.0
    low = close - 2.0
    volume = np.random.uniform(500, 5000, n)
    df = pd.DataFrame({"high": high, "low": low, "close": close, "volume": volume})

    vp = calculate_volume_profile(df, lookback=50, n_bins=20)
    assert "poc" in vp and "vah" in vp and "val" in vp
    assert vp["val"] <= vp["poc"] <= vp["vah"]


def test_walk_forward_backtest_zero_leakage():
    """Verify TimeSeriesSplitWithEmbargo guarantees zero leakage in walk-forward backtest."""
    np.random.seed(42)
    n = 150
    close = np.linspace(100, 180, n) + np.random.normal(0, 2, n)
    high = close + 2.0
    low = close - 2.0
    volume = np.full(n, 1000)
    df = pd.DataFrame({"high": high, "low": low, "close": close, "volume": volume})

    res = run_walk_forward_backtest(df, n_splits=3, embargo_bars=5, forward_bars=3)
    assert res["status"] == "VALIDATED"
    assert res["embargo_verified"] is True
    assert 0.0 <= res["brier_score"] <= 2.0
    assert 0.0 <= res["directional_accuracy"] <= 100.0


def test_indian_transaction_costs():
    """Verify authentic Indian statutory charges (brokerage, STT, GST, slippage)."""
    costs = calculate_indian_transaction_costs(entry_price=500.0, exit_price=520.0, shares=100)
    # Turnover = (500 + 520) * 100 = 102,000
    # Brokerage: min(20, 50000 * 0.0003=15) + min(20, 52000 * 0.0003=15.6) = ~30.6
    # STT: 102,000 * 0.001 = 102
    assert costs["total_cost"] > 100.0
    assert costs["stt"] == 102.0
    assert costs["brokerage"] > 0
    assert costs["gst"] > 0
    assert costs["slippage"] > 0


def test_prediction_api_status_endpoint():
    """Verify /api/prediction/status returns ONLINE and layer health."""
    client = TestClient(app)
    resp = client.get("/api/prediction/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ONLINE"
    assert "L1_market_data" in data["layers"]
    assert "L3_probabilistic_ensemble" in data["layers"]
    assert "L5_risk_and_range" in data["layers"]


@pytest.mark.asyncio
async def test_predict_stock_price_mocked(monkeypatch):
    """End-to-end test of 6-layer prediction engine with mocked Yahoo provider data."""
    from data.providers.yahoo import YahooProvider

    n = 250
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = np.linspace(240, 270, n)
    high = close + 2.0
    low = close - 2.0
    vol = np.random.uniform(1000, 5000, n)
    mock_df = pd.DataFrame({"high": high, "low": low, "close": close, "volume": vol}, index=dates)

    nifty_close = np.linspace(24000, 25000, n)
    mock_nifty = pd.DataFrame({"close": nifty_close}, index=dates)
    mock_vix = pd.DataFrame({"close": np.full(n, 14.5)}, index=dates)

    async def mock_get_batch_ohlcv(self, symbols, period="1y", interval="1d"):
        return {
            "VEDL": mock_df,
            "^NSEI": mock_nifty,
            "^INDIAVIX": mock_vix,
        }

    monkeypatch.setattr(YahooProvider, "get_batch_ohlcv", mock_get_batch_ohlcv)

    res = await predict_stock_price("VEDL", force_refresh=True)

    assert res["status"] == "PREDICTION_ACTIVE"
    assert res["symbol"] == "VEDL"
    assert res["company_name"] == "Vedanta Limited"
    assert res["data_delay_minutes"] == 15
    assert res["feed_source"] == "YAHOO_NSE_DELAYED"
    assert res["direction"] in {"UP", "DOWN", "SIDEWAYS"}
    assert "probabilities" in res
    probs = res["probabilities"]
    assert round(probs["up"] + probs["down"] + probs["sideways"], 1) == 100.0
    assert "price_levels" in res
    pl = res["price_levels"]
    assert pl["lower_range"] < pl["reference_price"] < pl["upper_range"]
    assert "rationale" in res and len(res["rationale"]) > 20
    assert "market_context" in res
    assert res["market_context"]["vix_regime"] == "NORMAL_SWING"


@pytest.mark.asyncio
async def test_predict_stock_insufficient_data(monkeypatch):
    """Verify rejection when < 200 bars are available."""
    from data.providers.yahoo import YahooProvider

    # Only 50 bars
    n = 50
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    mock_df = pd.DataFrame({"high": [10.0]*n, "low": [8.0]*n, "close": [9.0]*n, "volume": [100]*n}, index=dates)

    async def mock_get_batch_ohlcv(self, symbols, period="1y", interval="1d"):
        return {"TINY": mock_df}

    monkeypatch.setattr(YahooProvider, "get_batch_ohlcv", mock_get_batch_ohlcv)

    res = await predict_stock_price("TINY", force_refresh=True)
    assert res["status"] == "INSUFFICIENT_DATA"
    assert res["probabilities"] is None
    assert res["price_levels"] is None
    assert "Insufficient historical data" in res["rationale"]

