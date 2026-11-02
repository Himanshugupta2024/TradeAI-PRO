"""
TradeAI-PRO V2 — Yahoo Finance Vectorized Batch Provider.

Fetches multi-symbol quotes and historical candles in a single vectorized HTTP call
using `yf.download(..., threads=True, timeout=8)`.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import warnings
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf

from data.providers.base import MarketDataProvider, MarketQuote

logger = logging.getLogger(__name__)


def _normalize_yahoo_symbol(symbol: str) -> str:
    """Map internal clean symbol to Yahoo Finance ticker format."""
    sym = (symbol or "").strip().upper()
    if sym in ("NIFTY 50", "NIFTY", "^NSEI"):
        return "^NSEI"
    if sym in ("BANKNIFTY", "BANK NIFTY", "^NSEBANK"):
        return "^NSEBANK"
    if sym in ("SENSEX", "^BSESN"):
        return "^BSESN"
    if sym in ("NIFTY IT", "^CNXIT"):
        return "^CNXIT"
    if sym.endswith(".NS") or sym.endswith(".BO") or sym.startswith("^"):
        return sym
    if "/" in sym:  # Crypto or Forex like BTC/USDT or EUR/USD
        base, quote = sym.split("/", 1)
        return f"{base}-{quote}"
    # Standard NSE equity
    return f"{sym}.NS"


def _clean_symbol_from_yahoo(yahoo_sym: str) -> str:
    """Reverse map Yahoo ticker to internal clean symbol."""
    sym = (yahoo_sym or "").strip().upper()
    if sym == "^NSEI":
        return "NIFTY 50"
    if sym == "^NSEBANK":
        return "BANKNIFTY"
    if sym == "^BSESN":
        return "SENSEX"
    if sym == "^CNXIT":
        return "NIFTY IT"
    if sym.endswith(".NS"):
        return sym[:-3]
    if sym.endswith(".BO"):
        return sym[:-3]
    return sym


class YahooProvider(MarketDataProvider):
    """Production Yahoo Finance provider with vectorized multi-ticker downloading."""

    name: str = "yahoo"

    def __init__(self, timeout: float = 8.0):
        super().__init__()
        self.timeout = timeout

    async def get_quote(self, symbol: str) -> MarketQuote | None:
        """Fetch quote for a single symbol using the batch engine."""
        res = await self.get_batch_quotes([symbol])
        return res.get(symbol)

    async def get_batch_quotes(self, symbols: list[str]) -> dict[str, MarketQuote]:
        """Fetch quotes for multiple symbols in a single vectorized HTTP download."""
        if not symbols:
            return {}

        clean_to_yf = {s: _normalize_yahoo_symbol(s) for s in symbols}
        yf_tickers = list(set(clean_to_yf.values()))
        t0 = time.perf_counter()

        def _fetch_sync():
            return yf.download(
                tickers=yf_tickers,
                period="5d",
                interval="5m",
                group_by="ticker",
                threads=True,
                timeout=self.timeout,
                progress=False,
            )

        try:
            df = await asyncio.to_thread(_fetch_sync)
            latency_ms = (time.perf_counter() - t0) * 1000.0
            self.health.record_success(latency_ms)
        except Exception as exc:
            self.health.record_failure()
            logger.warning("[YahooProvider] Batch download failed: %s", exc)
            return {}

        if df is None or df.empty:
            return {}

        quotes: dict[str, MarketQuote] = {}
        now = datetime.now(timezone.utc)
        is_multi = len(yf_tickers) > 1

        for clean_sym, yf_sym in clean_to_yf.items():
            try:
                if is_multi:
                    if yf_sym not in df.columns.levels[0]:
                        continue
                    sym_df = df[yf_sym].dropna(how="all")
                else:
                    sym_df = df.dropna(how="all")

                if sym_df.empty:
                    continue

                last_row = sym_df.iloc[-1]
                close_val = float(last_row.get("Close", 0.0))
                open_val = float(last_row.get("Open", close_val))
                high_val = float(last_row.get("High", close_val))
                low_val = float(last_row.get("Low", close_val))
                vol_val = float(last_row.get("Volume", 0.0))

                quote = MarketQuote(
                    symbol=clean_sym,
                    price=close_val,
                    open=open_val,
                    high=high_val,
                    low=low_val,
                    close=close_val,
                    volume=vol_val,
                    source="YAHOO",
                    fetched_at=now,
                    quality_score=0.95,
                )

                if quote.is_valid():
                    quotes[clean_sym] = quote
            except Exception as parse_err:
                logger.debug("[YahooProvider] Parse error for %s: %s", clean_sym, parse_err)
                continue

        return quotes

    async def get_batch_ohlcv(
        self,
        symbols: list[str],
        period: str = "5d",
        interval: str = "5m",
    ) -> dict[str, pd.DataFrame]:
        """
        Fetch OHLCV candles for multiple symbols in a single vectorized HTTP download.
        Returns a dict of {symbol: pd.DataFrame} with normalized columns:
        ['timestamp', 'open', 'high', 'low', 'close', 'volume'].
        """
        if not symbols:
            return {}

        clean_to_yf = {s: _normalize_yahoo_symbol(s) for s in symbols}
        yf_tickers = list(set(clean_to_yf.values()))
        t0 = time.perf_counter()

        def _fetch_sync():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return yf.download(
                    tickers=yf_tickers,
                    period=period,
                    interval=interval,
                    group_by="ticker",
                    threads=True,
                    timeout=self.timeout,
                    progress=False,
                )

        try:
            df = await asyncio.to_thread(_fetch_sync)
            latency_ms = (time.perf_counter() - t0) * 1000.0
            self.health.record_success(latency_ms)
        except Exception as exc:
            self.health.record_failure()
            logger.warning("[YahooProvider] Batch OHLCV download failed: %s", exc)
            return {}

        if df is None or df.empty:
            return {}

        results: dict[str, pd.DataFrame] = {}
        is_multi = len(yf_tickers) > 1

        for clean_sym, yf_sym in clean_to_yf.items():
            try:
                if is_multi:
                    if yf_sym not in df.columns.levels[0]:
                        continue
                    sym_df = df[yf_sym].dropna(how="all").copy()
                else:
                    sym_df = df.dropna(how="all").copy()

                if sym_df.empty:
                    continue

                # Standardize column names to lowercase
                sym_df = sym_df.rename(columns={
                    "Open": "open", "High": "high", "Low": "low",
                    "Close": "close", "Volume": "volume",
                })
                keep = [c for c in ["open", "high", "low", "close", "volume"] if c in sym_df.columns]
                sym_df = sym_df[keep].copy()

                # Reset index to timestamp column
                if sym_df.index.name in ("Date", "Datetime", "Timestamp") or isinstance(sym_df.index, pd.DatetimeIndex):
                    sym_df = sym_df.reset_index()
                    col0 = sym_df.columns[0]
                    if col0.lower() in ("date", "datetime", "timestamp", "index"):
                        sym_df = sym_df.rename(columns={col0: "timestamp"})
                elif "timestamp" not in sym_df.columns:
                    sym_df = sym_df.reset_index()
                    col0 = sym_df.columns[0]
                    if col0.lower() in ("date", "datetime", "timestamp", "index"):
                        sym_df = sym_df.rename(columns={col0: "timestamp"})

                sym_df["timestamp"] = pd.to_datetime(sym_df["timestamp"], errors="coerce", format="mixed")
                sym_df = sym_df.dropna(subset=["timestamp"]).reset_index(drop=True)

                if sym_df["timestamp"].dt.tz is not None:
                    try:
                        from zoneinfo import ZoneInfo
                        sym_df["timestamp"] = sym_df["timestamp"].dt.tz_convert(ZoneInfo("Asia/Kolkata")).dt.tz_localize(None)
                    except Exception:
                        sym_df["timestamp"] = sym_df["timestamp"].dt.tz_localize(None)

                # Standardize to uniform datetime64[ns] resolution across daily and intraday
                sym_df["timestamp"] = pd.to_datetime(sym_df["timestamp"]).astype("datetime64[ns]")

                # Basic validation: must have at least 1 row and required columns
                if len(sym_df) > 0 and "close" in sym_df.columns and "open" in sym_df.columns:
                    results[clean_sym] = sym_df
            except Exception as e:
                logger.debug("[YahooProvider] OHLCV parse error for %s: %s", clean_sym, e)
                continue

        return results

    async def get_candles(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch historical candle bars for a single symbol."""
        yf_sym = _normalize_yahoo_symbol(symbol)
        period_map = {"1m": "1d", "5m": "5d", "15m": "5d", "1h": "1mo", "1d": "1y"}
        period = period_map.get(timeframe, "5d")

        def _fetch_sync():
            ticker = yf.Ticker(yf_sym)
            return ticker.history(period=period, interval=timeframe, timeout=self.timeout)

        t0 = time.perf_counter()
        try:
            df = await asyncio.to_thread(_fetch_sync)
            self.health.record_success((time.perf_counter() - t0) * 1000.0)
        except Exception as exc:
            self.health.record_failure()
            logger.warning("[YahooProvider] get_candles failed for %s: %s", symbol, exc)
            return []

        if df is None or df.empty:
            return []

        candles = []
        df = df.tail(limit)
        for ts, row in df.iterrows():
            try:
                candles.append({
                    "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row["Volume"]),
                })
            except Exception:
                continue

        return candles
