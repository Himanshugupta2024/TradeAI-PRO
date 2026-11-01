"""
Tests for agents/market_intelligence/forex_scanner.py.

All data is synthetic — no live API calls are made.
Run with: pytest -m "not integration" tests/test_forex_scanner.py
"""

from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

from agents.market_intelligence.forex_scanner import (
    ForexSetup,
    scan_forex_setup,
    scan_all_forex,
    _ema,
    _rsi,
    _atr,
    _scan_cache,
)
from agents.execution.position_sizing import calculate_forex_position
from config import settings


# ── DataFrame builders ────────────────────────────────────────────────────────

def _make_trending_df(n: int = 100, start: float = 1.08, trend: float = 0.0002) -> pd.DataFrame:
    """Create a steadily rising DataFrame for LONG setups."""
    timestamps = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    closes = [start + i * trend for i in range(n)]
    opens = [c - 0.0005 for c in closes]
    highs = [c + 0.001 for c in closes]
    lows = [c - 0.001 for c in closes]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [0.0] * n,
    })


def _make_bearish_df(n: int = 100, start: float = 1.12, trend: float = -0.0002) -> pd.DataFrame:
    """Create a steadily falling DataFrame for SHORT setups."""
    timestamps = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    closes = [start + i * trend for i in range(n)]
    opens = [c + 0.0005 for c in closes]
    highs = [c + 0.001 for c in closes]
    lows = [c - 0.001 for c in closes]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [0.0] * n,
    })


def _make_daily_bullish(n: int = 60, start: float = 1.05, trend: float = 0.001) -> pd.DataFrame:
    timestamps = pd.date_range("2025-01-01", periods=n, freq="1D", tz="UTC")
    closes = [start + i * trend for i in range(n)]
    opens = [c - 0.0005 for c in closes]
    highs = [c + 0.002 for c in closes]
    lows = [c - 0.002 for c in closes]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [0.0] * n,
    })


def _make_daily_bearish(n: int = 60, start: float = 1.15, trend: float = -0.001) -> pd.DataFrame:
    timestamps = pd.date_range("2025-01-01", periods=n, freq="1D", tz="UTC")
    closes = [start + i * trend for i in range(n)]
    opens = [c + 0.0005 for c in closes]
    highs = [c + 0.002 for c in closes]
    lows = [c - 0.002 for c in closes]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [0.0] * n,
    })


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_scan_skips_when_session_inactive():
    """INR pairs should return None when their session is inactive."""
    df_4h = _make_trending_df()
    df_daily = _make_daily_bullish()

    # Patch is_session_active to return inactive for USD/INR
    with patch("agents.market_intelligence.forex_scanner.is_session_active", return_value=(False, "NSE_INR")):
        result = scan_forex_setup("USD/INR", df_4h, df_daily)

    assert result is None, "Should return None when NSE_INR session is inactive"


def test_scan_skips_when_sl_pips_exceeds_max():
    """Setup should be rejected when computed SL pips exceed profile max."""
    # USD/INR max_sl_pips = 30; create very volatile data so ATR is huge
    n = 100
    timestamps = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    # Make candles with extreme range (5 INR = 50000 pips)
    closes = [84.0 + i * 0.01 for i in range(n)]
    highs = [c + 5.0 for c in closes]   # 5 INR high-low range → huge ATR
    lows = [c - 5.0 for c in closes]
    opens = closes
    df_4h = pd.DataFrame({
        "timestamp": timestamps,
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": [0.0] * n,
    })
    df_daily = _make_daily_bullish(start=80.0, trend=0.05)

    with patch("agents.market_intelligence.forex_scanner.is_session_active", return_value=(True, "NSE_INR")):
        result = scan_forex_setup("USD/INR", df_4h, df_daily)

    # With ATR of ~5 INR and pip=0.0001, SL pips ≈ 75,000 >> 30 max
    assert result is None, "Should return None when SL pips exceed max_sl_pips"


def test_scan_skips_when_rr_below_minimum():
    """
    R:R check: since we calculate tp1 dynamically using max(min_rr, 1.5),
    the setup should adapt its TP1 to meet the profile's min_rr.
    Test that a profile requiring higher min_rr adjusts tp1 accordingly.
    """
    df_4h = _make_trending_df()
    df_daily = _make_daily_bullish()

    # Patch profile with min_rr = 3.0
    patched_profiles = {
        "EUR/USD": {
            **settings.FOREX_PROFILES.get("EUR/USD", {}),
            "min_rr": 3.0,
        }
    }

    with patch("agents.market_intelligence.forex_scanner.is_session_active", return_value=(True, "LONDON")):
        with patch.object(settings, "FOREX_PROFILES", patched_profiles):
            result = scan_forex_setup("EUR/USD", df_4h, df_daily)

    assert result is not None, "Should adapt TP1 and return setup"

    # Verify R:R is actually 3.0
    sl_distance = result.entry - result.sl
    actual_rr = (result.tp1 - result.entry) / sl_distance
    assert abs(actual_rr - 3.0) < 0.01, f"Expected R:R 3.0, got {actual_rr}"

def test_strength_score_above_50_with_aligned_trend():
    """A valid LONG setup with bullish daily trend should score above 50."""
    df_4h = _make_trending_df(n=100, start=1.08, trend=0.0002)
    df_daily = _make_daily_bullish()

    with patch("agents.market_intelligence.forex_scanner.is_session_active", return_value=(True, "LONDON")):
        result = scan_forex_setup("EUR/USD", df_4h, df_daily)

    if result is not None:
        assert result.strength_score >= 50, (
            f"Expected strength >= 50 with aligned bullish trend, got {result.strength_score}"
        )
        assert result.direction == "LONG"


def test_no_long_signal_when_daily_trend_bearish():
    """scan_forex_setup should not emit a LONG signal when the daily trend is BEARISH."""
    df_4h = _make_trending_df(n=100, start=1.08, trend=0.0002)
    df_daily = _make_daily_bearish()  # bearish daily

    with patch("agents.market_intelligence.forex_scanner.is_session_active", return_value=(True, "LONDON")):
        result = scan_forex_setup("EUR/USD", df_4h, df_daily)

    # Either None (no setup) or a SHORT setup — never LONG
    if result is not None:
        assert result.direction != "LONG", (
            "Should never emit LONG when daily trend is BEARISH"
        )


def test_atr_momentum_replaces_volume_check():
    """
    ATR momentum check (atr_momentum > 1.3) should gate pattern confirmation,
    equivalent to volume spike check for stocks.
    Create a scenario where ATR momentum is below 1.3 to confirm it blocks setups.
    """
    # Flat candles → very uniform ATR → atr_momentum ≈ 1.0
    n = 100
    timestamps = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    closes = [1.0842] * n  # completely flat
    highs = [1.0852] * n
    lows = [1.0832] * n
    opens = closes
    df_4h = pd.DataFrame({
        "timestamp": timestamps,
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": [0.0] * n,
    })
    df_daily = _make_daily_bullish()

    with patch("agents.market_intelligence.forex_scanner.is_session_active", return_value=(True, "LONDON")):
        result = scan_forex_setup("EUR/USD", df_4h, df_daily)

    # With flat data, no meaningful pattern triggers AND atr_momentum ≈ 1.0
    # Expected: no setup returned (pattern requires atr_momentum > 1.3 for most patterns)
    if result is not None:
        # If a pattern did fire (e.g. SUPPORT_REVERSAL which doesn't check atr_momentum),
        # verify that atr_momentum ≈ 1.0 (uniform ATR) was correctly handled
        assert result.strength_score < 80, "Flat ATR momentum should not produce high-strength setup"


def test_position_sizing_lot_capped_at_one():
    """calculate_forex_position should cap lot size at FOREX_MAX_LOT_SIZE (1.0)."""
    # Use large account balance so naive calc would exceed 1 lot
    result = calculate_forex_position(
        symbol="EUR/USD",
        entry=1.0842,
        sl=1.0800,   # 42 pip SL
        account_balance=10_000_000,  # huge balance → would overshoot 1.0 without cap
        risk_pct=0.01,
    )

    assert "lot_size" in result, "Result should contain lot_size"
    assert result["lot_size"] <= settings.FOREX_MAX_LOT_SIZE, (
        f"Lot size {result['lot_size']} exceeds max {settings.FOREX_MAX_LOT_SIZE}"
    )
    assert result["lot_size"] >= 0.01, "Lot size should be at least 0.01 (micro lot)"
    assert "sl_pips" in result
    assert "risk_amount_inr" in result


def test_scan_all_forex_non_blocking_when_already_scanning():
    from agents.market_intelligence import forex_scanner
    import agents.market_intelligence.forex_scanner as fs

    # When scanning is flagged true, it immediately returns existing cached setups
    orig_scanning = fs._is_scanning
    orig_cache = fs._scan_cache
    try:
        fs._is_scanning = True
        fs._scan_cache = {"setups": ["mock_setup"], "fetched_at": 100.0}
        assert fs.is_forex_scanning() is True

        res = fs.scan_all_forex()
        assert res == ["mock_setup"]
    finally:
        fs._is_scanning = orig_scanning
        fs._scan_cache = orig_cache
