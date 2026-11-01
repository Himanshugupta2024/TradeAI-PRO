"""
Unit tests for data providers (Base contracts, YahooProvider, TwelveDataProvider).
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from data.providers.base import MarketQuote, ProviderHealth
from data.providers.yahoo import YahooProvider, _normalize_yahoo_symbol, _clean_symbol_from_yahoo
from data.providers.twelve_data import TwelveDataProvider


def test_market_quote_validity():
    # Valid quote
    q = MarketQuote(symbol="INFY", price=1500.0, high=1510.0, low=1490.0)
    assert q.is_valid() is True

    # Invalid: non-positive or non-finite price
    assert MarketQuote(symbol="INFY", price=-10.0).is_valid() is False
    assert MarketQuote(symbol="INFY", price=0.0).is_valid() is False
    assert MarketQuote(symbol="INFY", price=float("nan")).is_valid() is False

    # Invalid: low > high
    assert MarketQuote(symbol="INFY", price=100.0, high=90.0, low=110.0).is_valid() is False


def test_provider_health_circuit_breaker():
    health = ProviderHealth(name="test_source")
    assert health.is_healthy is True

    # 1 success
    health.record_success(latency_ms=120.0)
    assert health.avg_latency_ms == 120.0
    assert health.total_calls == 1

    # 2 consecutive failures
    health.record_failure(max_consecutive_errors=3)
    health.record_failure(max_consecutive_errors=3)
    assert health.is_healthy is True
    assert health.consecutive_errors == 2

    # 3rd failure trips the breaker
    health.record_failure(max_consecutive_errors=3, cooldown_seconds=10.0)
    assert health.is_healthy is False
    assert health.cooldown_until > 0.0


def test_yahoo_symbol_normalization():
    assert _normalize_yahoo_symbol("RELIANCE") == "RELIANCE.NS"
    assert _normalize_yahoo_symbol("NIFTY 50") == "^NSEI"
    assert _normalize_yahoo_symbol("SENSEX") == "^BSESN"
    assert _normalize_yahoo_symbol("BANKNIFTY") == "^NSEBANK"
    assert _normalize_yahoo_symbol("BTC/USDT") == "BTC-USDT"

    assert _clean_symbol_from_yahoo("^NSEI") == "NIFTY 50"
    assert _clean_symbol_from_yahoo("TCS.NS") == "TCS"


@pytest.mark.asyncio
async def test_twelve_data_credit_budgeting():
    provider = TwelveDataProvider(api_key="test_key", max_credits_per_min=2)

    # 2 credits available
    assert provider._has_credit_budget() is True
    provider._consume_credit()
    assert provider._has_credit_budget() is True
    provider._consume_credit()

    # 3rd call should report no budget
    assert provider._has_credit_budget() is False

    # Calling get_quote with exhausted budget must yield None without making network call
    res = await provider.get_quote("EUR/USD")
    assert res is None
