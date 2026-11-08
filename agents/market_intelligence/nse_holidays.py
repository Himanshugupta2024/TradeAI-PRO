"""
TradeAI-PRO — NSE Gazetted Exchange Holidays Calendar.
Maintains official scheduled trading holidays for NSE cash equities.
"""

from __future__ import annotations
from datetime import date, time as dtime
from typing import Dict, Set, Optional, Tuple

# Official NSE Standard Equity Trading Holidays (2025, 2026, 2027)
NSE_HOLIDAYS_MAP: Dict[date, str] = {
    # 2025 Holidays
    date(2025, 1, 26): "Republic Day",
    date(2025, 2, 26): "Mahashivratri",
    date(2025, 3, 14): "Holi",
    date(2025, 3, 31): "Id-Ul-Fitr (Ramzan Id)",
    date(2025, 4, 10): "Mahavir Jayanti",
    date(2025, 4, 14): "Dr. Ambedkar Jayanti",
    date(2025, 4, 18): "Good Friday",
    date(2025, 5, 1): "Maharashtra Day",
    date(2025, 6, 7): "Bakri Id / Eid ul-Adha",
    date(2025, 7, 6): "Muharram",
    date(2025, 8, 15): "Independence Day",
    date(2025, 8, 27): "Ganesh Chaturthi",
    date(2025, 10, 2): "Mahatma Gandhi Jayanti",
    date(2025, 10, 21): "Dussehra",
    date(2025, 11, 1): "Diwali Laxmi Pujan",
    date(2025, 11, 5): "Gurunanak Jayanti",
    date(2025, 12, 25): "Christmas",

    # 2026 Holidays
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 20): "Id-Ul-Fitr (Ramzan Id)",
    date(2026, 3, 27): "Ram Navami",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 27): "Bakri Id / Eid ul-Adha",
    date(2026, 6, 26): "Muharram",
    date(2026, 8, 15): "Independence Day",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 8): "Diwali Laxmi Pujan",
    date(2026, 11, 10): "Diwali Balipratipada",
    date(2026, 11, 24): "Gurunanak Jayanti",
    date(2026, 12, 25): "Christmas",

    # 2027 Holidays (Projected Gazetted)
    date(2027, 1, 26): "Republic Day",
    date(2027, 3, 22): "Holi",
    date(2027, 3, 26): "Good Friday",
    date(2027, 4, 14): "Dr. Ambedkar Jayanti",
    date(2027, 5, 1): "Maharashtra Day",
    date(2027, 8, 15): "Independence Day",
    date(2027, 10, 2): "Mahatma Gandhi Jayanti",
    date(2027, 12, 25): "Christmas",
}

NSE_HOLIDAYS_SET: Set[date] = set(NSE_HOLIDAYS_MAP.keys())

# Special scheduled trading sessions (e.g. Diwali Muhurat Trading, disaster recovery sessions)
# Format: {date: (start_time, end_time, session_name)}
SPECIAL_TRADING_SESSIONS: Dict[date, Tuple[dtime, dtime, str]] = {
    # 2026 Diwali Muhurat Trading: 14:20:00 to 15:30:00 IST
    date(2026, 11, 8): (dtime(14, 20, 0), dtime(15, 30, 0), "Diwali Muhurat Trading"),
}


def is_holiday(d: date) -> bool:
    """Check if the given date is an official scheduled NSE trading holiday."""
    return d in NSE_HOLIDAYS_SET


def get_holiday_name(d: date) -> str:
    """Return holiday name or empty string if not a holiday."""
    return NSE_HOLIDAYS_MAP.get(d, "")


def get_special_trading_session(d: date) -> Optional[Tuple[dtime, dtime, str]]:
    """Return special scheduled trading session (start_time, end_time, session_name) or None."""
    return SPECIAL_TRADING_SESSIONS.get(d)
