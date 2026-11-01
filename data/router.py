"""
TradeAI-PRO V2 — Central Market Data Router & Failover Engine.

Orchestrates multi-provider market data fetching with automatic circuit breakers,
in-memory TTL cache lookup, and graceful stale fallback.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from cache.memory import MemoryCache, default_memory_cache
from data.providers.base import MarketDataProvider, MarketQuote

logger = logging.getLogger(__name__)


class MarketDataRouter:
    """Production router that coordinates market data providers, caching, and failovers."""

    def __init__(
        self,
        providers: list[MarketDataProvider] | None = None,
        cache: MemoryCache | None = None,
        single_quote_timeout_sec: float = 4.0,
    ):
        self.providers = providers or []
        self.cache = cache or default_memory_cache
        self.quote_timeout = single_quote_timeout_sec

    def register_provider(self, provider: MarketDataProvider) -> None:
        """Add a provider to the routing chain."""
        self.providers.append(provider)

    async def get_quote(self, symbol: str) -> MarketQuote | None:
        """
        Fetch quote for symbol:
        1. In-memory cache hit -> return in <1ms
        2. Primary healthy provider -> return and cache
        3. Fallback healthy provider -> return and cache
        4. Stale cache fallback -> return with stale=True
        """
        # 1. Check L1 Memory Cache
        cached = self.cache.get_quote(symbol)
        if cached is not None and not cached.stale:
            return cached

        # 2. Iterate through healthy providers
        for provider in self.providers:
            if not provider.is_available():
                logger.debug("[MarketDataRouter] Provider '%s' is in cooldown, skipping", provider.name)
                continue

            try:
                quote = await asyncio.wait_for(
                    provider.get_quote(symbol),
                    timeout=self.quote_timeout,
                )
                if quote is not None and quote.is_valid():
                    self.cache.set_quote(symbol, quote)
                    return quote
            except Exception as exc:
                provider.health.record_failure()
                logger.debug(
                    "[MarketDataRouter] Provider '%s' failed for %s: %s",
                    provider.name,
                    symbol,
                    exc,
                )
                continue

        # 3. Last-resort stale cache grace period
        stale_quote = self.cache.get_stale_quote(symbol)
        if stale_quote is not None:
            logger.warning("[MarketDataRouter] Serving stale cached quote for %s", symbol)
            return stale_quote

        return None

    async def get_batch_quotes(self, symbols: list[str]) -> dict[str, MarketQuote]:
        """
        Efficient multi-symbol fetch:
        1. Collect all fresh quotes from cache
        2. Batch-download missing symbols via primary provider
        3. Fall back to secondary providers for remaining missing symbols
        """
        if not symbols:
            return {}

        results: dict[str, MarketQuote] = {}
        missing: list[str] = []

        # 1. Cache partition check
        for sym in symbols:
            q = self.cache.get_quote(sym)
            if q is not None and not q.stale:
                results[sym] = q
            else:
                missing.append(sym)

        if not missing:
            return results

        # 2. Iterate through providers for missing symbols
        for provider in self.providers:
            if not missing:
                break
            if not provider.is_available():
                continue

            try:
                fetched = await provider.get_batch_quotes(missing)
                for sym, quote in fetched.items():
                    if quote.is_valid():
                        results[sym] = quote
                        self.cache.set_quote(sym, quote)
                        if sym in missing:
                            missing.remove(sym)
            except Exception as exc:
                provider.health.record_failure()
                logger.warning(
                    "[MarketDataRouter] Batch fetch on '%s' failed: %s",
                    provider.name,
                    exc,
                )
                continue

        # 3. Stale cache fallback for any still missing symbols
        for sym in missing:
            stale_q = self.cache.get_stale_quote(sym)
            if stale_q is not None:
                results[sym] = stale_q

        return results

    async def get_candles(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch historical candle bars with failover."""
        for provider in self.providers:
            if not provider.is_available():
                continue
            try:
                candles = await asyncio.wait_for(
                    provider.get_candles(symbol, timeframe=timeframe, limit=limit),
                    timeout=8.0,
                )
                if candles:
                    return candles
            except Exception as exc:
                provider.health.record_failure()
                logger.debug(
                    "[MarketDataRouter] Candles fetch on '%s' failed for %s: %s",
                    provider.name,
                    symbol,
                    exc,
                )
                continue

        return []

    def get_health_status(self) -> dict[str, Any]:
        """Expose operational telemetry for all providers."""
        import time
        now = time.time()
        status: dict[str, Any] = {}
        for p in self.providers:
            h = p.health
            in_cooldown = not h.is_healthy and now < h.cooldown_until
            status[p.name] = {
                "healthy": h.is_healthy,
                "in_cooldown": in_cooldown,
                "cooldown_remaining_sec": max(0.0, round(h.cooldown_until - now, 1)) if in_cooldown else 0.0,
                "consecutive_errors": h.consecutive_errors,
                "total_calls": h.total_calls,
                "total_errors": h.total_errors,
                "avg_latency_ms": round(h.avg_latency_ms, 1),
            }
        return status


# Factory helper to construct standard default router
def create_default_router() -> MarketDataRouter:
    from data.providers.yahoo import YahooProvider
    from data.providers.twelve_data import TwelveDataProvider

    yahoo = YahooProvider(timeout=8.0)
    twelve = TwelveDataProvider()
    return MarketDataRouter(providers=[yahoo, twelve])
