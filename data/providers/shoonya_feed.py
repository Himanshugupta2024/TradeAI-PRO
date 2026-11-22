"""
TradeAI Pro — Shoonya Dedicated Feed Provider for MarketDataRouter.
Allows Box A to pull real-time ticks and 5m OHLCV bars directly from Box C (trading-feed).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from api.feed_client import convert_feed_bars_to_df, fetch_feed_snapshot
from data.providers.base import MarketDataProvider, MarketQuote

logger = logging.getLogger(__name__)


class ShoonyaFeedProvider(MarketDataProvider):
    """
    MarketDataProvider adapter for Box C (Dedicated Shoonya WebSocket Feed Service).
    Provides microsecond quote resolution and instant 5-minute bars.
    """

    name: str = "shoonya_feed"

    def __init__(self, feed_url: Optional[str] = None, timeout: float = 3.0):
        super().__init__()
        self.timeout = timeout
        if feed_url:
            import os
            os.environ["FEED_SERVICE_URL"] = feed_url.rstrip("/")

    async def get_quote(self, symbol: str) -> Optional[MarketQuote]:
        """Fetch quote for a single symbol from Box C."""
        t0 = time.perf_counter()
        snap = await fetch_feed_snapshot([symbol], timeout=self.timeout)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        if not snap:
            self.health.record_failure()
            return None

        quotes = snap.get("quotes", {})
        q_data = quotes.get(symbol.strip().upper())
        if not q_data or not q_data.get("lp"):
            self.health.record_failure()
            return None

        self.health.record_success(latency_ms)
        lp = float(q_data["lp"])
        prev_close = float(q_data["prev_close"]) if q_data.get("prev_close") else None
        change_pct = ((lp - prev_close) / prev_close * 100.0) if prev_close and prev_close > 0 else 0.0

        return MarketQuote(
            symbol=symbol,
            price=lp,
            open=float(q_data.get("open", lp)),
            high=float(q_data.get("high", lp)),
            low=float(q_data.get("low", lp)),
            close=lp,
            prev_close=prev_close,
            change_pct=round(change_pct, 2),
            volume=float(q_data.get("volume", 0.0)),
            source=snap.get("source", "SHOONYA_LIVE"),
            data_age_seconds=0.0,
            stale=False,
            quality_score=1.0,
        )

    async def get_batch_quotes(self, symbols: List[str]) -> Dict[str, MarketQuote]:
        """Fetch quotes for a batch of symbols from Box C."""
        if not symbols:
            return {}

        t0 = time.perf_counter()
        snap = await fetch_feed_snapshot(symbols, timeout=self.timeout)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        if not snap:
            self.health.record_failure()
            return {}

        quotes_data = snap.get("quotes", {})
        out: Dict[str, MarketQuote] = {}

        for sym in symbols:
            norm = sym.strip().upper()
            q = quotes_data.get(norm)
            if not q or not q.get("lp"):
                continue

            lp = float(q["lp"])
            prev_close = float(q["prev_close"]) if q.get("prev_close") else None
            change_pct = ((lp - prev_close) / prev_close * 100.0) if prev_close and prev_close > 0 else 0.0

            out[sym] = MarketQuote(
                symbol=sym,
                price=lp,
                open=float(q.get("open", lp)),
                high=float(q.get("high", lp)),
                low=float(q.get("low", lp)),
                close=lp,
                prev_close=prev_close,
                change_pct=round(change_pct, 2),
                volume=float(q.get("volume", 0.0)),
                source=snap.get("source", "SHOONYA_LIVE"),
                data_age_seconds=0.0,
                stale=False,
                quality_score=1.0,
            )

        if out:
            self.health.record_success(latency_ms)
        else:
            self.health.record_failure()

        return out

    async def get_candles(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Fetch historical candle bars from Box C."""
        snap = await fetch_feed_snapshot([symbol], timeout=self.timeout)
        if not snap:
            return []

        bars_data = snap.get("bars", {}).get(symbol.strip().upper(), [])
        return bars_data[-limit:]

    async def get_batch_ohlcv(
        self,
        symbols: List[str],
        period: str = "5d",
        interval: str = "5m",
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetch OHLCV DataFrames for multiple symbols.
        Serves 5m interval directly from Box C rolling bars.
        """
        if interval != "5m" or not symbols:
            return {}

        snap = await fetch_feed_snapshot(symbols, timeout=self.timeout)
        if not snap:
            self.health.record_failure()
            return {}

        bars_dict = snap.get("bars", {})
        results: Dict[str, pd.DataFrame] = {}

        for sym in symbols:
            norm = sym.strip().upper()
            bar_list = bars_dict.get(norm)
            if bar_list:
                df = convert_feed_bars_to_df(bar_list)
                if df is not None and not df.empty:
                    results[sym] = df

        if results:
            self.health.record_success(0.0)
        return results
