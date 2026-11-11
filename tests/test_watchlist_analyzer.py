"""
Unit tests for the Watchlist Real Per-Stock Quant Engine.
Tests:
- Pure indicator calculations (EMA, RSI, MACD, ATR, ADX, Bollinger width, Volume ratio, 52W distances, Swing pivots, RS vs benchmark).
- Explainable 5-factor scoring model (Sum = 100, factor bounds, no NaN/negative scores).
- Structural trade levels (LONG/SHORT R:R gate >= 1.5, SL ATR floor, HOLD/MONITOR support/resistance/invalidation).
- Dynamic time horizon derivation.
- Template-based rationale generation.
- Insufficient data (< 200 bars) handling.
- Company name resolution (e.g. PRECWIRE -> Precision Wires India Ltd.).
"""

import math
import numpy as np
import pandas as pd
import pytest

from agents.market_intelligence.watchlist_analyzer import (
    calculate_ema,
    calculate_rsi,
    calculate_macd,
    calculate_atr,
    calculate_adx,
    calculate_bollinger_width,
    calculate_volume_ratio,
    calculate_distance_52w,
    calculate_swing_pivots,
    calculate_relative_strength,
    score_stock_factors,
    calculate_trade_levels,
    calculate_time_horizon,
    build_factor_rationale,
    get_company_name,
)


@pytest.fixture
def synthetic_ohlcv():
    """Generate 250 daily bars with realistic price action."""
    np.random.seed(42)
    n = 250
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    returns = np.random.normal(0.0005, 0.015, n)
    price = 1000.0 * np.exp(np.cumsum(returns))

    high = price * (1.0 + np.random.uniform(0.002, 0.015, n))
    low = price * (1.0 - np.random.uniform(0.002, 0.015, n))
    open_p = (high + low) / 2.0
    close_p = price
    volume = np.random.uniform(500000, 2000000, n)

    df = pd.DataFrame(
        {
            "timestamp": dates,
            "open": open_p,
            "high": high,
            "low": low,
            "close": close_p,
            "volume": volume,
        }
    )
    return df


def test_company_name_resolution():
    assert get_company_name("PRECWIRE") == "Precision Wires India Ltd."
    assert get_company_name("PRECWIRE.NS") == "Precision Wires India Ltd."
    assert get_company_name("RELIANCE") == "Reliance Industries Ltd."
    assert get_company_name("UNKNOWNXYZ") == "UNKNOWNXYZ · NSE Equity"


def test_pure_indicators(synthetic_ohlcv):
    df = synthetic_ohlcv
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # 1. EMA
    ema20 = calculate_ema(close, 20)
    ema50 = calculate_ema(close, 50)
    ema200 = calculate_ema(close, 200)
    assert len(ema20) == len(close)
    assert not np.isnan(ema20.iloc[-1])
    assert not np.isnan(ema50.iloc[-1])
    assert not np.isnan(ema200.iloc[-1])

    # 2. RSI
    rsi = calculate_rsi(close, 14)
    assert len(rsi) == len(close)
    assert 0.0 <= rsi.iloc[-1] <= 100.0

    # 3. MACD
    macd, sig, hist = calculate_macd(close, 12, 26, 9)
    assert len(macd) == len(close)
    assert len(sig) == len(close)
    assert len(hist) == len(close)
    assert not np.isnan(macd.iloc[-1])
    assert not np.isnan(hist.iloc[-1])

    # 4. ATR
    atr = calculate_atr(high, low, close, 14)
    assert len(atr) == len(close)
    assert atr.iloc[-1] > 0

    # 5. ADX
    adx = calculate_adx(high, low, close, 14)
    assert len(adx) == len(close)
    assert 0.0 <= adx.iloc[-1] <= 100.0

    # 6. Bollinger Width
    bb_width = calculate_bollinger_width(close, 20, 2.0)
    assert len(bb_width) == len(close)
    assert bb_width.iloc[-1] > 0

    # 7. Volume Ratio
    vol_ratio = calculate_volume_ratio(volume, 20)
    assert vol_ratio > 0

    # 8. 52-Week Distances
    d52h, d52l = calculate_distance_52w(high, low, close, 250)
    assert d52h <= 0.0  # Close cannot be higher than 52w high
    assert d52l >= 0.0  # Close cannot be lower than 52w low

    # 9. Swing Pivots
    supports, resistances = calculate_swing_pivots(high, low, close, window=5)
    assert isinstance(supports, list)
    assert isinstance(resistances, list)
    for s in supports:
        assert s < close.iloc[-1]
    for r in resistances:
        assert r > close.iloc[-1]

    # 10. Relative Strength vs Benchmark
    bench_close = close * 0.98  # Underperforming benchmark
    rs = calculate_relative_strength(close, bench_close, 20)
    assert isinstance(rs, float)


def test_score_stock_factors_bullish_and_bearish():
    # Strong bullish scenario
    score, breakdown, bias = score_stock_factors(
        close=1500.0,
        ema20=1450.0,
        ema50=1400.0,
        ema200=1300.0,
        rsi=60.0,
        macd_hist=5.0,
        macd_hist_prev=3.0,
        vol_ratio=1.6,
        rs_20d=4.5,
        adx=28.0,
        dist_52h=-2.0,
    )
    assert bias == "BULLISH"
    assert 70 <= score <= 100
    assert breakdown["trend"] == 30
    assert breakdown["momentum"] == 25
    assert breakdown["volume"] == 15
    assert breakdown["relative_strength"] == 15
    assert breakdown["structure"] == 15
    assert sum(breakdown.values()) == score

    # Strong bearish scenario
    score_b, breakdown_b, bias_b = score_stock_factors(
        close=800.0,
        ema20=850.0,
        ema50=900.0,
        ema200=1000.0,
        rsi=40.0,
        macd_hist=-5.0,
        macd_hist_prev=-3.0,
        vol_ratio=1.6,
        rs_20d=-4.5,
        adx=28.0,
        dist_52h=-40.0,
    )
    assert bias_b == "BEARISH"
    assert 70 <= score_b <= 100
    assert breakdown_b["trend"] == 30
    assert sum(breakdown_b.values()) == score_b


def test_trade_levels_long_rr_gate():
    # Case 1: Qualified LONG with strong R:R >= 1.5
    levels = calculate_trade_levels(
        close=1000.0,
        high_last=1005.0,
        low_last=990.0,
        atr14=15.0,
        supports=[980.0, 960.0],
        resistances=[1060.0, 1080.0],
        trend_bias="BULLISH",
        total_score=85,
        adx=25.0,
    )
    assert levels["is_actionable"] is True
    assert levels["signal"] == "LONG"
    assert levels["entry"] > 1000.0
    assert levels["stop_loss"] < levels["entry"]
    assert levels["target"] > levels["entry"]
    assert levels["rr_ratio"] >= 1.5
    # Verify ATR floor on SL: risk >= 1.5 * atr14 (1.5 * 15 = 22.5)
    assert (levels["entry"] - levels["stop_loss"]) >= (1.5 * 15.0 - 0.05)


def test_trade_levels_hold_monitor_duality():
    # Case 2: Score 55 (HOLD) - must NOT emit entry/SL/TP, must emit key support/resistance
    levels = calculate_trade_levels(
        close=1000.0,
        high_last=1005.0,
        low_last=990.0,
        atr14=15.0,
        supports=[975.0, 950.0],
        resistances=[1025.0, 1050.0],
        trend_bias="BULLISH",
        total_score=55,
        adx=16.0,
    )
    assert levels["is_actionable"] is False
    assert levels["signal"] == "HOLD"
    assert levels["entry"] is None
    assert levels["stop_loss"] is None
    assert levels["target"] is None
    assert levels["rr_ratio"] is None
    assert levels["key_support"] == 975.0
    assert levels["key_resistance"] == 1025.0
    assert levels["invalidation_level"] == 975.0


def test_dynamic_time_horizon():
    # Actionable swing
    h = calculate_time_horizon(
        is_actionable=True,
        entry=1000.0,
        target=1045.0,  # 45 pts dist / 15 atr = 3 bars
        atr14=15.0,
        timeframe="1d",
    )
    assert h["est_bars"] == 3
    assert h["time_stop_bars"] == 6
    assert "2–4 Days" in h["short"]
    assert "Max 6 Bars" in h["barrier_short"]

    # Intraday
    h_intra = calculate_time_horizon(
        is_actionable=True,
        entry=1000.0,
        target=1010.0,
        atr14=5.0,
        timeframe="15m",
    )
    assert "Today (Intraday)" in h_intra["short"]
    assert "15:15 IST" in h_intra["barrier_short"]


def test_template_rationale_builder():
    rationale = build_factor_rationale(
        close=1500.0,
        ema20=1450.0,
        ema50=1400.0,
        ema200=1300.0,
        rsi=58.0,
        vol_ratio=1.4,
        rs_20d=2.1,
        adx=24.0,
        trend_bias="BULLISH",
    )
    assert "Above EMA 20/50/200" in rationale
    assert "RSI 58 rising" in rationale
    assert "volume 1.4x avg" in rationale
    assert "RS +2.1% vs NIFTY" in rationale


@pytest.mark.asyncio
async def test_analyze_watchlist_symbols_insufficient_data(monkeypatch):
    from agents.market_intelligence.watchlist_analyzer import analyze_watchlist_symbols
    from data.providers.yahoo import YahooProvider

    # Mock YahooProvider to return only 50 bars
    short_df = pd.DataFrame({
        "open": [100.0] * 50,
        "high": [105.0] * 50,
        "low": [95.0] * 50,
        "close": [102.0] * 50,
        "volume": [10000] * 50,
        "timestamp": pd.date_range("2026-01-01", periods=50, freq="B"),
    })

    async def mock_batch_ohlcv(*args, **kwargs):
        return {"NEWSTOCK": short_df, "^NSEI": short_df}

    monkeypatch.setattr(YahooProvider, "get_batch_ohlcv", mock_batch_ohlcv)

    res = await analyze_watchlist_symbols(["NEWSTOCK"], force_refresh=True)
    assert "NEWSTOCK" in res
    assert res["NEWSTOCK"]["status"] == "INSUFFICIENT_DATA"
    assert res["NEWSTOCK"]["confidence"] is None
    assert res["NEWSTOCK"]["signal"] == "INSUFFICIENT_DATA"
    assert res["NEWSTOCK"]["entry"] is None
    assert res["NEWSTOCK"]["stop_loss"] is None
    assert res["NEWSTOCK"]["target"] is None


@pytest.mark.asyncio
async def test_analyze_watchlist_symbols_sufficient_data(synthetic_ohlcv, monkeypatch):
    from agents.market_intelligence.watchlist_analyzer import analyze_watchlist_symbols
    from data.providers.yahoo import YahooProvider

    async def mock_batch_ohlcv(*args, **kwargs):
        return {"PRECWIRE": synthetic_ohlcv, "^NSEI": synthetic_ohlcv}

    monkeypatch.setattr(YahooProvider, "get_batch_ohlcv", mock_batch_ohlcv)

    res = await analyze_watchlist_symbols(["PRECWIRE"], force_refresh=True)
    assert "PRECWIRE" in res
    entry = res["PRECWIRE"]
    assert entry["status"] == "OK"
    assert entry["company_name"] == "Precision Wires India Ltd."
    assert entry["confidence"] is not None
    assert 0 <= entry["confidence"] <= 100
    assert entry["signal"] in ("LONG", "SHORT", "HOLD", "MONITOR")
    assert entry["factor_breakdown"] is not None
    assert "trend" in entry["factor_breakdown"]
    assert "momentum" in entry["factor_breakdown"]
    assert "volume" in entry["factor_breakdown"]
    assert "relative_strength" in entry["factor_breakdown"]
    assert "structure" in entry["factor_breakdown"]
    assert entry["as_of"] is not None
    assert entry["horizon"] is not None
    assert "short" in entry["horizon"]
    assert "barrier_short" in entry["horizon"]
    assert entry["rationale"] is not None

