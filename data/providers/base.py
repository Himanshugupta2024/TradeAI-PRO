"""
TradeAI-PRO V2 — Market Data Provider Abstraction Layer.

Defines the core data contracts, provider interfaces, and health telemetry
for all market data sources (NSE MCP, yfinance, Twelve Data, Bhavcopy, Binance).
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class MarketQuote:
    """Standardized quote snapshot returned by any data provider."""
    symbol: str
    price: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    source: str = "UNKNOWN"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    regular_market_time: datetime | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    data_age_seconds: float | None = None
    stale: bool = False
    quality_score: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)

    def is_valid(self) -> bool:
        """Sanity check: price must be positive, finite, and high >= low if present."""
        if not math.isfinite(self.price) or self.price <= 0:
            return False
        if self.high is not None and self.low is not None:
            if not (math.isfinite(self.high) and math.isfinite(self.low)):
                return False
            if self.high < self.low or self.low <= 0:
                return False
        return True


@dataclass(slots=True)
class ProviderHealth:
    """Operational health telemetry for a single data provider."""
    name: str
    is_healthy: bool = True
    consecutive_errors: int = 0
    total_calls: int = 0
    total_errors: int = 0
    avg_latency_ms: float = 0.0
    cooldown_until: float = 0.0  # epoch timestamp

    def record_success(self, latency_ms: float) -> None:
        self.total_calls += 1
        self.consecutive_errors = 0
        self.is_healthy = True
        # Rolling average latency
        if self.avg_latency_ms <= 0:
            self.avg_latency_ms = latency_ms
        else:
            self.avg_latency_ms = (self.avg_latency_ms * 0.8) + (latency_ms * 0.2)

    def record_failure(self, max_consecutive_errors: int = 3, cooldown_seconds: float = 60.0) -> None:
        self.total_calls += 1
        self.total_errors += 1
        self.consecutive_errors += 1
        if self.consecutive_errors >= max_consecutive_errors:
            import time
            self.is_healthy = False
            self.cooldown_until = time.time() + cooldown_seconds


class MarketDataProvider(ABC):
    """Abstract interface that every data source adapter must implement."""

    name: str = "base"

    def __init__(self):
        self.health = ProviderHealth(name=self.name)

    @abstractmethod
    async def get_quote(self, symbol: str) -> MarketQuote | None:
        """Fetch real-time or latest quote for a single symbol."""
        ...

    @abstractmethod
    async def get_candles(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch historical OHLCV candle bars for a symbol."""
        ...

    @abstractmethod
    async def get_batch_quotes(
        self,
        symbols: list[str],
    ) -> dict[str, MarketQuote]:
        """Fetch quotes for a batch of symbols in one efficient call."""
        ...

    async def get_batch_ohlcv(
        self,
        symbols: list[str],
        period: str = "5d",
        interval: str = "5m",
    ) -> dict[str, Any]:
        """Fetch OHLCV DataFrames for a batch of symbols (optional, default empty dict)."""
        return {}

    def is_available(self) -> bool:
        """Check if provider is healthy and not in cooldown."""
        import time
        if not self.health.is_healthy:
            if time.time() >= self.health.cooldown_until:
                # Cooldown expired, allow probe call
                self.health.is_healthy = True
                self.health.consecutive_errors = 0
                return True
            return False
        return True
