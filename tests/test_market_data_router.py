"""
Unit tests for MarketDataRouter (Failover, Circuit Breakers, Caching, Telemetry).
"""
import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from cache.memory import MemoryCache
from data.providers.base import MarketDataProvider, MarketQuote
from data.router import MarketDataRouter


class DummyProvider(MarketDataProvider):
    def __init__(self, name: str, should_fail: bool = False, latency_ms: float = 10.0):
        self.name = name
        super().__init__()
        self.should_fail = should_fail
        self.call_count = 0

    async def get_quote(self, symbol: str) -> MarketQuote | None:
        self.call_count += 1
        if self.should_fail:
            raise RuntimeError(f"Simulated failure on {self.name}")
        self.health.record_success(15.0)
        return MarketQuote(
            symbol=symbol,
            price=2000.0,
            source=self.name.upper(),
            fetched_at=datetime.now(timezone.utc),
        )

    async def get_candles(self, symbol: str, timeframe: str = "5m", limit: int = 100):
        return []

    async def get_batch_quotes(self, symbols: list[str]) -> dict[str, MarketQuote]:
        self.call_count += 1
        if self.should_fail:
            raise RuntimeError(f"Simulated batch failure on {self.name}")
        self.health.record_success(20.0)
        return {
            s: MarketQuote(symbol=s, price=100.0, source=self.name.upper())
            for s in symbols
        }


@pytest.mark.asyncio
async def test_router_cache_hit():
    cache = MemoryCache()
    primary = DummyProvider("primary")
    router = MarketDataRouter(providers=[primary], cache=cache)

    # Pre-seed cache
    q = MarketQuote(symbol="INFY", price=1550.0, source="CACHED")
    cache.set_quote("INFY", q)

    res = await router.get_quote("INFY")
    assert res is not None
    assert res.price == 1550.0
    assert res.source == "CACHED"
    # Primary should NOT have been called due to cache hit
    assert primary.call_count == 0


@pytest.mark.asyncio
async def test_router_failover_to_secondary():
    cache = MemoryCache()
    primary = DummyProvider("primary", should_fail=True)
    secondary = DummyProvider("secondary", should_fail=False)
    router = MarketDataRouter(providers=[primary, secondary], cache=cache)

    res = await router.get_quote("SBIN")
    assert res is not None
    assert res.source == "SECONDARY"
    assert primary.call_count == 1
    assert secondary.call_count == 1

    # Now verify it got cached
    res2 = await router.get_quote("SBIN")
    assert res2.price == 2000.0
    # Secondary call_count should remain 1 because it hit the cache
    assert secondary.call_count == 1


@pytest.mark.asyncio
async def test_router_circuit_breaker_bypasses_failed_provider():
    cache = MemoryCache()
    primary = DummyProvider("primary", should_fail=True)
    secondary = DummyProvider("secondary", should_fail=False)
    router = MarketDataRouter(providers=[primary, secondary], cache=cache)

    # Fail 3 times on primary to trip breaker
    for sym in ["S1", "S2", "S3"]:
        await router.get_quote(sym)

    assert primary.health.is_healthy is False
    assert primary.call_count == 3

    # 4th call: primary is in cooldown, router must skip primary entirely
    await router.get_quote("S4")
    assert primary.call_count == 3  # Not called!


@pytest.mark.asyncio
async def test_router_stale_fallback_when_all_fail():
    cache = MemoryCache(default_quote_ttl_sec=0.05, quote_stale_grace_sec=1.0)
    primary = DummyProvider("primary", should_fail=True)
    router = MarketDataRouter(providers=[primary], cache=cache)

    # Seed cache
    q = MarketQuote(symbol="WIPRO", price=450.0, source="INITIAL")
    cache.set_quote("WIPRO", q, ttl_sec=0.05)

    import asyncio
    await asyncio.sleep(0.08)  # Let fresh TTL expire

    # Router tries primary (which fails), then falls back to stale cache
    res = await router.get_quote("WIPRO")
    assert res is not None
    assert res.price == 450.0
    assert res.stale is True


@pytest.mark.asyncio
async def test_router_batch_quotes_and_health_telemetry():
    cache = MemoryCache()
    primary = DummyProvider("primary")
    router = MarketDataRouter(providers=[primary], cache=cache)

    quotes = await router.get_batch_quotes(["RELIANCE", "TCS", "HDFC"])
    assert len(quotes) == 3
    assert "RELIANCE" in quotes

    telemetry = router.get_health_status()
    assert "primary" in telemetry
    assert telemetry["primary"]["healthy"] is True
    assert telemetry["primary"]["total_calls"] >= 1
