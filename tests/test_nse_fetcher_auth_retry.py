"""A stale Yahoo crumb must trigger exactly one re-auth retry.

Yahoo rotates the cookie/crumb while the process stays alive. yfinance caches it
in a process-wide singleton, so afterwards every request 401s with "Invalid
Crumb" and the fetcher silently falls back to the stale bar cache. That is what
starved the feed for over an hour while the market was open.
"""

import os
import sys
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.market_intelligence import nse_fetcher  # noqa: E402


class _AuthError(Exception):
    pass


class _FakeTicker:
    calls = 0

    def __init__(self, ticker):
        self.ticker = ticker

    def history(self, **kwargs):
        _FakeTicker.calls += 1
        if _FakeTicker.calls == 1:
            raise _AuthError("HTTP Error 401: Invalid Crumb")
        return pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.5],
                "Volume": [1000],
            },
            index=pd.DatetimeIndex([pd.Timestamp.now()]),
        )


@pytest.fixture(autouse=True)
def _reset_calls():
    _FakeTicker.calls = 0
    yield
    _FakeTicker.calls = 0


def test_auth_error_resets_session_and_retries_once():
    with patch.object(nse_fetcher.yf, "Ticker", _FakeTicker), \
         patch.object(nse_fetcher, "_reset_yahoo_session", return_value=True) as reset:
        df = nse_fetcher._fetch_history_with_timeout(
            "TCS.NS", period="5d", interval="5m", timeout_sec=5
        )

    assert reset.call_count == 1
    assert _FakeTicker.calls == 2
    assert not df.empty


def test_non_auth_error_is_not_retried():
    class _Boom(_FakeTicker):
        def history(self, **kwargs):
            raise ValueError("some unrelated failure")

    with patch.object(nse_fetcher.yf, "Ticker", _Boom), \
         patch.object(nse_fetcher, "_reset_yahoo_session", return_value=True) as reset:
        with pytest.raises(ValueError):
            nse_fetcher._fetch_history_with_timeout(
                "TCS.NS", period="5d", interval="5m", timeout_sec=5
            )

    assert reset.call_count == 0


def test_auth_error_markers():
    assert nse_fetcher._looks_like_yahoo_auth_error(_AuthError("HTTP Error 401: Invalid Crumb"))
    assert nse_fetcher._looks_like_yahoo_auth_error(RuntimeError("401 Unauthorized"))
    assert not nse_fetcher._looks_like_yahoo_auth_error(ValueError("bad column"))
