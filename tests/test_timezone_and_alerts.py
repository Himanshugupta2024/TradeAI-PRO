"""
Unit tests for Option 4A: Timezone and Datetime Drift Verification.
Verifies Asia/Kolkata aware timestamps, Telegram alert clock integrity,
and naive-vs-aware datetime subtraction safety.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo
import pytest

from agents.alerts.telegram_bot import _ist_now, _ist_now_str, _IST
from agents.macro.risk_gauge import RiskIndicators, RiskGauge


def test_ist_now_timezone_binding():
    """Verify that _ist_now() returns an aware datetime in Asia/Kolkata."""
    now_ist = _ist_now()
    assert now_ist.tzinfo is not None
    assert str(now_ist.tzinfo) == "Asia/Kolkata" or now_ist.utcoffset() == timedelta(hours=5, minutes=30)


def test_telegram_ist_str_cloud_utc_conversion():
    """
    Simulate a cloud server where system time is UTC (e.g., 09:30:00 UTC).
    Verify that _ist_now_str accurately converts to 03:00:00 PM IST (5.5h ahead).
    """
    fixed_utc = datetime(2026, 11, 22, 9, 30, 0, tzinfo=timezone.utc)
    with patch("agents.alerts.telegram_bot.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: fixed_utc.astimezone(tz) if tz else fixed_utc
        ist_str = _ist_now_str()

        assert "22 Nov 2026" in ist_str
        assert "03:00 PM IST" in ist_str


def test_risk_indicators_naive_timestamp_deserialization():
    """
    Verify that RiskIndicators deserialization safely upgrades naive ISO timestamps
    to aware UTC timestamps, preventing TypeError in age subtraction.
    """
    naive_data = {
        "vix": 18.5,
        "vix_change_pct": 2.5,
        "btc_trend": "RISING",
        "nifty_trend": "RISING",
        "usdjpy_level": 154.2,
        "usdjpy_trend": "FLAT",
        "gold_trend": "FLAT",
        "risk_regime": "NEUTRAL",
        "india_vix": 14.2,
        "india_vix_trend": "FLAT",
        "ad_ratio": 1.2,
        "above_ema20_pct": 65.0,
        "breadth_regime": "STRONG_BREADTH",
        "timestamp": "2026-11-22T10:00:00",  # Naive ISO string without +00:00
    }

    indicators = RiskIndicators.from_dict(naive_data)
    assert indicators.timestamp.tzinfo is not None
    assert indicators.timestamp.tzinfo == timezone.utc

    gauge = RiskGauge(cache_ttl_seconds=300)
    # Must not raise TypeError: can't subtract offset-naive and offset-aware datetimes
    is_fresh = gauge._is_cache_fresh(indicators)
    assert isinstance(is_fresh, bool)


def test_centralized_rsi_thresholds():
    """Verify that centralized RSI boundaries are loaded from settings and govern scoring."""
    import pandas as pd
    from config import settings
    from agents.signal_engine.quality_scorer import _score_trend_alignment

    # Assert standard baseline values
    assert settings.RSI_OVERSOLD == 30
    assert settings.RSI_OVERBOUGHT == 70
    assert settings.RSI_EXTREME_OVERSOLD == 28
    assert settings.RSI_EXTREME_OVERBOUGHT == 72
    assert settings.RSI_OVERSOLD_TRENDING == 35
    assert settings.RSI_OVERBOUGHT_TRENDING == 65
    assert settings.RSI_MILD_OVERSOLD == 40
    assert settings.RSI_MILD_OVERBOUGHT == 60

    # In a BEARISH market, counter-trend LONG only earns credit if rsi < RSI_EXTREME_OVERSOLD (28)
    row_deep = pd.Series({"htf_trend": "BEARISH", "RSI_14": 25.0})
    assert _score_trend_alignment(row_deep, is_long=True) == 6

    row_not_deep = pd.Series({"htf_trend": "BEARISH", "RSI_14": 29.0})
    assert _score_trend_alignment(row_not_deep, is_long=True) == 0
