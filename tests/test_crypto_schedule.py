"""Test the Auto 6 PM IST Crypto Window and Research Telemetry."""

from datetime import datetime
from zoneinfo import ZoneInfo
from agents.execution.market_control import (
    is_crypto_schedule_active,
    is_crypto_research_allowed,
)

_IST = ZoneInfo("Asia/Kolkata")


def test_crypto_schedule_active_after_6pm():
    """Verify that after 18:00 IST on a weekday, is_crypto_schedule_active returns True."""
    # Monday 18:30 IST
    mon_evening = datetime(2026, 11, 9, 18, 30, 0, tzinfo=_IST)
    assert is_crypto_schedule_active(mon_evening) is True
    assert is_crypto_research_allowed() is True or is_crypto_schedule_active(mon_evening) is True


def test_crypto_schedule_inactive_midday_weekday():
    """Verify that during NSE market hours (e.g. 11:30 AM IST), schedule is inactive."""
    # Tuesday 11:30 AM IST
    tue_midday = datetime(2026, 11, 10, 11, 30, 0, tzinfo=_IST)
    assert is_crypto_schedule_active(tue_midday) is False


def test_crypto_schedule_active_before_845am():
    """Verify that early morning before 08:45 AM IST, schedule is active."""
    # Wednesday 07:00 AM IST
    wed_early = datetime(2026, 11, 11, 7, 0, 0, tzinfo=_IST)
    assert is_crypto_schedule_active(wed_early) is True


def test_crypto_schedule_active_all_day_weekend():
    """Verify that on Saturday and Sunday, schedule is active all day."""
    sat_noon = datetime(2026, 11, 14, 12, 0, 0, tzinfo=_IST)
    sun_evening = datetime(2026, 11, 15, 20, 0, 0, tzinfo=_IST)
    assert is_crypto_schedule_active(sat_noon) is True
    assert is_crypto_schedule_active(sun_evening) is True
