"""Tests for swing scanner staleness, regime gate, and dedupe helpers."""

import pandas as pd

from agents.market_intelligence.swing_scanner import (
    SwingSetup,
    apply_swing_regime_gate,
    dedupe_swing_setups_one_per_symbol,
    scan_swing_setups,
)
from config import settings


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


def test_swing_skips_when_last_bar_too_old(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 5.0)
    df = _minimal_daily_df(end_offset_days=20)
    assert scan_swing_setups(df, "TESTSYM") == []


def test_swing_allows_recent_bar_with_high_max_age(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    df = _minimal_daily_df(end_offset_days=1)
    out = scan_swing_setups(df, "TESTSYM")
    assert isinstance(out, list)


def test_apply_regime_gate_drops_long_in_bearish(monkeypatch):
    monkeypatch.setattr(settings, "SWING_REGIME_GATE_ENABLED", True)
    monkeypatch.setattr(settings, "SWING_BLOCK_LONG_IN_BEARISH_TREND", True)
    monkeypatch.setattr(settings, "SWING_BLOCK_SHORT_IN_BULLISH_TREND", False)
    long_s, short_s = _sample_long_short()
    ctx = {"index_trend": "BEARISH", "index_regime": "RANGING", "index_symbol": "NIFTY 50"}
    out = apply_swing_regime_gate([long_s, short_s], ctx)
    assert len(out) == 1
    assert out[0].direction == "SHORT"


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
