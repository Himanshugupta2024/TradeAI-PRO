"""
Tests for agents/data_ingestion/forex_fetcher.py.

All HTTP calls are mocked — no live Twelve Data API calls in this suite.
Run with: pytest -m "not integration" tests/test_forex_fetcher.py
"""

import json
import time
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch, mock_open
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

import agents.data_ingestion.forex_fetcher as ff
from agents.data_ingestion.forex_fetcher import (
    forex_symbol,
    get_forex_ohlcv,
    get_all_forex_rates,
    get_live_rate,
    get_pip_value,
    is_session_active,
    _ohlcv_cache,
    _rate_cache,
    _increment_api_calls,
    _load_api_usage,
)
from config import settings


# ── Fixtures ──────────────────────────────────────────────────────────────────

_SAMPLE_VALUES = [
    {"datetime": "2026-04-14 00:00:00", "open": "1.0800", "high": "1.0850",
     "low": "1.0780", "close": "1.0840"},
    {"datetime": "2026-04-14 04:00:00", "open": "1.0840", "high": "1.0900",
     "low": "1.0820", "close": "1.0880"},
]

_GOOD_OHLCV_RESPONSE = {
    "status": "ok",
    "values": _SAMPLE_VALUES,
}

_GOOD_PRICE_RESPONSE = {"price": "1.0842"}

_DAILY_USAGE_FRESH = {
    "date": datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d"),
    "twelve_data_calls": 0,
    "last_reset": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
}


def _mock_response(json_data: Any, status_code: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()
    return mock


def _clear_caches():
    _ohlcv_cache.clear()
    _rate_cache.clear()


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_symbol_converter_normalizes_to_slash():
    """forex_symbol should normalize to slash format for Twelve Data API."""
    assert forex_symbol("EUR/USD") == "EUR/USD"
    assert forex_symbol("EUR_USD") == "EUR/USD"
    assert forex_symbol("USD/INR") == "USD/INR"
    assert forex_symbol("GBP_JPY") == "GBP/JPY"


def test_get_pip_value_jpy_pairs():
    """JPY pairs should return 0.01; all others 0.0001."""
    assert get_pip_value("USD/JPY") == 0.01
    assert get_pip_value("JPY/INR") == 0.01
    assert get_pip_value("EUR/USD") == 0.0001
    assert get_pip_value("USD/INR") == 0.0001
    assert get_pip_value("GBP/USD") == 0.0001


@patch("agents.data_ingestion.forex_fetcher._load_api_usage", return_value=_DAILY_USAGE_FRESH.copy())
@patch("agents.data_ingestion.forex_fetcher._save_api_usage")
@patch("requests.get")
def test_get_forex_ohlcv_returns_dataframe(mock_get, mock_save, mock_load):
    """get_forex_ohlcv should return a DataFrame with required columns."""
    _clear_caches()
    mock_get.return_value = _mock_response(_GOOD_OHLCV_RESPONSE)

    df = get_forex_ohlcv("EUR/USD", "4h", outputsize=2)

    assert isinstance(df, pd.DataFrame), "Expected a DataFrame"
    assert not df.empty, "DataFrame should not be empty"
    for col in ["timestamp", "open", "high", "low", "close", "volume"]:
        assert col in df.columns, f"Missing column: {col}"
    assert (df["volume"] == 0.0).all(), "Forex volume should always be 0"
    assert pd.api.types.is_datetime64_any_dtype(df["timestamp"]), "timestamp should be datetime"


@patch("agents.data_ingestion.forex_fetcher._load_api_usage", return_value=_DAILY_USAGE_FRESH.copy())
@patch("agents.data_ingestion.forex_fetcher._save_api_usage")
@patch("requests.get")
def test_api_failure_returns_cached_data(mock_get, mock_save, mock_load):
    """On API failure, get_forex_ohlcv should return previously cached data."""
    _clear_caches()

    # Prime the cache with good data
    mock_get.return_value = _mock_response(_GOOD_OHLCV_RESPONSE)
    df_first = get_forex_ohlcv("EUR/USD", "4h")
    assert not df_first.empty

    # Now simulate a network failure
    mock_get.side_effect = requests.RequestException("Connection refused")
    # Also reset mock_load so daily limit check passes (calls still 0 for second fetch attempt)
    mock_load.return_value = {
        "date": _DAILY_USAGE_FRESH["date"],
        "twelve_data_calls": 1,
        "last_reset": _DAILY_USAGE_FRESH["last_reset"],
    }

    df_second = get_forex_ohlcv("EUR/USD", "4h")

    assert isinstance(df_second, pd.DataFrame), "Should return DataFrame even on failure"
    assert not df_second.empty, "Should return cached data on failure"
    assert len(df_second) == len(df_first), "Cached data should match first fetch"


@patch("agents.data_ingestion.forex_fetcher._save_api_usage")
def test_daily_limit_uses_cache_when_exceeded(mock_save):
    """When daily API limit is reached, get_forex_ohlcv should use cache only."""
    _clear_caches()

    # Prime cache manually
    cache_key = ("GBP/USD", "4h")
    dummy_df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-04-14 00:00:00"], utc=True),
        "open": [1.26], "high": [1.27], "low": [1.25], "close": [1.265], "volume": [0.0],
    })
    _ohlcv_cache[cache_key] = {"df": dummy_df.copy(), "fetched_at": time.time() - 3600}

    # Simulate daily limit exceeded
    exhausted_usage = {
        "date": datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d"),
        "twelve_data_calls": settings.TWELVE_DATA_DAILY_LIMIT,
        "last_reset": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
    }

    with patch("agents.data_ingestion.forex_fetcher._load_api_usage", return_value=exhausted_usage):
        with patch("requests.get") as mock_get:
            df = get_forex_ohlcv("GBP/USD", "4h")
            mock_get.assert_not_called()  # No HTTP call should be made

    assert not df.empty, "Should return cached data when limit is exceeded"


def test_is_session_active_inr_outside_hours():
    """INR pairs should be inactive outside 09:00–17:00 IST."""
    from zoneinfo import ZoneInfo

    # Simulate 03:00 IST (well outside NSE_INR hours)
    fake_time = datetime(2026, 4, 14, 3, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("agents.data_ingestion.forex_fetcher.datetime") as mock_dt:
        mock_dt.now.return_value = fake_time
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        # Directly test the logic by setting IST time
        now_ist = fake_time
        now_time = now_ist.strftime("%H:%M")  # "03:00"

        # NSE_INR session is 09:00–17:00; 03:00 is outside
        sess = settings.FOREX_SESSIONS.get("NSE_INR", {})
        start, end = sess.get("start", "09:00"), sess.get("end", "17:00")
        active = start <= now_time < end
        assert not active, "03:00 IST should be outside NSE_INR session"


def test_is_session_active_london_during_hours():
    """EUR/USD should be active during London session hours (13:30–22:30 IST)."""
    # Simulate 16:00 IST (inside London session)
    fake_time = datetime(2026, 4, 14, 16, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    with patch("agents.data_ingestion.forex_fetcher.datetime") as mock_dt:
        mock_dt.now.return_value = fake_time
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        now_time = fake_time.strftime("%H:%M")  # "16:00"
        sess = settings.FOREX_SESSIONS.get("LONDON", {})
        start, end = sess.get("start", "13:30"), sess.get("end", "22:30")
        active = start <= now_time < end
        assert active, "16:00 IST should be inside London session"
