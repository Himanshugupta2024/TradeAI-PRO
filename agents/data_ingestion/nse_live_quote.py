"""
Multi-Tier Real-Time NSE Live Quote Ingestion Client with Transparent Staleness & Integrity Guardrails.

Tiers:
  1. Direct NSE India Web API (sub-second, official day VWAP & delivery %) -> LIVE_NSE
  2. nsepython wrapper fallback -> NSEPYTHON
  3. yfinance FastInfo fallback (15m delayed exchange data) -> DELAYED_YFINANCE
  4. Cache Fallback (TTL expired) -> STALE_CACHE

Provides normalized market snapshot with verified exchange timestamp, freshness calculation,
and fail-closed trade eligibility enforcement.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, replace
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
    source: str  # LIVE_NSE | NSEPYTHON | LIVE_TV | DELAYED_YFINANCE | STALE_CACHE
    timestamp_ist: str  # Retained for backwards compatibility (ISO format string)
    data_timestamp: Optional[str] = None  # Actual exchange/provider update time (ISO 8601) or None
    fetched_at: str = ""  # Server fetch timestamp (ISO 8601)
    freshness_seconds: Optional[float] = None  # age of exchange data in seconds, or None if unknown
    is_realtime: bool = False  # True only if exchange timestamp verified and within 60s
    trade_eligible: bool = False  # True if eligible for intraday trade execution
    status_reason: str = ""  # Machine/human readable reason for freshness/eligibility status

    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = self.timestamp_ist or datetime.now(_IST).isoformat()
        if not self.timestamp_ist:
            self.timestamp_ist = self.fetched_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_exchange_timestamp(raw_val: Any) -> Optional[datetime]:
    """
    Parse exchange timestamps from various formats returned by NSE APIs and providers.
    Returns timezone-aware datetime in Asia/Kolkata (_IST) or None if unparseable.
    """
    if not raw_val:
        return None

    if isinstance(raw_val, (int, float)):
        try:
            ts = float(raw_val)
            if ts > 1e11:  # milliseconds
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=_IST)
        except Exception:
            return None

    if not isinstance(raw_val, str):
        return None

    raw_str = raw_val.strip()
    if not raw_str:
        return None

    # Try ISO format
    try:
        dt = datetime.fromisoformat(raw_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_IST)
        return dt.astimezone(_IST)
    except Exception:
        pass

    # Try NSE standard formats: e.g. "08-Nov-2026 15:30:00", "08-Nov-2026 15:30:00 IST"
    clean_str = raw_str.replace(" IST", "").strip()
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%B-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y",
    ):
        try:
            dt = datetime.strptime(clean_str, fmt)
            return dt.replace(tzinfo=_IST)
        except Exception:
            continue

    return None


def is_nse_market_open(dt: Optional[datetime] = None) -> bool:
    """Check if the given datetime (or current IST time) is within NSE cash trading hours (09:15 to 15:30 IST, Mon-Fri)."""
    now = dt or datetime.now(_IST)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def validate_quote_for_trade(
    quote: Optional[NSELiveQuote],
    trade_type: str = "INTRADAY",
    max_staleness_seconds: float = 60.0,
) -> tuple[bool, str]:
    """
    Validate whether a market quote is fresh and verified enough for trading.

    Fail-closed policy:
      - If quote is None -> (False, "NO_QUOTE_DATA")
      - If source is STALE_CACHE -> (False, "STALE_CACHE_EXPIRED")
      - If trade_type == "INTRADAY":
          - If data_timestamp is None -> (False, "TIMESTAMP_UNKNOWN_FAIL_CLOSED")
          - If source == "DELAYED_YFINANCE" or not is_realtime -> (False, "DELAYED_FEED_INTRADAY_BLOCKED")
          - If freshness_seconds is None or > max_staleness_seconds -> (False, "STALE_QUOTE_EXCEEDS_60S")
          - If not trade_eligible -> (False, quote.status_reason or "INTRADAY_NOT_ELIGIBLE")
          - Return (True, "ELIGIBLE_REALTIME")
      - If trade_type == "SWING":
          - Swing allows completed EOD bars (even if delayed yfinance)
          - Return (True, "ELIGIBLE_EOD")
    """
    if quote is None:
        return False, "NO_QUOTE_DATA"

    if quote.source == "STALE_CACHE":
        return False, "STALE_CACHE_EXPIRED"

    trade_upper = (trade_type or "INTRADAY").upper()

    if trade_upper == "INTRADAY":
        if quote.data_timestamp is None:
            return False, "TIMESTAMP_UNKNOWN_FAIL_CLOSED"
        if quote.source == "DELAYED_YFINANCE" or not quote.is_realtime:
            return False, "DELAYED_FEED_INTRADAY_BLOCKED"
        if quote.freshness_seconds is None or quote.freshness_seconds > max_staleness_seconds:
            return False, f"STALE_QUOTE_EXCEEDS_{int(max_staleness_seconds)}S"
        if not quote.trade_eligible:
            return False, quote.status_reason or "INTRADAY_NOT_ELIGIBLE"
        return True, "ELIGIBLE_REALTIME"

    elif trade_upper == "INTRADAY_SIGNAL":
        # Signal GENERATION may run on the honest 15m-delayed public feed,
        # but such signals are never trade-executable (trade_eligible stays False).
        ts = quote.data_timestamp or quote.timestamp_ist
        if ts is None:
            return False, "TIMESTAMP_UNKNOWN_FAIL_CLOSED"
        if quote.freshness_seconds is not None and quote.freshness_seconds > 1200:
            return False, "DELAYED_FEED_TOO_STALE_20M"
        return True, ("ELIGIBLE_REALTIME" if quote.is_realtime else "ELIGIBLE_DELAYED_15M")

    elif trade_upper == "SWING":
        return True, "ELIGIBLE_EOD"

    return False, f"UNKNOWN_TRADE_TYPE_{trade_type}"


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
    """Client for fetching live quotes with multi-tier fallback, caching, and fail-closed validation."""

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
            age = now - ts
            if age <= self._cache_ttl:
                # Return cached quote with age-adjusted freshness
                updated_freshness = (
                    cached_quote.freshness_seconds + age
                    if cached_quote.freshness_seconds is not None
                    else None
                )
                is_still_fresh = updated_freshness is not None and updated_freshness <= 60.0
                return replace(
                    cached_quote,
                    freshness_seconds=updated_freshness,
                    is_realtime=cached_quote.is_realtime and is_still_fresh,
                    trade_eligible=cached_quote.trade_eligible and is_still_fresh,
                )

        # On cloud hosting (Render) or lightweight mode, NSE India website blocks international
        # datacenter IPs with connection timeouts. Fast-track to reliable TradingView scanner (Tier 0)
        # with fallback to yfinance fast_info.
        is_cloud = bool(
            os.getenv("RENDER")
            or os.getenv("LIGHTWEIGHT_API_MODE", "false").lower() in ("1", "true", "yes")
        )
        if is_cloud:
            quote = self._fetch_tradingview(clean, avg_volume) or self._fetch_yfinance(symbol, avg_volume)
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

        # Tier 4: Fallback to expired cache if available
        if quote is None and clean in self._cache:
            ts, cached_quote = self._cache[clean]
            age = now - ts
            quote = replace(
                cached_quote,
                source="STALE_CACHE",
                is_realtime=False,
                trade_eligible=False,
                status_reason=f"STALE_CACHE_FALLBACK_{int(age)}S",
                freshness_seconds=(cached_quote.freshness_seconds or 0.0) + age,
            )
            return quote

        if quote is not None:
            self._cache[clean] = (now, quote)
        return quote

    def _fetch_tradingview(self, clean_symbol: str, avg_volume: float) -> Optional[NSELiveQuote]:
        """Tier 0: TradingView India scanner — real-time, works from datacenter IPs."""
        try:
            import asyncio
            from data.providers.tradingview import TradingViewProvider

            provider = TradingViewProvider(timeout=6.0)
            try:
                asyncio.get_running_loop()
                return None  # inside a running loop — skip TV tier, caller is async context
            except RuntimeError:
                pass  # normal case: sync worker thread (scan runs via asyncio.to_thread)

            quotes = asyncio.run(provider.get_batch_quotes([clean_symbol]))
            mq = quotes.get(clean_symbol)
            if mq is None or not mq.is_valid():
                return None

            now_ist = datetime.now(_IST)
            ts = mq.regular_market_time or mq.fetched_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_ist = ts.astimezone(_IST)
            freshness = max(0.0, (now_ist - ts_ist).total_seconds())
            typical = ((mq.high or mq.price) + (mq.low or mq.price) + mq.price) / 3.0
            rvol = (float(mq.volume or 0) / avg_volume) if avg_volume else 1.0

            return NSELiveQuote(
                symbol=clean_symbol,
                ltp=float(mq.price),
                change_pct=float(mq.change_pct or 0.0),
                open=float(mq.open or mq.price),
                high=float(mq.high or mq.price),
                low=float(mq.low or mq.price),
                previous_close=float(mq.prev_close or mq.price),
                vwap=round(float(typical), 2),
                total_volume=float(mq.volume or 0),
                delivery_pct=None,
                rvol=round(rvol, 2),
                source="LIVE_TV",
                timestamp_ist=ts_ist.isoformat(),
                data_timestamp=ts_ist.isoformat(),
                fetched_at=now_ist.isoformat(),
                freshness_seconds=freshness,
                is_realtime=freshness <= 60,
                trade_eligible=freshness <= 60,
                status_reason="LIVE_TV_SCANNER" if freshness <= 60 else f"TV_AGE_{int(freshness)}S",
            )
        except Exception as exc:
            logger.debug("TradingView quote tier failed for %s: %s", clean_symbol, exc)
            return None

    def _fetch_direct_nse(self, clean_symbol: str, avg_volume: float) -> Optional[NSELiveQuote]:
        """Tier 1: Direct NSE India HTTP API -> LIVE_NSE with verified exchange timestamp."""
        try:
            import requests

            now_ist = datetime.now(_IST)
            fetched_at_str = now_ist.isoformat()

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
                resp_data = resp.json()
                data = resp_data.get("data", [])
                root_ts = resp_data.get("timestamp")
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

                        raw_time = item.get("timestamp") or item.get("lastUpdateTime") or root_ts
                        data_dt = _parse_exchange_timestamp(raw_time)
                        data_ts_str = data_dt.isoformat() if data_dt else None
                        freshness_sec = (
                            max(0.0, (now_ist - data_dt).total_seconds())
                            if data_dt
                            else None
                        )
                        is_open = is_nse_market_open(now_ist)

                        if data_dt is not None and freshness_sec is not None:
                            is_rt = freshness_sec <= 60.0
                            is_eligible = is_rt and is_open
                            reason = (
                                "VERIFIED_REALTIME"
                                if is_eligible
                                else ("MARKET_CLOSED_LAST_CLOSE" if not is_open else f"STALE_DATA_{int(freshness_sec)}S")
                            )
                        else:
                            is_rt = False
                            is_eligible = False
                            reason = "TIMESTAMP_UNKNOWN_FAIL_CLOSED"

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
                            source="LIVE_NSE",
                            timestamp_ist=fetched_at_str,
                            data_timestamp=data_ts_str,
                            fetched_at=fetched_at_str,
                            freshness_seconds=freshness_sec,
                            is_realtime=is_rt,
                            trade_eligible=is_eligible,
                            status_reason=reason,
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

            raw_time = (
                data.get("metadata", {}).get("lastUpdateTime")
                or pi.get("lastUpdateTime")
                or data.get("lastUpdateTime")
            )
            data_dt = _parse_exchange_timestamp(raw_time)
            data_ts_str = data_dt.isoformat() if data_dt else None
            freshness_sec = (
                max(0.0, (now_ist - data_dt).total_seconds()) if data_dt else None
            )
            is_open = is_nse_market_open(now_ist)

            if data_dt is not None and freshness_sec is not None:
                is_rt = freshness_sec <= 60.0
                is_eligible = is_rt and is_open
                reason = (
                    "VERIFIED_REALTIME"
                    if is_eligible
                    else ("MARKET_CLOSED_LAST_CLOSE" if not is_open else f"STALE_DATA_{int(freshness_sec)}S")
                )
            else:
                is_rt = False
                is_eligible = False
                reason = "TIMESTAMP_UNKNOWN_FAIL_CLOSED"

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
                source="LIVE_NSE",
                timestamp_ist=fetched_at_str,
                data_timestamp=data_ts_str,
                fetched_at=fetched_at_str,
                freshness_seconds=freshness_sec,
                is_realtime=is_rt,
                trade_eligible=is_eligible,
                status_reason=reason,
            )
        except Exception as exc:
            logger.debug("Direct NSE quote failed for %s: %s", clean_symbol, exc)
            return None

    def _fetch_nsepython(self, clean_symbol: str, avg_volume: float) -> Optional[NSELiveQuote]:
        """Tier 2: nsepython wrapper fallback -> NSEPYTHON with strict timestamp validation."""
        try:
            from nsepython import nse_eq, nse_get_index_quote

            now_ist = datetime.now(_IST)
            fetched_at_str = now_ist.isoformat()
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

                    raw_time = raw.get("timestamp") or raw.get("lastUpdateTime")
                    data_dt = _parse_exchange_timestamp(raw_time)
                    data_ts_str = data_dt.isoformat() if data_dt else None
                    freshness_sec = (
                        max(0.0, (now_ist - data_dt).total_seconds()) if data_dt else None
                    )
                    is_open = is_nse_market_open(now_ist)

                    # Fail-closed: nsepython is NOT assumed realtime unless timestamp is verified
                    if data_dt is not None and freshness_sec is not None:
                        is_rt = freshness_sec <= 60.0
                        is_eligible = is_rt and is_open
                        reason = (
                            "VERIFIED_REALTIME"
                            if is_eligible
                            else ("MARKET_CLOSED_LAST_CLOSE" if not is_open else f"STALE_DATA_{int(freshness_sec)}S")
                        )
                    else:
                        is_rt = False
                        is_eligible = False
                        reason = "TIMESTAMP_UNKNOWN_FAIL_CLOSED"

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
                        source="NSEPYTHON",
                        timestamp_ist=fetched_at_str,
                        data_timestamp=data_ts_str,
                        fetched_at=fetched_at_str,
                        freshness_seconds=freshness_sec,
                        is_realtime=is_rt,
                        trade_eligible=is_eligible,
                        status_reason=reason,
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

            raw_time = (
                raw.get("metadata", {}).get("lastUpdateTime")
                or pi.get("lastUpdateTime")
                or raw.get("lastUpdateTime")
            )
            data_dt = _parse_exchange_timestamp(raw_time)
            data_ts_str = data_dt.isoformat() if data_dt else None
            freshness_sec = (
                max(0.0, (now_ist - data_dt).total_seconds()) if data_dt else None
            )
            is_open = is_nse_market_open(now_ist)

            # Strict: Do not assume realtime unless valid timestamp is verified <= 60s
            if data_dt is not None and freshness_sec is not None:
                is_rt = freshness_sec <= 60.0
                is_eligible = is_rt and is_open
                reason = (
                    "VERIFIED_REALTIME"
                    if is_eligible
                    else ("MARKET_CLOSED_LAST_CLOSE" if not is_open else f"STALE_DATA_{int(freshness_sec)}S")
                )
            else:
                is_rt = False
                is_eligible = False
                reason = "TIMESTAMP_UNKNOWN_FAIL_CLOSED"

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
                source="NSEPYTHON",
                timestamp_ist=fetched_at_str,
                data_timestamp=data_ts_str,
                fetched_at=fetched_at_str,
                freshness_seconds=freshness_sec,
                is_realtime=is_rt,
                trade_eligible=is_eligible,
                status_reason=reason,
            )
        except Exception as exc:
            logger.debug("nsepython quote failed for %s: %s", clean_symbol, exc)
            return None

    def _fetch_yfinance(self, symbol: str, avg_volume: float) -> Optional[NSELiveQuote]:
        """Tier 3: yfinance fast_info fallback -> DELAYED_YFINANCE (~15m delayed, intraday blocked)."""
        try:
            now_ist = datetime.now(_IST)
            fetched_at_str = now_ist.isoformat()

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

            # yfinance fast_info does not provide sub-second exchange tick timestamp,
            # and public feeds are ~15m delayed. Fail-closed: intraday trade_eligible is False.
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
                source="DELAYED_YFINANCE",
                timestamp_ist=fetched_at_str,
                data_timestamp=fetched_at_str,
                fetched_at=fetched_at_str,
                freshness_seconds=900.0,
                is_realtime=False,
                trade_eligible=False,
                status_reason="DELAYED_YFINANCE_UNSAFE_INTRADAY",
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
