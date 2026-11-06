"""Tests for mark-to-market quote freshness (no network)."""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.valuation import (  # noqa: E402
    IST,
    CRYPTO_MAX_AGE_SEC,
    NSE_CLOSED_MAX_AGE_SEC,
    NSE_OPEN_MAX_AGE_SEC,
    NSE_WEEKEND_MAX_AGE_SEC,
    _quote_age_seconds,
    _stale_threshold_seconds,
    resolve_open_trades_valuation,
)


class _Trade:
    def __init__(self, symbol="BTC/USDT", direction="LONG", entry=100.0, qty=2.0, sl=95.0, target=110.0):
        self.id = 1
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry
        self.quantity = qty
        self.stop_loss = sl
        self.target = target
        self.entry_time = "2026-11-06T10:00:00+05:30"
        self.signal_type = "SWING"
        self.regime = "TRENDING"
        self.confidence = 70.0
        self.initial_quantity = qty
        self.original_target = target
        self.target_multiplier = 1.0
        self.partial_target = 0.0
        self.partial_taken = False
        self.realized_pnl = 0.0
        self.fx_rate_entry = 1.0


class _Engine:
    def __init__(self, trades):
        self.open_trades = list(trades)
        self.trades = []
        self.capital = 100000.0
        self.initial_capital = 100000.0

    def _trade_total_pnl(self, trade):
        return 0.0


class _Svc:
    def __init__(self, market_data):
        self._market_data = market_data


class _FXRecord:
    rate = 1.0
    source = "TEST"
    timestamp = "2026-11-06T00:00:00+00:00"


class _FXService:
    def get_fx_rate(self, *args, **kwargs):
        return _FXRecord()


def _df(ts, close=105.0):
    return pd.DataFrame([{
        "timestamp": pd.Timestamp(ts),
        "close": close,
        "high": close + 1.0,
        "low": close - 1.0,
    }])


def _run(engine, svc, fetch_result=None, refresh_stale=True):
    async def _fake_fetch(symbol):
        return fetch_result or {}

    with patch("api.valuation._fetch_single_quote", side_effect=_fake_fetch), \
         patch("api.valuation.FXRateService.get_instance", return_value=_FXService()):
        return asyncio.run(
            resolve_open_trades_valuation(engine, svc, refresh_stale=refresh_stale)
        )


def test_thresholds_are_market_aware():
    open_now = datetime(2026, 11, 6, 10, 0, tzinfo=IST)
    closed_now = datetime(2026, 11, 6, 20, 0, tzinfo=IST)
    weekend_now = datetime(2026, 11, 7, 12, 0, tzinfo=IST)

    assert _stale_threshold_seconds("TCS", now=open_now) == NSE_OPEN_MAX_AGE_SEC
    assert _stale_threshold_seconds("TCS", now=closed_now) == NSE_CLOSED_MAX_AGE_SEC
    assert _stale_threshold_seconds("TCS", now=weekend_now) == NSE_WEEKEND_MAX_AGE_SEC
    assert _stale_threshold_seconds("BTC/USDT", now=open_now) == CRYPTO_MAX_AGE_SEC


def test_naive_timestamp_is_treated_as_ist():
    stamp = datetime(2026, 11, 6, 10, 0, 0)
    now = datetime(2026, 11, 6, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
    assert _quote_age_seconds(stamp, now=now) == 0.0


def test_stale_crypto_quote_is_flagged():
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    result = _run(_Engine([_Trade()]), _Svc({"BTC/USDT": _df(old)}))
    trade = result["open_trades"][0]

    assert trade["quote_status"] == "STALE"
    assert trade["is_stale_quote"] is True
    assert trade["quote_age_seconds"] > 4 * 3600
    assert trade["priced"] is True
    assert trade["pnl_status"] == "STALE"
    assert result["summary"]["stale_quotes_count"] == 1
    assert result["summary"]["valuation_status"] == "STALE"


def test_fresh_crypto_quote_is_live():
    fresh = datetime.now(timezone.utc) - timedelta(minutes=2)
    result = _run(_Engine([_Trade()]), _Svc({"BTC/USDT": _df(fresh)}))
    trade = result["open_trades"][0]

    assert trade["quote_status"] == "LIVE"
    assert trade["priced"] is True
    assert trade["pnl_status"] == "MARKED"
    assert result["summary"]["valuation_status"] == "COMPLETE"


def test_missing_quote_is_refetched():
    fresh_quote = {
        "close": 104.0,
        "high": 105.0,
        "low": 103.0,
        "source": "BINANCE_FEED",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    result = _run(_Engine([_Trade()]), _Svc({}), fetch_result=fresh_quote)
    trade = result["open_trades"][0]

    assert trade["quote_status"] == "LIVE"
    assert trade["quote_source"] == "BINANCE_FEED"
    assert trade["current_price"] == 104.0
    assert trade["priced"] is True


def test_stale_quote_is_refetched():
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    fresh_quote = {
        "close": 106.0,
        "high": 107.0,
        "low": 105.0,
        "source": "BINANCE_FEED",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    result = _run(_Engine([_Trade()]), _Svc({"BTC/USDT": _df(old)}), fetch_result=fresh_quote)
    trade = result["open_trades"][0]

    assert trade["quote_status"] == "LIVE"
    assert trade["current_price"] == 106.0


def test_stale_quote_not_refetched_by_default():
    """The overview endpoint must not pay network latency for stale quotes."""
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    fresh_quote = {
        "close": 106.0,
        "high": 107.0,
        "low": 105.0,
        "source": "BINANCE_FEED",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    result = _run(
        _Engine([_Trade()]),
        _Svc({"BTC/USDT": _df(old)}),
        fetch_result=fresh_quote,
        refresh_stale=False,
    )
    trade = result["open_trades"][0]

    assert trade["quote_status"] == "STALE"
    assert trade["current_price"] == 105.0


def test_unpriced_fallback_is_explicit():
    result = _run(_Engine([_Trade()]), _Svc({}), fetch_result={})
    trade = result["open_trades"][0]

    assert trade["quote_status"] == "UNPRICED"
    assert trade["is_entry_fallback"] is True
    assert trade["priced"] is False
    assert trade["pnl_status"] == "UNPRICED"
    assert trade["unrealized_pnl"] == 0.0
    assert result["summary"]["missing_quotes_count"] == 1
    assert result["summary"]["unpriced_quotes_count"] == 1
    assert result["summary"]["valuation_status"] == "PARTIAL"
