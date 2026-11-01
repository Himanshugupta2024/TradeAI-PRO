"""
Unit tests for MemoryCache (Quotes, Stale Grace, Candle-Keyed Features).
"""
import time
import pytest
from datetime import datetime, timezone

from cache.memory import MemoryCache
from data.providers.base import MarketQuote


def test_quote_fresh_and_expiration():
    cache = MemoryCache(default_quote_ttl_sec=0.1, quote_stale_grace_sec=0.5)
    quote = MarketQuote(
        symbol="TCS",
        price=3500.0,
        source="TEST",
        fetched_at=datetime.now(timezone.utc),
    )
    cache.set_quote("TCS", quote)

    # 1. Fresh hit
    fresh = cache.get_quote("TCS")
    assert fresh is not None
    assert fresh.price == 3500.0
    assert fresh.stale is False

    # 2. Wait for quote TTL to expire, but within stale grace
    time.sleep(0.12)
    assert cache.get_quote("TCS") is None

    # Stale grace should still serve the quote with stale=True
    stale = cache.get_stale_quote("TCS")
    assert stale is not None
    assert stale.stale is True

    # 3. Wait past stale grace
    time.sleep(0.4)
    assert cache.get_stale_quote("TCS") is None


def test_candle_keyed_features():
    cache = MemoryCache()
    key = "RELIANCE:5m:2026-11-02T10:35:00"
    features = {"rsi": 58.2, "macd": 1.45, "atr": 12.0}

    cache.set_features(key, features, ttl_sec=0.15)
    assert cache.get_features(key) == features

    time.sleep(0.18)
    assert cache.get_features(key) is None


def test_cache_capacity_eviction():
    cache = MemoryCache(max_entries_per_partition=5)
    for i in range(10):
        q = MarketQuote(symbol=f"SYM_{i}", price=100.0 + i)
        cache.set_quote(f"SYM_{i}", q, ttl_sec=0.05)

    # Allow old entries to expire
    time.sleep(0.06)
    # Add new entry triggering eviction
    cache.set_quote("NEW_SYM", MarketQuote(symbol="NEW_SYM", price=500.0))

    # Total quotes should be bounded <= max_entries
    assert len(cache._quotes) <= 6
