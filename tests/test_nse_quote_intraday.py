"""NSE equity marks must come from the intraday series, not the daily bar.

A daily bar is timestamped 00:00 IST, so valuing open positions from
interval="1d" labels every quote ~12h stale the moment the session opens and
marks P&L against the previous close. These tests pin the intraday-first rule.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.valuation import (  # noqa: E402
    IST,
    NSE_OPEN_MAX_AGE_SEC,
    _assess_quote,
    _fetch_nse_quote,
    _fetch_single_quote,
)


def _bars(rows):
    return pd.DataFrame(rows)


def _bar(ts, close=2500.0):
    return {
        "timestamp": pd.Timestamp(ts),
        "close": close,
        "high": close + 5.0,
        "low": close - 5.0,
        "volume": 1000,
    }


def _now_ist_naive():
    return datetime.now(IST).replace(tzinfo=None)


def _patch_fetch(seq):
    """Patch fetch_ohlcv with a sequence of frames keyed by call order."""
    calls = {"n": 0}

    def _fake(symbol, period="5d", interval="5m"):
        idx = calls["n"]
        calls["n"] += 1
        return seq[idx] if idx < len(seq) else None

    return patch("agents.market_intelligence.nse_fetcher.fetch_ohlcv", side_effect=_fake), calls


def test_prefers_recent_intraday_bar():
    fetch, calls = _patch_fetch([_bars([_bar(_now_ist_naive() - timedelta(minutes=3))])])
    with fetch:
        quote = asyncio.run(_fetch_nse_quote("TCS"))

    assert quote["source"] == "NSE_YFINANCE_5M"
    assert quote["close"] == 2500.0
    assert calls["n"] == 1


def test_quote_from_intraday_bar_is_not_stale_during_session():
    bar_ts = _now_ist_naive() - timedelta(minutes=3)
    fetch, _ = _patch_fetch([_bars([_bar(bar_ts)])])
    with fetch:
        quote = asyncio.run(_fetch_single_quote("TCS"))

    assessed = _assess_quote(
        "TCS",
        quote["close"],
        quote["high"],
        quote["low"],
        quote["source"],
        quote["timestamp"],
    )
    assert assessed["age_seconds"] < NSE_OPEN_MAX_AGE_SEC
    assert assessed["is_stale"] is False


def test_falls_back_to_daily_when_intraday_bar_is_ancient():
    old_intraday = _now_ist_naive() - timedelta(days=10)
    daily = _bars([_bar(_now_ist_naive().replace(hour=0, minute=0, second=0), close=2480.0)])
    fetch, calls = _patch_fetch([_bars([_bar(old_intraday)]), daily])
    with fetch:
        quote = asyncio.run(_fetch_nse_quote("TCS"))

    assert quote["source"] == "NSE_YFINANCE_DAILY"
    assert quote["close"] == 2480.0
    assert calls["n"] == 2


def test_returns_empty_dict_when_no_frame_has_a_usable_close():
    fetch, _ = _patch_fetch([None, pd.DataFrame()])
    with fetch:
        assert asyncio.run(_fetch_nse_quote("TCS")) == {}
