"""
Indian Equity Market (NSE) Session & Holiday Management Engine.

Covers:
  - Exact NSE Cash Market Trading Sessions:
      * Pre-market: 09:00 - 09:15 IST
      * Opening Period / ORB window: 09:15 - 09:45 IST
      * Normal Trading Session: 09:45 - 14:30 IST
      * Closing Period (no new entries): 14:30 - 15:15 IST
      * Mandatory Square-off: 15:15 - 15:30 IST
      * Post-market: 15:30 - 16:00 IST
      * Closed / Night: 16:00 - 09:00 IST
      * Weekend: Saturday & Sunday
      * NSE Calendar Holidays (2026 Schedule)
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# NSE Standard Trading Holidays for 2026 (Major national & festival closures)
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),  # Republic Day
    date(2026, 3, 3),   # Holi
    date(2026, 3, 20),  # Id-Ul-Fitr (Ramzan Id)
    date(2026, 3, 27),  # Ram Navami
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),   # Maharashtra Day
    date(2026, 5, 27),  # Bakri Id / Eid ul-Adha
    date(2026, 6, 26),  # Muharram
    date(2026, 8, 15),  # Independence Day
    date(2026, 10, 2),  # Mahatma Gandhi Jayanti
    date(2026, 10, 20), # Dussehra
    date(2026, 11, 8),  # Diwali Laxmi Pujan (Muhurat session separate)
    date(2026, 11, 10), # Diwali Balipratipada
    date(2026, 11, 24), # Gurunanak Jayanti
    date(2026, 12, 25), # Christmas
}


@dataclass
class MarketSessionInfo:
    session_state: str  # PRE_MARKET | OPENING_PERIOD | NORMAL_SESSION | CLOSING_PERIOD | SQUARE_OFF | POST_MARKET | CLOSED | WEEKEND | HOLIDAY
    is_trading_active: bool  # True strictly during 09:15 - 15:15 IST on trading days
    allows_new_entries: bool  # True strictly during 09:15 - 14:30 IST on trading days
    should_square_off: bool  # True after 15:15 IST or outside trading session
    current_time_ist: str
    session_date: str
    session_label: str
    data_freshness_mode: str  # LIVE | DELAYED | EOD | STALE | CLOSED
    status_color: str  # green | amber | red | blue | gray

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_current_ist_datetime() -> datetime:
    """Current timezone-aware IST datetime."""
    return datetime.now(_IST)


def is_nse_holiday(d: date) -> bool:
    """Check if date is a weekend or scheduled NSE trading holiday."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return True
    return d in NSE_HOLIDAYS_2026


def evaluate_market_session(now: Optional[datetime] = None) -> MarketSessionInfo:
    """
    Deterministic evaluation of the current Indian market session.
    Guarantees that signals from yesterday or weekend candles are never
    treated as active live opportunities.
    """
    dt = now
    if dt is None:
        dt = datetime.now(_IST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=_IST)
    else:
        dt = dt.astimezone(_IST)

    curr_date = dt.date()
    curr_time = dt.time()
    iso_time = dt.strftime("%Y-%m-%d %H:%M:%S IST")
    session_date_str = curr_date.isoformat()

    # Weekend
    if curr_date.weekday() >= 5:
        return MarketSessionInfo(
            session_state="WEEKEND",
            is_trading_active=False,
            allows_new_entries=False,
            should_square_off=True,
            current_time_ist=iso_time,
            session_date=session_date_str,
            session_label="Market Closed (Weekend)",
            data_freshness_mode="CLOSED",
            status_color="gray",
        )

    # Scheduled holiday
    if curr_date in NSE_HOLIDAYS_2026:
        return MarketSessionInfo(
            session_state="HOLIDAY",
            is_trading_active=False,
            allows_new_entries=False,
            should_square_off=True,
            current_time_ist=iso_time,
            session_date=session_date_str,
            session_label="Market Closed (NSE Holiday)",
            data_freshness_mode="CLOSED",
            status_color="gray",
        )

    # Time boundaries in IST
    t_pre_start = dtime(9, 0)
    t_open = dtime(9, 15)
    t_orb_end = dtime(9, 45)
    t_entry_cutoff = dtime(14, 30)
    t_square_off = dtime(15, 15)
    t_close = dtime(15, 30)
    t_post_end = dtime(16, 0)

    if curr_time < t_pre_start:
        return MarketSessionInfo(
            session_state="CLOSED",
            is_trading_active=False,
            allows_new_entries=False,
            should_square_off=True,
            current_time_ist=iso_time,
            session_date=session_date_str,
            session_label="Pre-Session (Opens 09:15 IST)",
            data_freshness_mode="EOD",
            status_color="blue",
        )

    if t_pre_start <= curr_time < t_open:
        return MarketSessionInfo(
            session_state="PRE_MARKET",
            is_trading_active=False,
            allows_new_entries=False,
            should_square_off=False,
            current_time_ist=iso_time,
            session_date=session_date_str,
            session_label="Pre-Market Session (Order Matching)",
            data_freshness_mode="DELAYED",
            status_color="amber",
        )

    if t_open <= curr_time < t_orb_end:
        return MarketSessionInfo(
            session_state="OPENING_PERIOD",
            is_trading_active=True,
            allows_new_entries=True,
            should_square_off=False,
            current_time_ist=iso_time,
            session_date=session_date_str,
            session_label="Opening Period (09:15-09:45 ORB Window)",
            data_freshness_mode="LIVE",
            status_color="green",
        )

    if t_orb_end <= curr_time < t_entry_cutoff:
        return MarketSessionInfo(
            session_state="NORMAL_SESSION",
            is_trading_active=True,
            allows_new_entries=True,
            should_square_off=False,
            current_time_ist=iso_time,
            session_date=session_date_str,
            session_label="Live Trading Session",
            data_freshness_mode="LIVE",
            status_color="green",
        )

    if t_entry_cutoff <= curr_time < t_square_off:
        return MarketSessionInfo(
            session_state="CLOSING_PERIOD",
            is_trading_active=True,
            allows_new_entries=False,
            should_square_off=False,
            current_time_ist=iso_time,
            session_date=session_date_str,
            session_label="Closing Session (No New Entries / Trailing Only)",
            data_freshness_mode="LIVE",
            status_color="amber",
        )

    if t_square_off <= curr_time < t_close:
        return MarketSessionInfo(
            session_state="SQUARE_OFF",
            is_trading_active=False,
            allows_new_entries=False,
            should_square_off=True,
            current_time_ist=iso_time,
            session_date=session_date_str,
            session_label="Intraday Auto Square-Off Active",
            data_freshness_mode="LIVE",
            status_color="red",
        )

    if t_close <= curr_time < t_post_end:
        return MarketSessionInfo(
            session_state="POST_MARKET",
            is_trading_active=False,
            allows_new_entries=False,
            should_square_off=True,
            current_time_ist=iso_time,
            session_date=session_date_str,
            session_label="Post-Market Closing Run",
            data_freshness_mode="EOD",
            status_color="blue",
        )

    return MarketSessionInfo(
        session_state="CLOSED",
        is_trading_active=False,
        allows_new_entries=False,
        should_square_off=True,
        current_time_ist=iso_time,
        session_date=session_date_str,
        session_label="Market Closed (Session Ended)",
        data_freshness_mode="EOD",
        status_color="gray",
    )
