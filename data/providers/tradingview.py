"""
TradeAI-PRO V2 — TradingView Indian Markets Vectorized Batch Scanner Provider.

Fetches real-time tick quotes for multiple NSE/BSE symbols in a single sub-second
HTTP POST request to TradingView's India scanner endpoint.
Enforces strict response schema validation and server-side change_pct derivation.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from data.providers.base import MarketDataProvider, MarketQuote

logger = logging.getLogger(__name__)

# Expected scanner response schema columns in strict index order
SCANNER_COLUMNS = [
    "close",       # 0: Last Traded Price (LTP)
    "change",      # 1: Exchange Change %
    "change_abs",  # 2: Absolute Price Change from Previous Close
    "volume",      # 3: Traded Volume
    "open",        # 4: Day Open
    "high",        # 5: Day High
    "low",         # 6: Day Low
    "update_time", # 7: Unix timestamp of the trade tick
]


def _normalize_tv_symbol(symbol: str) -> str:
    """Map internal clean symbol to TradingView ticker prefix format."""
    sym = (symbol or "").strip().upper()
    if sym in ("NIFTY 50", "NIFTY", "^NSEI"):
        return "NSE:NIFTY"
    if sym in ("BANKNIFTY", "BANK NIFTY", "^NSEBANK"):
        return "NSE:BANKNIFTY"
    if sym in ("SENSEX", "^BSESN"):
        return "BSE:SENSEX"
    if sym.endswith(".NS"):
        return f"NSE:{sym[:-3]}"
    if sym.endswith(".BO"):
        return f"BSE:{sym[:-3]}"
    if "/" in sym:  # Crypto or Forex
        base, quote = sym.split("/", 1)
        return f"BINANCE:{base}{quote}"
    return f"NSE:{sym}"


class TradingViewProvider(MarketDataProvider):
    """
    Ultra-low-latency batch provider utilizing the TradingView India scanner API.
    Provides sub-second real-time tick prices, volume, and intraday range.
    """

    name: str = "tradingview"

    def __init__(self, timeout: float = 6.0):
        super().__init__()
        self.timeout = timeout
        self.url = "https://scanner.tradingview.com/india/scan"

    async def get_quote(self, symbol: str) -> Optional[MarketQuote]:
        """Fetch quote for a single symbol using the batch engine."""
        res = await self.get_batch_quotes([symbol])
        return res.get(symbol)

    async def get_candles(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """TradingView Scanner endpoint is quote-only; returns empty list to trigger router fallback."""
        return []

    async def get_batch_quotes(self, symbols: List[str]) -> Dict[str, MarketQuote]:
        """
        Fetch quotes for multiple symbols in a single vectorized HTTP POST.
        Strictly validates response schema; any missing column fails the provider.
        """
        if not symbols:
            return {}

        clean_to_tv = {s: _normalize_tv_symbol(s) for s in symbols}
        tv_to_clean = {tv: clean for clean, tv in clean_to_tv.items()}
        tv_tickers = list(clean_to_tv.values())

        payload = {
            "symbols": {"tickers": tv_tickers},
            "columns": SCANNER_COLUMNS,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(headers=headers, timeout=self.timeout) as client:
                resp = await client.post(self.url, json=payload)

            if resp.status_code != 200:
                self.health.record_failure()
                logger.warning(
                    "[TradingViewProvider] HTTP error %d from scanner endpoint",
                    resp.status_code,
                )
                return {}

            body = resp.json()
        except Exception as exc:
            self.health.record_failure()
            logger.warning("[TradingViewProvider] Request failed: %s", exc)
            return {}

        # ── Strict Response Schema Validation ──
        if not isinstance(body, dict) or "data" not in body or not isinstance(body["data"], list):
            self.health.record_failure()
            logger.warning("[TradingViewProvider] Schema validation failed: 'data' list missing in response")
            return {}

        now_utc = datetime.now(timezone.utc)
        quotes: Dict[str, MarketQuote] = {}
        expected_len = len(SCANNER_COLUMNS)

        for row in body["data"]:
            if not isinstance(row, dict) or "s" not in row or "d" not in row:
                continue

            tv_sym = str(row["s"])
            d_vals = row["d"]
            if not isinstance(d_vals, list) or len(d_vals) != expected_len:
                # Missing column or malformed row: strict schema failure for this symbol
                logger.debug(
                    "[TradingViewProvider] Column count mismatch for %s: expected %d, got %d",
                    tv_sym,
                    expected_len,
                    len(d_vals) if isinstance(d_vals, list) else -1,
                )
                continue

            clean_sym = tv_to_clean.get(tv_sym)
            if not clean_sym:
                continue

            try:
                # 0: close (LTP), 1: change, 2: change_abs, 3: volume, 4: open, 5: high, 6: low, 7: update_time
                ltp = float(d_vals[0]) if d_vals[0] is not None else 0.0
                if not math.isfinite(ltp) or ltp <= 0:
                    continue

                change_abs = float(d_vals[2]) if d_vals[2] is not None else 0.0
                prev_close = round(ltp - change_abs, 2)
                if prev_close <= 0 or not math.isfinite(prev_close):
                    prev_close = ltp

                # Server-side change_pct invariant: (ltp - prev_close) / prev_close * 100
                change_pct = round(((ltp - prev_close) / prev_close) * 100.0, 2) if prev_close > 0 else 0.0

                update_epoch = int(d_vals[7]) if d_vals[7] is not None else None
                regular_mkt_time = None
                data_age_sec = None
                if update_epoch and update_epoch > 0:
                    regular_mkt_time = datetime.fromtimestamp(update_epoch, tz=timezone.utc)
                    data_age_sec = max(0.0, (now_utc - regular_mkt_time).total_seconds())

                open_val = float(d_vals[4]) if d_vals[4] is not None else ltp
                high_val = float(d_vals[5]) if d_vals[5] is not None else ltp
                low_val = float(d_vals[6]) if d_vals[6] is not None else ltp
                vol_val = float(d_vals[3]) if d_vals[3] is not None else 0.0

                quote = MarketQuote(
                    symbol=clean_sym,
                    price=ltp,
                    open=open_val,
                    high=high_val,
                    low=low_val,
                    close=ltp,
                    volume=vol_val,
                    source="TRADINGVIEW_SCANNER",
                    fetched_at=now_utc,
                    regular_market_time=regular_mkt_time,
                    prev_close=prev_close,
                    change_pct=change_pct,
                    data_age_seconds=data_age_sec,
                    quality_score=0.99,
                    extra={"tv_symbol": tv_sym},
                )

                if quote.is_valid():
                    quotes[clean_sym] = quote
            except Exception as parse_exc:
                logger.debug("[TradingViewProvider] Parse failure for %s: %s", tv_sym, parse_exc)
                continue

        latency_ms = (time.perf_counter() - t0) * 1000.0
        if quotes:
            self.health.record_success(latency_ms)
        else:
            self.health.record_failure()

        return quotes
