"""
Tests for scripts/deploy.py:
Verifies NSE market hours safety check and pre-flight logic.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pytest

from scripts.deploy import is_nse_market_open

_IST = ZoneInfo("Asia/Kolkata")


def test_nse_market_hours_weekend():
    """Weekend should always return is_open=False."""
    # Sunday
    sun_utc = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)
    is_open, reason = is_nse_market_open(sun_utc)
    assert is_open is False
    assert "Weekend" in reason


def test_nse_market_hours_active_session():
    """Wednesday 11:30 AM IST (06:00 UTC) must return is_open=True."""
    wed_utc = datetime(2026, 11, 25, 6, 0, 0, tzinfo=timezone.utc)
    is_open, reason = is_nse_market_open(wed_utc)
    assert is_open is True
    assert "Active NSE Session" in reason


def test_nse_market_hours_pre_market():
    """Wednesday 08:30 AM IST (03:00 UTC) must return is_open=False."""
    wed_utc = datetime(2026, 11, 25, 3, 0, 0, tzinfo=timezone.utc)
    is_open, reason = is_nse_market_open(wed_utc)
    assert is_open is False
    assert "Pre-market" in reason


def test_nse_market_hours_post_market():
    """Wednesday 16:00 PM IST (10:30 UTC) must return is_open=False."""
    wed_utc = datetime(2026, 11, 25, 10, 30, 0, tzinfo=timezone.utc)
    is_open, reason = is_nse_market_open(wed_utc)
    assert is_open is False
    assert "Post-market" in reason
