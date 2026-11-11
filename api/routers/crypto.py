"""
TradeAI PRO — Crypto Domain Router.
===================================
Routes:
  - GET|HEAD /crypto, /crypto/
  - GET /api/crypto/meme
  - GET /api/crypto/news
  - GET /api/crypto/intelligence
  - GET /api/crypto/proxy/binance/24hr
  - GET /api/crypto/proxy/binance/klines
  - GET /api/crypto/prediction
"""

from __future__ import annotations

import os
import sys
import time
import json
import math
import logging
import statistics
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any
from collections import deque
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import httpx
from fastapi import APIRouter, Query, HTTPException, Request as FastAPIRequest
from fastapi.responses import JSONResponse, FileResponse

from config import settings
import api.service as svc
from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dashboard")

_DASH_HTML_HEADERS = {
    "Cache-Control": "no-store, must-revalidate",
    "Pragma": "no-cache",
}

_CRYPTO_PREDICTION_HISTORY: deque[dict] = deque(maxlen=60)
_NEWS_SOURCE_BACKOFF_UNTIL: dict[str, float] = {}
_NEWS_SOURCE_FAIL_COUNT: dict[str, int] = {}

router = APIRouter(tags=["crypto"])

@router.api_route("/crypto", methods=["GET", "HEAD"])

@router.api_route("/crypto/", methods=["GET", "HEAD"])

async def serve_crypto():

    """Serve the live crypto dashboard page."""

    crypto = os.path.join(STATIC_DIR, "crypto.html")

    if os.path.exists(crypto):

        return FileResponse(crypto, headers=_DASH_HTML_HEADERS)

    return FileResponse(

        os.path.join(STATIC_DIR, "index.html"), headers=_DASH_HTML_HEADERS

    )



_meme_cache: dict = {"payload": None, "timestamp": 0.0}

_MEME_CACHE_TTL_SEC: float = 60.0

_meme_refresh_lock = asyncio.Lock()

def _get_fallback_meme_payload() -> dict:

    return {

        "meme_coins": [],

        "total": 0,

        "static_universe": len(getattr(settings, "MEME_COIN_SYMBOLS", [])),

        "discovered_universe": 0,

        "dynamic_discovery_enabled": getattr(settings, "MEME_DYNAMIC_DISCOVERY_ENABLED", False),

        "buy_count": 0,

        "sell_count": 0,

        "hold_count": 0,

        "skip_count": 0,

        "hot_count": 0,

        "updated_at": datetime.now().isoformat(),

        "status": "warming_up",

    }

async def _refresh_meme_signals_bg():

    if _meme_refresh_lock.locked():

        return

    async with _meme_refresh_lock:

        try:

            await _compute_meme_signals_raw()

        except Exception as exc:

            logger.warning("Background meme signals refresh failed: %s", exc)

@router.get("/api/crypto/meme")

async def get_meme_coin_signals():
    from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed
    if not is_crypto_research_allowed():
        return JSONResponse(
            status_code=403,
            content={"error": "Crypto & Forex trading is disabled under NSE Focus Mode (Section 6A). Unlock via Market Control with operator password."}
        )

    """

    Dedicated endpoint for the Meme Coin Dashboard section.

    Non-blocking stale-while-revalidate pattern (sub-5ms on cache hit).

    """

    now_ts = time.time()

    cached = _meme_cache.get("payload")

    if cached is not None:

        if (now_ts - _meme_cache.get("timestamp", 0.0)) >= _MEME_CACHE_TTL_SEC:

            if not _meme_refresh_lock.locked():

                asyncio.create_task(_refresh_meme_signals_bg())

        return cached

    # Non-blocking cold start: if cache is empty, trigger background refresh and return fallback immediately
    if not _meme_refresh_lock.locked():
        asyncio.create_task(_refresh_meme_signals_bg())

    return _get_fallback_meme_payload()

async def _compute_meme_signals_raw():

    now_ts = time.time()

    if _meme_cache["payload"] is not None and (now_ts - _meme_cache["timestamp"]) < _MEME_CACHE_TTL_SEC:

        return _meme_cache["payload"]

    """

    Dedicated endpoint for the Meme Coin Dashboard section.

    Fetches live Binance klines for meme coins and computes meme-tuned

    BUY/SELL/HOLD signals with confidence calibration.

    Legacy note: ``_meme_signal`` and the ML predictor now emit the shared
    LONG/SHORT vocabulary; these helpers still accept BUY/SELL so old callers
    and cached payloads keep working.

    """

    import math

    import statistics

    COIN_EMOJIS = {

        "DOGE": "🐶",

        "SHIB": "🐱",

        "BONK": "🐶",

        "WIF": "🐶",

        "PEPE": "🐸",

        "FLOKI": "🐕",

        "PNUT": "🐿️",

        "BOME": "📚",

        "MEW": "🐱",

        "POPCAT": "😸",

        "BRETT": "🧢",

        "TURBO": "🤖",

        "NEIRO": "🐶",

        "GOAT": "🐐",

        "CHILLGUY": "😎",

        "DEGEN": "🎰",

        "TRUMP": "🎩",

        "FARTCOIN": "💨",

        "MOODENG": "🦛",

        "SPX": "📈",

        "GIGA": "💪",

        "PONKE": "🐒",

        # 2025 additions

        "MICHI": "🐱",

        "MYRO": "🐶",

        "SLERF": "🦥",

        "SILLY": "🐉",

        "MUMU": "🐂",

        "DOGS": "🐕",

        "HMSTR": "🐹",

        "BABYDOGE": "🐶",

        "LADYS": "👸",

        "WOJAK": "😢",

    }

    def _calc_rsi(closes: list[float], period: int = 14) -> float | None:

        if len(closes) < period + 1:

            return None

        gains, losses = [], []

        for i in range(1, len(closes)):

            d = closes[i] - closes[i - 1]

            gains.append(max(d, 0))

            losses.append(max(-d, 0))

        avg_gain = statistics.mean(gains[-period:])

        avg_loss = statistics.mean(losses[-period:])

        if avg_loss == 0:

            return 100.0

        rs = avg_gain / avg_loss

        return round(100 - (100 / (1 + rs)), 2)

    def _calc_ema(closes: list[float], period: int = 20) -> float | None:

        if len(closes) < period:

            return None

        k = 2 / (period + 1)

        ema = statistics.mean(closes[:period])

        for price in closes[period:]:

            ema = price * k + ema * (1 - k)

        return round(ema, 8)

    def _vol_spike(volumes: list[float]) -> float:

        if len(volumes) < 5:

            return 1.0

        recent = volumes[-1]

        avg = (

            statistics.mean(volumes[-22:-1])

            if len(volumes) >= 22

            else statistics.mean(volumes[:-1])

        )

        return round(recent / avg, 2) if avg > 0 else 1.0

    def _meme_signal(

        closes: list[float],

        rsi: float | None,

        ema20: float | None,

        change_24h: float = 0.0,

        vol_spike: float = 1.0,

        news_sentiment: str = "neutral",

    ) -> tuple[str, float]:

        """

        Multi-factor meme signal — combines RSI + EMA + momentum + volume + news sentiment.

        LONG conditions (any one enough):

          A. RSI < 40 (oversold dip)

          B. RSI 40-55 + positive 24h momentum (>+2%) + price above EMA20

          C. Volume spike >2x + RSI < 60 + positive momentum

        SHORT conditions (any one enough):

          A. RSI > 65 (overbought)

          B. RSI 55-65 + negative 24h momentum (<-3%) + price below EMA20

          C. Price >20% above EMA20 (extended)

        HOLD: everything else

        News sentiment adjustment:

          - LONG + bullish news = confidence boost +10%

          - LONG + bearish news = downgrade to HOLD (20% penalty)

          - SHORT + bearish news = confidence boost +10%

          - SHORT + bullish news = downgrade to HOLD (20% penalty)

        """

        if rsi is None or not closes:

            return "HOLD", 0.38

        price = closes[-1]

        price_vs_ema = ((price - ema20) / ema20 * 100) if ema20 else 0.0

        above_ema = price_vs_ema > 0

        # ── LONG Logic ───────────────────────────────────────────

        # A: Oversold RSI

        if rsi < 30:

            conf = 0.58 + (30 - rsi) / 30 * 0.28  # 0.58–0.86

            if price_vs_ema < -20:  # extreme dump — reduce

                conf *= 0.80

            base_signal = "LONG"

            base_conf = round(min(conf, 0.90), 2)

        # B: Momentum long — positive trend in healthy RSI range

        elif 45 <= rsi <= 60 and change_24h >= 5.0 and above_ema:

            conf = 0.52 + min(change_24h, 30) / 100  # 0.52–0.82

            if vol_spike >= 1.5:

                conf = min(conf + 0.08, 0.85)  # volume boost

            base_signal = "LONG"

            base_conf = round(conf, 2)

        # C: Volume pump signal

        elif vol_spike >= 3.0 and rsi < 70 and change_24h > 2.0:

            conf = 0.50 + (vol_spike - 3.0) / 10  # 0.50–0.65+

            base_signal = "LONG"

            base_conf = round(min(conf, 0.75), 2)

        # ── SHORT Logic ───────────────────────────────────────────

        # A: Classic overbought

        elif rsi > 75:

            conf = 0.55 + (rsi - 75) / 25 * 0.30  # 0.55–0.85

            if price_vs_ema > 30:

                conf = min(conf + 0.07, 0.95)

            base_signal = "SHORT"

            base_conf = round(conf, 2)

        # B: Momentum reversal

        elif 60 <= rsi <= 75 and change_24h <= -5.0 and not above_ema:

            conf = 0.52 + min(abs(change_24h), 20) / 100

            base_signal = "SHORT"

            base_conf = round(conf, 2)

        # C: Extended far above EMA — take profit zone

        elif price_vs_ema > 30 and rsi > 65:

            base_signal = "SHORT"

            base_conf = 0.60

        # ── HOLD ─────────────────────────────────────────────────

        else:

            mid_dist = abs(rsi - 52)

            base_conf = max(0.35, 0.50 - mid_dist / 120)

            base_signal = "HOLD"

            base_conf = round(base_conf, 2)

        # ── News Sentiment Adjustment ─────────────────────────────

        if news_sentiment == "bullish":

            if base_signal == "LONG":

                final_conf = min(base_conf + 0.10, 0.95)

                return "LONG", round(final_conf, 2)

            elif base_signal == "SHORT":

                return "HOLD", round(base_conf * 0.8, 2)

        elif news_sentiment == "bearish":

            if base_signal == "SHORT":

                final_conf = min(base_conf + 0.10, 0.95)

                return "SHORT", round(final_conf, 2)

            elif base_signal == "LONG":

                return "HOLD", round(base_conf * 0.8, 2)

        return base_signal, base_conf

    async def _fetch_binance_klines(sym: str, limit: int = 60) -> dict | None:

        """Fetch 1h klines from Binance public API (no auth needed)."""

        url = (

            f"https://api.binance.com/api/v3/klines"

            f"?symbol={sym}USDT&interval=1h&limit={limit}"

        )

        try:

            resp = await asyncio.to_thread(

                lambda: __import__("requests").get(url, timeout=2.5)

            )

            if resp.status_code != 200:

                return None

            raw = resp.json()

            closes = [float(c[4]) for c in raw]

            volumes = [float(c[5]) for c in raw]

            price = closes[-1] if closes else 0.0

            change = ((closes[-1] - closes[0]) / closes[0] * 100) if closes[0] else 0.0

            return {

                "closes": closes,

                "volumes": volumes,

                "price": price,

                "change_24h": round(change, 2),

            }

        except Exception:

            return None

    def _backtest_meme_signal(closes: list[float], volumes: list[float]) -> float:

        """Mini-backtest over the last 60 hours to compute strategy win-rate for this coin."""

        if len(closes) < 30:

            return 0.50

        wins = 0

        total = 0

        for i in range(25, len(closes) - 3):

            c_window = closes[:i]

            v_window = volumes[:i]

            rsi = _calc_rsi(c_window)

            ema20 = _calc_ema(c_window, 20)

            vol_spike = _vol_spike(v_window)

            change = (

                ((c_window[-1] - c_window[0]) / c_window[0] * 100)

                if c_window[0]

                else 0.0

            )

            sig, _ = _meme_signal(c_window, rsi, ema20, change, vol_spike, "neutral")

            if sig in ("LONG", "BUY"):

                # Look ahead 3 hours (very short term)

                future_max = max(closes[i : i + 4])

                entry = c_window[-1]

                if future_max > entry * 1.02:  # 2% profit target met

                    wins += 1

                total += 1

        if total == 0:

            return 0.50

        return round(wins / total, 2)

    async def _discover_binance_meme_bases(

        limit: int,

        min_quote_volume_usdt: float,

    ) -> list[str]:

        """Discover liquid meme candidates from Binance 24h ticker stats."""

        if limit <= 0:

            return []

        meme_base_allowlist = {

            "DOGE",

            "SHIB",

            "BONK",

            "WIF",

            "BOME",

            "MEW",

            "POPCAT",

            "PEPE",

            "FLOKI",

            "BRETT",

            "TURBO",

            "NEIRO",

            "GOAT",

            "CHILLGUY",

            "DEGEN",

            "PNUT",

            "TRUMP",

            "FARTCOIN",

            "MOODENG",

            "SPX",

            "GIGA",

            "PONKE",

            "MICHI",

            "MYRO",

            "SLERF",

            "SILLY",

            "MUMU",

            "DOGS",

            "HMSTR",

            "BABYDOGE",

            "LADYS",

            "WOJAK",

            "MOG",

            "PENGU",

            "AIDOGE",

            "MEME",

        }

        meme_keywords = (

            "dog",

            "shib",

            "pepe",

            "bonk",

            "wif",

            "floki",

            "meme",

            "frog",

            "cat",

            "wojak",

            "degen",

            "trump",

            "baby",

            "hamster",

            "pnut",

            "mood",

            "fart",

            "goat",

            "neiro",

            "ponke",

            "michi",

            "myro",

            "slerf",

            "silly",

            "ladys",

            "giga",

            "spx",

            "brett",

            "turbo",

            "bome",

            "popcat",

            "mumu",

            "dogs",

            "mew",

            "mog",

            "pengu",

        )

        excluded_bases = {

            "BTC",

            "ETH",

            "BNB",

            "XRP",

            "SOL",

            "ADA",

            "DOT",

            "LINK",

            "TRX",

            "LTC",

            "AVAX",

            "ATOM",

            "TON",

            "NEAR",

            "APT",

            "SUI",

            "ARB",

            "OP",

            "UNI",

            "AAVE",

            "MKR",

            "INJ",

            "ETC",

            "XLM",

            "HBAR",

            "FIL",

            "ICP",

        }

        url = "https://api.binance.com/api/v3/ticker/24hr"

        try:

            resp = await asyncio.to_thread(

                lambda: __import__("requests").get(url, timeout=3.0)

            )

            if resp.status_code != 200:

                return []

            rows = resp.json()

            candidates: list[tuple[str, float]] = []

            for row in rows:

                symbol = str(row.get("symbol", "")).upper()

                if not symbol.endswith("USDT"):

                    continue

                base = symbol[:-4]

                if not base.isalpha() or base in excluded_bases:

                    continue

                quote_volume = float(row.get("quoteVolume") or 0.0)

                if quote_volume < min_quote_volume_usdt:

                    continue

                base_l = base.lower()

                if base in meme_base_allowlist or any(

                    k in base_l for k in meme_keywords

                ):

                    candidates.append((base, quote_volume))

            candidates.sort(key=lambda item: item[1], reverse=True)

            discovered: list[str] = []

            seen: set[str] = set()

            for base, _ in candidates:

                if base in seen:

                    continue

                seen.add(base)

                discovered.append(base)

                if len(discovered) >= limit:

                    break

            return discovered

        except Exception as e:

            logger.warning("Dynamic meme discovery failed: %s", e)

            return []

    def _calibrate_signal_confidence(

        signal: str,

        confidence: float,

        backtest_win_rate: float,

    ) -> tuple[str, float]:

        """Blend model confidence with recent local hit-rate and penalize weak regimes."""

        calibrated = (confidence * 0.70) + (backtest_win_rate * 0.30)

        # Longs are unreliable when this coin's short-term hit-rate is weak.

        if signal in ("LONG", "BUY") and backtest_win_rate < 0.35:

            signal = "HOLD"

            calibrated *= 0.82

        # Shorts are less reliable when recent tape strongly favors rebounds.

        if signal in ("SHORT", "SELL") and backtest_win_rate > 0.65:

            signal = "HOLD"

            calibrated *= 0.85

        calibrated = max(0.0, min(0.95, calibrated))

        return signal, round(calibrated, 2)

    # ── Fetch News & All Meme Coins Concurrently ──────────────────────

    news_items = []

    try:

        news_payload = await get_crypto_news(limit=25)

        news_items = news_payload.get("items", [])

    except Exception as e:

        logger.warning(f"Meme news fetch failed: {e}")

    static_bases = [sym.split("/")[0] for sym in settings.MEME_COIN_SYMBOLS]

    discovered_bases: list[str] = []

    if settings.MEME_DYNAMIC_DISCOVERY_ENABLED:

        discover_limit = max(0, int(settings.MEME_DYNAMIC_DISCOVERY_LIMIT))

        min_quote_volume = max(0.0, float(settings.MEME_DYNAMIC_MIN_QUOTE_VOLUME_USDT))

        discovered_bases = await _discover_binance_meme_bases(

            discover_limit, min_quote_volume

        )

    max_tracked = max(1, int(settings.MEME_MAX_TRACKED_COINS))

    meme_bases = list(dict.fromkeys(static_bases + discovered_bases))[:max_tracked]

    discovered_set = set(discovered_bases)

    kline_tasks = {

        base: asyncio.create_task(_fetch_binance_klines(base)) for base in meme_bases

    }

    try:

        from agents.data_ingestion.dexscreener_client import analyze_meme_coin_risk

        dex_tasks = {

            base: asyncio.create_task(analyze_meme_coin_risk(base))

            for base in meme_bases

        }

    except ImportError:

        logger.warning(

            "DexScreener client not found. Running without on-chain rug filters."

        )

        dex_tasks = {}

    await asyncio.gather(*kline_tasks.values(), return_exceptions=True)

    if dex_tasks:

        await asyncio.gather(*dex_tasks.values(), return_exceptions=True)

    results = []

    for base in meme_bases:

        data = None

        try:

            data = kline_tasks[base].result()

        except Exception:

            pass

        dex_risk = {}

        try:

            if base in dex_tasks:

                dex_risk = dex_tasks[base].result()

        except Exception:

            pass

        # Fallback to service market_data cache if Binance fetch failed

        if data is None:

            sym_key = f"{base}/USDT"

            df = svc._market_data.get(sym_key)

            if df is None:

                df = svc._market_data.get(f"{base}USDT")

            if df is not None and not df.empty and "close" in df.columns:

                closes = df["close"].dropna().tolist()

                volumes = (

                    df["volume"].dropna().tolist() if "volume" in df.columns else []

                )

                price = closes[-1] if closes else 0.0

                pct = (

                    ((closes[-1] - closes[0]) / closes[0] * 100)

                    if closes and closes[0]

                    else 0.0

                )

                data = {

                    "closes": closes,

                    "volumes": volumes,

                    "price": price,

                    "change_24h": round(pct, 2),

                }

        if not data or not data.get("closes"):

            continue  # skip coin — no data at all

        closes = data["closes"]

        volumes = data["volumes"]

        price = data["price"]

        sym_key = f"{base}/USDT"

        # ── Accurate News Sentiment ─────────────────────────

        base_lower = base.lower()

        mentioned_news = [

            item

            for item in news_items

            if base_lower in str(item.get("title", "")).lower()

        ]

        if mentioned_news:

            pos_words = {"surge", "rally", "breakout", "bull", "buy", "up"}

            neg_words = {"crash", "dump", "sell", "bear", "hack", "drop"}

            title = str(mentioned_news[0].get("title", "")).lower()

            pw = sum(1 for w in pos_words if w in title)

            nw = sum(1 for w in neg_words if w in title)

            if pw > nw:

                news_sentiment = "bullish"

            elif nw > pw:

                news_sentiment = "bearish"

            else:

                news_sentiment = "neutral"

        else:

            # Fallback to global context score if not directly in news

            cached_sig = svc._signal_cache.get(sym_key)

            if cached_sig is None:

                cached_sig = svc._signal_cache.get(f"{base}USDT")

            ctx_score = 0.0

            if cached_sig is not None and not cached_sig.empty:

                raw_ctx = cached_sig.iloc[-1].get("context_score", 0.0)

                ctx_score = (

                    float(raw_ctx)

                    if raw_ctx and not math.isnan(float(raw_ctx))

                    else 0.0

                )

            news_sentiment = (

                "bullish"

                if ctx_score > 0.15

                else "bearish"

                if ctx_score < -0.15

                else "neutral"

            )

        rsi = _calc_rsi(closes)

        ema20 = _calc_ema(closes, 20)

        spike = _vol_spike(volumes)

        change_24h = data.get("change_24h", 0.0)

        backtest_win_rate = _backtest_meme_signal(closes, volumes)

        # ── Enhanced ML-based Prediction ─────────────────────────

        try:

            from agents.ai_engine.meme_predictor import get_enhanced_prediction

            signal, confidence, pred_metadata = get_enhanced_prediction(

                closes=closes,

                volumes=volumes,

                rsi=rsi,

                ema20=ema20,

                change_24h=change_24h,

                vol_spike=spike,

                news_sentiment=news_sentiment,

                dex_risk=dex_risk,

                symbol=base,

            )

            signal, confidence = _calibrate_signal_confidence(

                signal,

                confidence,

                backtest_win_rate,

            )

            # Extract reasoning

            top_factors = pred_metadata.get("top_factors", [])

            pred_features = pred_metadata.get("features", {})

        except ImportError:

            logger.warning("Enhanced predictor not available, using legacy signal")

            # Fallback to legacy method

            signal, base_confidence = _meme_signal(

                closes, rsi, ema20, change_24h, spike, news_sentiment

            )

            signal, confidence = _calibrate_signal_confidence(

                signal,

                base_confidence,

                backtest_win_rate,

            )

            top_factors = []

            pred_features = {}

        # On-Chain DexScreener Overrides (Rug Pull / Wash Trade Protection)

        risk_level = "HIGH"

        risk_note = "Meme coin — extreme volatility. Use small position sizes only."

        if dex_risk:

            if not dex_risk.get("passed_filters", True):

                if signal in ("LONG", "BUY"):

                    signal = "SKIP"

                    confidence = 0.0

                risk_level = "EXTREME DANGER"

                risk_note = " | ".join(

                    dex_risk.get(

                        "warnings", ["High risk of manipulation or wash trading."]

                    )

                )

            elif dex_risk.get("risk_score", 100) < 80:

                risk_level = "VERY HIGH"

                if dex_risk.get("warnings"):

                    risk_note = dex_risk["warnings"][0]

        results.append(

            {

                "symbol": sym_key,

                "base": base,

                "emoji": COIN_EMOJIS.get(base, "🪙"),

                "signal": signal,

                "confidence": confidence,

                "backtest_win_rate": backtest_win_rate,

                "price": price,

                "change_24h": data.get("change_24h", 0.0),

                "rsi": rsi,

                "ema20": ema20,

                "trend": "BULLISH" if (ema20 and price > ema20) else "BEARISH",

                "volume_spike": spike,

                "is_hot": spike >= 2.0,

                "news_sentiment": news_sentiment,

                "risk_level": risk_level,

                "risk_note": risk_note,

                "on_chain_score": dex_risk.get("risk_score", "N/A"),

                "discovered": base in discovered_set,

                # Enhanced prediction metadata

                "prediction_factors": top_factors[:3] if top_factors else [],

                "volatility": pred_features.get("volatility", 0.0),

                "trend_strength": pred_features.get("trend_strength", 0.0),

            }

        )

    # Sort: LONG first → highest confidence → alphabetical

    signal_order = {"LONG": 0, "BUY": 0, "HOLD": 1, "SHORT": 2, "SELL": 2, "SKIP": 3}

    results.sort(key=lambda x: (signal_order.get(x["signal"], 1), -x["confidence"]))

    payload = {

        "meme_coins": results,

        "total": len(results),

        "static_universe": len(static_bases),

        "discovered_universe": len(discovered_bases),

        "dynamic_discovery_enabled": settings.MEME_DYNAMIC_DISCOVERY_ENABLED,

        # Keys kept as buy_count/sell_count so existing clients do not break;
        # both spellings of the vocabulary are counted.
        "buy_count": sum(1 for r in results if r["signal"] in ("LONG", "BUY")),

        "sell_count": sum(1 for r in results if r["signal"] in ("SHORT", "SELL")),

        "hold_count": sum(1 for r in results if r["signal"] == "HOLD"),

        "skip_count": sum(1 for r in results if r["signal"] == "SKIP"),

        "hot_count": sum(1 for r in results if r["is_hot"]),

        "updated_at": datetime.now().isoformat(),

    }

    _meme_cache["payload"] = payload

    _meme_cache["timestamp"] = time.time()

    return payload


@router.get("/api/crypto/news")

async def get_crypto_news(limit: int = Query(12, ge=3, le=40)):
    from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed
    if not is_crypto_research_allowed():
        return JSONResponse(
            status_code=403,
            content={"error": "Crypto & Forex trading is disabled under NSE Focus Mode (Section 6A). Unlock via Market Control with operator password."}
        )

    """Fetch latest crypto headlines from multiple free/keyless RSS sources."""

    feeds = [

        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),

        ("Cointelegraph", "https://cointelegraph.com/rss"),

        ("Decrypt", "https://decrypt.co/feed"),

        ("CryptoSlate", "https://cryptoslate.com/feed/"),

        ("The Block", "https://www.theblock.co/rss.xml"),

        ("Blockworks", "https://blockworks.co/feed"),

    ]

    def _norm_text(node: ET.Element | None) -> str:

        if node is None:

            return ""

        txt = (node.text or "").strip()

        return " ".join(txt.split())

    def _fetch_feed(source: str, url: str) -> list[dict]:

        last_exc: Exception | None = None

        # Retry with exponential backoff to reduce transient source failures.

        for attempt in range(3):

            try:

                req = Request(

                    url,

                    headers={

                        "User-Agent": (

                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "

                            "AppleWebKit/537.36 (KHTML, like Gecko) "

                            "Chrome/124.0.0.0 Safari/537.36"

                        )

                    },

                )

                with urlopen(req, timeout=10) as resp:

                    payload = resp.read()

                root = ET.fromstring(payload)

                # Support RSS (<item>) and Atom (<entry>) feeds.

                items = root.findall(".//item")

                if not items:

                    items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

                out: list[dict] = []

                for it in items[:20]:

                    title = _norm_text(it.find("title")) or _norm_text(

                        it.find("{http://www.w3.org/2005/Atom}title")

                    )

                    link = _norm_text(it.find("link"))

                    if not link:

                        atom_link = it.find("{http://www.w3.org/2005/Atom}link")

                        if atom_link is not None:

                            link = str(atom_link.attrib.get("href") or "").strip()

                    pub = (

                        _norm_text(it.find("pubDate"))

                        or _norm_text(it.find("published"))

                        or _norm_text(it.find("{http://www.w3.org/2005/Atom}updated"))

                    )

                    if not title or not link:

                        continue

                    out.append(

                        {

                            "title": title,

                            "url": link,

                            "source": source,

                            "published": pub,

                        }

                    )

                return out

            except Exception as exc:

                last_exc = exc

                if attempt < 2:

                    time.sleep(0.35 * (2**attempt))

        if last_exc is not None:

            raise last_exc

        return []

    all_news: list[dict] = []

    sources_ok: list[str] = []

    failed_sources: list[str] = []

    now_ts = time.time()

    for source, url in feeds:

        backoff_until = _NEWS_SOURCE_BACKOFF_UNTIL.get(source, 0.0)

        if now_ts < backoff_until:

            failed_sources.append(source)

            continue

        try:

            all_news.extend(await asyncio.to_thread(_fetch_feed, source, url))

            sources_ok.append(source)

            _NEWS_SOURCE_FAIL_COUNT.pop(source, None)

            _NEWS_SOURCE_BACKOFF_UNTIL.pop(source, None)

        except Exception as exc:

            fails = _NEWS_SOURCE_FAIL_COUNT.get(source, 0) + 1

            _NEWS_SOURCE_FAIL_COUNT[source] = fails

            backoff_seconds = min(1800, 30 * (2 ** min(fails, 6)))

            _NEWS_SOURCE_BACKOFF_UNTIL[source] = now_ts + backoff_seconds

            last_warn = _NEWS_SOURCE_LAST_WARN_TS.get(source, 0.0)

            # Avoid warning spam for persistently blocked feeds.

            if now_ts - last_warn >= 300:

                logger.warning(

                    "Crypto news feed failed (%s): %s (backoff %ss)",

                    source,

                    exc,

                    int(backoff_seconds),

                )

                _NEWS_SOURCE_LAST_WARN_TS[source] = now_ts

            failed_sources.append(source)

    # Deduplicate by URL while preserving source diversity.

    seen_urls: set[str] = set()

    unique_news: list[dict] = []

    for row in all_news:

        u = row.get("url") or ""

        if not u or u in seen_urls:

            continue

        seen_urls.add(u)

        unique_news.append(row)

    return {

        "items": unique_news[:limit],

        "sources_ok": sources_ok,

        "failed_sources": failed_sources,

        "updated_at": datetime.now(timezone.utc).isoformat(),

    }

@router.get("/api/crypto/intelligence")

async def get_crypto_intelligence():
    """
    Phase 6 - Aggregated crypto cockpit intelligence payload.
    Mirrors /api/overview/intelligence contract for frontend reuse.
    Includes: top_setups, avoid_list, market_guardrails, futures_context, theme_leadership.
    """
    from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed
    if not is_crypto_research_allowed():
        return JSONResponse(
            status_code=403,
            content={"error": "Crypto & Forex trading is disabled under NSE Focus Mode (Section 6A). Unlock via Market Control with operator password."}
        )

    try:

        payload = await asyncio.to_thread(svc._compute_crypto_intelligence)

        return payload

    except Exception as exc:

        logger.error("Crypto intelligence failed: %s", exc)

        return {

            "top_setups": [],

            "avoid_list": [],

            "market_guardrails": {

                "mood": "UNKNOWN",

                "mood_score": 50,

                "why_market_hint": "Intelligence fetch failed.",

            },

            "futures_context": {

                "per_symbol": [],

                "updated_at": None,

                "source": "unavailable",

            },

            "theme_leadership": [],

            "ranking_meta": {

                "total_tracked": 0,

                "actionable": 0,

                "blocked_high_quality": 0,

            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }

_binance_proxy_cache = {
    "timestamp": 0.0,
    "payload": None,
}
_binance_symbol_cache = {}
_binance_klines_cache = {}

@router.get("/api/crypto/proxy/binance/24hr")
async def proxy_binance_24hr(symbol: str | None = Query(default=None)):
    """Proxy Binance 24hr ticker with 15s caching to resolve CSP and provider fetch issues (F08).
    Supports optional ?symbol= parameter with automatic Binance Futures fallback for meme tokens (e.g. CHILLGUY).
    """
    now = time.time()
    import httpx

    # Case 1: Specific symbol query (e.g. ?symbol=CHILLGUYUSDT)
    if symbol:
        sym = symbol.strip().upper()
        cached_sym = _binance_symbol_cache.get(sym)
        if cached_sym and (now - cached_sym["timestamp"] < 15.0):
            return JSONResponse(
                content=cached_sym["payload"],
                headers={"Cache-Control": "public, max-age=15", "X-Cache": "HIT"},
            )

        endpoints = [
            f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={sym}",
            f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}",
            f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym}",
        ]
        for url in endpoints:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(url, headers={"User-Agent": "TradeAI-Pro/1.0"})
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, dict) and data.get("symbol"):
                            _binance_symbol_cache[sym] = {"timestamp": now, "payload": data}
                            return JSONResponse(
                                content=data,
                                headers={"Cache-Control": "public, max-age=15", "X-Cache": "MISS"},
                            )
            except Exception as exc:
                logger.warning("Binance single ticker proxy fetch failed for %s: %s", url, exc)

        if cached_sym:
            return JSONResponse(
                content=cached_sym["payload"],
                headers={"Cache-Control": "public, max-age=5", "X-Cache": "STALE"},
            )
        return JSONResponse(
            status_code=404,
            content={"error": f"Symbol {sym} unavailable across Binance Spot and Futures.", "status": "NOT_FOUND"},
        )

    # Case 2: Bulk 24hr tickers
    if _binance_proxy_cache["payload"] and (now - _binance_proxy_cache["timestamp"] < 15.0):
        return JSONResponse(
            content=_binance_proxy_cache["payload"],
            headers={"Cache-Control": "public, max-age=15", "X-Cache": "HIT"},
        )

    endpoints = [
        "https://data-api.binance.vision/api/v3/ticker/24hr",
        "https://api.binance.com/api/v3/ticker/24hr",
    ]
    for url in endpoints:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers={"User-Agent": "TradeAI-Pro/1.0"})
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        # Merge prominent meme/futures tickers that may not exist on spot
                        try:
                            f_resp = await client.get("https://fapi.binance.com/fapi/v1/ticker/24hr", headers={"User-Agent": "TradeAI-Pro/1.0"})
                            if f_resp.status_code == 200:
                                f_data = f_resp.json()
                                if isinstance(f_data, list):
                                    existing_syms = {t.get("symbol") for t in data if isinstance(t, dict)}
                                    for ft in f_data:
                                        if isinstance(ft, dict) and ft.get("symbol") and ft.get("symbol").endswith("USDT"):
                                            if ft["symbol"] not in existing_syms:
                                                data.append(ft)
                        except Exception as f_exc:
                            logger.debug("Optional futures ticker merge skipped: %s", f_exc)

                        _binance_proxy_cache["timestamp"] = now
                        _binance_proxy_cache["payload"] = data
                        return JSONResponse(
                            content=data,
                            headers={"Cache-Control": "public, max-age=15", "X-Cache": "MISS"},
                        )
        except Exception as exc:
            logger.warning("Binance proxy fetch failed for %s: %s", url, exc)

    if _binance_proxy_cache["payload"]:
        return JSONResponse(
            content=_binance_proxy_cache["payload"],
            headers={"Cache-Control": "public, max-age=5", "X-Cache": "STALE"},
        )

    return JSONResponse(
        status_code=502,
        content={"error": "Binance upstream ticker feed unavailable.", "status": "UPSTREAM_UNAVAILABLE"},
    )

@router.get("/api/crypto/proxy/binance/klines")
async def proxy_binance_klines(symbol: str = Query(...), interval: str = Query("1h"), limit: int = Query(80)):
    """Proxy Binance klines with 20s caching to resolve CSP and provider fetch issues (F08).
    Supports Spot with automatic fallback to Binance Futures for meme tokens (e.g. CHILLGUY).
    """
    now = time.time()
    sym = symbol.strip().upper()
    cache_key = f"{sym}_{interval}_{limit}"
    cached = _binance_klines_cache.get(cache_key)
    if cached and (now - cached["timestamp"] < 20.0):
        return JSONResponse(
            content=cached["payload"],
            headers={"Cache-Control": "public, max-age=20", "X-Cache": "HIT"},
        )

    endpoints = [
        f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}",
        f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}",
        f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={interval}&limit={limit}",
    ]
    import httpx
    for u in endpoints:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(u, headers={"User-Agent": "TradeAI-Pro/1.0"})
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        _binance_klines_cache[cache_key] = {"timestamp": now, "payload": data}
                        return JSONResponse(
                            content=data,
                            headers={"Cache-Control": "public, max-age=20", "X-Cache": "MISS"},
                        )
        except Exception as exc:
            logger.warning("Binance klines proxy fetch failed for %s: %s", u, exc)

    if cached:
        return JSONResponse(content=cached["payload"], headers={"Cache-Control": "public, max-age=5", "X-Cache": "STALE"})

    return JSONResponse(
        status_code=502,
        content={"error": "Binance upstream klines feed unavailable.", "status": "UPSTREAM_UNAVAILABLE"},
    )

@router.get("/api/crypto/prediction")

async def get_crypto_prediction(timeframe: str = Query(default="1h")):
    from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed
    if not is_crypto_forex_enabled():
        return JSONResponse(
            status_code=403,
            content={"error": "Crypto & Forex trading is disabled under NSE Focus Mode (Section 6A). Unlock via Market Control with operator password."}
        )

    """Calculated crypto prediction from live market, news sentiment, and macro context."""

    if timeframe not in ("1h", "4h", "1d"):

        timeframe = "1h"

    def _fetch_json(url: str) -> object:

        req = Request(

            url,

            headers={

                "User-Agent": (

                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "

                    "AppleWebKit/537.36 (KHTML, like Gecko) "

                    "Chrome/124.0.0.0 Safari/537.36"

                )

            },

        )

        with urlopen(req, timeout=10) as resp:

            return json.loads(resp.read())

    def _calc_rsi(closes: list[float], period: int = 14) -> float | None:

        if len(closes) < period + 1:

            return None

        gains = 0.0

        losses = 0.0

        for i in range(1, period + 1):

            d = closes[i] - closes[i - 1]

            if d > 0:

                gains += d

            else:

                losses -= d

        avg_g = gains / period

        avg_l = losses / period

        for i in range(period + 1, len(closes)):

            d = closes[i] - closes[i - 1]

            if d > 0:

                avg_g = (avg_g * (period - 1) + d) / period

                avg_l = (avg_l * (period - 1)) / period

            else:

                avg_l = (avg_l * (period - 1) - d) / period

                avg_g = (avg_g * (period - 1)) / period

        if avg_l == 0:

            return 100.0 if avg_g > 0 else 50.0

        rs = avg_g / avg_l

        return 100.0 - (100.0 / (1.0 + rs))

    def _vol_spike(volumes: list[float]) -> float:

        if len(volumes) < 21:

            return 1.0

        recent = volumes[-1]

        base = float(np.mean(volumes[-21:-1]))

        return recent / base if base > 0 else 1.0

    def _ema(values: list[float], period: int) -> float | None:

        if len(values) < period:

            return None

        alpha = 2.0 / (period + 1)

        v = float(np.mean(values[:period]))

        for x in values[period:]:

            v = alpha * x + (1.0 - alpha) * v

        return float(v)

    def _macd_hist(closes: list[float]) -> float | None:

        if len(closes) < 35:

            return None

        alpha12 = 2.0 / 13.0

        alpha26 = 2.0 / 27.0

        alpha9 = 2.0 / 10.0

        ema12 = float(np.mean(closes[:12]))

        ema26 = float(np.mean(closes[:26]))

        macd_series: list[float] = []

        for c in closes:

            ema12 = alpha12 * c + (1.0 - alpha12) * ema12

            ema26 = alpha26 * c + (1.0 - alpha26) * ema26

            macd_series.append(ema12 - ema26)

        signal = float(np.mean(macd_series[:9]))

        for m in macd_series[9:]:

            signal = alpha9 * m + (1.0 - alpha9) * signal

        return float(macd_series[-1] - signal)

    def _bb_pos(closes: list[float], period: int = 20) -> float | None:

        if len(closes) < period:

            return None

        window = closes[-period:]

        mid = float(np.mean(window))

        std = float(np.std(window))

        if std == 0:

            return 0.5

        upper = mid + 2 * std

        lower = mid - 2 * std

        band = upper - lower

        if band <= 0:

            return 0.5

        return float((closes[-1] - lower) / band)

    def _sentiment_from_titles(items: list[dict]) -> tuple[float, int, int]:

        titles = [

            str(row.get("title") or "").strip() for row in items if row.get("title")

        ]

        if not titles:

            return 0.0, 0, 0

        try:

            # We initialize and run FinBERT locally

            import logging

            from transformers import pipeline

            # Use module level caching for the pipeline if possible

            global _FINBERT_PIPELINE

            if "_FINBERT_PIPELINE" not in globals():

                global _FINBERT_PIPELINE

                _FINBERT_PIPELINE = None

            if _FINBERT_PIPELINE is None:

                _FINBERT_PIPELINE = pipeline(

                    "text-classification", model="ProsusAI/finbert"

                )

            results = _FINBERT_PIPELINE(titles)

            score = 0.0

            hits_pos = 0

            hits_neg = 0

            for res in results:

                label = res["label"]

                s = res["score"]

                if label == "positive":

                    hits_pos += 1

                    score += s

                elif label == "negative":

                    hits_neg += 1

                    score -= s

            return score / max(len(results), 1), hits_pos, hits_neg

        except Exception as e:

            # Fallback to simple word counting if transformers fails

            pos_words = {

                "surge",

                "rally",

                "breakout",

                "adoption",

                "approval",

                "launch",

                "partnership",

                "bull",

                "bullish",

                "upgrade",

                "rebound",

                "inflow",

                "record",

                "gain",

                "recovery",

                "accumulate",

            }

            neg_words = {

                "crash",

                "dump",

                "selloff",

                "hack",

                "ban",

                "lawsuit",

                "liquidation",

                "bear",

                "bearish",

                "outflow",

                "downgrade",

                "risk",

                "warning",

                "fraud",

                "decline",

                "drop",

            }

            score = 0.0

            hits_pos = 0

            hits_neg = 0

            for row in items:

                title = str(row.get("title") or "").lower()

                if not title:

                    continue

                pw = sum(1 for w in pos_words if w in title)

                nw = sum(1 for w in neg_words if w in title)

                if pw > nw:

                    hits_pos += 1

                    score += 1.0

                elif nw > pw:

                    hits_neg += 1

                    score -= 1.0

            denom = max(len(items), 1)

            return score / denom, hits_pos, hits_neg

    def _hero_signal(

        sym: str, ticker: dict, closes: list[float], volumes: list[float]

    ) -> dict:

        pct = float(ticker.get("priceChangePercent") or 0.0)

        rsi = _calc_rsi(closes)

        vs = _vol_spike(volumes)

        macd_h = _macd_hist(closes)

        bb_pos = _bb_pos(closes)

        mom = 0.0

        if len(closes) >= 8 and closes[-8] != 0:

            mom = ((closes[-1] - closes[-8]) / closes[-8]) * 100.0

        score = 0.0

        reasons: list[str] = []

        pct_component = max(-1.2, min(1.2, pct / 4.0))

        score += pct_component

        reasons.append(f"24h momentum {pct:+.2f}%")

        if rsi is not None:

            rsi_component = max(-1.2, min(1.2, (50.0 - rsi) / 16.0))

            score += rsi_component

            if rsi < 33:

                reasons.append(f"RSI {rsi:.1f} oversold")

            elif rsi > 68:

                reasons.append(f"RSI {rsi:.1f} overbought")

        mom_component = max(-1.0, min(1.0, mom / 2.8))

        score += mom_component

        if abs(mom) >= 0.5:

            reasons.append(f"trend impulse {mom:+.2f}%")

        if macd_h is not None:

            macd_component = max(-0.8, min(0.8, macd_h * 250.0))

            score += macd_component

            reasons.append(

                "MACD hist positive" if macd_h >= 0 else "MACD hist negative"

            )

        if bb_pos is not None:

            # Lower band (0.0) is rebound-positive, upper (1.0) is mean-revert risk.

            bb_component = max(-0.6, min(0.6, (0.5 - bb_pos) * 1.8))

            score += bb_component

        if vs >= 1.8:

            vol_component = min(0.9, (vs - 1.0) * 0.35)

            if pct >= 0:

                score += vol_component

                reasons.append(f"volume confirmation x{vs:.1f}")

            else:

                score -= vol_component

                reasons.append(f"sell volume x{vs:.1f}")

        action = "HOLD"

        if score >= 1.2:

            action = "BUY"

        elif score <= -1.2:

            action = "SELL"

        conf = int(

            max(35, min(95, round(42 + abs(score) * 13 + min(abs(pct), 9.0) * 1.2)))

        )

        return {

            "symbol": sym,

            "action": action,

            "confidence": conf,

            "score": round(score, 3),

            "rsi": round(rsi, 2) if rsi is not None else None,

            "macd_hist": round(macd_h, 5) if macd_h is not None else None,

            "bb_pos": round(bb_pos, 3) if bb_pos is not None else None,

            "volume_spike": round(vs, 2),

            "momentum_8h_pct": round(mom, 2),

            "reasons": reasons[:4],

        }

    try:

        # 1) Live market snapshot from Binance

        raw_tickers = await asyncio.to_thread(

            _fetch_json, "https://api.binance.com/api/v3/ticker/24hr"

        )

        if not isinstance(raw_tickers, list):

            raise ValueError("Unexpected ticker payload")

        usdt = [

            t

            for t in raw_tickers

            if isinstance(t, dict)

            and str(t.get("symbol", "")).endswith("USDT")

            and float(t.get("quoteVolume") or 0.0) > 1_000_000

            and "DOWN" not in str(t.get("symbol"))

            and "UP" not in str(t.get("symbol"))

            and "BEAR" not in str(t.get("symbol"))

            and "BULL" not in str(t.get("symbol"))

        ]

        ticker_map = {str(t.get("symbol")): t for t in usdt}

        btc_pct = float(

            (ticker_map.get("BTCUSDT") or {}).get("priceChangePercent") or 0.0

        )

        eth_pct = float(

            (ticker_map.get("ETHUSDT") or {}).get("priceChangePercent") or 0.0

        )

        gainers_n = sum(

            1 for t in usdt if float(t.get("priceChangePercent") or 0.0) > 0

        )

        losers_n = sum(1 for t in usdt if float(t.get("priceChangePercent") or 0.0) < 0)

        breadth = (gainers_n - losers_n) / max(gainers_n + losers_n, 1)

        # 2) Fear & Greed

        fg_value: int | None = None

        try:

            fg_raw = await asyncio.to_thread(

                _fetch_json, "https://api.alternative.me/fng/?limit=1"

            )

            if isinstance(fg_raw, dict):

                fg_s = (fg_raw.get("data") or [{}])[0].get("value")

                fg_value = int(fg_s) if fg_s is not None else None

        except Exception as exc:

            logger.warning("Fear&Greed fetch failed: %s", exc)

        # 3) Context mood from internal fetcher

        ctx_payload = await get_market_mood_api()

        ctx_mood = str(ctx_payload.get("crypto_mood") or "NEUTRAL").upper()

        ctx_event_risk = bool(ctx_payload.get("active_event_risk"))

        ctx_news_sent = float(ctx_payload.get("avg_news_sentiment") or 0.0)

        # 4) Multi-source news sentiment

        news_payload = await get_crypto_news(limit=24)

        news_items = list(news_payload.get("items") or [])

        news_score, pos_hits, neg_hits = _sentiment_from_titles(news_items)

        # 5) Hero signals from live klines

        hero_syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

        hero_signals: list[dict] = []

        for sym in hero_syms:

            ticker = ticker_map.get(sym)

            if not ticker:

                continue

            try:

                kline_url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={timeframe}&limit=80"

                kraw = await asyncio.to_thread(_fetch_json, kline_url)

                if not isinstance(kraw, list):

                    continue

                closes = [

                    float(r[4]) for r in kraw if isinstance(r, list) and len(r) > 5

                ]

                vols = [float(r[5]) for r in kraw if isinstance(r, list) and len(r) > 5]

                hero_signals.append(_hero_signal(sym, ticker, closes, vols))

            except Exception as exc:

                logger.warning("Hero signal calc failed for %s: %s", sym, exc)

        hero_signals.sort(key=lambda r: r.get("confidence", 0), reverse=True)

        # 6) Combined prediction score

        score = 0.0

        reasons: list[str] = []

        if btc_pct >= 2.0:

            score += 1.2

            reasons.append(f"BTC +{btc_pct:.2f}%")

        elif btc_pct <= -2.0:

            score -= 1.2

            reasons.append(f"BTC {btc_pct:.2f}%")

        if eth_pct >= 2.0:

            score += 0.8

            reasons.append(f"ETH +{eth_pct:.2f}%")

        elif eth_pct <= -2.0:

            score -= 0.8

            reasons.append(f"ETH {eth_pct:.2f}%")

        score += breadth * 2.0

        reasons.append(f"breadth {(breadth * 100):.0f}%")

        if fg_value is not None:

            if fg_value < 30:

                score += 0.7

                reasons.append(f"Fear&Greed {fg_value} (contrarian +)")

            elif fg_value > 70:

                score -= 0.7

                reasons.append(f"Fear&Greed {fg_value} (greed risk)")

        if ctx_mood == "RISK_ON":

            score += 0.8

            reasons.append("context mood risk-on")

        elif ctx_mood == "RISK_OFF":

            score -= 0.8

            reasons.append("context mood risk-off")

        if ctx_event_risk:

            score -= 0.5

            reasons.append("event risk active")

        if ctx_news_sent > 0.15:

            score += 0.5

            reasons.append("internal news sentiment +")

        elif ctx_news_sent < -0.10:

            score -= 0.5

            reasons.append("internal news sentiment -")

        if news_score > 0.10:

            score += 0.6

            reasons.append(f"external news positive ({pos_hits}/{len(news_items)})")

        elif news_score < -0.10:

            score -= 0.6

            reasons.append(f"external news negative ({neg_hits}/{len(news_items)})")

        direction = "NEUTRAL"

        if score >= 1.0:

            direction = "BULLISH_BIAS"

        elif score <= -1.0:

            direction = "BEARISH_BIAS"

        confidence = int(max(35, min(93, round(38 + abs(score) * 18))))

        horizon_map = {"1h": "6-24h", "4h": "1-3d", "1d": "3-10d"}

        horizon = horizon_map.get(timeframe, "6-24h")

        # Resolve previous pending outcome using latest BTC move sign as a proxy.

        if _CRYPTO_PREDICTION_HISTORY:

            prev = _CRYPTO_PREDICTION_HISTORY[-1]

            if prev.get("actual") is None:

                actual = (

                    "UP" if btc_pct > 0.15 else "DOWN" if btc_pct < -0.15 else "FLAT"

                )

                prev_dir = str(prev.get("direction") or "")

                hit: bool | None = None

                if prev_dir == "BULLISH_BIAS":

                    hit = actual == "UP"

                elif prev_dir == "BEARISH_BIAS":

                    hit = actual == "DOWN"

                prev["actual"] = actual

                prev["hit"] = hit

        _CRYPTO_PREDICTION_HISTORY.append(

            {

                "ts": datetime.now(timezone.utc).isoformat(),

                "direction": direction,

                "confidence": confidence,

                "score": round(score, 3),

                "btc_24h_pct": round(btc_pct, 3),

                "timeframe": timeframe,

                "actual": None,

                "hit": None,

            }

        )

        resolved = [h for h in _CRYPTO_PREDICTION_HISTORY if h.get("hit") is not None]

        hit_rate = (

            round(100.0 * sum(1 for h in resolved if h.get("hit")) / len(resolved), 1)

            if resolved

            else None

        )

        return {

            "prediction": {

                "direction": direction,

                "confidence": confidence,

                "score": round(score, 3),

                "horizon": horizon,

                "reasons": reasons[:6],

                "inputs": {

                    "btc_24h_pct": round(btc_pct, 3),

                    "eth_24h_pct": round(eth_pct, 3),

                    "breadth": round(breadth, 4),

                    "fear_greed": fg_value,

                    "context_mood": ctx_mood,

                    "context_news_sentiment": round(ctx_news_sent, 4),

                    "external_news_sentiment": round(news_score, 4),

                },

            },

            "signals": hero_signals[:5],

            "signal_timeframe": timeframe,

            "prediction_history": list(_CRYPTO_PREDICTION_HISTORY)[-12:],

            "accuracy_tracker": {

                "resolved": len(resolved),

                "hit_rate_pct": hit_rate,

            },

            "sources": {

                "market": ["Binance Spot /api/v3"],

                "sentiment": [

                    "Alternative.me Fear&Greed",

                    "Internal context mood",

                    "Crypto RSS headlines",

                ],

                "news_ok": news_payload.get("sources_ok") or [],

                "news_failed": news_payload.get("failed_sources") or [],

            },

            "calculated_at": datetime.now(timezone.utc).isoformat(),

            "real_data": True,

        }

    except Exception as exc:

        logger.error("Crypto prediction failed: %s", exc)

        return {

            "prediction": {

                "direction": "NEUTRAL",

                "confidence": 20,

                "score": 0.0,

                "horizon": "6-24h",

                "reasons": [

                    "Prediction unavailable due to upstream market/news fetch failure."

                ],

                "inputs": {},

            },

            "signals": [],

            "prediction_history": list(_CRYPTO_PREDICTION_HISTORY)[-12:],

            "accuracy_tracker": {"resolved": 0, "hit_rate_pct": None},

            "sources": {

                "market": ["Binance Spot /api/v3"],

                "sentiment": ["Alternative.me", "Internal context", "Crypto RSS"],

                "news_ok": [],

                "news_failed": [],

            },

            "calculated_at": datetime.now(timezone.utc).isoformat(),

            "real_data": False,

            "error": str(exc),

        }

