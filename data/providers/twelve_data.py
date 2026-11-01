"""
TradeAI-PRO V2 — Twelve Data Provider (Fallback & Forex).

Guarded with rate budgeting to stay within Twelve Data's 8 credits/minute free-tier cap.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from config import settings
from data.providers.base import MarketDataProvider, MarketQuote

logger = logging.getLogger(__name__)


class TwelveDataProvider(MarketDataProvider):
    """Twelve Data API provider with built-in credit rate budgeting."""

    name: str = "twelve_data"

    def __init__(self, api_key: str | None = None, max_credits_per_min: int = 8):
        super().__init__()
        self.api_key = api_key or getattr(settings, "TWELVE_DATA_API_KEY", "") or ""
        self.max_credits_per_min = max_credits_per_min
        self._call_timestamps: list[float] = []

    def _has_credit_budget(self) -> bool:
        """Check if we have credits remaining in the rolling 60-second window."""
        now = time.time()
        # Evict timestamps older than 60s
        self._call_timestamps = [t for t in self._call_timestamps if now - t < 60.0]
        return len(self._call_timestamps) < self.max_credits_per_min

    def _consume_credit(self) -> None:
        self._call_timestamps.append(time.time())

    async def get_quote(self, symbol: str) -> MarketQuote | None:
        """Fetch quote for symbol if budget allows; returns None if budget exhausted."""
        if not self.api_key:
            return None

        if not self._has_credit_budget():
            logger.debug("[TwelveDataProvider] Rate budget exhausted (8/min cap). Yielding fallback.")
            return None

        clean_sym = symbol.replace("/", "_").replace("-", "_")
        url = f"https://api.twelvedata.com/price?symbol={clean_sym}&apikey={self.api_key}"

        t0 = time.perf_counter()
        try:
            self._consume_credit()
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    self.health.record_failure()
                    return None
                data = resp.json()
                price_str = data.get("price")
                if not price_str:
                    return None
                price = float(price_str)
                latency_ms = (time.perf_counter() - t0) * 1000.0
                self.health.record_success(latency_ms)

                quote = MarketQuote(
                    symbol=symbol,
                    price=price,
                    source="TWELVE_DATA",
                    fetched_at=datetime.now(timezone.utc),
                    quality_score=0.90,
                )
                return quote if quote.is_valid() else None
        except Exception as exc:
            self.health.record_failure()
            logger.debug("[TwelveDataProvider] Quote fetch failed for %s: %s", symbol, exc)
            return None

    async def get_candles(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch historical candles if budget allows."""
        if not self.api_key or not self._has_credit_budget():
            return []

        clean_sym = symbol.replace("/", "_").replace("-", "_")
        url = (
            f"https://api.twelvedata.com/time_series?symbol={clean_sym}&interval={timeframe}"
            f"&outputsize={limit}&apikey={self.api_key}"
        )

        t0 = time.perf_counter()
        try:
            self._consume_credit()
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    self.health.record_failure()
                    return []
                data = resp.json()
                values = data.get("values", [])
                self.health.record_success((time.perf_counter() - t0) * 1000.0)

                candles = []
                for v in reversed(values):
                    try:
                        candles.append({
                            "timestamp": v.get("datetime"),
                            "open": float(v["open"]),
                            "high": float(v["high"]),
                            "low": float(v["low"]),
                            "close": float(v["close"]),
                            "volume": float(v.get("volume", 0.0) or 0.0),
                        })
                    except Exception:
                        continue
                return candles
        except Exception as exc:
            self.health.record_failure()
            logger.debug("[TwelveDataProvider] Candles failed for %s: %s", symbol, exc)
            return []

    async def get_batch_quotes(self, symbols: list[str]) -> dict[str, MarketQuote]:
        """Fetch individual quotes up to remaining credits."""
        quotes = {}
        for s in symbols:
            if not self._has_credit_budget():
                break
            q = await self.get_quote(s)
            if q:
                quotes[s] = q
        return quotes
