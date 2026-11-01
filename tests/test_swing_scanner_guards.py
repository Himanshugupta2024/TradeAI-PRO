"""Tests for swing scanner staleness, regime gate, and dedupe helpers."""

import numpy as np
import pandas as pd

from agents.market_intelligence.swing_scanner import (
    SwingSetup,
    _adx_gate_ok,
    _check_ema_cross,
    _check_inside_bar,
    _check_range_breakout,
    _check_rsi_divergence,
    _finalize_swing_targets,
    _rolling_vol_ratio,
    apply_swing_regime_gate,
    compute_sector_momentum_map,
    dedupe_swing_setups_one_per_symbol,
    scan_all_symbols,
    scan_swing_setups,
)
from config import settings
import agents.market_intelligence.swing_scanner as swing_scanner_mod


def _minimal_daily_df(*, end_offset_days: int = 0, n: int = 40) -> pd.DataFrame:
    end = pd.Timestamp.now().normalize() - pd.Timedelta(days=end_offset_days)
    dates = pd.date_range(end=end, periods=n, freq="D")
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 200_000.0,
            "RSI_14": 50.0,
            "EMA_20": 100.0,
            "EMA_50": 99.0,
            "ATR_14": 1.0,
        }
    )


def test_finalize_short_keeps_pattern_t1_when_fib_would_reduce_rr():
    """Fib should not replace T1 when it would materially reduce preserved pattern R:R."""
    s = SwingSetup(
        symbol="BPCL",
        pattern="Trend Continuation (Watchlist)",
        direction="SHORT",
        strength=90,
        entry_zone=292.95,
        stop_loss=307.6,
        target_1=243.97,
        target_2=219.5,
        risk_reward=3.3,
        timeframe="5-10 days",
        description="test",
        indicators={},
        fib_tp1=0.0,
        fib_tp2=290.95,
    )
    _finalize_swing_targets(s, 292.95)
    assert s.fib_tp1 == 0.0 and s.fib_tp2 == 0.0
    assert s.target_1 == 243.97
    assert s.target_2 == 219.5
    assert s.target_1 > s.target_2


def test_finalize_long_keeps_pattern_t1_and_repairs_t2_order():
    s = SwingSetup(
        symbol="X",
        pattern="TEST",
        direction="LONG",
        strength=80,
        entry_zone=100.0,
        stop_loss=98.0,
        target_1=110.0,
        target_2=105.0,
        risk_reward=2.0,
        timeframe="5d",
        description="test",
        indicators={},
        fib_tp1=0.0,
        fib_tp2=106.0,
    )
    _finalize_swing_targets(s, 100.0)
    assert s.target_1 == 110.0
    assert s.target_2 == 112.0
    assert s.target_1 < s.target_2


def _sample_long_short() -> tuple[SwingSetup, SwingSetup]:
    long_setup = SwingSetup(
        symbol="X",
        pattern="TEST",
        direction="LONG",
        strength=80,
        entry_zone=100.0,
        stop_loss=98.0,
        target_1=104.0,
        target_2=106.0,
        risk_reward=2.0,
        timeframe="5d",
        description="test",
        indicators={},
    )
    short_setup = SwingSetup(
        symbol="X",
        pattern="TEST2",
        direction="SHORT",
        strength=70,
        entry_zone=100.0,
        stop_loss=102.0,
        target_1=96.0,
        target_2=94.0,
        risk_reward=2.0,
        timeframe="5d",
        description="test",
        indicators={},
    )
    return long_setup, short_setup


def _mk_setup(symbol: str, pattern: str, watchlist: bool) -> SwingSetup:
    return SwingSetup(
        symbol=symbol,
        pattern=pattern,
        direction="LONG",
        strength=70,
        entry_zone=100.0,
        stop_loss=98.0,
        target_1=104.0,
        target_2=106.0,
        risk_reward=2.0,
        timeframe="5d",
        description="test",
        indicators={"watchlist": watchlist},
    )


def test_swing_skips_when_last_bar_too_old(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 5.0)
    df = _minimal_daily_df(end_offset_days=20)
    assert scan_swing_setups(df, "TESTSYM") == []


def test_mtf_gate_blocks_long_when_trend_bearish(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    monkeypatch.setattr(settings, "SWING_MTF_CONFIRM_ENABLED", True)
    monkeypatch.setattr(settings, "SWING_MTF_BLOCK_ON_UNKNOWN", False)

    df = _minimal_daily_df(end_offset_days=1)
    out = scan_swing_setups(
        df,
        "TESTSYM",
        mtf_trend="BEARISH",
        allow_fallback=True,
    )
    assert out == []


def test_mtf_gate_allows_long_on_bullish(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    monkeypatch.setattr(settings, "SWING_MTF_CONFIRM_ENABLED", True)

    df = _minimal_daily_df(end_offset_days=1)
    out = scan_swing_setups(
        df,
        "TESTSYM",
        mtf_trend="BULLISH",
        allow_fallback=True,
    )
    assert out
    assert out[0].direction == "LONG"
    assert out[0].indicators.get("mtf_trend") == "BULLISH"


def test_scan_all_symbols_hides_watchlist_rows_by_default(monkeypatch):
    monkeypatch.setattr(settings, "SWING_INCLUDE_WATCHLIST_IN_PORTFOLIO_SCAN", False)
    monkeypatch.setattr(
        swing_scanner_mod,
        "scan_swing_setups",
        lambda *args, **kwargs: [
            _mk_setup("TESTSYM", "Setup (Watchlist)", True),
            _mk_setup("TESTSYM", "20-Day Range Breakout (Bullish)", False),
        ],
    )
    monkeypatch.setattr(
        "agents.data_ingestion.context_fetcher.build_context_fetcher", lambda: None
    )

    out = scan_all_symbols({"TESTSYM": _minimal_daily_df()})
    assert len(out) == 1
    assert out[0]["pattern"] == "20-Day Range Breakout (Bullish)"


def test_scan_all_symbols_can_include_watchlist_rows(monkeypatch):
    monkeypatch.setattr(settings, "SWING_INCLUDE_WATCHLIST_IN_PORTFOLIO_SCAN", True)
    monkeypatch.setattr(
        swing_scanner_mod,
        "scan_swing_setups",
        lambda *args, **kwargs: [_mk_setup("TESTSYM", "Setup (Watchlist)", True)],
    )
    monkeypatch.setattr(
        "agents.data_ingestion.context_fetcher.build_context_fetcher", lambda: None
    )

    out = scan_all_symbols({"TESTSYM": _minimal_daily_df()})
    assert len(out) == 1
    assert out[0]["pattern"] == "Setup (Watchlist)"


def test_swing_allows_recent_bar_with_high_max_age(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    df = _minimal_daily_df(end_offset_days=1)
    out = scan_swing_setups(df, "TESTSYM")
    assert isinstance(out, list)


def test_swing_lookup_fallback_emits_watch_setup(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    df = _minimal_daily_df(end_offset_days=1)
    out = scan_swing_setups(df, "TESTSYM", allow_fallback=True)
    assert len(out) >= 1
    assert out[0].pattern == "Trend Continuation (Watchlist)"


def test_watchlist_strength_is_capped_after_bonuses(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    monkeypatch.setattr(settings, "SWING_WATCHLIST_MAX_STRENGTH", 72)
    df = _minimal_daily_df(end_offset_days=1)
    out = scan_swing_setups(
        df,
        "TESTSYM",
        sector_momentum=2.5,
        sector="Pharma",
        allow_fallback=True,
    )
    assert len(out) >= 1
    assert out[0].indicators.get("watchlist") is True
    assert out[0].strength <= 72


def test_sector_hard_gate_blocks_long_against_headwind(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    monkeypatch.setattr(settings, "SWING_SECTOR_HARD_GATE_ENABLED", True)
    monkeypatch.setattr(settings, "SWING_SECTOR_HEADWIND_BLOCK_THRESHOLD", -1.5)
    df = _minimal_daily_df(end_offset_days=1)
    out = scan_swing_setups(
        df,
        "TESTSYM",
        sector_momentum=-2.0,
        sector="Pharma",
        allow_fallback=True,
    )
    assert out == []


def test_sector_hard_gate_blocks_short_against_tailwind(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    monkeypatch.setattr(settings, "SWING_SECTOR_HARD_GATE_ENABLED", True)
    monkeypatch.setattr(settings, "SWING_SECTOR_TAILWIND_BLOCK_SHORT_THRESHOLD", 1.5)

    df = _minimal_daily_df(end_offset_days=1)
    # Force fallback short conditions.
    df["EMA_20"] = 101.0
    df["EMA_50"] = 102.0
    df["close"] = 100.0
    df["RSI_14"] = 45.0

    out = scan_swing_setups(
        df,
        "TESTSYM",
        sector_momentum=2.0,
        sector="Pharma",
        allow_fallback=True,
    )
    assert out == []


def test_inside_bar_low_volume_is_watchlist_not_actionable(monkeypatch):
    monkeypatch.setattr(settings, "SWING_VOL_INSIDE_BAR", 2.0)
    monkeypatch.setattr(settings, "SWING_VOL_INSIDE_BAR_MIN_ACTIONABLE", 1.0)

    now = pd.Timestamp.now().normalize()
    dates = pd.date_range(end=now, periods=7, freq="D")
    df = pd.DataFrame(
        {
            "timestamp": dates,
            "open": [100, 100, 100, 100, 100, 101, 104],
            "high": [102, 102, 105, 104, 103, 104, 108],
            "low": [98, 98, 95, 96, 97, 97, 102],
            "close": [100, 100, 100, 100, 100, 101, 106],
            "volume": [100_000, 100_000, 100_000, 100_000, 100_000, 100_000, 20_000],
        }
    )

    setup = _check_inside_bar(df, "TESTSYM", atr=2.0)
    assert setup is not None
    assert setup.direction == "LONG"
    assert "Watchlist" in setup.pattern
    assert setup.indicators.get("watchlist") is True
    assert setup.indicators.get("watchlist_reason") == "low_volume"
    assert setup.indicators.get("vol_ratio", 0) < 1.0


def test_apply_regime_gate_drops_long_in_bearish(monkeypatch):
    monkeypatch.setattr(settings, "SWING_REGIME_GATE_ENABLED", True)
    monkeypatch.setattr(settings, "SWING_BLOCK_LONG_IN_BEARISH_TREND", True)
    monkeypatch.setattr(settings, "SWING_BLOCK_SHORT_IN_BULLISH_TREND", False)
    long_s, short_s = _sample_long_short()
    ctx = {"index_trend": "BEARISH", "index_regime": "RANGING", "index_symbol": "NIFTY 50"}
    out = apply_swing_regime_gate([long_s, short_s], ctx)
    assert len(out) == 1
    assert out[0].direction == "SHORT"


def test_apply_regime_gate_blocks_both_sides_in_choppy(monkeypatch):
    monkeypatch.setattr(settings, "SWING_REGIME_GATE_ENABLED", True)
    monkeypatch.setattr(settings, "SWING_BLOCK_LONG_IN_CHOPPY_TREND", True)
    monkeypatch.setattr(settings, "SWING_BLOCK_SHORT_IN_CHOPPY_TREND", True)
    long_s, short_s = _sample_long_short()
    ctx = {"index_trend": "CHOPPY", "index_regime": "RANGING", "index_symbol": "NIFTY 50"}
    out = apply_swing_regime_gate([long_s, short_s], ctx)
    assert out == []


def test_dedupe_keeps_highest_strength_then_rr(monkeypatch):
    monkeypatch.setattr(settings, "SWING_DEDUPE_ONE_PER_SYMBOL", True)
    a, _ = _sample_long_short()
    b = SwingSetup(
        symbol="X",
        pattern="B",
        direction="LONG",
        strength=90,
        entry_zone=100.0,
        stop_loss=98.0,
        target_1=104.0,
        target_2=106.0,
        risk_reward=1.5,
        timeframe="5d",
        description="b",
        indicators={},
    )
    c = SwingSetup(
        symbol="X",
        pattern="C",
        direction="LONG",
        strength=90,
        entry_zone=100.0,
        stop_loss=98.0,
        target_1=104.0,
        target_2=106.0,
        risk_reward=2.5,
        timeframe="5d",
        description="c",
        indicators={},
    )
    out = dedupe_swing_setups_one_per_symbol([a, b, c])
    assert len(out) == 1
    # Primary is C (strength tie 90 → higher R:R 2.5); confluence merges A,B,C → bonus +10
    assert out[0].pattern == "C (+2 confluent)"
    assert out[0].confluence_count == 3
    assert "TEST" in out[0].confluence_patterns and "C" in out[0].confluence_patterns
    assert out[0].strength == 100  # 90 + min(15, 5*2)


def test_dedupe_conflicting_direction_prefers_higher_peak_strength(monkeypatch):
    monkeypatch.setattr(settings, "SWING_CONFLUENCE_ENABLED", True)
    long_s = SwingSetup(
        symbol="X",
        pattern="LONGPAT",
        direction="LONG",
        strength=70,
        entry_zone=100.0,
        stop_loss=98.0,
        target_1=104.0,
        target_2=106.0,
        risk_reward=2.0,
        timeframe="5d",
        description="L",
        indicators={},
    )
    short_s = SwingSetup(
        symbol="X",
        pattern="SHORTPAT",
        direction="SHORT",
        strength=85,
        entry_zone=100.0,
        stop_loss=102.0,
        target_1=96.0,
        target_2=94.0,
        risk_reward=2.0,
        timeframe="5d",
        description="S",
        indicators={},
    )
    out = dedupe_swing_setups_one_per_symbol([long_s, short_s])
    assert len(out) == 1
    assert out[0].direction == "SHORT"
    assert out[0].confluence_count == 1


def test_dedupe_legacy_max_when_confluence_disabled(monkeypatch):
    monkeypatch.setattr(settings, "SWING_CONFLUENCE_ENABLED", False)
    a, _ = _sample_long_short()
    b = SwingSetup(
        symbol="X",
        pattern="HIGH",
        direction="LONG",
        strength=95,
        entry_zone=100.0,
        stop_loss=98.0,
        target_1=104.0,
        target_2=106.0,
        risk_reward=2.0,
        timeframe="5d",
        description="h",
        indicators={},
    )
    out = dedupe_swing_setups_one_per_symbol([a, b])
    assert len(out) == 1
    assert out[0].pattern == "HIGH"


# ── Phase 1: Accuracy gates ─────────────────────────────────────────────────


def test_rolling_vol_ratio_uses_recent_window(monkeypatch):
    """3-day rolling avg / 20-day avg should smooth a single-day spike."""
    monkeypatch.setattr(settings, "SWING_VOL_ROLLING_DAYS", 3)
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=25, freq="D")
    # 20-day avg ~ 100_000; last 3 bars: 50_000, 60_000, 2_000_000 → mean ≈ 703_333
    # Single-bar ratio would be ~20x (misleading); rolling 3-day ~7x (truer picture).
    vols = [100_000] * 22 + [50_000, 60_000, 2_000_000]
    df = pd.DataFrame({"timestamp": dates, "volume": vols})
    ratio, recent, avg20 = _rolling_vol_ratio(df)
    single_bar_ratio = vols[-1] / (sum(vols[-20:]) / 20)
    assert ratio < single_bar_ratio  # Smoothed down vs single-day
    assert avg20 > 0
    assert recent > 0


def test_rolling_vol_ratio_safe_on_short_df():
    """Short DF falls back to available-bars baseline; empty DF returns (1.0, 0, 0)."""
    df = pd.DataFrame({"volume": [100, 200, 300]})
    ratio, recent, avg20 = _rolling_vol_ratio(df)
    assert avg20 == 200.0  # mean of all 3 bars as baseline
    assert recent == 200.0  # tail(3) mean = 200
    assert ratio == 1.0

    empty = pd.DataFrame({"volume": []})
    r2, rec2, avg2 = _rolling_vol_ratio(empty)
    assert r2 == 1.0 and rec2 == 0.0 and avg2 == 0.0

    missing = pd.DataFrame({"close": [1, 2, 3]})
    r3, _, _ = _rolling_vol_ratio(missing)
    assert r3 == 1.0


def test_adx_gate_blocks_when_below_floor(monkeypatch):
    monkeypatch.setattr(settings, "SWING_ADX_MIN_FOR_BREAKOUT", 20.0)
    df_weak = pd.DataFrame({"ADX_14": [15.0]})
    df_strong = pd.DataFrame({"ADX_14": [22.0]})
    df_none = pd.DataFrame({"ADX_14": [0.0]})
    ok_weak, _ = _adx_gate_ok(df_weak)
    ok_strong, _ = _adx_gate_ok(df_strong)
    ok_none, _ = _adx_gate_ok(df_none)
    assert ok_weak is False
    assert ok_strong is True
    assert ok_none is True  # 0 ADX = missing/not available → don't block


def test_range_breakout_blocked_by_low_adx(monkeypatch):
    """Breakout patterns must be gated by ADX floor."""
    monkeypatch.setattr(settings, "SWING_ADX_MIN_FOR_BREAKOUT", 20.0)
    monkeypatch.setattr(settings, "SWING_REQUIRE_2BAR_BREAKOUT_CONFIRM", False)
    monkeypatch.setattr(settings, "SWING_VOL_RANGE_BREAKOUT_HARD_BLOCK", 0.5)

    now = pd.Timestamp.now().normalize()
    dates = pd.date_range(end=now, periods=25, freq="D")
    # Build a 20-day range 95-105, then a breakout bar close=112 with low ADX.
    highs = [105] * 20 + [106, 107, 108, 109, 113]
    lows = [95] * 20 + [94, 95, 96, 97, 106]
    closes = [100] * 20 + [100, 101, 102, 103, 112]
    df = pd.DataFrame(
        {
            "timestamp": dates,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [300_000] * 25,
            "ADX_14": [10.0] * 25,  # Weak trend
        }
    )
    out = _check_range_breakout(df, "TESTSYM", atr=2.0)
    assert out is None  # ADX gate must block

    # Same data but strong ADX → setup fires.
    df["ADX_14"] = 28.0
    out2 = _check_range_breakout(df, "TESTSYM", atr=2.0)
    assert out2 is not None
    assert out2.direction == "LONG"


def test_range_breakout_requires_2bar_confirmation(monkeypatch):
    """With 2-bar confirm ON, a single-bar spike above range is rejected."""
    monkeypatch.setattr(settings, "SWING_ADX_MIN_FOR_BREAKOUT", 0.0)
    monkeypatch.setattr(settings, "SWING_REQUIRE_2BAR_BREAKOUT_CONFIRM", True)
    monkeypatch.setattr(settings, "SWING_VOL_RANGE_BREAKOUT_HARD_BLOCK", 0.5)

    now = pd.Timestamp.now().normalize()
    dates = pd.date_range(end=now, periods=25, freq="D")
    # Prev close=103 (inside range), current=112 (above range high 105). Single-bar.
    highs = [105] * 20 + [104, 104, 104, 104, 113]
    lows = [95] * 20 + [94, 95, 96, 97, 106]
    closes = [100] * 20 + [101, 102, 103, 103, 112]
    df = pd.DataFrame(
        {
            "timestamp": dates,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [300_000] * 25,
            "ADX_14": [28.0] * 25,
        }
    )
    out = _check_range_breakout(df, "TESTSYM", atr=2.0)
    assert out is None  # Single-bar breakout rejected when confirmation required

    # Two-bar confirmed: prev close=108 also above range_high=105.
    closes2 = [100] * 20 + [101, 102, 103, 108, 112]
    df2 = df.copy()
    df2["close"] = closes2
    df2["open"] = closes2
    out2 = _check_range_breakout(df2, "TESTSYM", atr=2.0)
    assert out2 is not None
    assert out2.direction == "LONG"


def test_ema_cross_requires_sufficient_history(monkeypatch):
    """Golden cross must not fire when df has too few bars (EMA_200 unreliable)."""
    monkeypatch.setattr(settings, "SWING_MIN_BARS_FOR_EMA200", 220)
    monkeypatch.setattr(settings, "SWING_MIN_CLEAN_EMA200_TAIL", 20)

    now = pd.Timestamp.now().normalize()
    dates = pd.date_range(end=now, periods=60, freq="D")  # Only 60 bars
    df = pd.DataFrame(
        {
            "timestamp": dates,
            "close": [100.0] * 60,
            "high": [101.0] * 60,
            "low": [99.0] * 60,
            "volume": [100_000] * 60,
            "EMA_50": [99.0] * 59 + [101.0],   # Fake cross on last bar
            "EMA_200": [100.0] * 60,
            "ADX_14": [25.0] * 60,
        }
    )
    out = _check_ema_cross(df, "TESTSYM", atr=1.0)
    assert out is None  # Blocked due to insufficient history


def test_ema_cross_blocked_when_recent_ema200_has_nans(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MIN_BARS_FOR_EMA200", 220)
    monkeypatch.setattr(settings, "SWING_MIN_CLEAN_EMA200_TAIL", 20)

    now = pd.Timestamp.now().normalize()
    dates = pd.date_range(end=now, periods=250, freq="D")
    ema200 = [100.0] * 230 + [np.nan] * 5 + [100.0] * 15
    df = pd.DataFrame(
        {
            "timestamp": dates,
            "close": [100.0] * 250,
            "high": [101.0] * 250,
            "low": [99.0] * 250,
            "volume": [100_000] * 250,
            "EMA_50": [99.0] * 249 + [101.0],
            "EMA_200": ema200,
            "ADX_14": [25.0] * 250,
        }
    )
    out = _check_ema_cross(df, "TESTSYM", atr=1.0)
    assert out is None  # Last 20 bars had NaNs → not clean


def _real_pattern_df() -> pd.DataFrame:
    """Build a DF that fires a real (non-watchlist) 20-day range breakout pattern.

    Range ~95-105 for 20 days, then 2-bar bullish breakout to ~108 with volume,
    sized to comfortably clear the R:R gate.
    SL = range_high - 0.5*ATR = 105 - 0.5 = 104.5; entry=108; risk=3.5.
    TP1 = entry + range_width = 108 + 10 = 118; reward=10; R:R ~ 2.86.
    """
    now = pd.Timestamp.now().normalize()
    dates = pd.date_range(end=now, periods=40, freq="D")
    # Consolidation: 20 bars between 95-105.
    consolidation = 20
    pre = 40 - consolidation - 2  # bars before the range (also 95-105 flat)
    opens = [100.0] * pre + [100.0] * consolidation + [106.5, 107.5]
    highs = [101.0] * pre + [105.0, 104.0] * (consolidation // 2) + [108.5, 109.5]
    lows = [99.0] * pre + [95.0, 96.0] * (consolidation // 2) + [106.0, 107.0]
    closes = [100.0] * pre + [100.0] * consolidation + [107.5, 108.5]
    # 3-day rolling volume: (150k, 400k, 500k) mean ≈ 350k; 20-day avg ~200k; ratio ≈ 1.75.
    vols = [200_000] * pre + [200_000] * consolidation + [400_000, 500_000]
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
            "RSI_14": [60.0] * 40,
            "EMA_20": [102.0] * 40,
            "EMA_50": [100.0] * 40,
            "ATR_14": [1.0] * 40,
            "ADX_14": [28.0] * 40,
        }
    )


def test_news_decoupled_from_strength_by_default(monkeypatch):
    """When SWING_NEWS_AFFECTS_STRENGTH=False, strongly negative news must not reduce strength."""
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    monkeypatch.setattr(settings, "SWING_NEWS_AFFECTS_STRENGTH", False)
    # Use the real-pattern DF; gap-up pattern will fire (watchlist or real).
    # For news-strength decoupling the watchlist path is fine — the code still
    # skips strength penalty there, so strengths must match regardless.
    df = _real_pattern_df()

    good = scan_swing_setups(df, "TESTSYM", news_context={"context_score": 0.0})
    bad = scan_swing_setups(df, "TESTSYM", news_context={"context_score": -0.9})
    assert good and bad
    # Core invariant: decoupled mode -> strength identical regardless of news.
    assert good[0].strength == bad[0].strength


def test_news_affects_strength_when_opted_in(monkeypatch):
    """Legacy opt-in: news penalty reduces strength. Use a non-watchlist setup."""
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    monkeypatch.setattr(settings, "SWING_NEWS_AFFECTS_STRENGTH", True)
    # Build a setup that is NOT watchlist — synthetic SwingSetup + direct penalty helper.
    from agents.market_intelligence.swing_scanner import _apply_news_penalty

    neutral_strength, neutral_flag = _apply_news_penalty(80, 0.0, "LONG")
    penalised_strength, penalty_flag = _apply_news_penalty(80, -0.9, "LONG")
    assert neutral_flag is False
    assert penalty_flag is True
    assert penalised_strength < neutral_strength


def test_rsi_divergence_detects_bullish_pivot_case(monkeypatch):
    monkeypatch.setattr(settings, "SWING_RSI_DIVERGENCE_LOOKBACK", 35)
    monkeypatch.setattr(settings, "SWING_RSI_DIVERGENCE_PIVOT_WINDOW", 2)
    monkeypatch.setattr(settings, "SWING_RSI_DIVERGENCE_MIN_PRICE_DELTA_PCT", 0.2)
    monkeypatch.setattr(settings, "SWING_RSI_DIVERGENCE_MIN_RSI_DELTA", 1.5)

    n = 45
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="D")
    close = np.array([110.0, 109.0, 108.5, 108.0, 107.5, 107.0, 106.5, 106.0, 105.0, 104.0,
                      103.5, 103.0, 102.8, 103.4, 104.0, 104.8, 105.5, 104.2, 103.1, 102.5,
                      101.8, 101.2, 100.8, 100.4, 100.0, 100.6, 101.3, 102.2, 101.1, 100.4,
                      99.8, 99.2, 98.9, 99.6, 100.5, 101.1, 101.8, 102.4, 102.9, 103.3,
                      103.7, 104.1, 104.4, 104.8, 105.2])
    rsi = np.array([46.0, 45.0, 44.0, 43.5, 42.8, 42.0, 41.5, 41.0, 39.8, 38.5,
                    37.2, 36.5, 35.4, 36.8, 38.2, 40.1, 42.0, 40.5, 39.4, 38.8,
                    38.0, 37.5, 37.0, 36.8, 36.3, 37.8, 39.2, 40.8, 39.9, 39.0,
                    38.4, 38.0, 37.8, 39.5, 41.2, 42.8, 44.0, 45.3, 46.4, 47.2,
                    48.0, 48.8, 49.4, 50.0, 50.6])
    df = pd.DataFrame(
        {
            "timestamp": dates,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": [180_000.0] * n,
            "RSI_14": rsi,
        }
    )

    setup = _check_rsi_divergence(df, "TESTSYM", atr=1.5)
    assert setup is not None
    assert setup.direction == "LONG"


def test_rsi_divergence_detects_bearish_pivot_case(monkeypatch):
    monkeypatch.setattr(settings, "SWING_RSI_DIVERGENCE_LOOKBACK", 35)
    monkeypatch.setattr(settings, "SWING_RSI_DIVERGENCE_PIVOT_WINDOW", 2)
    monkeypatch.setattr(settings, "SWING_RSI_DIVERGENCE_MIN_PRICE_DELTA_PCT", 0.2)
    monkeypatch.setattr(settings, "SWING_RSI_DIVERGENCE_MIN_RSI_DELTA", 1.0)

    n = 45
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="D")
    close = np.array([
        90.0, 90.5, 91.0, 91.6, 92.1, 92.7, 93.2, 93.8, 94.5, 95.1,
        95.8, 96.4, 97.0, 97.6, 98.2, 98.8, 99.4, 100.1, 100.8, 101.4,
        102.0, 102.8, 103.6, 104.4, 105.2, 104.5, 103.8, 103.2, 102.7, 103.4,
        104.1, 104.9, 105.7, 106.4, 107.0, 106.2, 105.5, 104.9, 104.3, 103.8,
        103.3, 102.9, 102.5, 102.2, 101.9,
    ])
    rsi = np.array([
        54.0, 54.7, 55.4, 56.1, 56.8, 57.5, 58.2, 58.9, 59.6, 60.3,
        61.0, 61.7, 62.4, 63.1, 63.8, 64.5, 65.2, 65.9, 66.6, 67.3,
        68.0, 68.8, 69.6, 70.4, 71.3, 70.7, 70.1, 69.6, 69.2, 69.8,
        70.2, 70.6, 70.8, 70.6, 70.2, 69.4, 68.6, 67.9, 67.2, 66.5,
        65.9, 65.3, 64.7, 64.1, 63.6,
    ])
    df = pd.DataFrame(
        {
            "timestamp": dates,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": [180_000.0] * n,
            "RSI_14": rsi,
        }
    )

    setup = _check_rsi_divergence(df, "TESTSYM", atr=1.5)
    assert setup is not None
    assert setup.direction == "SHORT"


def test_sector_momentum_uses_median_by_default(monkeypatch):
    """Median must resist a single outlier heavyweight."""
    monkeypatch.setattr(settings, "SWING_SECTOR_MOMENTUM_AGG", "median")

    # Patch NSE_SECTOR_MAP so our fake symbols resolve to Banking.
    monkeypatch.setattr(
        settings, "NSE_SECTOR_MAP", {"A": "Banking", "B": "Banking", "C": "Banking"}
    )

    # Also patch the module-level attribute read by the scanner.
    import config.settings as cfg
    monkeypatch.setattr(cfg, "NSE_SECTOR_MAP", {"A": "Banking", "B": "Banking", "C": "Banking"})

    def _df(first_close: float, last_close: float) -> pd.DataFrame:
        dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=10, freq="D")
        closes = np.linspace(first_close, last_close, num=10)
        return pd.DataFrame(
            {
                "timestamp": dates,
                "close": closes,
                "high": closes * 1.01,
                "low": closes * 0.99,
                "volume": [100_000] * 10,
            }
        )

    symbol_data = {
        "A": _df(100.0, 101.0),
        "B": _df(100.0, 101.5),
        "C": _df(100.0, 115.0),   # outlier
    }
    mom_median = compute_sector_momentum_map(symbol_data)

    # Compare against the mean aggregation: median should be clearly lower
    # because of the outlier.
    monkeypatch.setattr(settings, "SWING_SECTOR_MOMENTUM_AGG", "mean")
    monkeypatch.setattr(cfg, "SWING_SECTOR_MOMENTUM_AGG", "mean")
    mom_mean = compute_sector_momentum_map(symbol_data)

    assert mom_median["Banking"] < mom_mean["Banking"]
    # Outlier should lift the mean by at least 1.5x the median.
    assert mom_mean["Banking"] > mom_median["Banking"] * 1.5


def test_1h_mtf_returns_none_when_data_unavailable():
    from agents.market_intelligence.swing_scanner import _check_1h_mtf_bullish

    ok, reason = _check_1h_mtf_bullish(None)
    assert ok is None
    assert "unavailable" in reason

    ok_empty, reason_empty = _check_1h_mtf_bullish(pd.DataFrame())
    assert ok_empty is None
    assert "unavailable" in reason_empty


def test_1h_mtf_missing_does_not_add_bonus_or_badge():
    from agents.market_intelligence.swing_scanner import scan_swing_setups

    df = _minimal_daily_df(end_offset_days=1)
    setups = scan_swing_setups(
        df,
        "TESTSYM",
        sector_momentum=2.5,
        sector="Pharma",
        df_1h=None,  # explicitly None
        allow_fallback=True,
    )
    for s in setups:
        assert "✅ 1H confirmed" not in s.description
        assert s.mtf_confirmed is False


def test_meets_min_rr_gate_strict():
    from agents.market_intelligence.swing_scanner import _meets_min_rr

    # Zero risk -> rejected
    assert _meets_min_rr(100.0, 100.0, 105.0, "LONG") is False
    # Low reward (0.4 R < 1.8) -> rejected
    assert _meets_min_rr(100.0, 95.0, 102.0, "LONG") is False
    # Valid R:R (2.0 R >= 1.8) -> accepted
    assert _meets_min_rr(100.0, 95.0, 110.0, "LONG") is True
