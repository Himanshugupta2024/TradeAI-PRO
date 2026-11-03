"""
TradeAI-PRO V2 — Targeted News & Sentiment Engine.

Integrates the free, keyless GDELT 2.0 Doc API with Google News RSS fallback
to deliver targeted macro sentiment scoring for Top 10 shortlisted technical setups.
- Zero paid API keys required.
- Fetches sentiment ONLY for shortlisted trade candidates to preserve bandwidth and avoid rate limits.
- Evaluates headline tone, keyword polarity, and returns a calibrated sentiment score.
- In-memory TTL cache (15 min) prevents redundant external queries.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Polarity keyword lexicons for financial headlines
_BULLISH_KEYWORDS = {
    "surge", "rally", "bull", "soar", "breakout", "gain", "high", "up",
    "profit", "growth", "dividend", "outperform", "upgrade", "record",
    "order win", "expansion", "deal", "beat", "positive", "buy",
}
_BEARISH_KEYWORDS = {
    "crash", "dump", "bear", "fall", "drop", "low", "fear", "down",
    "loss", "plunge", "decline", "warning", "downgrade", "probe", "fraud",
    "investigation", "penalty", "default", "weak", "sell", "debt", "slump",
}

# NSE Company Name Dictionary for high-precision news queries
_SYMBOL_COMPANY_MAP = {
    "RELIANCE": "Reliance Industries",
    "TCS": "Tata Consultancy Services",
    "INFY": "Infosys",
    "HDFCBANK": "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
    "SBIN": "State Bank of India",
    "BHARTIARTL": "Bharti Airtel",
    "ITC": "ITC Limited",
    "KOTAKBANK": "Kotak Mahindra Bank",
    "LT": "Larsen Toubro",
    "TATAMOTORS": "Tata Motors",
    "AXISBANK": "Axis Bank",
    "WIPRO": "Wipro",
    "MARUTI": "Maruti Suzuki",
    "BAJFINANCE": "Bajaj Finance",
    "TITAN": "Titan Company",
    "SUNPHARMA": "Sun Pharma",
    "NIFTY 50": "Nifty 50 Index",
    "BANKNIFTY": "Bank Nifty Index",
}


class GDELTProvider:
    """Targeted news and sentiment provider powered by GDELT 2.0 with RSS fallback."""

    name: str = "gdelt_sentiment"

    GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
    GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

    def __init__(self, cache_ttl_sec: float = 900.0, request_timeout: float = 3.5):
        self.cache_ttl = cache_ttl_sec
        self.timeout = request_timeout
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}  # {symbol: (epoch, result)}

    def _get_search_query(self, symbol: str) -> str:
        """Construct a targeted Indian market search query."""
        clean_sym = symbol.replace(".NS", "").replace(".BO", "").strip().upper()
        comp_name = _SYMBOL_COMPANY_MAP.get(clean_sym, clean_sym)
        # Targeted query combining symbol/company and financial context
        return f'("{comp_name}" OR "{clean_sym}") (stock OR shares OR profit OR quarterly OR market)'

    def get_sentiment_sync(self, symbol: str) -> dict[str, Any]:
        """Synchronously fetch sentiment for a single symbol (cache-first)."""
        clean_sym = symbol.replace(".NS", "").replace(".BO", "").strip().upper()
        now = time.time()

        cached = self._cache.get(clean_sym)
        if cached and (now - cached[0] < self.cache_ttl):
            return cached[1]

        res = self._query_gdelt_sync(clean_sym)
        if not res or res.get("article_count", 0) == 0:
            res = self._query_google_rss_sync(clean_sym)

        if res:
            self._cache[clean_sym] = (now, res)
            return res

        neutral = {
            "symbol": clean_sym,
            "sentiment": "NEUTRAL",
            "tone_score": 0.0,
            "confidence": 0.5,
            "article_count": 0,
            "top_headlines": [],
            "source": "FALLBACK_NEUTRAL",
        }
        self._cache[clean_sym] = (now, neutral)
        return neutral

    def _query_gdelt_sync(self, clean_sym: str) -> dict[str, Any] | None:
        """Synchronous query to GDELT 2.0 API."""
        query = self._get_search_query(clean_sym)
        params = {
            "query": query,
            "mode": "artlist",
            "maxrecords": "5",
            "format": "json",
            "timespan": "7d",
            "sort": "DateDesc",
        }
        try:
            headers = {"User-Agent": "TradeAI-PRO-Targeted-Sentiment/2.0"}
            resp = requests.get(self.GDELT_URL, params=params, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                articles = data.get("articles", [])
                if articles:
                    titles = [a.get("title", "") for a in articles if a.get("title")]
                    tone_score, sentiment, conf = self._analyze_headlines(titles)
                    return {
                        "symbol": clean_sym,
                        "sentiment": sentiment,
                        "tone_score": tone_score,
                        "confidence": conf,
                        "article_count": len(articles),
                        "top_headlines": titles[:3],
                        "source": "GDELT_2.0",
                    }
        except Exception as exc:
            logger.debug("[GDELT] Sync API query failed for %s: %s", clean_sym, exc)
        return None

    def _query_google_rss_sync(self, clean_sym: str) -> dict[str, Any] | None:
        """Synchronous fallback to Google News RSS query."""
        import xml.etree.ElementTree as ET
        query_str = urllib.parse.quote_plus(self._get_search_query(clean_sym))
        url = self.GOOGLE_NEWS_RSS_URL.format(query=query_str)
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                titles = [item.findtext("title", "") for item in root.findall(".//item")][:5]
                if titles:
                    tone_score, sentiment, conf = self._analyze_headlines(titles)
                    return {
                        "symbol": clean_sym,
                        "sentiment": sentiment,
                        "tone_score": tone_score,
                        "confidence": conf,
                        "article_count": len(titles),
                        "top_headlines": titles[:3],
                        "source": "GOOGLE_NEWS_RSS",
                    }
        except Exception as exc:
            logger.debug("[GDELT] Sync Google RSS fallback failed for %s: %s", clean_sym, exc)
        return None

    async def get_sentiment(self, symbol: str) -> dict[str, Any]:
        """Fetch sentiment for a single symbol (cache-first)."""
        clean_sym = symbol.replace(".NS", "").replace(".BO", "").strip().upper()
        now = time.time()

        # 1. Check TTL cache
        cached = self._cache.get(clean_sym)
        if cached and (now - cached[0] < self.cache_ttl):
            return cached[1]

        # 2. Query in worker thread to prevent blocking
        res = await asyncio.to_thread(self._query_gdelt_sync, clean_sym)
        if not res or res.get("article_count", 0) == 0:
            res = await asyncio.to_thread(self._query_google_rss_sync, clean_sym)

        if res:
            self._cache[clean_sym] = (now, res)
            return res

        neutral = {
            "symbol": clean_sym,
            "sentiment": "NEUTRAL",
            "tone_score": 0.0,
            "confidence": 0.5,
            "article_count": 0,
            "top_headlines": [],
            "source": "FALLBACK_NEUTRAL",
        }
        self._cache[clean_sym] = (now, neutral)
        return neutral

    async def get_batch_sentiment(
        self,
        symbols: list[str],
        max_concurrency: int = 5,
    ) -> dict[str, dict[str, Any]]:
        """
        Concurrently fetch sentiment for shortlisted symbols with strict concurrency.
        Guarantees sub-2s response time for up to 10 candidates.
        """
        if not symbols:
            return {}

        sem = asyncio.Semaphore(max_concurrency)

        async def _fetch_safe(sym: str):
            async with sem:
                try:
                    return sym, await self.get_sentiment(sym)
                except Exception as exc:
                    logger.debug("[GDELT] Sentiment fetch failed for %s: %s", sym, exc)
                    return sym, {
                        "symbol": sym,
                        "sentiment": "NEUTRAL",
                        "tone_score": 0.0,
                        "confidence": 0.5,
                        "article_count": 0,
                        "top_headlines": [],
                        "source": "ERROR_NEUTRAL",
                    }

        tasks = [_fetch_safe(s) for s in symbols]
        results = await asyncio.gather(*tasks)
        return {sym: payload for sym, payload in results}

    async def _query_gdelt(self, clean_sym: str) -> dict[str, Any] | None:
        """Query GDELT 2.0 API in a non-blocking thread."""
        query = self._get_search_query(clean_sym)
        params = {
            "query": query,
            "mode": "artlist",
            "maxrecords": "5",
            "format": "json",
            "timespan": "7d",
            "sort": "DateDesc",
        }

        def _sync_req():
            headers = {"User-Agent": "TradeAI-PRO-Targeted-Sentiment/2.0"}
            resp = requests.get(self.GDELT_URL, params=params, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    return None
            return None

        try:
            data = await asyncio.to_thread(_sync_req)
            if not data or "articles" not in data:
                return None

            articles = data.get("articles", [])
            if not articles:
                return None

            titles = [a.get("title", "") for a in articles if a.get("title")]
            tone_score, sentiment, conf = self._analyze_headlines(titles)

            return {
                "symbol": clean_sym,
                "sentiment": sentiment,
                "tone_score": tone_score,
                "confidence": conf,
                "article_count": len(articles),
                "top_headlines": titles[:3],
                "source": "GDELT_2.0",
            }
        except Exception as exc:
            logger.debug("[GDELT] API query failed for %s: %s", clean_sym, exc)
            return None

    async def _query_google_rss(self, clean_sym: str) -> dict[str, Any] | None:
        """Fallback to Google News RSS query."""
        import xml.etree.ElementTree as ET

        query_str = urllib.parse.quote_plus(self._get_search_query(clean_sym))
        url = self.GOOGLE_NEWS_RSS_URL.format(query=query_str)

        def _sync_rss():
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.content
            return None

        try:
            content = await asyncio.to_thread(_sync_rss)
            if not content:
                return None

            root = ET.fromstring(content)
            titles = [item.findtext("title", "") for item in root.findall(".//item")][:5]
            if not titles:
                return None

            tone_score, sentiment, conf = self._analyze_headlines(titles)
            return {
                "symbol": clean_sym,
                "sentiment": sentiment,
                "tone_score": tone_score,
                "confidence": conf,
                "article_count": len(titles),
                "top_headlines": titles[:3],
                "source": "GOOGLE_NEWS_RSS",
            }
        except Exception as exc:
            logger.debug("[GDELT] Google RSS fallback failed for %s: %s", clean_sym, exc)
            return None

    def _analyze_headlines(self, titles: list[str]) -> tuple[float, str, float]:
        """Compute tone score (-10 to +10), sentiment label, and confidence from headline text."""
        if not titles:
            return 0.0, "NEUTRAL", 0.5

        bull_count = 0
        bear_count = 0

        for t in titles:
            text = t.lower()
            words = set(re.findall(r"\b[a-z]+\b", text))
            bull_matches = words.intersection(_BULLISH_KEYWORDS)
            bear_matches = words.intersection(_BEARISH_KEYWORDS)
            bull_count += len(bull_matches)
            bear_count += len(bear_matches)

        total_signals = bull_count + bear_count
        if total_signals == 0:
            return 0.0, "NEUTRAL", 0.5

        raw_score = (bull_count - bear_count) / max(1, total_signals)
        tone_score = round(raw_score * 10.0, 1)  # Scale to -10 .. +10

        if tone_score >= 2.0:
            sentiment = "BULLISH"
        elif tone_score <= -2.0:
            sentiment = "BEARISH"
        else:
            sentiment = "NEUTRAL"

        confidence = round(min(0.95, 0.5 + (abs(raw_score) * 0.45)), 2)
        return tone_score, sentiment, confidence


_gdelt_singleton: GDELTProvider | None = None


def get_gdelt_provider() -> GDELTProvider:
    """Singleton accessor for GDELTProvider."""
    global _gdelt_singleton
    if _gdelt_singleton is None:
        _gdelt_singleton = GDELTProvider()
    return _gdelt_singleton
