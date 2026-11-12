"""
TradeAI-PRO — NSE Gazetted Exchange Holidays Calendar.
Maintains official scheduled trading holidays for NSE cash equities.
Loads holidays dynamically from static config/nse_holidays_<year>.json,
with fail-closed behavior to CLOSED if current year config is missing.
"""

from __future__ import annotations
import json
import logging
import os
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Dict, Set, Optional, Tuple

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_LOADED_HOLIDAYS_MAP: Dict[date, str] = {}
_LOADED_SPECIAL_SESSIONS: Dict[date, Tuple[dtime, dtime, str]] = {}
_MISSING_YEARS_WARNED: Set[int] = set()


def _load_year_holidays(year: int) -> bool:
    """Load JSON holiday file for a given year into memory."""
    json_path = _CONFIG_DIR / f"nse_holidays_{year}.json"
    if not json_path.exists():
        if year not in _MISSING_YEARS_WARNED:
            logger.critical(
                "[NSE Holidays] MISSING HOLIDAY CALENDAR: %s not found! Fail-closed mode active.",
                json_path,
            )
            _MISSING_YEARS_WARNED.add(year)
        return False

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        holidays = data.get("holidays", {})
        for d_str, name in holidays.items():
            dt_obj = date.fromisoformat(d_str)
            _LOADED_HOLIDAYS_MAP[dt_obj] = name

        special = data.get("special_sessions", {})
        for d_str, sess in special.items():
            dt_obj = date.fromisoformat(d_str)
            start_parts = [int(p) for p in sess.get("start", "18:15:00").split(":")]
            end_parts = [int(p) for p in sess.get("end", "19:15:00").split(":")]
            _LOADED_SPECIAL_SESSIONS[dt_obj] = (
                dtime(*start_parts),
                dtime(*end_parts),
                sess.get("name", "Special Trading Session"),
            )
        return True
    except Exception as exc:
        logger.error("[NSE Holidays] Error parsing %s: %s", json_path, exc)
        return False


# Pre-load known calendar years
for _y in (2025, 2026, 2027):
    _load_year_holidays(_y)

# Current year startup check
_current_year = datetime.now().year
if _current_year not in (2025, 2026, 2027):
    if not _load_year_holidays(_current_year):
        logger.warning(
            "[NSE Holidays] Current year %d holiday calendar not found in %s! Fail-closed active.",
            _current_year,
            _CONFIG_DIR,
        )

# Maintain legacy dictionary references for backward compatibility
NSE_HOLIDAYS_MAP = _LOADED_HOLIDAYS_MAP
NSE_HOLIDAYS_SET: Set[date] = set(_LOADED_HOLIDAYS_MAP.keys())
SPECIAL_TRADING_SESSIONS = _LOADED_SPECIAL_SESSIONS


def is_holiday(d: date) -> bool:
    """
    Check if the given date is an official scheduled NSE trading holiday.
    Fail-closed: If calendar for year is missing, treated as holiday / closed.
    """
    if d.year not in (2025, 2026, 2027) and d.year not in _MISSING_YEARS_WARNED:
        _load_year_holidays(d.year)

    if d in _LOADED_HOLIDAYS_MAP:
        return True
    if d.year in _MISSING_YEARS_WARNED:
        # Fail-closed policy for unconfigured future/past years
        return True
    return False


def get_holiday_name(d: date) -> str:
    """Return holiday name or empty string if not a holiday."""
    if d in _LOADED_HOLIDAYS_MAP:
        return _LOADED_HOLIDAYS_MAP[d]
    if d.year in _MISSING_YEARS_WARNED:
        return "Unconfigured Calendar (Fail-Closed to Market Closed)"
    return ""


def get_special_trading_session(d: date) -> Optional[Tuple[dtime, dtime, str]]:
    """Return special scheduled trading session (start_time, end_time, session_name) or None."""
    return _LOADED_SPECIAL_SESSIONS.get(d)
