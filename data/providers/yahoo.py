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
import httpx

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

    async def _fetch_single_chart_meta(
        self,
        client: httpx.AsyncClient,
        clean_sym: str,
        yf_sym: str,
    ) -> MarketQuote | None:
        """Fetch real-time tick price, true chartPreviousClose, and trade epoch via Yahoo Query1 Chart Meta."""
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}?interval=1m&range=1d"
        try:
            resp = await client.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                return None
            data = resp.json()
            results = data.get("chart", {}).get("result")
            if not results:
                return None
            meta = results[0].get("meta", {})
            ltp = meta.get("regularMarketPrice")
            if ltp is None or not math.isfinite(ltp) or ltp <= 0:
                return None

            prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
            change_pct = None
            if prev_close and float(prev_close) > 0:
                change_pct = round(((float(ltp) - float(prev_close)) / float(prev_close)) * 100.0, 2)

            mkt_time_epoch = meta.get("regularMarketTime")
            regular_mkt_time = None
            data_age_sec = None
            now_utc = datetime.now(timezone.utc)
            if mkt_time_epoch:
                regular_mkt_time = datetime.fromtimestamp(mkt_time_epoch, tz=timezone.utc)
                data_age_sec = max(0.0, (now_utc - regular_mkt_time).total_seconds())

            open_val = float(meta.get("regularMarketDayOpen") or ltp)
            high_val = float(meta.get("regularMarketDayHigh") or ltp)
            low_val = float(meta.get("regularMarketDayLow") or ltp)
            vol_val = float(meta.get("regularMarketVolume") or 0.0)

            quote = MarketQuote(
                symbol=clean_sym,
                price=float(ltp),
                open=open_val,
                high=high_val,
                low=low_val,
                close=float(ltp),
                volume=vol_val,
                source="YAHOO_CHART_META",
                fetched_at=now_utc,
                regular_market_time=regular_mkt_time,
                prev_close=float(prev_close) if prev_close else None,
                change_pct=change_pct,
                data_age_seconds=data_age_sec,
                quality_score=0.98,
                extra={
                    "exchange_name": meta.get("exchangeName"),
                    "currency": meta.get("currency"),
                },
            )
            return quote if quote.is_valid() else None
        except Exception as exc:
            logger.debug("[YahooProvider] Chart meta fetch failed for %s: %s", clean_sym, exc)
            return None

    async def get_batch_quotes(self, symbols: list[str]) -> dict[str, MarketQuote]:
        """Fetch quotes for multiple symbols via Yahoo Query1 Chart Meta, with yf.download fallback."""
        if not symbols:
            return {}

        clean_to_yf = {s: _normalize_yahoo_symbol(s) for s in symbols}
        t0 = time.perf_counter()
        quotes: dict[str, MarketQuote] = {}

        # 1. Primary path: Vectorized async fetch via Yahoo Query1 Chart Meta (sub-second tick prices)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=self.timeout) as client:
                tasks = [
                    self._fetch_single_chart_meta(client, clean_sym, yf_sym)
                    for clean_sym, yf_sym in clean_to_yf.items()
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, MarketQuote) and res.is_valid():
                        quotes[res.symbol] = res
        except Exception as exc:
            logger.warning("[YahooProvider] Primary Query1 batch fetch error: %s", exc)

        missing = [s for s in symbols if s not in quotes]

        # 2. Fallback path for missing symbols: legacy yf.download
        if missing:
            missing_yf = list({clean_to_yf[s] for s in missing})
            def _fetch_sync():
                return yf.download(
                    tickers=missing_yf,
                    period="5d",
                    interval="5m",
                    group_by="ticker",
                    threads=True,
                    timeout=self.timeout,
                    progress=False,
                )

            try:
                df = await asyncio.to_thread(_fetch_sync)
                if df is not None and not df.empty:
                    now = datetime.now(timezone.utc)
                    is_multi = len(missing_yf) > 1
                    for clean_sym in missing:
                        yf_sym = clean_to_yf[clean_sym]
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

                            prev_close_val = None
                            change_pct_val = None
                            if len(sym_df) > 1:
                                prev_close_val = float(sym_df.iloc[-2].get("Close", close_val))
                                if prev_close_val > 0:
                                    change_pct_val = round(((close_val - prev_close_val) / prev_close_val) * 100.0, 2)

                            quote = MarketQuote(
                                symbol=clean_sym,
                                price=close_val,
                                open=open_val,
                                high=high_val,
                                low=low_val,
                                close=close_val,
                                volume=vol_val,
                                source="YAHOO_YF_FALLBACK",
                                fetched_at=now,
                                prev_close=prev_close_val,
                                change_pct=change_pct_val,
                                quality_score=0.90,
                            )
                            if quote.is_valid():
                                quotes[clean_sym] = quote
                        except Exception as parse_err:
                            logger.debug("[YahooProvider] Fallback parse error for %s: %s", clean_sym, parse_err)
            except Exception as fb_exc:
                logger.warning("[YahooProvider] Fallback yf.download failed: %s", fb_exc)

        latency_ms = (time.perf_counter() - t0) * 1000.0
        if quotes:
            self.health.record_success(latency_ms)
        else:
            self.health.record_failure()

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
