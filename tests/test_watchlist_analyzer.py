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


def test_persist_and_hydrate_watchlist_eod_snapshot(monkeypatch):
    from agents.market_intelligence.watchlist_analyzer import (
        persist_watchlist_eod_snapshot,
        hydrate_watchlist_cache_from_db,
        _WATCHLIST_ANALYSIS_CACHE,
    )
    import db.runtime_state as rs

    fake_db = {}

    def mock_set(key, val):
        fake_db[key] = val
        return True

    def mock_get(key, default=None):
        return fake_db.get(key, default)

    monkeypatch.setattr(rs, "set_runtime_state", mock_set)
    monkeypatch.setattr(rs, "get_runtime_state", mock_get)

    sample_quotes = {
        "RELIANCE": {
            "symbol": "RELIANCE",
            "ltp": 1339.7,
            "change_pct": 0.5,
            "signal": "HOLD",
            "key_support": 1329.35,
            "key_resistance": 1344.9,
            "invalidation_level": 1329.35,
            "horizon": {"short": "5–8 Days (Swing)", "barrier_short": "Max 10 Bars"},
        },
        "HDFCBANK": {
            "symbol": "HDFCBANK",
            "ltp": 809.8,
            "change_pct": 1.2,
            "signal": "LONG",
            "entry": 809.8,
            "stop_loss": 802.4,
            "target": 821.7,
            "horizon": {"short": "5–8 Days (Swing)", "barrier_short": "Max 10 Bars"},
        },
    }

    # 1. Persist to DB
    ok = persist_watchlist_eod_snapshot(sample_quotes, session_date="2026-11-12")
    assert ok is True
    assert "watchlist:eod_snapshot:v1" in fake_db
    stored = fake_db["watchlist:eod_snapshot:v1"]
    assert stored["count"] == 2
    assert stored["session_date"] == "2026-11-12"
    assert stored["settlement_type"] == "OFFICIAL_VWAP_SETTLED"

    # Clear memory cache
    _WATCHLIST_ANALYSIS_CACHE.clear()
    assert len(_WATCHLIST_ANALYSIS_CACHE) == 0

    # 2. Hydrate from DB
    count = hydrate_watchlist_cache_from_db()
    assert count == 2
    assert "RELIANCE" in _WATCHLIST_ANALYSIS_CACHE
    assert _WATCHLIST_ANALYSIS_CACHE["RELIANCE"]["ltp"] == 1339.7
    assert _WATCHLIST_ANALYSIS_CACHE["HDFCBANK"]["signal"] == "LONG"


def test_extract_and_blend_premarket_base_state(synthetic_ohlcv):
    from agents.market_intelligence.watchlist_analyzer import (
        extract_premarket_base_state,
        blend_live_quote_with_base_state,
    )

    base = extract_premarket_base_state(synthetic_ohlcv, "RELIANCE")
    assert base is not None
    assert base["symbol"] == "RELIANCE"
    assert base["close_prev"] > 0
    assert base["ema19_prev"] > 0
    assert base["ema49_prev"] > 0
    assert base["ema199_prev"] > 0
    assert base["atr13_prev"] > 0

    # Live tick blending (O(1) without DataFrame)
    blended = blend_live_quote_with_base_state(
        base,
        ltp=base["close_prev"] * 1.02,
        high=base["close_prev"] * 1.03,
        low=base["close_prev"] * 0.99,
        volume=base["vol_20d_avg"] * 1.5,
    )
    assert blended["ema20"] > 0
    assert blended["ema50"] > 0
    assert blended["ema200"] > 0
    assert blended["atr14"] > 0
    assert blended["vol_ratio"] == 1.5
    assert blended["high_20d"] >= base["high_20d"]


def test_get_nse_expected_volume_fraction():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from agents.market_intelligence.watchlist_analyzer import get_nse_expected_volume_fraction

    ist = ZoneInfo("Asia/Kolkata")
    # 09:15 open -> ~0.03
    t_open = datetime(2026, 11, 12, 9, 15, tzinfo=ist)
    assert get_nse_expected_volume_fraction(t_open) == 0.03

    # 09:45 (30m elapsed) -> ~0.155
    t_morning = datetime(2026, 11, 12, 9, 45, tzinfo=ist)
    frac_morning = get_nse_expected_volume_fraction(t_morning)
    assert 0.14 <= frac_morning <= 0.17

    # 10:15 (60m elapsed) -> 0.28
    t_rush_end = datetime(2026, 11, 12, 10, 15, tzinfo=ist)
    assert get_nse_expected_volume_fraction(t_rush_end) == 0.28

    # 15:30 close -> 1.0
    t_close = datetime(2026, 11, 12, 15, 30, tzinfo=ist)
    assert get_nse_expected_volume_fraction(t_close) == 1.0

    # 16:00 post-market -> 1.0
    t_post = datetime(2026, 11, 12, 16, 0, tzinfo=ist)
    assert get_nse_expected_volume_fraction(t_post) == 1.0


def test_calculate_volume_ratio_time_of_day_rvol():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from agents.market_intelligence.watchlist_analyzer import calculate_volume_ratio

    ist = ZoneInfo("Asia/Kolkata")
    # 20 days with 100,000 avg volume
    vol_history = [100000.0] * 19

    # At 09:45 AM (30 min into day): expected fraction is ~0.155 (15,500 shares)
    t_0945 = datetime(2026, 11, 12, 9, 45, tzinfo=ist)

    # Suppose current bar has 31,000 shares (2x normal morning volume)
    vol_series = pd.Series(vol_history + [31000.0])
    rvol = calculate_volume_ratio(vol_series, window=20, dt_ist=t_0945)
    # Expected: 31,000 / ~15,500 ≈ 2.0x
    assert 1.9 <= rvol <= 2.1

    # At 16:00 (EOD): same 31,000 shares against 100,000 avg is 0.31x
    t_eod = datetime(2026, 11, 12, 16, 0, tzinfo=ist)
    rvol_eod = calculate_volume_ratio(vol_series, window=20, dt_ist=t_eod)
    assert 0.30 <= rvol_eod <= 0.32


def test_composite_relative_strength():
    from agents.market_intelligence.watchlist_analyzer import calculate_relative_strength

    stock_close = pd.Series([100.0] * 19 + [105.0])    # +5% 20d return
    bench_close = pd.Series([1000.0] * 19 + [1020.0])  # +2% 20d return
    # 20d structural alpha = +3.0%

    # Case A: Standalone 20d
    rs_pure = calculate_relative_strength(stock_close, bench_close, 20)
    assert rs_pure == 3.0

    # Case B: Composite with Intraday (Stock is +0.5%, NIFTY is -1.5% -> intraday alpha = +2.0%)
    rs_comp = calculate_relative_strength(
        stock_close, bench_close, 20,
        intraday_stock_chg=0.5,
        intraday_bench_chg=-1.5,
    )
    # Expected: 3.0 * 0.7 + 2.0 * 0.3 = 2.1 + 0.6 = 2.7
    assert rs_comp == 2.7


def test_india_vix_regime_gating():
    from agents.market_intelligence.watchlist_analyzer import calculate_trade_levels

    close = 1000.0
    atr = 20.0
    supports = [980.0, 960.0]
    resistances = [1050.0, 1080.0]

    # Scenario 1: Normal VIX (< 14) -> score 72 qualifies for LONG (threshold 70), SL 1.5x ATR (30 pts)
    norm = calculate_trade_levels(
        close=close, high_last=1005.0, low_last=995.0, atr14=atr,
        supports=supports, resistances=resistances, trend_bias="BULLISH",
        total_score=72, adx=25.0, vix_val=12.5,
    )
    assert norm["signal"] == "LONG"
    assert norm["is_actionable"] is True
    assert norm["vix_regime"] == "NORMAL"
    assert norm["vix_threshold"] == 70
    assert norm["vix_atr_multiplier"] == 1.5

    # Scenario 2: Elevated VIX (14-18) -> score 72 is suppressed to HOLD (needs 74)
    elev_suppressed = calculate_trade_levels(
        close=close, high_last=1005.0, low_last=995.0, atr14=atr,
        supports=supports, resistances=resistances, trend_bias="BULLISH",
        total_score=72, adx=25.0, vix_val=16.0,
    )
    assert elev_suppressed["signal"] == "HOLD"
    assert elev_suppressed["is_actionable"] is False
    assert elev_suppressed["vix_regime"] == "ELEVATED"
    assert elev_suppressed["vix_threshold"] == 74

    # Scenario 3: High Volatility VIX (> 18) -> score 76 is suppressed to HOLD (needs 78)
    high_suppressed = calculate_trade_levels(
        close=close, high_last=1005.0, low_last=995.0, atr14=atr,
        supports=supports, resistances=resistances, trend_bias="BULLISH",
        total_score=76, adx=25.0, vix_val=21.0,
    )
    assert high_suppressed["signal"] == "HOLD"
    assert high_suppressed["is_actionable"] is False
    assert high_suppressed["vix_regime"] == "HIGH_VOLATILITY"

    # Scenario 4: High Volatility VIX (> 18) -> score 80 qualifies, but SL floor widened to 2.0x ATR (40 pts)
    high_qualified = calculate_trade_levels(
        close=close, high_last=1005.0, low_last=995.0, atr14=atr,
        supports=supports, resistances=resistances, trend_bias="BULLISH",
        total_score=80, adx=25.0, vix_val=21.0,
    )
    assert high_qualified["signal"] == "LONG"
    assert high_qualified["is_actionable"] is True
    assert high_qualified["vix_regime"] == "HIGH_VOLATILITY"
    assert high_qualified["vix_threshold"] == 78
    assert high_qualified["vix_atr_multiplier"] == 2.0
    # Entry is 1005 * 1.001 = 1006.01; SL floor = entry - 2.0 * 20 = 966.01
    assert high_qualified["stop_loss"] <= (high_qualified["entry"] - 2.0 * atr)



