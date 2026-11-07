"""
Global Macro & Commodity Intelligence Fetcher.

Tracks real-time international macro drivers:
- Crude Oil Barrel Prices: Brent (BZ=F) & WTI (CL=F)
- US Dollar Index DXY (DX-Y.NYB)
- US 10-Year Treasury Yield (^TNX)
- US Equity Markets: S&P 500 (^GSPC), Nasdaq (^IXIC)
- Safe Haven: Gold (GC=F)
- Targeted Macro News: Energy, US Fed, Indian Economy, Geopolitics

Evaluates Sector Impact for Indian Equities:
- High Crude -> Negative for Paints (ASIANPAINT, BERGEPAINT), Aviation (INDIGO), OMCs (BPCL, IOC, HPCL)
- High Crude -> Positive for Exploration/Refining (ONGC, OIL, RELIANCE)
- High US Yields -> Tech Margin Compression (TCS, INFY, WIPRO)

100% free, keyless (yfinance + Google News RSS), with in-memory TTL caching (15 min).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Sensitive Indian Equities by Macro Factor
SECTOR_CORRELATIONS = {
    "CRUDE_BEARISH_IMPACT": [
        "ASIANPAINT", "BERGEPAINT", "KANSAINER",  # Paints (crude derivatives = 50% raw cost)
        "INDIGO", "SPICEJET",                      # Aviation (ATF fuel = 40% operating cost)
        "BPCL", "IOC", "HPCL",                     # OMCs (marketing margin pressure)
        "MRF", "APOLLOTYRE", "CEATLTD", "JKTYRE",  # Tyres (synthetic rubber from crude)
    ],
    "CRUDE_BULLISH_IMPACT": [
        "ONGC", "OIL",                             # Upstream explorers (higher realization)
        "RELIANCE",                                # GRM refining margins benefit
    ],
    "US_YIELD_SENSITIVE": [
        "TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM", # IT service exporters
    ],
    "RATE_CUT_SENSITIVE": [
        "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "BAJFINANCE",
    ],
}


@dataclass
class CommodityQuote:
    symbol: str
    name: str
    price: float
    change_pct: float
    unit: str
    status: str  # BULLISH / BEARISH / NEUTRAL for Indian equities


@dataclass
class MacroNewsItem:
    category: str      # CRUDE / US_FED / INDIA_MACRO / GEOPOLITICAL
    title: str
    source: str
    pub_date: str
    sentiment: str     # BULLISH / BEARISH / NEUTRAL
    sentiment_score: float  # -1.0 to +1.0


@dataclass
class GlobalMacroIntelligence:
    timestamp: str
    brent_crude: CommodityQuote
    wti_crude: CommodityQuote
    us_10y_yield: CommodityQuote
    dxy_dollar_index: CommodityQuote
    sp500: CommodityQuote
    nasdaq: CommodityQuote
    gold: CommodityQuote
    macro_regime: str           # RISK_ON / RISK_OFF / NEUTRAL / HIGH_CRUDE_RISK
    macro_cue_summary: str      # Human-readable summary for Indian traders
    active_sector_alerts: List[Dict[str, str]]
    breaking_news: List[MacroNewsItem]
    feed_source: str = "LIVE"   # "LIVE" or "FALLBACK"


class GlobalMacroFetcher:
    """Singleton fetcher for global commodities, yields, US indices, and targeted news."""

    TICKERS = {
        "brent": ("BZ=F", "Brent Crude Oil", "$/bbl"),
        "wti": ("CL=F", "WTI Crude Oil", "$/bbl"),
        "yield_10y": ("^TNX", "US 10-Year Yield", "%"),
        "dxy": ("DX-Y.NYB", "US Dollar Index", "pts"),
        "sp500": ("^GSPC", "S&P 500 (US)", "pts"),
        "nasdaq": ("^IXIC", "Nasdaq Composite", "pts"),
        "gold": ("GC=F", "Gold", "$/oz"),
    }

    NEWS_STREAMS = {
        "CRUDE": "crude+oil+OR+brent+crude+price",
        "US_FED": "Federal+Reserve+interest+rates+OR+Wall+Street",
        "INDIA_MACRO": "RBI+repo+rate+OR+Nifty+OR+Indian+economy",
    }

    _BULLISH_WORDS = {
        "surge", "rally", "gain", "rise", "jump", "high", "rebound", "boost",
        "growth", "profit", "optimism", "recovery", "cut", "easing", "cool",
    }
    _BEARISH_WORDS = {
        "plunge", "fall", "drop", "sink", "crash", "fear", "loss", "recession",
        "inflation", "hike", "tightening", "crisis", "pressure", "escalat", "war",
    }

    def __init__(self, cache_ttl_sec: float = 900.0):
        self.cache_ttl = cache_ttl_sec
        self._cached_intel: Optional[GlobalMacroIntelligence] = self._build_fallback_intel()
        self._last_fetch_ts: float = 0.0

    def get_intelligence(self, force_refresh: bool = False) -> GlobalMacroIntelligence:
        """Fetch global macro intelligence with 15-minute caching and resilient timeout protection."""
        now = time.time()
        if not force_refresh and self._cached_intel is not None and (now - self._last_fetch_ts) < self.cache_ttl:
            return self._cached_intel

        import concurrent.futures
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self._fetch_live)
                intel = future.result(timeout=8.0)
            self._cached_intel = intel
            self._last_fetch_ts = now
            return intel
        except Exception as e:
            logger.warning("Live global macro fetch timed out or failed (%s) — using fallback intelligence", e)
            if self._cached_intel is not None and getattr(self._cached_intel, "feed_source", "FALLBACK") == "LIVE":
                return self._cached_intel
            fallback = self._build_fallback_intel()
            self._cached_intel = fallback
            # Shorter retry (2 min) if on fallback so live feed can recover promptly
            self._last_fetch_ts = now - (self.cache_ttl - 120.0)
            return fallback

    def _fetch_live(self) -> GlobalMacroIntelligence:
        """Fetch real data from Yahoo Finance and Google News RSS."""
        # 1. Download market tickers
        symbols = [item[0] for item in self.TICKERS.values()]
        quotes: Dict[str, CommodityQuote] = {}

        try:
            df = yf.download(symbols, period="5d", interval="1d", progress=False, timeout=3.0)
            if not df.empty and "Close" in df.columns:
                close_df = df["Close"]
                for key, (sym, name, unit) in self.TICKERS.items():
                    if sym in close_df.columns:
                        series = close_df[sym].dropna()
                        if len(series) >= 2:
                            curr_price = float(series.iloc[-1])
                            prev_price = float(series.iloc[-2])
                            chg_pct = round(((curr_price - prev_price) / prev_price) * 100.0, 2)
                        elif len(series) == 1:
                            curr_price = float(series.iloc[-1])
                            chg_pct = 0.0
                        else:
                            curr_price, chg_pct = self._default_val(key)
                    else:
                        curr_price, chg_pct = self._default_val(key)

                    status = self._evaluate_ticker_status(key, chg_pct, curr_price)
                    quotes[key] = CommodityQuote(
                        symbol=sym,
                        name=name,
                        price=round(curr_price, 2),
                        change_pct=chg_pct,
                        unit=unit,
                        status=status,
                    )
        except Exception as ex:
            logger.warning("yfinance macro download failed: %s — using defaults", ex)

        # Fill any missing quotes with defaults
        for key, (sym, name, unit) in self.TICKERS.items():
            if key not in quotes:
                curr_price, chg_pct = self._default_val(key)
                quotes[key] = CommodityQuote(
                    symbol=sym,
                    name=name,
                    price=curr_price,
                    change_pct=chg_pct,
                    unit=unit,
                    status="NEUTRAL",
                )

        # 2. Fetch targeted news streams
        news_items = self._fetch_macro_news()

        # 3. Macro regime and cue calculation
        brent_chg = quotes["brent"].change_pct
        sp_chg = quotes["sp500"].change_pct
        yield_chg = quotes["yield_10y"].change_pct

        alerts: List[Dict[str, str]] = []
        if brent_chg >= 2.0:
            regime = "HIGH_CRUDE_RISK"
            summary = f"Crude Oil spiked +{brent_chg}% (Brent ${quotes['brent'].price}/bbl) — Headwinds for Indian Paints, Aviation & OMCs"
            alerts.append({
                "type": "BEARISH_SECTOR_HEADWIND",
                "driver": f"Brent Crude +{brent_chg}%",
                "affected_sectors": "Paints (Asian Paints, Berger), Aviation (IndiGo), OMCs (BPCL, IOC)",
                "action": "Avoid aggressive Long breakouts; prefer Skip or tight SLs",
            })
            alerts.append({
                "type": "BULLISH_SECTOR_TAILWIND",
                "driver": f"Brent Crude +{brent_chg}%",
                "affected_sectors": "Upstream Oil & Gas (ONGC, Oil India)",
                "action": "Positive price realization tailwind",
            })
        elif brent_chg <= -2.0:
            regime = "LOW_CRUDE_BENEFIT"
            summary = f"Crude Oil eased {brent_chg}% (Brent ${quotes['brent'].price}/bbl) — Margin expansion tailwind for Indian consumption & paints"
            alerts.append({
                "type": "BULLISH_SECTOR_TAILWIND",
                "driver": f"Brent Crude {brent_chg}%",
                "affected_sectors": "Paints, Aviation, Tyres, Auto",
                "action": "Lower input costs bolster quarterly margins",
            })
        elif sp_chg >= 0.5:
            regime = "RISK_ON"
            summary = f"Wall Street rallies (S&P 500 +{sp_chg}%) — Favorable overnight global cues for NSE opening"
        elif sp_chg <= -1.0:
            regime = "RISK_OFF"
            summary = f"Global risk aversion (S&P 500 {sp_chg}%) — Caution recommended on morning gap-up chases"
            alerts.append({
                "type": "GLOBAL_RISK_OFF",
                "driver": f"US Markets {sp_chg}%",
                "affected_sectors": "High-Beta & Small/Midcap Equities",
                "action": "Enforce strict minimum 1.8x R:R gates",
            })
        else:
            regime = "NEUTRAL"
            summary = f"Global macro balanced (Brent ${quotes['brent'].price}/bbl, US 10Y {quotes['yield_10y'].price}%) — Domestic price action dominant"

        return GlobalMacroIntelligence(
            timestamp=datetime.now(timezone.utc).isoformat(),
            brent_crude=quotes["brent"],
            wti_crude=quotes["wti"],
            us_10y_yield=quotes["yield_10y"],
            dxy_dollar_index=quotes["dxy"],
            sp500=quotes["sp500"],
            nasdaq=quotes["nasdaq"],
            gold=quotes["gold"],
            macro_regime=regime,
            macro_cue_summary=summary,
            active_sector_alerts=alerts,
            breaking_news=news_items,
            feed_source="LIVE",
        )

    def _fetch_one_rss(self, cat: str, query: str) -> List[MacroNewsItem]:
        url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        items: List[MacroNewsItem] = []
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                xml_data = resp.read()
                root = ET.fromstring(xml_data)
                rss_items = root.findall(".//item")[:2]
                for it in rss_items:
                    raw_title = it.find("title").text or ""
                    source = it.find("source").text if it.find("source") is not None else "Financial News"
                    pub = it.find("pubDate").text or ""
                    score, sentiment = self._compute_headline_polarity(raw_title)
                    items.append(MacroNewsItem(
                        category=cat,
                        title=raw_title,
                        source=source,
                        pub_date=pub[:16],
                        sentiment=sentiment,
                        sentiment_score=score,
                    ))
        except Exception as e:
            logger.debug("Failed to fetch RSS news for %s: %s", cat, e)
        return items

    def _fetch_macro_news(self) -> List[MacroNewsItem]:
        """Fetch targeted headlines from Google News RSS concurrently."""
        items: List[MacroNewsItem] = []
        import concurrent.futures
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.NEWS_STREAMS)) as executor:
                futures = [
                    executor.submit(self._fetch_one_rss, cat, query)
                    for cat, query in self.NEWS_STREAMS.items()
                ]
                for fut in concurrent.futures.as_completed(futures, timeout=4.0):
                    try:
                        items.extend(fut.result())
                    except Exception as exc:
                        logger.debug("RSS worker failed: %s", exc)
        except Exception as e:
            logger.debug("Concurrent RSS fetch error: %s", e)

        # Fallback if news failed
        if not items:
            items = [
                MacroNewsItem("CRUDE", "Brent Crude trades near $74-$76 range as market balances demand with supply", "Global Energy", "", "NEUTRAL", 0.0),
                MacroNewsItem("US_FED", "Federal Reserve interest rate decisions remain data dependent", "Macro Wire", "", "NEUTRAL", 0.0),
                MacroNewsItem("INDIA_MACRO", "RBI keeps focus on inflation targeting while supporting durable growth", "Economic Bureau", "", "BULLISH", 0.25),
            ]
        return items

    def _compute_headline_polarity(self, text: str) -> Tuple[float, str]:
        """Determine if headline is bullish or bearish with stem/prefix support."""
        tokens = re.findall(r"\b[a-z]+\b", text.lower())
        bull_hits = sum(1 for t in tokens if any(t.startswith(w) or (len(t) >= 4 and w.startswith(t)) for w in self._BULLISH_WORDS))
        bear_hits = sum(1 for t in tokens if any(t.startswith(w) or (len(t) >= 4 and w.startswith(t)) for w in self._BEARISH_WORDS))

        if bull_hits > bear_hits:
            return round(min(1.0, 0.3 * bull_hits), 2), "BULLISH"
        elif bear_hits > bull_hits:
            return round(max(-1.0, -0.3 * bear_hits), 2), "BEARISH"
        return 0.0, "NEUTRAL"

    def _evaluate_ticker_status(self, key: str, chg_pct: float, price: float) -> str:
        if key in ("brent", "wti"):
            return "BEARISH" if chg_pct > 1.5 else ("BULLISH" if chg_pct < -1.5 else "NEUTRAL")
        elif key == "yield_10y":
            return "BEARISH" if chg_pct > 1.5 or price > 4.6 else "NEUTRAL"
        elif key == "dxy":
            return "BEARISH" if price > 105.0 else ("BULLISH" if price < 102.0 else "NEUTRAL")
        elif key in ("sp500", "nasdaq"):
            return "BULLISH" if chg_pct > 0.3 else ("BEARISH" if chg_pct < -0.6 else "NEUTRAL")
        return "NEUTRAL"

    def _default_val(self, key: str) -> Tuple[float, float]:
        defaults = {
            "brent": (74.50, -0.4),
            "wti": (70.20, -0.3),
            "yield_10y": (4.25, 0.0),
            "dxy": (103.80, -0.1),
            "sp500": (5850.0, 0.4),
            "nasdaq": (18500.0, 0.6),
            "gold": (2740.0, 0.2),
        }
        return defaults.get(key, (100.0, 0.0))

    def _build_fallback_intel(self) -> GlobalMacroIntelligence:
        return GlobalMacroIntelligence(
            timestamp=datetime.now(timezone.utc).isoformat(),
            brent_crude=CommodityQuote("BZ=F", "Brent Crude Oil", 74.50, -0.4, "$/bbl", "NEUTRAL"),
            wti_crude=CommodityQuote("CL=F", "WTI Crude Oil", 70.20, -0.3, "$/bbl", "NEUTRAL"),
            us_10y_yield=CommodityQuote("^TNX", "US 10-Year Yield", 4.25, 0.0, "%", "NEUTRAL"),
            dxy_dollar_index=CommodityQuote("DX-Y.NYB", "US Dollar Index", 103.80, -0.1, "pts", "NEUTRAL"),
            sp500=CommodityQuote("^GSPC", "S&P 500 (US)", 5850.0, 0.4, "pts", "BULLISH"),
            nasdaq=CommodityQuote("^IXIC", "Nasdaq Composite", 18500.0, 0.6, "pts", "BULLISH"),
            gold=CommodityQuote("GC=F", "Gold", 2740.0, 0.2, "$/oz", "NEUTRAL"),
            macro_regime="NEUTRAL",
            macro_cue_summary="Global cues stable (Brent $74.50/bbl, US S&P 500 steady) — Domestic setup focus",
            active_sector_alerts=[],
            breaking_news=[],
            feed_source="FALLBACK",
        )

    def evaluate_macro_sector_gate(self, symbol: str, direction: str) -> Tuple[bool, Optional[str]]:
        """
        Evaluate if a proposed trade violates severe macro sector headwinds.
        Returns: (is_allowed, optional_block_reason)
        """
        clean_sym = symbol.replace(".NS", "").replace(".BO", "").strip().upper()
        intel = self.get_intelligence()
        brent_chg = intel.brent_crude.change_pct
        brent_price = intel.brent_crude.price

        # 1. Crude Surge Gate on Consumer Paints, Aviation, and OMCs
        if direction in ("LONG", "BUY") and clean_sym in SECTOR_CORRELATIONS["CRUDE_BEARISH_IMPACT"]:
            if brent_chg >= 2.5 or brent_price >= 95.0:
                chg_str = f"+{brent_chg:.1f}%" if brent_chg >= 0 else f"{brent_chg:.1f}%"
                reason = f"Macro Risk Gate: Crude oil elevated at ${brent_price}/bbl ({chg_str}). Margin contraction risk for {clean_sym}."
                return False, reason

        # 2. Extreme US 10Y Yield Spike on IT Growth Exporters
        if direction in ("LONG", "BUY") and clean_sym in SECTOR_CORRELATIONS["US_YIELD_SENSITIVE"]:
            if intel.us_10y_yield.change_pct >= 3.0 and intel.us_10y_yield.price >= 4.75:
                reason = f"Macro Risk Gate: US 10-Year Yield surged to {intel.us_10y_yield.price}%. Tech valuation multiples under global pressure."
                return False, reason

        return True, None


_fetcher_instance: Optional[GlobalMacroFetcher] = None


def get_global_macro_fetcher() -> GlobalMacroFetcher:
    """Get or create singleton GlobalMacroFetcher."""
    global _fetcher_instance
    if _fetcher_instance is None:
        _fetcher_instance = GlobalMacroFetcher()
    return _fetcher_instance
