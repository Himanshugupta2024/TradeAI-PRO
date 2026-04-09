"""Context fetcher for 24hr + 7-day market context snapshots.

News: crypto uses free API + RSS (CoinDesk/CryptoPanic) with pubDate buckets for
24h vs 7d keyword sentiment. NSE uses Google News RSS by default; MarketAux is
opt-in (set CONTEXT_USE_PAID_APIS=true + MARKETAUX_API_KEY). Economic calendar
uses NSE HTTP by default; RapidAPI is opt-in with the same flag + RAPIDAPI_KEY.
Weekly bias uses ~7d price change (Binance klines / yfinance).

Cache-first; neutral fallbacks on failures so the signal pipeline stays non-blocking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import quote_plus

import requests
from sqlalchemy import delete

from config import settings
from db.connection import SessionLocal
from db.models import ContextSnapshot
from agents.data_ingestion.nse_native_quote import try_nsepython_stock_snapshot
from utils.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

_BULLISH_WORDS = {"surge", "rally", "bull", "soar", "breakout", "gain", "high", "up"}
_BEARISH_WORDS = {"crash", "dump", "bear", "fall", "drop", "low", "fear", "down"}


@dataclass
class ContextSnapshotData:
    """Context snapshot payload for one symbol."""

    symbol: str
    timestamp_utc: datetime
    price_change_24hr_pct: float
    volume_change_24hr_pct: float
    news_sentiment_24hr: float
    news_sentiment_7d: float
    news_volume_spike: bool
    weekly_trend_bias: str
    oi_change_7d_pct: float
    btc_dominance_7d_trend: str
    days_to_next_event: int
    event_risk_active: bool
    context_score: float


class ContextFetcher:
    """Fetches, caches, and persists market context snapshots."""

    def __init__(self, http_get: Any = None, now_provider: Any = None):
        self._http_get = http_get or requests.get
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self._root = Path(__file__).resolve().parents[2]
        self._context_cache_path = self._root / settings.CONTEXT_CACHE_FILE
        self._news_cache_path = self._root / settings.NEWS_CACHE_FILE
        self._usage_path = self._root / settings.DAILY_API_USAGE_FILE
        self._global_cache: dict[str, Any] = self._read_json(self._context_cache_path)
        self._news_cache: dict[str, Any] = self._read_json(self._news_cache_path)
        self._daily_usage: dict[str, Any] = self._read_json(self._usage_path)

    async def get_snapshot(self, symbol: str) -> ContextSnapshotData:
        """Return a context snapshot for symbol using cache-first refresh."""
        return await asyncio.to_thread(self.get_snapshot_sync, symbol)

    def get_snapshot_sync(self, symbol: str) -> ContextSnapshotData:
        """Synchronous context fetch for scheduler and tests."""
        normalized = self._normalize_symbol(symbol)
        now = self._now()

        cached = self._get_cached_snapshot(normalized)
        if cached and self._is_fresh(cached.get("cached_at"), settings.CONTEXT_REFRESH_INTERVAL):
            return self._snapshot_from_cache(cached)

        try:
            snapshot = self._build_snapshot(normalized, now)
            self._save_snapshot_cache(snapshot)
            self._persist_snapshot(snapshot)
            return snapshot
        except Exception as exc:
            logger.error("Context fetch failed for %s: %s", normalized, exc)
            if cached:
                return self._snapshot_from_cache(cached)
            return self._neutral_snapshot(normalized, now)

    def get_upcoming_events(self) -> list[dict[str, Any]]:
        """Get upcoming high-impact events from cached or fresh sources."""
        now = self._now()
        events_cache = self._global_cache.get("events", {})
        if self._is_fresh(events_cache.get("cached_at"), settings.CALENDAR_REFRESH_INTERVAL):
            return events_cache.get("items", [])

        events = self._fetch_upcoming_events(now)
        self._global_cache["events"] = {
            "cached_at": now.isoformat(),
            "items": events,
        }
        self._write_json(self._context_cache_path, self._global_cache)
        return events

    def _build_snapshot(self, symbol: str, now: datetime) -> ContextSnapshotData:
        is_crypto = symbol.endswith("USDT")
        price_change = 0.0
        volume_24h = 0.0

        if is_crypto:
            ticker = self._fetch_binance_ticker(symbol)
            price_change = float(ticker.get("price_change_pct", 0.0))
            volume_24h = float(ticker.get("volume", 0.0))
            news = self._fetch_crypto_news(self._coin_from_symbol(symbol), now)
            dominance = self._fetch_coingecko_global()
            trending = self._fetch_coingecko_trending()
            dominance_trend = self._derive_btc_dominance_trend(dominance.get("btc_dominance", 0.0))
            trending_boost = 0.10 if self._coin_from_symbol(symbol).lower() in trending else 0.0
            change_7d = self._fetch_crypto_price_change_7d(symbol)
        else:
            stock = self._fetch_nse_stock(symbol)
            price_change = float(stock.get("price_change_pct", 0.0))
            volume_24h = float(stock.get("volume", 0.0))
            news = self._fetch_nse_news(symbol)
            dominance_trend = "FLAT"
            trending_boost = 0.0
            change_7d = self._fetch_stock_price_change_7d(symbol)

        volume_change = self._compute_volume_change(symbol, volume_24h)
        news_sentiment_24, news_sentiment_7 = self._news_sentiments_from_payload(news)
        news_volume_spike = self._compute_news_spike(symbol, int(news.get("count_24h", 0)))

        weekly_bias = self._weekly_bias_from_7d_return(change_7d)
        oi_change_7d = 0.0

        events = self.get_upcoming_events()
        days_to_next, event_active = self._derive_event_risk(events, now)

        snapshot = ContextSnapshotData(
            symbol=symbol,
            timestamp_utc=now,
            price_change_24hr_pct=price_change,
            volume_change_24hr_pct=volume_change,
            news_sentiment_24hr=news_sentiment_24,
            news_sentiment_7d=news_sentiment_7,
            news_volume_spike=news_volume_spike,
            weekly_trend_bias=weekly_bias,
            oi_change_7d_pct=oi_change_7d,
            btc_dominance_7d_trend=dominance_trend,
            days_to_next_event=days_to_next,
            event_risk_active=event_active,
            context_score=0.0,
        )

        score = self._compute_context_score(snapshot)
        score += trending_boost
        snapshot.context_score = max(-1.0, min(1.0, score))
        return snapshot

    def _compute_context_score(self, snapshot: ContextSnapshotData) -> float:
        """Compute composite context score with the required formula."""
        score = 0.0
        w24 = float(getattr(settings, "CONTEXT_NEWS_WEIGHT_24HR", 0.15))
        w7 = float(getattr(settings, "CONTEXT_NEWS_WEIGHT_7D", 0.15))
        score += snapshot.news_sentiment_24hr * w24 + snapshot.news_sentiment_7d * w7
        score += 0.20 if snapshot.weekly_trend_bias == "BULLISH" else -0.20 if snapshot.weekly_trend_bias == "BEARISH" else 0.0
        score += min(snapshot.volume_change_24hr_pct / 100.0, 0.15)
        score -= 0.25 if snapshot.event_risk_active else 0.0
        score -= 0.10 if snapshot.btc_dominance_7d_trend == "RISING" and snapshot.symbol != "BTCUSDT" else 0.0
        return max(-1.0, min(1.0, score))

    def _persist_snapshot(self, snapshot: ContextSnapshotData) -> None:
        """Persist latest snapshot and purge records older than 90 days."""
        session = SessionLocal()
        try:
            row = ContextSnapshot(
                symbol=snapshot.symbol,
                timestamp_utc=snapshot.timestamp_utc.replace(tzinfo=None),
                price_change_24hr_pct=snapshot.price_change_24hr_pct,
                volume_change_24hr_pct=snapshot.volume_change_24hr_pct,
                news_sentiment_24hr=snapshot.news_sentiment_24hr,
                news_volume_spike=1 if snapshot.news_volume_spike else 0,
                weekly_trend_bias=snapshot.weekly_trend_bias,
                oi_change_7d_pct=snapshot.oi_change_7d_pct,
                btc_dominance_7d_trend=snapshot.btc_dominance_7d_trend,
                days_to_next_event=snapshot.days_to_next_event,
                event_risk_active=1 if snapshot.event_risk_active else 0,
                context_score=snapshot.context_score,
            )
            session.add(row)
            cutoff = datetime.utcnow() - timedelta(days=90)
            session.execute(delete(ContextSnapshot).where(ContextSnapshot.timestamp_utc < cutoff))
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.error("Context snapshot persistence failed for %s: %s", snapshot.symbol, exc)
        finally:
            session.close()

    def _save_snapshot_cache(self, snapshot: ContextSnapshotData) -> None:
        data = asdict(snapshot)
        data["timestamp_utc"] = snapshot.timestamp_utc.isoformat()
        self._global_cache.setdefault("symbols", {})[snapshot.symbol] = {
            "cached_at": self._now().isoformat(),
            "snapshot": data,
        }
        self._write_json(self._context_cache_path, self._global_cache)

    def _get_cached_snapshot(self, symbol: str) -> dict[str, Any] | None:
        return self._global_cache.get("symbols", {}).get(symbol)

    def _snapshot_from_cache(self, payload: dict[str, Any]) -> ContextSnapshotData:
        data = dict(payload.get("snapshot", {}))
        ts = data.get("timestamp_utc")
        data["timestamp_utc"] = datetime.fromisoformat(ts) if ts else self._now()
        if "news_sentiment_7d" not in data:
            data["news_sentiment_7d"] = 0.0
        return ContextSnapshotData(**data)

    def _neutral_snapshot(self, symbol: str, now: datetime) -> ContextSnapshotData:
        return ContextSnapshotData(
            symbol=symbol,
            timestamp_utc=now,
            price_change_24hr_pct=0.0,
            volume_change_24hr_pct=0.0,
            news_sentiment_24hr=0.0,
            news_sentiment_7d=0.0,
            news_volume_spike=False,
            weekly_trend_bias="NEUTRAL",
            oi_change_7d_pct=0.0,
            btc_dominance_7d_trend="FLAT",
            days_to_next_event=99,
            event_risk_active=False,
            context_score=0.0,
        )

    def _fetch_binance_ticker(self, symbol: str) -> dict[str, float]:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={quote_plus(symbol)}"
        data = self._get_json(url, timeout=10)
        return {
            "price_change_pct": float(data.get("priceChangePercent", 0.0)),
            "volume": float(data.get("volume", 0.0)),
            "high": float(data.get("highPrice", 0.0)),
            "low": float(data.get("lowPrice", 0.0)),
            "trade_count": float(data.get("count", 0.0)),
        }

    def _fetch_crypto_news(self, coin: str, now: datetime) -> dict[str, Any]:
        cache_key = f"crypto_{coin.upper()}"
        cached = self._news_cache.get(cache_key, {})
        if self._is_fresh(cached.get("cached_at"), settings.NEWS_REFRESH_INTERVAL):
            return cached.get(
                "payload",
                {"count_24h": 0, "count_7d": 0, "sentiment_24h": 0.0, "sentiment_7d": 0.0},
            )

        headlines_api: list[str] = []
        free_url = f"https://free-crypto-news.vercel.app/api/news?coin={quote_plus(coin.upper())}"
        try:
            payload = self._get_json(free_url, timeout=12)
            headlines_api = self._extract_headlines(payload)
        except Exception as exc:
            logger.warning("Free crypto news failed for %s: %s", coin, exc)

        rss_items = self._fetch_crypto_rss_items()
        titles_24, titles_7 = self._bucket_rss_titles_by_age(rss_items, now, coin)

        if titles_24:
            s24 = self._keyword_sentiment(titles_24)
        elif headlines_api:
            s24 = self._keyword_sentiment(headlines_api)
        else:
            s24 = 0.0

        if titles_7:
            s7 = self._keyword_sentiment(titles_7)
        elif headlines_api:
            s7 = self._keyword_sentiment(headlines_api)
        else:
            s7 = s24

        count_24 = len(titles_24) if titles_24 else len(headlines_api)
        count_7 = max(len(titles_7), count_24)

        result = {
            "count_24h": int(count_24),
            "count_7d": int(count_7),
            "sentiment_24h": float(s24),
            "sentiment_7d": float(s7),
        }
        self._news_cache[cache_key] = {
            "cached_at": self._now().isoformat(),
            "payload": result,
        }
        self._write_json(self._news_cache_path, self._news_cache)
        return result

    def _fetch_crypto_rss_headlines(self) -> list[str]:
        """Flat headlines from crypto RSS feeds (legacy helper)."""
        return [t for t, _ in self._fetch_crypto_rss_items()]

    def _fetch_crypto_rss_items(self) -> list[tuple[str, datetime | None]]:
        urls = [
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://cryptopanic.com/news/rss/",
        ]
        items: list[tuple[str, datetime | None]] = []
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0"}
        for url in urls:
            try:
                response = self._http_get(url, timeout=10, headers=headers)
                response.raise_for_status()
                text = response.text or ""
                items.extend(self._extract_rss_items(text))
            except Exception:
                continue
        return items[:80]

    def _bucket_rss_titles_by_age(
        self,
        items: list[tuple[str, datetime | None]],
        now: datetime,
        coin: str,
    ) -> tuple[list[str], list[str]]:
        titles_24: list[str] = []
        titles_7: list[str] = []
        cutoff_7d = now - timedelta(days=7)
        cutoff_24h = now - timedelta(hours=24)
        for title, pub in items:
            if not self._headline_matches_crypto_coin(title, coin):
                continue
            if pub is None or pub < cutoff_7d:
                continue
            titles_7.append(title)
            if pub >= cutoff_24h:
                titles_24.append(title)
        return titles_24, titles_7

    def _headline_matches_crypto_coin(self, title: str, coin: str) -> bool:
        t = title.lower()
        c = coin.upper()
        aliases = {
            "BTC": ("btc", "bitcoin"),
            "ETH": ("eth", "ethereum"),
            "SOL": ("sol", "solana"),
            "BNB": ("bnb", "binance"),
        }
        if c.lower() in t:
            return True
        for key, needles in aliases.items():
            if c == key and any(n in t for n in needles):
                return True
        return False

    def _fetch_coingecko_global(self) -> dict[str, float]:
        cached = self._global_cache.get("coingecko_global", {})
        ttl = max(60, settings.COINGECKO_CACHE_MINUTES * 60)
        if self._is_fresh(cached.get("cached_at"), ttl):
            return cached.get("payload", {})

        data = self._get_json("https://api.coingecko.com/api/v3/global", timeout=12)
        payload = {
            "btc_dominance": float(data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0.0)),
            "market_cap_change_24h": float(data.get("data", {}).get("market_cap_change_percentage_24h_usd", 0.0)),
        }
        self._global_cache["coingecko_global"] = {
            "cached_at": self._now().isoformat(),
            "payload": payload,
        }
        self._write_json(self._context_cache_path, self._global_cache)
        return payload

    def _fetch_coingecko_trending(self) -> set[str]:
        cached = self._global_cache.get("coingecko_trending", {})
        ttl = max(300, settings.COINGECKO_CACHE_MINUTES * 60)
        if self._is_fresh(cached.get("cached_at"), ttl):
            return set(cached.get("payload", []))

        data = self._get_json("https://api.coingecko.com/api/v3/search/trending", timeout=12)
        symbols = {
            str(item.get("item", {}).get("symbol", "")).lower()
            for item in data.get("coins", [])
            if item.get("item", {}).get("symbol")
        }
        self._global_cache["coingecko_trending"] = {
            "cached_at": self._now().isoformat(),
            "payload": sorted(symbols),
        }
        self._write_json(self._context_cache_path, self._global_cache)
        return symbols

    def _fetch_nse_stock(self, symbol: str) -> dict[str, float]:
        base = symbol.replace(".NS", "").replace("^", "").strip()
        native = try_nsepython_stock_snapshot(base)
        if native is not None:
            return native

        url = f"https://nse-api-khaki.vercel.app/stock?symbol={quote_plus(base)}"
        data = self._get_json(url, timeout=12)

        change = data.get("dayChangePercent")
        if change is None and isinstance(data.get("change"), (int, float)) and isinstance(data.get("previousClose"), (int, float)):
            prev = float(data.get("previousClose", 0.0))
            change = (float(data["change"]) / prev * 100.0) if prev else 0.0

        return {
            "price_change_pct": float(change or 0.0),
            "volume": float(data.get("volume", 0.0) or 0.0),
        }

    def _news_sentiments_from_payload(self, news: dict[str, Any]) -> tuple[float, float]:
        """Normalize legacy single `sentiment` vs split 24h / 7d fields."""
        raw_24 = news.get("sentiment_24h", news.get("sentiment"))
        raw_7 = news.get("sentiment_7d", raw_24)
        s24 = float(raw_24) if raw_24 is not None else 0.0
        s7 = float(raw_7) if raw_7 is not None else s24
        return s24, s7

    def _weekly_bias_from_7d_return(self, change_7d_pct: float) -> str:
        if change_7d_pct > 1.5:
            return "BULLISH"
        if change_7d_pct < -1.5:
            return "BEARISH"
        return "NEUTRAL"

    def _fetch_crypto_price_change_7d(self, symbol: str) -> float:
        url = f"https://api.binance.com/api/v3/klines?symbol={quote_plus(symbol)}&interval=1d&limit=8"
        try:
            response = self._http_get(url, timeout=10)
            response.raise_for_status()
            raw = response.json()
        except Exception:
            return 0.0
        if not isinstance(raw, list) or len(raw) < 2:
            return 0.0
        try:
            open_first = float(raw[0][1])
            close_last = float(raw[-1][4])
        except (TypeError, ValueError, IndexError):
            return 0.0
        if open_first <= 0:
            return 0.0
        return (close_last - open_first) / open_first * 100.0

    def _fetch_stock_price_change_7d(self, symbol: str) -> float:
        import yfinance as yf

        key = symbol.replace(".NS", "").strip().upper()
        ytick = settings.NSE_TICKER_MAP.get(key)
        if not ytick:
            if key in ("NIFTY50", "NIFTY"):
                ytick = settings.NSE_TICKER_MAP.get("NIFTY 50")
            elif "BANKNIFTY" in key.replace(" ", "") or key == "BANKNIFTY":
                ytick = settings.NSE_TICKER_MAP.get("BANKNIFTY")
        if not ytick:
            compact = key.replace(" ", "")
            if not compact:
                return 0.0
            ytick = f"{compact}.NS"
        try:
            hist = yf.Ticker(ytick).history(period="10d", interval="1d")
        except Exception:
            return 0.0
        if hist is None or hist.empty or len(hist) < 2:
            return 0.0
        first = float(hist["Close"].iloc[0])
        last = float(hist["Close"].iloc[-1])
        if first <= 0:
            return 0.0
        return (last - first) / first * 100.0

    def _fetch_nse_news(self, symbol: str) -> dict[str, Any]:
        base = symbol.replace(".NS", "").upper()
        cache_key = f"nse_{base}"
        cached = self._news_cache.get(cache_key, {})
        if self._is_fresh(cached.get("cached_at"), settings.NEWS_CACHE_MINUTES * 60):
            return cached.get(
                "payload",
                {"count_24h": 0, "count_7d": 0, "sentiment_24h": 0.0, "sentiment_7d": 0.0},
            )

        now = self._now()
        api_key = getattr(settings, "MARKETAUX_API_KEY", "")
        use_marketaux = (
            bool(getattr(settings, "CONTEXT_USE_PAID_APIS", False))
            and bool(api_key)
            and self._can_use_marketaux(base)
        )
        if use_marketaux:
            payload = self._fetch_nse_news_marketaux(base, now)
            self._increment_marketaux_usage()
        else:
            payload = self._fetch_nse_news_rss_free(base, now)

        self._news_cache[cache_key] = {"cached_at": now.isoformat(), "payload": payload}
        self._write_json(self._news_cache_path, self._news_cache)
        return payload

    def _fetch_nse_news_marketaux(self, base: str, now: datetime) -> dict[str, Any]:
        api_key = getattr(settings, "MARKETAUX_API_KEY", "")
        from_date = (now - timedelta(days=7)).date().isoformat()
        to_date = now.date().isoformat()
        url = (
            "https://api.marketaux.com/v1/news/all"
            f"?symbols={quote_plus(base)}"
            "&filter_entities=true&language=en"
            f"&published_after={from_date}T00:00"
            f"&published_before={to_date}T23:59"
            f"&api_token={quote_plus(api_key)}"
        )
        data = self._get_json(url, timeout=15)
        articles = data.get("data", []) if isinstance(data, dict) else []
        cutoff_7d = now - timedelta(days=7)
        sentiments_24: list[float] = []
        sentiments_7: list[float] = []
        count_24 = 0
        count_7d = 0
        for item in articles:
            published_at = item.get("published_at")
            published_dt: datetime | None = None
            if published_at:
                try:
                    published_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                except Exception:
                    published_dt = None
            if published_dt is None or published_dt < cutoff_7d:
                continue
            count_7d += 1
            score = item.get("sentiment_score")
            if isinstance(score, (float, int)):
                sc = float(score)
                sentiments_7.append(sc)
                if (now - published_dt) <= timedelta(hours=24):
                    sentiments_24.append(sc)
                    count_24 += 1

        s7 = float(mean(sentiments_7)) if sentiments_7 else 0.0
        s24 = float(mean(sentiments_24)) if sentiments_24 else (s7 if sentiments_7 else 0.0)
        return {
            "count_24h": count_24,
            "count_7d": count_7d,
            "sentiment_24h": s24,
            "sentiment_7d": s7,
        }

    def _fetch_nse_news_rss_free(self, base: str, now: datetime) -> dict[str, Any]:
        """Google News RSS (no API key) for ~7d window; keyword sentiment for 24h vs 7d buckets."""
        q = quote_plus(f"{base} NSE India stock OR {base} share price BSE")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0"
            ),
        }
        try:
            response = self._http_get(url, timeout=14, headers=headers)
            response.raise_for_status()
            items = self._extract_rss_items(response.text or "")
        except Exception as exc:
            logger.warning("NSE RSS news failed for %s: %s", base, exc)
            return {"count_24h": 0, "count_7d": 0, "sentiment_24h": 0.0, "sentiment_7d": 0.0}

        cutoff_7d = now - timedelta(days=7)
        cutoff_24h = now - timedelta(hours=24)
        titles_24: list[str] = []
        titles_7: list[str] = []
        for title, pub in items:
            if pub is None or pub < cutoff_7d:
                continue
            titles_7.append(title.strip())
            if pub >= cutoff_24h:
                titles_24.append(title.strip())

        s24 = self._keyword_sentiment(titles_24) if titles_24 else self._keyword_sentiment(titles_7)
        s7 = self._keyword_sentiment(titles_7) if titles_7 else float(s24)
        return {
            "count_24h": len(titles_24),
            "count_7d": len(titles_7),
            "sentiment_24h": float(s24),
            "sentiment_7d": float(s7),
        }

    def _extract_rss_items(self, xml_text: str) -> list[tuple[str, datetime | None]]:
        out: list[tuple[str, datetime | None]] = []
        pos = 0
        while True:
            i = xml_text.find("<item>", pos)
            if i == -1:
                break
            j = xml_text.find("</item>", i)
            if j == -1:
                break
            block = xml_text[i + 6 : j]
            title_raw = self._first_xml_tag_text(block, "title")
            pub_raw = self._first_xml_tag_text(block, "pubDate")
            pos = j + 7
            if not title_raw:
                continue
            title = (
                title_raw.replace("<![CDATA[", "").replace("]]>", "").strip()
            )
            if not title or "rss" in title.lower():
                continue
            dt: datetime | None = None
            if pub_raw:
                try:
                    dt = parsedate_to_datetime(pub_raw.strip())
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = dt.astimezone(timezone.utc)
                except (TypeError, ValueError):
                    dt = None
            out.append((title, dt))
        return out

    def _first_xml_tag_text(self, block: str, tag: str) -> str | None:
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
        if not m:
            return None
        return m.group(1).strip()

    def _fetch_upcoming_events(self, now: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        # RapidAPI economic calendar (requires key; off unless CONTEXT_USE_PAID_APIS).
        rapid_key = getattr(settings, "RAPIDAPI_KEY", "")
        if getattr(settings, "CONTEXT_USE_PAID_APIS", False) and rapid_key:
            try:
                start = now.date().isoformat()
                end = (now + timedelta(days=7)).date().isoformat()
                url = f"https://economic-calendar.horizonfx.id/events?from={start}&to={end}"
                response = self._http_get(
                    url,
                    timeout=15,
                    headers={
                        "X-RapidAPI-Key": rapid_key,
                        "X-RapidAPI-Host": "economic-calendar.horizonfx.id",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                rows = payload if isinstance(payload, list) else payload.get("data", [])
                events.extend(self._normalize_event_rows(rows, now))
            except Exception as exc:
                logger.warning("RapidAPI calendar fetch failed: %s", exc)

        # NSE corporate events endpoint.
        try:
            nse_url = "https://www.nseindia.com/api/event-calendar"
            response = self._http_get(
                nse_url,
                timeout=15,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://www.nseindia.com",
                },
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload if isinstance(payload, list) else payload.get("data", [])
            events.extend(self._normalize_event_rows(rows, now, default_impact="HIGH"))
        except Exception as exc:
            logger.warning("NSE event-calendar fetch failed: %s", exc)

        deduped: dict[str, dict[str, Any]] = {}
        for event in events:
            key = f"{event['event_name']}|{event['date']}"
            deduped[key] = event

        filtered = sorted(deduped.values(), key=lambda e: e["days_away"])
        return filtered[:50]

    def _normalize_event_rows(
        self,
        rows: list[Any],
        now: datetime,
        default_impact: str = "HIGH",
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            impact = str(row.get("impact") or row.get("importance") or default_impact).upper()
            if impact != "HIGH":
                continue
            name = str(row.get("event") or row.get("event_name") or row.get("title") or "Unknown Event")
            dt_text = row.get("date") or row.get("datetime") or row.get("time")
            event_dt = self._parse_event_datetime(dt_text)
            if event_dt is None:
                continue
            days_away = max(0, (event_dt.date() - now.date()).days)
            if days_away > 7:
                continue
            out.append(
                {
                    "event_name": name,
                    "date": event_dt.isoformat(),
                    "impact": impact,
                    "days_away": days_away,
                    "relevant_markets": row.get("country") or row.get("currency") or "GLOBAL",
                }
            )
        return out

    def _derive_event_risk(self, events: list[dict[str, Any]], now: datetime) -> tuple[int, bool]:
        _ = now
        if not events:
            return 99, False
        days = min(int(e.get("days_away", 99)) for e in events)
        return days, days <= settings.EVENT_RISK_DAYS_THRESHOLD

    def _compute_volume_change(self, symbol: str, current_volume: float) -> float:
        history = self._global_cache.setdefault("volume_history", {}).setdefault(symbol, [])
        baseline = mean(history[-7:]) if history else current_volume
        baseline = max(float(baseline), 1.0)
        change_pct = ((current_volume - baseline) / baseline) * 100.0
        history.append(float(current_volume))
        self._global_cache["volume_history"][symbol] = history[-30:]
        return float(change_pct)

    def _compute_news_spike(self, symbol: str, count_24h: int) -> bool:
        key = f"spike_{symbol}"
        hist = self._news_cache.setdefault(key, {}).setdefault("history", [])
        baseline = mean(hist[-7:]) if hist else 0.1
        hist.append(count_24h)
        self._news_cache[key]["history"] = hist[-30:]
        self._write_json(self._news_cache_path, self._news_cache)
        return count_24h > (settings.NEWS_SPIKE_MULTIPLIER * max(baseline, 0.1))

    def _derive_btc_dominance_trend(self, btc_dom: float) -> str:
        history = self._global_cache.setdefault("btc_dominance_history", [])
        history.append(float(btc_dom))
        history = history[-7:]
        self._global_cache["btc_dominance_history"] = history
        if len(history) < 2:
            return "FLAT"
        diff = history[-1] - history[0]
        if diff > 0.2:
            return "RISING"
        if diff < -0.2:
            return "FALLING"
        return "FLAT"

    def _can_use_marketaux(self, symbol: str) -> bool:
        top_n = max(1, settings.MARKETAUX_TOP_SYMBOLS)
        priority = [
            s.replace(".NS", "") for s in settings.NSE_SYMBOLS if isinstance(s, str)
        ][:top_n]
        if symbol not in priority:
            return False

        self._reset_usage_if_needed()
        count = int(self._daily_usage.get("marketaux_count", 0))
        return count < max(1, settings.MARKETAUX_DAILY_LIMIT)

    def _increment_marketaux_usage(self) -> None:
        self._reset_usage_if_needed()
        self._daily_usage["marketaux_count"] = int(self._daily_usage.get("marketaux_count", 0)) + 1
        self._write_json(self._usage_path, self._daily_usage)

    def _reset_usage_if_needed(self) -> None:
        today_ist = datetime.now(_IST).date().isoformat()
        if self._daily_usage.get("date_ist") != today_ist:
            self._daily_usage = {"date_ist": today_ist, "marketaux_count": 0}
            self._write_json(self._usage_path, self._daily_usage)

    def _get_json(self, url: str, timeout: int = 10) -> dict[str, Any]:
        response = self._http_get(url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        return {"data": payload}

    def _extract_headlines(self, payload: dict[str, Any]) -> list[str]:
        candidates: list[Any] = []
        for key in ("news", "data", "articles", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if not candidates and isinstance(payload.get("items"), list):
            candidates = payload["items"]

        headlines: list[str] = []
        for item in candidates:
            if isinstance(item, dict):
                title = item.get("title") or item.get("headline")
                if isinstance(title, str):
                    headlines.append(title)
            elif isinstance(item, str):
                headlines.append(item)
        return headlines

    def _extract_rss_titles(self, xml_text: str) -> list[str]:
        titles: list[str] = []
        start = 0
        while True:
            open_idx = xml_text.find("<title>", start)
            close_idx = xml_text.find("</title>", start)
            if open_idx == -1 or close_idx == -1 or close_idx <= open_idx:
                break
            title = xml_text[open_idx + 7:close_idx].strip()
            start = close_idx + 8
            if title and "rss" not in title.lower():
                titles.append(title)
        return titles

    def _keyword_sentiment(self, headlines: list[str]) -> float:
        if not headlines:
            return 0.0
        bullish = 0
        bearish = 0
        for headline in headlines:
            words = {w.strip(".,:;!?()[]{}\"'").lower() for w in headline.split()}
            bullish += sum(1 for word in words if word in _BULLISH_WORDS)
            bearish += sum(1 for word in words if word in _BEARISH_WORDS)
        score = (bullish - bearish) / max(len(headlines), 1)
        return max(-1.0, min(1.0, float(score)))

    def _parse_event_datetime(self, value: Any) -> datetime | None:
        if not value:
            return None
        text = str(value).strip()
        for parser in (
            lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
            lambda s: datetime.strptime(s, "%Y-%m-%d"),
            lambda s: datetime.strptime(s, "%d-%m-%Y"),
            lambda s: datetime.strptime(s, "%d %b %Y"),
        ):
            try:
                dt = parser(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue
        return None

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            if not path.exists():
                return {}
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        atomic_write_json(path, payload, ensure_ascii=True)

    def _is_fresh(self, iso_ts: str | None, max_age_seconds: int) -> bool:
        if not iso_ts:
            return False
        try:
            ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        except Exception:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (self._now() - ts).total_seconds() <= max_age_seconds

    def _normalize_symbol(self, symbol: str) -> str:
        s = symbol.strip().upper()
        if "/" in s:
            s = s.replace("/", "")
        return s

    def _coin_from_symbol(self, symbol: str) -> str:
        if symbol.endswith("USDT"):
            return symbol[:-4]
        return symbol


def build_context_fetcher() -> ContextFetcher:
    """Factory helper for scheduled runners and endpoints."""
    return ContextFetcher()
