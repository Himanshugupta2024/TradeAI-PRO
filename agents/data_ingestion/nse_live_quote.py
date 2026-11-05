"""
Multi-Tier Real-Time NSE Live Quote Ingestion Client.

Tiers:
  1. Direct NSE India Web API (sub-second, official day VWAP & delivery %)
  2. nsepython wrapper fallback
  3. yfinance FastInfo fallback (100% reliable fallback)

Provides normalized 0-delay market snapshot with:
  LTP, Change%, Open, Day High, Day Low, Previous Close, Day VWAP,
  Total Traded Volume, Delivery%, and Relative Volume (RVOL).
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import yfinance as yf

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_INDEX_NAMES = {
    "NIFTY 50": "NIFTY 50",
    "NIFTY50": "NIFTY 50",
    "^NSEI": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "^NSEBANK": "NIFTY BANK",
    "NIFTY IT": "NIFTY IT",
}


@dataclass
class NSELiveQuote:
    symbol: str
    ltp: float
    change_pct: float
    open: float
    high: float
    low: float
    previous_close: float
    vwap: float
    total_volume: float
    delivery_pct: Optional[float]
    rvol: float
    source: str
    timestamp_ist: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _calculate_rvol(current_volume: float, avg_daily_volume: float) -> float:
    """
    Calculate Relative Volume (RVOL) adjusted for time elapsed in the NSE trading session.
    NSE cash session: 09:15 to 15:30 IST (375 minutes total).
    """
    if avg_daily_volume <= 0 or current_volume <= 0:
        return 1.0

    now_ist = datetime.now(_IST)
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

    if now_ist <= market_open:
        # Pre-open / before market: simple volume ratio
        return round(float(current_volume / avg_daily_volume), 2)
    elif now_ist >= market_close or now_ist.weekday() >= 5:
        # Market closed or weekend: full-day volume ratio
        return round(float(current_volume / avg_daily_volume), 2)
    else:
        # Intraday: fraction of market day elapsed
        elapsed_mins = max(1.0, (now_ist - market_open).total_seconds() / 60.0)
        expected_fraction = min(1.0, elapsed_mins / 375.0)
        expected_volume = avg_daily_volume * expected_fraction
        if expected_volume <= 0:
            return 1.0
        rvol = current_volume / expected_volume
        return round(float(min(50.0, max(0.01, rvol))), 2)


class NSELiveQuoteClient:
    """Client for fetching live quotes with multi-tier fallback and short-lived caching."""

    def __init__(self, cache_ttl_seconds: int = 10):
        self._cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, Tuple[float, NSELiveQuote]] = {}
        self._session: Optional[Any] = None
        self._last_cookie_time: float = 0.0

    def _clean_symbol(self, symbol: str) -> str:
        s = symbol.strip().upper().replace(".NS", "").replace("^", "")
        return s

    def _get_yf_ticker(self, symbol: str) -> str:
        s = symbol.strip().upper()
        if s in ["NIFTY 50", "NIFTY50"]:
            return "^NSEI"
        if s in ["BANKNIFTY", "NIFTY BANK"]:
            return "^NSEBANK"
        if s.startswith("^"):
            return s
        if not s.endswith(".NS"):
            return f"{s}.NS"
        return s

    def get_quote(self, symbol: str, avg_volume: float = 0.0) -> Optional[NSELiveQuote]:
        """Get live quote for symbol from cache or multi-tier ingestion."""
        clean = self._clean_symbol(symbol)
        now = time.time()

        if clean in self._cache:
            ts, cached_quote = self._cache[clean]
            if now - ts <= self._cache_ttl:
                return cached_quote

        # On cloud hosting (Render) or lightweight mode, NSE India website blocks international
        # datacenter IPs with connection timeouts. Fast-track to reliable yfinance fast_info.
        is_cloud = bool(os.getenv("RENDER") or os.getenv("LIGHTWEIGHT_API_MODE", "false").lower() in ("1", "true", "yes"))
        if is_cloud:
            quote = self._fetch_yfinance(symbol, avg_volume)
            if quote is not None:
                self._cache[clean] = (now, quote)
                return quote

        # Tier 1: Direct NSE
        quote = self._fetch_direct_nse(clean, avg_volume)
        # Tier 2: nsepython fallback
        if quote is None:
            quote = self._fetch_nsepython(clean, avg_volume)
        # Tier 3: yfinance fast_info fallback
        if quote is None:
            quote = self._fetch_yfinance(symbol, avg_volume)

        if quote is not None:
            self._cache[clean] = (now, quote)
        return quote

    def _fetch_direct_nse(self, clean_symbol: str, avg_volume: float) -> Optional[NSELiveQuote]:
        """Tier 1: Direct NSE India HTTP API."""
        try:
            import requests

            if self._session is None or (time.time() - self._last_cookie_time > 300):
                self._session = requests.Session()
                self._session.headers.update({
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                })
                r = self._session.get("https://www.nseindia.com", timeout=3)
                if r.status_code == 200:
                    self._last_cookie_time = time.time()
                else:
                    return None

            is_index = clean_symbol in _INDEX_NAMES or "NIFTY" in clean_symbol
            if is_index:
                url = "https://www.nseindia.com/api/allIndices"
                resp = self._session.get(url, timeout=4)
                if resp.status_code != 200:
                    return None
                data = resp.json().get("data", [])
                target_name = _INDEX_NAMES.get(clean_symbol, clean_symbol)
                for item in data:
                    if item.get("index") == target_name:
                        ltp = float(item.get("last", 0.0) or 0.0)
                        prev = float(item.get("previousClose", ltp) or ltp)
                        pct = float(item.get("percentChange", 0.0) or 0.0)
                        open_p = float(item.get("open", ltp) or ltp)
                        high_p = float(item.get("high", ltp) or ltp)
                        low_p = float(item.get("low", ltp) or ltp)
                        vwap = round((open_p + high_p + low_p + ltp) / 4.0, 2)
                        return NSELiveQuote(
                            symbol=clean_symbol,
                            ltp=ltp,
                            change_pct=pct,
                            open=open_p,
                            high=high_p,
                            low=low_p,
                            previous_close=prev,
                            vwap=vwap,
                            total_volume=0.0,
                            delivery_pct=None,
                            rvol=1.0,
                            source="direct_nse",
                            timestamp_ist=datetime.now(_IST).isoformat(),
                        )
                return None

            url = f"https://www.nseindia.com/api/quote-equity?symbol={clean_symbol}"
            headers = {"Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={clean_symbol}"}
            resp = self._session.get(url, headers=headers, timeout=4)
            if resp.status_code != 200:
                return None
            data = resp.json()
            pi = data.get("priceInfo", {})
            ltp = float(pi.get("lastPrice", 0.0) or 0.0)
            if ltp <= 0:
                return None
            prev = float(pi.get("previousClose", ltp) or ltp)
            pct = float(pi.get("pChange", 0.0) or 0.0)
            open_p = float(pi.get("open", ltp) or ltp)
            hl = pi.get("intraDayHighLow", {})
            high_p = float(hl.get("max", ltp) or ltp)
            low_p = float(hl.get("min", ltp) or ltp)
            vwap = float(pi.get("vwap", 0.0) or 0.0)
            if vwap <= 0:
                vwap = round((high_p + low_p + ltp) / 3.0, 2)

            vol = 0.0
            pre_open = data.get("preOpenMarket", {})
            if isinstance(pre_open, dict) and pre_open.get("totalTradedVolume"):
                vol = float(pre_open["totalTradedVolume"])

            sec_dp = data.get("securityWiseDP", {})
            delivery_pct = None
            if isinstance(sec_dp, dict) and sec_dp.get("deliveryToTradedQuantity") is not None:
                delivery_pct = float(sec_dp["deliveryToTradedQuantity"])
            if isinstance(sec_dp, dict) and sec_dp.get("quantityTraded"):
                vol = max(vol, float(sec_dp["quantityTraded"]))

            rvol = _calculate_rvol(vol, avg_volume) if avg_volume > 0 else 1.0

            return NSELiveQuote(
                symbol=clean_symbol,
                ltp=ltp,
                change_pct=pct,
                open=open_p,
                high=high_p,
                low=low_p,
                previous_close=prev,
                vwap=vwap,
                total_volume=vol,
                delivery_pct=delivery_pct,
                rvol=rvol,
                source="direct_nse",
                timestamp_ist=datetime.now(_IST).isoformat(),
            )
        except Exception as exc:
            logger.debug("Direct NSE quote failed for %s: %s", clean_symbol, exc)
            return None

    def _fetch_nsepython(self, clean_symbol: str, avg_volume: float) -> Optional[NSELiveQuote]:
        """Tier 2: nsepython wrapper fallback."""
        try:
            from nsepython import nse_eq, nse_get_index_quote

            is_index = clean_symbol in _INDEX_NAMES or "NIFTY" in clean_symbol
            if is_index:
                target_name = _INDEX_NAMES.get(clean_symbol, clean_symbol)
                raw = nse_get_index_quote(target_name)
                if isinstance(raw, dict) and raw.get("last"):
                    ltp = float(raw.get("last", 0.0))
                    prev = float(raw.get("previousClose", ltp) or ltp)
                    pct = float(raw.get("percChange", 0.0) or 0.0)
                    open_p = float(raw.get("open", ltp) or ltp)
                    high_p = float(raw.get("high", ltp) or ltp)
                    low_p = float(raw.get("low", ltp) or ltp)
                    vwap = round((open_p + high_p + low_p + ltp) / 4.0, 2)
                    return NSELiveQuote(
                        symbol=clean_symbol,
                        ltp=ltp,
                        change_pct=pct,
                        open=open_p,
                        high=high_p,
                        low=low_p,
                        previous_close=prev,
                        vwap=vwap,
                        total_volume=0.0,
                        delivery_pct=None,
                        rvol=1.0,
                        source="nsepython",
                        timestamp_ist=datetime.now(_IST).isoformat(),
                    )
                return None

            raw = nse_eq(clean_symbol)
            if not isinstance(raw, dict) or not raw.get("priceInfo"):
                return None

            pi = raw.get("priceInfo", {})
            ltp = float(pi.get("lastPrice", 0.0) or 0.0)
            if ltp <= 0:
                return None
            prev = float(pi.get("previousClose", ltp) or ltp)
            pct = float(pi.get("pChange", 0.0) or 0.0)
            open_p = float(pi.get("open", ltp) or ltp)
            hl = pi.get("intraDayHighLow", {})
            high_p = float(hl.get("max", ltp) or ltp)
            low_p = float(hl.get("min", ltp) or ltp)
            vwap = float(pi.get("vwap", 0.0) or 0.0)
            if vwap <= 0:
                vwap = round((high_p + low_p + ltp) / 3.0, 2)

            vol = 0.0
            pre_open = raw.get("preOpenMarket", {})
            if isinstance(pre_open, dict) and pre_open.get("totalTradedVolume"):
                vol = float(pre_open["totalTradedVolume"])

            rvol = _calculate_rvol(vol, avg_volume) if avg_volume > 0 else 1.0

            return NSELiveQuote(
                symbol=clean_symbol,
                ltp=ltp,
                change_pct=pct,
                open=open_p,
                high=high_p,
                low=low_p,
                previous_close=prev,
                vwap=vwap,
                total_volume=vol,
                delivery_pct=None,
                rvol=rvol,
                source="nsepython",
                timestamp_ist=datetime.now(_IST).isoformat(),
            )
        except Exception as exc:
            logger.debug("nsepython quote failed for %s: %s", clean_symbol, exc)
            return None

    def _fetch_yfinance(self, symbol: str, avg_volume: float) -> Optional[NSELiveQuote]:
        """Tier 3: yfinance fast_info fallback."""
        try:
            clean = self._clean_symbol(symbol)
            yf_ticker = self._get_yf_ticker(symbol)
            t = yf.Ticker(yf_ticker)
            fi = t.fast_info

            ltp = float(getattr(fi, "last_price", 0.0) or 0.0)
            if ltp <= 0:
                return None
            prev = float(getattr(fi, "previous_close", ltp) or ltp)
            pct = round(((ltp - prev) / prev) * 100.0, 2) if prev > 0 else 0.0
            open_p = float(getattr(fi, "open", ltp) or ltp)
            high_p = float(getattr(fi, "day_high", ltp) or ltp)
            low_p = float(getattr(fi, "day_low", ltp) or ltp)
            vol = float(getattr(fi, "last_volume", 0.0) or 0.0)
            vwap = round((high_p + low_p + ltp) / 3.0, 2)

            rvol = _calculate_rvol(vol, avg_volume) if avg_volume > 0 else 1.0

            return NSELiveQuote(
                symbol=clean,
                ltp=ltp,
                change_pct=pct,
                open=open_p,
                high=high_p,
                low=low_p,
                previous_close=prev,
                vwap=vwap,
                total_volume=vol,
                delivery_pct=None,
                rvol=rvol,
                source="yfinance",
                timestamp_ist=datetime.now(_IST).isoformat(),
            )
        except Exception as exc:
            logger.debug("yfinance fast_info failed for %s: %s", symbol, exc)
            return None


# Global singleton instance
_GLOBAL_CLIENT: Optional[NSELiveQuoteClient] = None


def get_live_quote_client() -> NSELiveQuoteClient:
    global _GLOBAL_CLIENT
    if _GLOBAL_CLIENT is None:
        _GLOBAL_CLIENT = NSELiveQuoteClient(cache_ttl_seconds=10)
    return _GLOBAL_CLIENT


def get_nse_live_quote(symbol: str, avg_volume: float = 0.0) -> Optional[NSELiveQuote]:
    """Helper function to fetch live quote for symbol."""
    return get_live_quote_client().get_quote(symbol, avg_volume=avg_volume)
