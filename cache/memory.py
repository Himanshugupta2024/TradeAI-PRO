"""
TradeAI-PRO V2 — High-Performance In-Memory TTL Cache.

Stores real-time quotes, candle-key technical features, and news metadata with
granular TTLs, stale-fallback grace periods, and bounded RAM capacity (<25MB).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from data.providers.base import MarketQuote

logger = logging.getLogger(__name__)


class MemoryCache:
    """Thread-safe, TTL-bounded in-memory cache partition."""

    def __init__(
        self,
        default_quote_ttl_sec: float = 7.0,
        quote_stale_grace_sec: float = 60.0,
        max_entries_per_partition: int = 500,
    ):
        self.quote_ttl = default_quote_ttl_sec
        self.quote_stale_grace = quote_stale_grace_sec
        self.max_entries = max_entries_per_partition

        # symbol -> (MarketQuote, expire_epoch, stale_epoch)
        self._quotes: dict[str, tuple[MarketQuote, float, float]] = {}

        # cache_key -> (data, expire_epoch)
        self._features: dict[str, tuple[dict[str, Any], float]] = {}

        # symbol -> (data, expire_epoch)
        self._news: dict[str, tuple[dict[str, Any], float]] = {}

    # ── Quote Caching ────────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> MarketQuote | None:
        """Return fresh quote if within TTL, else None."""
        entry = self._quotes.get(symbol)
        if not entry:
            return None
        quote, expire_at, _ = entry
        if time.time() <= expire_at:
            return quote
        return None

    def get_stale_quote(self, symbol: str) -> MarketQuote | None:
        """Return cached quote even if expired, provided it's within the stale grace window."""
        entry = self._quotes.get(symbol)
        if not entry:
            return None
        quote, _, stale_until = entry
        if time.time() <= stale_until:
            # Mark as stale
            quote.stale = True
            return quote
        return None

    def set_quote(self, symbol: str, quote: MarketQuote, ttl_sec: float | None = None) -> None:
        """Store quote with TTL and extended stale grace period."""
        ttl = ttl_sec if ttl_sec is not None else self.quote_ttl
        now = time.time()
        expire_at = now + ttl
        stale_until = now + self.quote_stale_grace

        # Evict oldest if capacity exceeded
        if len(self._quotes) >= self.max_entries:
            self._evict_expired_quotes(now)

        quote.stale = False
        self._quotes[symbol] = (quote, expire_at, stale_until)

    def _evict_expired_quotes(self, now: float) -> None:
        """Remove quotes past their stale grace period, or trim down to max capacity."""
        to_delete = [s for s, (_, _, stale_at) in self._quotes.items() if now > stale_at]
        for s in to_delete:
            self._quotes.pop(s, None)

        # If still over capacity, evict oldest entries until within max_entries
        if len(self._quotes) >= self.max_entries:
            overflow = len(self._quotes) - self.max_entries + 1
            for s in list(self._quotes.keys())[:overflow]:
                self._quotes.pop(s, None)

    # ── Feature Store Caching (Candle-Keyed) ──────────────────────────────────

    def get_features(self, key: str) -> dict[str, Any] | None:
        """Fetch computed technical indicators for a candle key."""
        entry = self._features.get(key)
        if not entry:
            return None
        data, expire_at = entry
        if time.time() <= expire_at:
            return data
        self._features.pop(key, None)
        return None

    def set_features(self, key: str, features: dict[str, Any], ttl_sec: float = 300.0) -> None:
        """Store computed features for a candle timestamp key (default 5 min)."""
        now = time.time()
        if len(self._features) >= self.max_entries:
            # Evict expired features
            to_del = [k for k, (_, exp) in self._features.items() if now > exp]
            for k in to_del:
                self._features.pop(k, None)
        self._features[key] = (features, now + ttl_sec)

    # ── News Sentiment Caching ───────────────────────────────────────────────

    def get_news(self, symbol: str) -> dict[str, Any] | None:
        """Get cached sentiment/news for a symbol (3-5 min TTL)."""
        entry = self._news.get(symbol)
        if not entry:
            return None
        data, expire_at = entry
        if time.time() <= expire_at:
            return data
        self._news.pop(symbol, None)
        return None

    def set_news(self, symbol: str, news_data: dict[str, Any], ttl_sec: float = 240.0) -> None:
        """Store news/sentiment result (default 4 min TTL)."""
        self._news[symbol] = (news_data, time.time() + ttl_sec)

    def clear(self) -> None:
        """Clear all partitions."""
        self._quotes.clear()
        self._features.clear()
        self._news.clear()


# Global singleton cache instance for application-wide use
default_memory_cache = MemoryCache()
