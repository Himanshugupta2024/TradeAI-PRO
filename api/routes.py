"""HTTP and WebSocket route handlers for the trading API."""



import asyncio

import json

import logging

import os

import time

from collections import deque

from dataclasses import asdict

from datetime import datetime, timedelta, timezone

from urllib.request import Request, urlopen

from xml.etree import ElementTree as ET



import numpy as np

import pandas as pd

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from fastapi.responses import FileResponse, JSONResponse, Response



from agents.analytics.trade_journal import get_journal

from agents.backtester.engine import Backtester

from agents.data_ingestion.context_fetcher import build_context_fetcher

from agents.feature_engine.indicators import calculate_indicators

from agents.feature_engine.regime_detector import detect_regime

from agents.feature_engine.trend import classify_trend, propagate_trend_to_ltf

from agents.market_intelligence.breakout_scanner import (

    scan_all_symbols as breakout_scan_all,

)

from agents.market_intelligence.index_mover import calculate_index_movers

from agents.market_intelligence.market_data import fetch_fii_dii_flow

from agents.market_intelligence.nse_fetcher import (

    fetch_arbitrary_symbol,

    fetch_nse_gainers_losers,

    fetch_ohlcv,

    fetch_sector_performance,

    search_symbols,

    validate_nse_symbol,

)

from agents.market_intelligence.swing_scanner import (

    compute_sector_momentum_map,

    scan_swing_setups,

)

from agents.market_intelligence.swing_scanner import (

    scan_all_symbols as swing_scan_all,

)

from agents.signal_engine.signal_generator import generate_signals

from agents.stock_screener import opportunity_stocks

from config import settings

from db import intraday_store, swing_store

from db.connection import SessionLocal

from db.models import MorningScan



try:

    from agents.analytics.signal_tracker import (

        get_leaderboard,

        get_win_rate,

        log_signal,

        score_outcomes,

    )

except Exception:



    def get_win_rate(symbol: str | None = None, last_n: int = 100):

        return {

            "symbol": symbol or "ALL",

            "total_signals": 0,

            "win_rate_pct": None,

            "avg_outcome_pct": None,

            "message": "Signal tracker module not available in this deployment",

        }



    def get_leaderboard(limit: int = 10):

        return []



    def score_outcomes(*args, **kwargs):

        return 0



    def log_signal(*args, **kwargs):

        return None





try:

    from agents.notifications.telegram_alerts import (

        is_configured as tg_is_configured,

    )

    from agents.notifications.telegram_alerts import (

        send_signal_alert,

        send_whale_alert,

    )

except Exception:



    def send_signal_alert(*args, **kwargs):

        return False



    def send_whale_alert(*args, **kwargs):

        return False



    def tg_is_configured():

        return False





from api import service as svc

from api.paths import STATIC_DIR



logger = logging.getLogger(__name__)



# ── Forex module imports (optional — graceful fallback if module not present) ──

try:

    from agents.data_ingestion.forex_fetcher import get_all_forex_rates, get_forex_ohlcv

    from agents.market_intelligence.forex_scanner import (

        is_forex_scanning,

        scan_all_forex,

        scan_forex_setup,

    )



    _FOREX_AVAILABLE = True

except Exception as _forex_import_err:

    logger.warning("Forex module not available: %s", _forex_import_err)

    _FOREX_AVAILABLE = False



    def get_all_forex_rates():

        return {}



    def get_forex_ohlcv(*args, **kwargs):

        import pandas as pd



        return pd.DataFrame()



    def is_forex_scanning():

        return False



    def scan_all_forex():

        return []



    def scan_forex_setup(*args, **kwargs):

        return None





router = APIRouter()



# Dashboard HTML is edited often during dev; avoid stale cached documents masking UI changes.

_DASH_HTML_HEADERS = {

    "Cache-Control": "no-store, must-revalidate",

    "Pragma": "no-cache",

}



_CRYPTO_PREDICTION_HISTORY: deque[dict] = deque(maxlen=60)

_NSE_PREDICTION_HISTORY: deque[dict] = deque(maxlen=60)

_NEWS_SOURCE_BACKOFF_UNTIL: dict[str, float] = {}

_NEWS_SOURCE_FAIL_COUNT: dict[str, int] = {}

_NEWS_SOURCE_LAST_WARN_TS: dict[str, float] = {}

_BTC_DOM_BACKOFF_UNTIL: float = 0.0

_BTC_DOM_LAST_WARN_TS: float = 0.0





@router.get("/favicon.ico", include_in_schema=False)

async def favicon():

    """Serve favicon at site root — browsers request /favicon.ico on the API host."""

    path = os.path.join(STATIC_DIR, "favicon.ico")

    if os.path.isfile(path):

        return FileResponse(path, media_type="image/x-icon")

    return Response(status_code=204)





@router.get("/api/health")

async def health_check():

    """Health check endpoint — shows loading progress and server status."""

    try:
        from agents.ai_engine.ensemble import get_model_status
        ai_engine_info = get_model_status()
    except Exception as _e:
        ai_engine_info = {"mode": "Statistical Heuristic (fallback)", "badge_text": "AI Engine: Statistical Heuristic (0/3 models fitted)", "fitted_count": 0}

    return {

        "status": "ok",

        "loading": svc._loading_status,

        "feed": svc._get_feed_status(),

        "symbols_loaded": len(svc._market_data),

        "signals_cached": len(svc._signal_cache),

        "ai_engine": ai_engine_info,

        "last_refresh": svc._last_refresh.isoformat()

        if svc._last_refresh != datetime.min

        else None,

        "uptime": datetime.now().isoformat(),

    }





@router.get("/api/signals/all")

async def get_all_signals():

    """Get the latest signal for every tracked symbol."""

    # Lazy bootstrap: if startup is still warming up and cache is empty,

    # trigger one background refresh so the dashboard does not stay blank.

    if not svc._signal_cache and not svc._lazy_bootstrap_started:

        lock = svc._get_refresh_lock()

        if not lock.locked():

            svc._lazy_bootstrap_started = True



            async def _bootstrap_once():

                try:

                    await svc._refresh_market_data()

                except Exception as e:

                    logger.warning("Lazy signal bootstrap failed: %s", e)

                finally:

                    svc._lazy_bootstrap_started = False



            asyncio.create_task(_bootstrap_once())



    results = []

    for symbol in svc._signal_cache:

        sig = svc._get_signal_for_symbol(symbol)

        if sig:

            results.append(sig)

    return {"signals": results, "updated_at": datetime.now().isoformat()}





@router.get("/api/crypto/meme")

async def get_meme_coin_signals():

    """

    Dedicated endpoint for the Meme Coin Dashboard section.

    Fetches live Binance klines for meme coins and computes meme-tuned

    BUY/SELL/HOLD signals with confidence calibration.

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



        BUY conditions (any one enough):

          A. RSI < 40 (oversold dip)

          B. RSI 40-55 + positive 24h momentum (>+2%) + price above EMA20

          C. Volume spike >2x + RSI < 60 + positive momentum



        SELL conditions (any one enough):

          A. RSI > 65 (overbought)

          B. RSI 55-65 + negative 24h momentum (<-3%) + price below EMA20

          C. Price >20% above EMA20 (extended)



        HOLD: everything else



        News sentiment adjustment:

          - BUY + bullish news = confidence boost +10%

          - BUY + bearish news = downgrade to HOLD (20% penalty)

          - SELL + bearish news = confidence boost +10%

          - SELL + bullish news = downgrade to HOLD (20% penalty)

        """

        if rsi is None or not closes:

            return "HOLD", 0.38



        price = closes[-1]

        price_vs_ema = ((price - ema20) / ema20 * 100) if ema20 else 0.0

        above_ema = price_vs_ema > 0



        # ── BUY Logic ───────────────────────────────────────────

        # A: Oversold RSI

        if rsi < 30:

            conf = 0.58 + (30 - rsi) / 30 * 0.28  # 0.58–0.86

            if price_vs_ema < -20:  # extreme dump — reduce

                conf *= 0.80

            base_signal = "BUY"

            base_conf = round(min(conf, 0.90), 2)



        # B: Momentum BUY — positive trend in healthy RSI range

        elif 45 <= rsi <= 60 and change_24h >= 5.0 and above_ema:

            conf = 0.52 + min(change_24h, 30) / 100  # 0.52–0.82

            if vol_spike >= 1.5:

                conf = min(conf + 0.08, 0.85)  # volume boost

            base_signal = "BUY"

            base_conf = round(conf, 2)



        # C: Volume pump signal

        elif vol_spike >= 3.0 and rsi < 70 and change_24h > 2.0:

            conf = 0.50 + (vol_spike - 3.0) / 10  # 0.50–0.65+

            base_signal = "BUY"

            base_conf = round(min(conf, 0.75), 2)



        # ── SELL Logic ───────────────────────────────────────────

        # A: Classic overbought

        elif rsi > 75:

            conf = 0.55 + (rsi - 75) / 25 * 0.30  # 0.55–0.85

            if price_vs_ema > 30:

                conf = min(conf + 0.07, 0.95)

            base_signal = "SELL"

            base_conf = round(conf, 2)



        # B: Momentum reversal

        elif 60 <= rsi <= 75 and change_24h <= -5.0 and not above_ema:

            conf = 0.52 + min(abs(change_24h), 20) / 100

            base_signal = "SELL"

            base_conf = round(conf, 2)



        # C: Extended far above EMA — take profit zone

        elif price_vs_ema > 30 and rsi > 65:

            base_signal = "SELL"

            base_conf = 0.60



        # ── HOLD ─────────────────────────────────────────────────

        else:

            mid_dist = abs(rsi - 52)

            base_conf = max(0.35, 0.50 - mid_dist / 120)

            base_signal = "HOLD"

            base_conf = round(base_conf, 2)



        # ── News Sentiment Adjustment ─────────────────────────────

        if news_sentiment == "bullish":

            if base_signal == "BUY":

                final_conf = min(base_conf + 0.10, 0.95)

                return "BUY", round(final_conf, 2)

            elif base_signal == "SELL":

                return "HOLD", round(base_conf * 0.8, 2)



        elif news_sentiment == "bearish":

            if base_signal == "SELL":

                final_conf = min(base_conf + 0.10, 0.95)

                return "SELL", round(final_conf, 2)

            elif base_signal == "BUY":

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

                lambda: __import__("requests").get(url, timeout=8)

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

            if sig == "BUY":

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

                lambda: __import__("requests").get(url, timeout=10)

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



        # BUYs are unreliable when this coin's short-term hit-rate is weak.

        if signal == "BUY" and backtest_win_rate < 0.35:

            signal = "HOLD"

            calibrated *= 0.82



        # SELLs are less reliable when recent tape strongly favors rebounds.

        if signal == "SELL" and backtest_win_rate > 0.65:

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

                if signal == "BUY":

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



    # Sort: BUY first → highest confidence → alphabetical

    signal_order = {"BUY": 0, "HOLD": 1, "SELL": 2, "SKIP": 3}

    results.sort(key=lambda x: (signal_order.get(x["signal"], 1), -x["confidence"]))



    return {

        "meme_coins": results,

        "total": len(results),

        "static_universe": len(static_bases),

        "discovered_universe": len(discovered_bases),

        "dynamic_discovery_enabled": settings.MEME_DYNAMIC_DISCOVERY_ENABLED,

        "buy_count": sum(1 for r in results if r["signal"] == "BUY"),

        "sell_count": sum(1 for r in results if r["signal"] == "SELL"),

        "hold_count": sum(1 for r in results if r["signal"] == "HOLD"),

        "skip_count": sum(1 for r in results if r["signal"] == "SKIP"),

        "hot_count": sum(1 for r in results if r["is_hot"]),

        "updated_at": datetime.now().isoformat(),

    }





# ── Signal Win Rate Endpoints ─────────────────────────────────────





@router.get("/api/signals/winrate")

async def get_signal_winrate(

    symbol: str = Query(None, description="Filter by symbol e.g. BONK/USDT"),

):

    """

    Get signal win rate for one coin or all coins.

    Win = BUY signal followed by >= 3% price rise in 24h.

    Results appear after 24h of live signal data.

    """

    data = await asyncio.to_thread(get_win_rate, symbol, 100)

    return data





@router.get("/api/signals/leaderboard")

async def get_signal_leaderboard():

    """

    Top 10 coins by signal win rate (needs min 5 scored signals each).

    Refreshes automatically as new signals are scored every 30 min.

    """

    board = await asyncio.to_thread(get_leaderboard, 10)

    return {"leaderboard": board, "updated_at": datetime.now().isoformat()}





# ── Telegram Alert Endpoints ──────────────────────────────────────





@router.get("/api/telegram/status")

async def telegram_status():

    """Check if Telegram bot is configured and ready."""

    import os



    configured = tg_is_configured()

    return {

        "configured": configured,

        "enabled": os.getenv("TELEGRAM_ALERTS_ENABLED", "false").lower() == "true",

        "token_set": bool(os.getenv("TELEGRAM_BOT_TOKEN")),

        "chat_id_set": bool(os.getenv("TELEGRAM_CHAT_ID")),

        "setup_guide": "Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID + TELEGRAM_ALERTS_ENABLED=true to your .env file",

    }





@router.post("/api/telegram/test")

async def telegram_test_alert():

    """Send a test alert to verify Telegram bot is working."""

    if not tg_is_configured():

        return {

            "success": False,

            "message": "Telegram not configured. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env",

        }

    sent = await asyncio.to_thread(

        send_signal_alert,

        "TEST/USDT",

        "BUY",

        0.001234,

        0.75,

        rsi=28.5,

        change_24h=12.3,

        volume_spike=3.2,

        news_sentiment="bullish",

        reason="Test alert from TradeAI PRO dashboard",

    )

    return {

        "success": sent,

        "message": "Test alert sent! Check your Telegram."

        if sent

        else "Send failed — check bot token and chat ID",

    }





@router.get("/api/symbols")

async def get_symbols():

    """List all available symbols."""

    return {"symbols": list(svc._market_data.keys())}





@router.get("/api/market/{symbol}")

async def get_market_data(

    symbol: str,

    limit: int = Query(100, le=500),

    interval: str = Query("5m"),

):

    """

    Get OHLCV + indicator data for a symbol.

    Supports ?interval=5m|15m|1h|1d for the chart timeframe selector.

    Non-5m intervals are fetched on-demand and indicators are calculated fresh.

    """

    # Guard against route shadowing: /api/market/mood can be captured here

    # when dynamic routes are registered before static ones.

    if symbol.lower() == "mood":

        return svc._compute_market_mood()



    symbol = symbol.replace("-", "/")

    if interval not in ("5m", "15m", "1h", "1d"):

        interval = "5m"



    if interval == "5m" and symbol in svc._market_data:

        df = svc._market_data[symbol].tail(limit)

        return {

            "symbol": symbol,

            "data": svc._build_market_records(df),

            "interval": interval,

        }



    # On-demand fetch for non-default intervals (or symbol not yet in cache)

    period_map = {"5m": "5d", "15m": "10d", "1h": "30d", "1d": "90d"}

    period = period_map.get(interval, "5d")



    try:

        df = await asyncio.to_thread(fetch_ohlcv, symbol, period, interval)

        if df is None or df.empty:

            df = await asyncio.to_thread(

                fetch_arbitrary_symbol, symbol, period, interval

            )

        if df is None or df.empty:

            return {"error": f"Symbol {symbol} not found"}

        df = calculate_indicators(df)

    except Exception as e:

        logger.error("On-demand market fetch error for %s@%s: %s", symbol, interval, e)

        return {"error": str(e)}



    df = df.tail(limit)

    return {

        "symbol": symbol,

        "data": svc._build_market_records(df),

        "interval": interval,

    }





@router.get("/api/signal/{symbol}")

async def get_current_signal(symbol: str):

    """Get the latest signal for a symbol."""

    symbol = symbol.replace("-", "/")

    result = svc._get_signal_for_symbol(symbol)

    if result is None:

        return {"error": f"Symbol {symbol} not found"}

    return result





@router.get("/api/trades")

async def get_trades(limit: int = Query(20, le=100)):

    """Get recent trade history from real paper trading engine."""

    engine = svc._get_engine()

    raw_history = engine.get_history(limit=limit)

    enriched = []

    for t in raw_history:

        d = dict(t)

        d.setdefault("timestamp", d.get("exit_time") or d.get("entry_time") or "")

        d.setdefault("exit_reason", d.get("status") or "")

        enriched.append(d)

    return {"trades": enriched}





@router.get("/api/risk/state")

async def get_risk_state():

    """Get current execution risk state (heat, drawdown, circuit breaker)."""

    engine = svc._get_engine()

    return engine.get_risk_state()





@router.get("/api/ai/weights")

async def get_ai_weight_state():

    """Expose dynamic ensemble weight state for monitoring and debugging."""

    try:

        from agents.ai_engine.ensemble import get_dynamic_weight_state



        return get_dynamic_weight_state()

    except Exception as exc:

        logger.error("AI weight state error: %s", exc)

        return JSONResponse(status_code=500, content={"error": str(exc)})





@router.post("/api/risk/reset-circuit")

async def reset_risk_circuit(rebase_capital: bool = Query(False)):

    """Manually reset circuit breaker and consecutive-loss pause.



    Use rebase_capital=true to set current capital as new drawdown baseline.

    """

    engine = svc._get_engine()

    state = engine.reset_circuit_breaker(rebase_capital=rebase_capital)

    return {"ok": True, "risk": state}





@router.get("/api/paper/scorecard")

async def get_paper_scorecard():

    """Return summary scorecard for the paper trading banner in the dashboard."""

    try:

        engine = svc._get_engine()

        stats = engine.get_stats()

        open_cnt = len(engine.open_trades)

        total_closed = stats.get("total_trades", 0)

        win_rate = stats.get("win_rate")



        if total_closed >= 30 and win_rate is not None and win_rate >= 55.0:

            unlock_level = "green"

            validated = True

            msg = "Strategy validated (WR ≥ 55% over 30+ trades)"

        elif total_closed >= 15 and win_rate is not None and win_rate >= 50.0:

            unlock_level = "orange"

            validated = False

            msg = f"Good progress ({total_closed}/30 trades, WR: {win_rate:.1f}%)"

        elif total_closed >= 10 and win_rate is not None and win_rate < 45.0:

            unlock_level = "red"

            validated = False

            msg = f"Low win rate ({win_rate:.1f}% over {total_closed} trades)"

        else:

            unlock_level = "grey"

            validated = False

            msg = f"Building sample ({total_closed}/30 paper trades)"



        return {

            "enabled": True,

            "total_trades": total_closed,

            "open_trades": open_cnt,

            "win_rate_pct": win_rate if total_closed > 0 else None,

            "unlock_level": unlock_level,

            "unlock_message": msg,

            "validated": validated,

            "capital": stats.get("capital", 0),

            "total_pnl": stats.get("total_pnl", 0),

        }

    except Exception as exc:

        logger.error("Paper scorecard endpoint error: %s", exc)

        return {

            "enabled": True,

            "total_trades": 0,

            "open_trades": 0,

            "win_rate_pct": None,

            "unlock_level": "grey",

            "unlock_message": "Initializing paper trading...",

            "validated": False,

        }





@router.post("/api/paper/resolve")

async def resolve_paper_trades():

    """Resolve open paper trades against latest cached prices."""

    try:

        engine = svc._get_engine()

        prices = {}

        for sym, d in svc._market_data.items():

            if isinstance(d, dict) and "close" in d:

                prices[sym] = {"high": d.get("high", d["close"]), "low": d.get("low", d["close"]), "close": d["close"]}

        closed = engine.update_prices(prices)

        return {

            "ok": True,

            "resolved": len(closed) if closed else 0,

            "remaining_open": len(engine.open_trades),

        }

    except Exception as exc:

        logger.error("Resolve paper trades error: %s", exc)

        return {"ok": False, "resolved": 0, "remaining_open": 0, "error": str(exc)}


@router.post("/api/paper/trade")
async def place_paper_trade_endpoint(payload: dict):
    """Manually place a paper trade from the dashboard."""
    symbol = str(payload.get("symbol") or "").strip().upper()
    direction = str(payload.get("direction") or "LONG").strip().upper()
    try:
        entry = float(payload.get("entry") or 0.0)
        stop_loss = float(payload.get("stop_loss") or 0.0)
        target = float(payload.get("target") or 0.0)
        atr = float(payload.get("atr") or 0.0)
    except (ValueError, TypeError):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid numerical values for entry, stop_loss, or target."},
        )
    strategy = str(payload.get("strategy") or "MANUAL")
    regime = str(payload.get("regime") or "TRENDING")
    confidence = float(payload.get("confidence") or payload.get("ai_confidence") or 0.0)
    rsi_at_entry = float(payload.get("rsi") or payload.get("rsi_at_entry") or 0.0)
    volume_above_avg = bool(payload.get("volume_above_avg", False))
    min_rr = float(payload.get("min_rr") or 0.0) if payload.get("min_rr") else None

    if not symbol or entry <= 0 or stop_loss <= 0 or target <= 0:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid trade details: symbol, entry, stop_loss, and target must be > 0."},
        )

    engine = svc._get_engine()
    trade = engine.execute(
        symbol=symbol,
        direction=direction,
        entry=entry,
        sl=stop_loss,
        tp=target,
        atr=atr,
        signal_type=strategy,
        regime=regime,
        rsi_at_entry=rsi_at_entry,
        volume_above_avg=volume_above_avg,
        confidence=confidence,
        min_rr=min_rr,
    )
    if not trade:
        risk = engine.get_risk_state()
        reason = getattr(engine, "last_rejection_reason", None) or "Trade blocked by Risk Manager"
        if reason == "Trade blocked by Risk Manager":
            if risk.get("circuit_breaker_active"):
                reason = f"Circuit Breaker active ({risk.get('circuit_breaker_reason') or 'drawdown limit'})"
            elif int(risk.get("consecutive_losses", 0)) >= int(risk.get("max_consecutive_losses", 3)):
                reason = f"Max consecutive losses reached ({risk.get('consecutive_losses')}/{risk.get('max_consecutive_losses')}) - trading paused for today"
            elif float(risk.get("portfolio_heat_pct", 0)) > float(risk.get("portfolio_heat_max", 0.06)):
                reason = f"Portfolio heat ({round(float(risk.get('portfolio_heat_pct', 0))*100, 1)}%) exceeds max ({round(float(risk.get('portfolio_heat_max', 0.06))*100, 1)}%)"
            elif symbol in getattr(engine, "_cooldowns", {}) and engine._cooldowns[symbol] > 0:
                reason = f"{symbol} is in cooldown ({engine._cooldowns[symbol]} candles remaining)"
        return JSONResponse(
            status_code=400,
            content={
                "error": reason,
                "risk_state": risk,
            },
        )


    trade_dict = trade.__dict__ if hasattr(trade, "__dict__") else dict(trade)
    stats = engine.get_stats()
    return {
        "status": "ok",
        "trade": trade_dict,
        "stats": stats,
        "open_trades_count": len(engine.get_open_trades()),
    }


@router.get("/api/paper/open")
async def get_open_paper_trades_endpoint():
    """Get all currently open paper trades."""
    engine = svc._get_engine()
    return {"open_trades": engine.get_open_trades()}


@router.post("/api/paper/close/{trade_id}")
async def close_paper_trade_endpoint(trade_id: int, payload: dict = None):
    """Manually close an open paper trade at exit price."""
    engine = svc._get_engine()
    payload = payload or {}
    exit_price = float(payload.get("exit_price") or 0.0)

    if exit_price <= 0:
        for t in engine.open_trades:
            if t.id == trade_id:
                d = svc._market_data.get(t.symbol)
                if isinstance(d, dict) and "close" in d:
                    exit_price = float(d["close"])
                elif hasattr(d, "iloc") and not d.empty and "close" in d.columns:
                    exit_price = float(d["close"].iloc[-1])
                else:
                    exit_price = float(t.entry_price)
                break

    trade = engine.close_manual(trade_id=trade_id, exit_price=exit_price)
    if not trade:
        return JSONResponse(
            status_code=404,
            content={"error": f"Open trade #{trade_id} not found"},
        )
    trade_dict = trade.__dict__ if hasattr(trade, "__dict__") else dict(trade)
    return {"status": "closed", "trade": trade_dict, "stats": engine.get_stats()}





# ── Self-Learning Feedback Loop endpoints ─────────────────────────────────────





@router.get("/api/learning/stats")

async def get_learning_stats_endpoint():

    """

    Return AI self-learning insights + current learned weights.



    Analyses last 500 closed paper trades from trade_outcomes SQLite table.

    Returns:

      - win rate by signal type (RSI_BOUNCE, MACD_CROSS, BREAKOUT, ...)

      - win rate by market regime (TRENDING / RANGING / HIGH_VOLATILITY)

      - win rate by RSI zone at entry

      - volume impact analysis

      - model accuracy (confidence ≥ 50% trades that won)

      - current learned weights for each signal component

    """

    try:

        from agents.analytics.feedback_loop import get_learning_stats



        return get_learning_stats()

    except Exception as exc:

        logger.error("Learning stats error: %s", exc)

        return {

            "insights": {

                "signal_type_stats": {},

                "regime_stats": {},

                "rsi_zone_stats": {},

                "volume_stats": {},

                "overall": {

                    "total_trades": 0,

                    "win_rate": 0,

                    "avg_pnl_pct": 0,

                    "model_accuracy": 0,

                },

                "best_signal_type": "—",

                "worst_signal_type": "—",

                "best_regime": "—",

                "optimal_rsi_zone": "—",

            },

            "weights": {},

            "error": str(exc),

        }





@router.get("/api/learning/outcomes")

async def get_learning_outcomes(limit: int = Query(100, le=500)):

    """

    Return recent raw trade outcomes logged to the feedback loop.

    Used by the dashboard to display the Learning Stats trade history table.

    """

    try:

        from agents.analytics.feedback_loop import get_recent_outcomes



        return {"outcomes": get_recent_outcomes(limit=limit)}

    except Exception as exc:

        logger.error("Learning outcomes error: %s", exc)

        return {"outcomes": [], "error": str(exc)}





@router.get("/api/portfolio")

async def get_portfolio():

    """Get open positions and unrealized P&L from real trading engine."""

    from collections import defaultdict



    ist_tz = timezone(timedelta(hours=5, minutes=30))



    def _safe_float(v, default=0.0) -> float:

        try:

            return float(v)

        except Exception:

            return float(default)



    def _safe_div(num: float, den: float) -> float:

        if den == 0:

            return 0.0

        return float(num) / float(den)



    def _total_trade_pnl(trade: dict) -> float:

        total = trade.get("total_pnl")

        if total is not None:

            return _safe_float(total, 0.0)

        return _safe_float(trade.get("pnl"), 0.0) * _safe_float(

            trade.get("quantity"), 1.0

        )



    def _parse_iso_dt(raw: str | None):

        if not raw:

            return None

        s = str(raw).strip()

        if not s:

            return None

        if s.endswith("Z"):

            s = s[:-1] + "+00:00"

        try:

            dt = datetime.fromisoformat(s)

            if dt.tzinfo is None:

                dt = dt.replace(tzinfo=ist_tz)

            return dt.astimezone(ist_tz)

        except Exception:

            return None



    def _norm_direction(raw: str) -> str:

        d = str(raw or "").upper()

        if d == "BUY":

            return "LONG"

        if d == "SELL":

            return "SHORT"

        return d or "LONG"



    def _sector_for_symbol(symbol: str) -> str:

        sym = str(symbol or "")

        if "/" in sym:

            return "Crypto"

        base = sym.replace(".NS", "")

        if base in getattr(settings, "NSE_SECTOR_MAP", {}):

            return settings.NSE_SECTOR_MAP.get(base, "Others")

        try:

            return svc._get_sector_for_symbol(base)

        except Exception:

            return "Others"



    engine = svc._get_engine()

    open_trades = engine.get_open_trades()

    closed_trades = engine.get_history(limit=2000)

    now_ist = datetime.now(tz=ist_tz)



    total_unrealized = 0

    total_open_risk = 0.0

    enriched_open = []

    sector_exposure: dict[str, float] = defaultdict(float)



    for t in open_trades:

        symbol = t["symbol"]

        direction = _norm_direction(t.get("direction", "LONG"))

        entry = _safe_float(t.get("entry_price"), 0.0)

        stop_loss = _safe_float(t.get("stop_loss"), entry)

        quantity = _safe_float(t.get("quantity"), 0.0)

        initial_qty = _safe_float(t.get("initial_quantity"), quantity)



        if symbol in svc._market_data:

            current_close = float(svc._market_data[symbol].iloc[-1]["close"])

            if direction == "LONG":

                unrealized = (current_close - entry) * quantity

            else:

                unrealized = (entry - current_close) * quantity

        else:

            current_close = entry

            unrealized = 0.0



        initial_risk = abs(entry - stop_loss) * max(initial_qty, 0.0)

        open_risk = abs(entry - stop_loss) * max(quantity, 0.0)

        total_open_risk += open_risk

        total_unrealized += unrealized



        r_multiple = _safe_div(unrealized, initial_risk)

        if current_close > 0:

            if direction == "LONG":

                sl_dist_pct = (

                    _safe_div((current_close - stop_loss), current_close) * 100

                )

            else:

                sl_dist_pct = (

                    _safe_div((stop_loss - current_close), current_close) * 100

                )

        else:

            sl_dist_pct = 0.0



        entry_dt = _parse_iso_dt(t.get("entry_time"))

        hours_in_trade = 0.0

        if entry_dt is not None:

            hours_in_trade = max((now_ist - entry_dt).total_seconds() / 3600.0, 0.0)

        if hours_in_trade >= 24:

            time_human = f"{hours_in_trade / 24.0:.1f}d"

        elif hours_in_trade > 0:

            time_human = f"{hours_in_trade:.1f}h"

        else:

            time_human = "—"



        sector = _sector_for_symbol(symbol)

        sector_exposure[sector] += open_risk



        row = dict(t)

        row["direction"] = direction

        row["current_price"] = round(current_close, 2)

        row["unrealized_pnl"] = round(unrealized, 2)

        row["initial_risk"] = round(initial_risk, 2)

        row["open_risk"] = round(open_risk, 2)

        row["r_multiple"] = round(r_multiple, 3)

        row["sl_distance_pct"] = round(sl_dist_pct, 3)

        row["time_in_trade_hours"] = round(hours_in_trade, 2)

        row["time_in_trade_human"] = time_human

        row["sector"] = sector

        enriched_open.append(row)



    for row in enriched_open:

        row["heat_contribution_pct"] = round(

            _safe_div(row.get("open_risk", 0.0), total_open_risk) * 100.0, 2

        )



    # Risk-adjusted performance metrics from closed trades

    returns = np.array(

        [_safe_float(t.get("pnl_pct"), 0.0) / 100.0 for t in closed_trades], dtype=float

    )

    rf_daily = 0.065 / 252.0

    if len(returns) >= 2:

        excess = returns - rf_daily

        std = float(np.std(excess, ddof=1))

        sharpe = float(np.mean(excess) / std * np.sqrt(252.0)) if std > 0 else 0.0



        downside = excess[excess < 0]

        downside_std = float(np.std(downside, ddof=1)) if len(downside) >= 2 else 0.0

        if downside_std > 0:

            sortino = float(np.mean(excess) / downside_std * np.sqrt(252.0))

        else:

            sortino = None if float(np.mean(excess)) >= 0 else 0.0

    else:

        sharpe = 0.0

        sortino = 0.0



    gross_profit = sum(

        _total_trade_pnl(t) for t in closed_trades if _total_trade_pnl(t) > 0

    )

    gross_loss = abs(

        sum(_total_trade_pnl(t) for t in closed_trades if _total_trade_pnl(t) < 0)

    )

    if gross_loss > 0:

        profit_factor = round(gross_profit / gross_loss, 3)

    elif gross_profit > 0:

        profit_factor = None

    else:

        profit_factor = 0.0



    # Drawdown tracker using realized equity curve + current unrealized

    equity_curve = engine.get_equity_curve()

    curve_points: list[tuple[datetime | None, float]] = []

    curve_values: list[float] = []

    for p in equity_curve:

        val = _safe_float((p or {}).get("equity"), np.nan)

        if not np.isfinite(val):

            continue

        ts = _parse_iso_dt((p or {}).get("timestamp"))

        curve_points.append((ts, val))

        curve_values.append(val)



    current_value = _safe_float(getattr(engine, "capital", 0.0), 0.0) + float(

        total_unrealized

    )

    series = list(curve_values)

    series.append(current_value)



    running_peak = -np.inf

    max_drawdown_pct = 0.0

    for val in series:

        running_peak = max(running_peak, val)

        if running_peak > 0:

            dd = (running_peak - val) / running_peak * 100.0

            max_drawdown_pct = max(max_drawdown_pct, dd)



    peak_value = max(series) if series else max(current_value, 1.0)

    current_drawdown_pct = (

        (peak_value - current_value) / peak_value * 100.0 if peak_value > 0 else 0.0

    )



    drawdown_days = 0.0

    if current_drawdown_pct > 1e-9:

        last_peak_ts = None

        for ts, val in curve_points:

            if peak_value > 0 and abs(val - peak_value) <= max(1e-6, peak_value * 1e-8):

                if ts is not None and (last_peak_ts is None or ts > last_peak_ts):

                    last_peak_ts = ts

        if last_peak_ts is not None:

            drawdown_days = max((now_ist - last_peak_ts).total_seconds() / 86400.0, 0.0)



    if drawdown_days >= 1:

        drawdown_label = f"{drawdown_days:.1f}d"

    elif drawdown_days > 0:

        drawdown_label = f"{drawdown_days * 24.0:.1f}h"

    else:

        drawdown_label = "0d"



    # Concentration warning from sector risk contribution

    concentration_warnings: list[str] = []

    concentration_by_sector = []

    for sec, exposure in sorted(

        sector_exposure.items(), key=lambda kv: kv[1], reverse=True

    ):

        pct = _safe_div(exposure, total_open_risk) * 100.0

        concentration_by_sector.append(

            {

                "sector": sec,

                "open_risk": round(exposure, 2),

                "risk_share_pct": round(pct, 2),

            }

        )

        if total_open_risk > 0 and pct >= 40.0:

            concentration_warnings.append(

                f"HIGH CONCENTRATION: {sec} = {pct:.0f}% of open risk"

            )



    heat_pct = round(engine.get_portfolio_heat_pct() * 100.0, 2)

    caution_level = (

        "HIGH" if concentration_warnings else ("MEDIUM" if heat_pct >= 40 else "LOW")

    )

    caution_message = (

        concentration_warnings[0]

        if concentration_warnings

        else (

            "Portfolio heat elevated — monitor position clustering."

            if heat_pct >= 40

            else ""

        )

    )



    # Today's realized attribution by signal type and regime

    today_ist = now_ist.date()

    sig_attr: dict[str, float] = defaultdict(float)

    reg_attr: dict[str, float] = defaultdict(float)

    total_today_realized = 0.0

    trades_today = 0



    for t in closed_trades:

        exit_dt = _parse_iso_dt(t.get("exit_time"))

        if exit_dt is None or exit_dt.date() != today_ist:

            continue

        pnl_val = _total_trade_pnl(t)

        total_today_realized += pnl_val

        trades_today += 1

        sig = str(t.get("signal_type") or "COMBINED").upper()

        reg = str(t.get("regime") or "UNKNOWN").upper()

        sig_attr[sig] += pnl_val

        reg_attr[reg] += pnl_val



    def _attr_rows(d: dict[str, float]) -> list[dict]:

        rows = []

        for k, pnl_val in sorted(d.items(), key=lambda kv: abs(kv[1]), reverse=True):

            rows.append(

                {

                    "key": k,

                    "pnl": round(pnl_val, 2),

                    "pct_of_total": round(

                        _safe_div(pnl_val, total_today_realized) * 100.0, 2

                    )

                    if abs(total_today_realized) > 1e-9

                    else 0.0,

                    "trades": sum(

                        1

                        for t in closed_trades

                        if str(

                            t.get("signal_type")

                            if d is sig_attr

                            else t.get("regime") or ""

                        ).upper()

                        == k

                        and (_parse_iso_dt(t.get("exit_time")) or now_ist).date()

                        == today_ist

                    ),

                }

            )

        return rows



    closed_count = engine.get_stats().get("total_trades", 0)



    return {

        "open_positions": enriched_open,

        "total_open": len(enriched_open),

        "total_unrealized_pnl": round(total_unrealized, 2),

        "total_portfolio_value": round(current_value, 2),

        "total_open_risk": round(total_open_risk, 2),

        "total_closed": closed_count,

        "risk_metrics": {

            "sharpe_ratio": round(sharpe, 3),

            "sortino_ratio": None if sortino is None else round(sortino, 3),

            "profit_factor": profit_factor,

            "risk_free_rate_annual": 0.065,

        },

        "drawdown": {

            "current_drawdown_pct": round(max(current_drawdown_pct, 0.0), 3),

            "max_drawdown_pct": round(max(max_drawdown_pct, 0.0), 3),

            "duration_days": round(drawdown_days, 3),

            "duration_label": drawdown_label,

            "peak_value": round(peak_value, 2),

            "current_value": round(current_value, 2),

        },

        "concentration_warnings": concentration_warnings,

        "concentration_by_sector": concentration_by_sector,

        "portfolio_caution": {

            "caution_level": caution_level,

            "caution_message": caution_message,

            "heat_pct": heat_pct,

        },

        "pnl_attribution": {

            "today_realized_pnl": round(total_today_realized, 2),

            "today_trade_count": trades_today,

            "by_signal_type": _attr_rows(sig_attr),

            "by_regime": _attr_rows(reg_attr),

        },

    }





@router.get("/api/performance")

async def get_performance():

    """Get detailed performance metrics and equity curve from real trading engine."""

    from collections import Counter, defaultdict



    def _trade_total_pnl(trade: dict) -> float:

        total = trade.get("total_pnl")

        if total is not None:

            try:

                return float(total)

            except Exception:

                pass

        try:

            return float(trade.get("pnl", 0) or 0) * float(

                trade.get("quantity", 1) or 1

            )

        except Exception:

            return 0.0



    engine = svc._get_engine()

    stats = engine.get_stats()

    equity_curve = engine.get_equity_curve()



    closed_trades = engine.get_history(limit=1000)

    wins = [t for t in closed_trades if _trade_total_pnl(t) > 0]

    losses = [t for t in closed_trades if _trade_total_pnl(t) <= 0]



    # ── Monthly P&L breakdown ──────────────────────────

    monthly: dict[str, float] = defaultdict(float)

    for t in closed_trades:

        key = (t.get("exit_time") or "")[:7]  # "2026-03"

        if key:

            monthly[key] += _trade_total_pnl(t)

    monthly_pnl = [{"month": k, "pnl": round(v, 2)} for k, v in sorted(monthly.items())]



    # ── Exit reason breakdown ──────────────────────────

    exit_counts = Counter(t.get("status", "UNKNOWN") for t in closed_trades)

    exit_reasons = [{"reason": r, "count": c} for r, c in exit_counts.most_common()]



    # ── P&L by symbol (top 10) ─────────────────────────

    sym_pnl: dict[str, float] = defaultdict(float)

    sym_trades: dict[str, int] = defaultdict(int)

    for t in closed_trades:

        sym = t.get("symbol", "?")

        sym_pnl[sym] += _trade_total_pnl(t)

        sym_trades[sym] += 1

    symbol_breakdown = sorted(

        [

            {"symbol": k, "pnl": round(v, 2), "trades": sym_trades[k]}

            for k, v in sym_pnl.items()

        ],

        key=lambda x: abs(x["pnl"]),

        reverse=True,

    )[:10]



    # ── Return % ───────────────────────────────────────

    initial = engine.initial_capital

    current = stats.get("capital", initial)

    total_return_pct = round((current - initial) / initial * 100, 2) if initial else 0



    return {

        "metrics": {

            "total_trades": stats.get("total_trades", 0),

            "winning_trades": stats.get("winning_trades", 0),

            "losing_trades": stats.get("losing_trades", 0),

            "win_rate": stats.get("win_rate", 0),

            "total_pnl": stats.get("total_pnl", 0),

            "total_return_pct": total_return_pct,

            "avg_pnl": stats.get("avg_pnl", 0),

            "avg_win": stats.get("avg_win", 0),

            "avg_loss": stats.get("avg_loss", 0),

            "best_trade": stats.get("best_trade", 0),

            "worst_trade": stats.get("worst_trade", 0),

            "sharpe_ratio": stats.get("sharpe_ratio", 0),

            "max_drawdown": stats.get("max_drawdown", 0),

            "max_drawdown_pct": stats.get("max_drawdown_pct", 0),

            "profit_factor": stats.get("profit_factor", 0),

            "capital": current,

            "initial_capital": initial,

            "daily_pnl": stats.get("daily_pnl", 0),

        },

        "status": {

            "feed": svc._get_feed_status(),

            "symbols_loaded": len(svc._market_data),

            "signals_cached": len(svc._signal_cache),

        },

        "equity_curve": equity_curve,

        "trade_distribution": {"wins": len(wins), "losses": len(losses)},

        "monthly_pnl": monthly_pnl,

        "exit_reasons": exit_reasons,

        "symbol_breakdown": symbol_breakdown,

        "recent_trades": list(reversed(closed_trades[-50:])),

    }





@router.get("/api/regime/{symbol}")

async def get_regime(symbol: str):

    """Get current market regime for a symbol."""

    symbol = symbol.replace("-", "/")

    if symbol not in svc._regime_cache:

        return {"error": f"Symbol {symbol} not found"}



    return {

        "symbol": symbol,

        "regime": svc._regime_cache[symbol],

        "timestamp": datetime.now().isoformat(),

    }





@router.get("/api/stats")

async def get_stats():

    """Get dashboard summary statistics from real trading engine."""

    engine = svc._get_engine()

    stats = engine.get_stats()



    return {

        "total_trades": stats.get("total_trades", 0),

        "winning_trades": stats.get("winning_trades", 0),

        "win_rate": stats.get("win_rate", 0),

        "total_pnl": stats.get("total_pnl", 0),

        "avg_pnl": stats.get("avg_pnl", 0),

        "best_trade": stats.get("best_trade", 0),

        "worst_trade": stats.get("worst_trade", 0),

        "capital": stats.get("capital", 100000),
        "open_trades": stats.get("open_trades", len(engine.get_open_trades())),

    }





@router.get("/api/backtest/{symbol}")

async def run_backtest(

    symbol: str,

    days: int = Query(

        default=5, ge=5, le=30, description="Backtest window in days (5-30)"

    ),

):

    """Run backtest for a symbol with a selectable 5-30 day window."""

    symbol = symbol.replace("-", "/")

    if symbol not in svc._signal_cache:

        return {"error": f"Symbol {symbol} not found"}



    df = svc._signal_cache[symbol].copy()



    # Extended windows need fresh historical fetch because live cache is intraday-short.

    if days > 5:

        try:

            if "/" in symbol:

                from agents.data_ingestion.binance_fetcher import fetch_historical



                ext_df = await asyncio.to_thread(fetch_historical, symbol, "1h", days)

            else:

                ext_df = await asyncio.to_thread(fetch_ohlcv, symbol, "1mo", "5m")



            if ext_df is None or ext_df.empty:

                return {"error": f"Unable to load {days}-day history for {symbol}"}



            ext_df = calculate_indicators(ext_df)

            ext_df = classify_trend(ext_df)

            regime = detect_regime(ext_df)

            df = generate_signals(ext_df, regime=regime, symbol=symbol)

        except Exception as exc:

            logger.error(

                "Backtest extended fetch failed for %s (%sd): %s", symbol, days, exc

            )

            return {"error": f"Extended backtest unavailable for {symbol} right now"}



    if "timestamp" in df.columns:

        cutoff = datetime.now() - timedelta(days=days)

        ts = pd.to_datetime(df["timestamp"], errors="coerce")

        df = df.loc[ts >= cutoff].copy()



    if df.empty:

        return {"error": f"No backtest data available for last {days} days"}



    bt = Backtester(initial_capital=100000, slippage_pct=0.05)

    result = bt.run(df)



    return {

        "symbol": symbol,

        "window_days": days,

        "bars_used": int(len(df)),

        "total_trades": result.total_trades,

        "win_rate": round(result.win_rate, 1),

        "total_pnl": round(result.total_pnl, 2),

        "max_drawdown": round(result.max_drawdown, 2),

        "max_drawdown_pct": round(result.max_drawdown_pct, 1),

        "profit_factor": round(result.profit_factor, 2),

        "sharpe_ratio": round(result.sharpe_ratio, 2),

    }





# ── Market Intelligence Endpoints ───────────────────────

@router.get("/api/screener/gainers")

async def screener_gainers():

    """

    Top NSE gainers with X-Factor multiplier.

    Primary: fetch from NSE screener API.

    Fallback: compute from svc._market_data cache if API fails/times out.

    """

    # Try precomputed cache first for instant response

    if svc._movers_cache.get("gainers"):

        data = svc._movers_cache.get("gainers")

        if data:

            logger.info("Screener gainers from precomputed cache: %d stocks", len(data))

            return {

                "data": data,

                "real_data": True,

                "source": "cache",

                "updated_at": svc._movers_cache.get("updated_at"),

            }



    # Fallback to direct cache computation

    if svc._market_data:

        data = svc._compute_gainers_losers_from_cache("gainers")

        if data:

            logger.info("Screener gainers from cache: %d stocks", len(data))

            return {

                "data": data,

                "real_data": True,

                "source": "cache",

                "updated_at": datetime.now().isoformat(),

            }



    # Cache empty — try yFinance batch (with short timeout)

    try:

        data = await asyncio.wait_for(

            asyncio.to_thread(fetch_nse_gainers_losers, "gainers"),

            timeout=5.0,

        )

    except asyncio.TimeoutError:

        logger.warning("Screener gainers timed out (5s) — no cache available")

        data = []

    except Exception as e:

        logger.error("Screener gainers fetch failed: %s", e)

        data = []



    return {

        "data": data or [],

        "real_data": bool(data),

        "updated_at": datetime.now().isoformat(),

    }





@router.get("/api/screener/losers")

async def screener_losers():

    """

    Top NSE losers with X-Factor multiplier.

    Primary: fetch from NSE screener API.

    Fallback: compute from svc._market_data cache if API fails/times out.

    """

    # Try precomputed cache first for instant response

    if svc._movers_cache.get("losers"):

        data = svc._movers_cache.get("losers")

        if data:

            logger.info("Screener losers from precomputed cache: %d stocks", len(data))

            return {

                "data": data,

                "real_data": True,

                "source": "cache",

                "updated_at": svc._movers_cache.get("updated_at"),

            }



    # Fallback to direct cache computation

    if svc._market_data:

        data = svc._compute_gainers_losers_from_cache("losers")

        if data:

            logger.info("Screener losers from cache: %d stocks", len(data))

            return {

                "data": data,

                "real_data": True,

                "source": "cache",

                "updated_at": datetime.now().isoformat(),

            }



    # Cache empty — try yFinance batch (with short timeout)

    try:

        data = await asyncio.wait_for(

            asyncio.to_thread(fetch_nse_gainers_losers, "losers"),

            timeout=5.0,

        )

    except asyncio.TimeoutError:

        logger.warning("Screener losers timed out (5s) — no cache available")

        data = []

    except Exception as e:

        logger.error("Screener losers fetch failed: %s", e)

        data = []



    return {

        "data": data or [],

        "real_data": bool(data),

        "updated_at": datetime.now().isoformat(),

    }





@router.get("/api/sectors")

async def sector_heatmap():

    """

    Sectoral performance heatmap data.

    Cache-first: compute from svc._market_data for instant response.

    Fallback: fetch from yFinance if cache is empty.

    """

    # Try cache first for instant response

    if svc._market_data:

        sectors = svc._compute_sectors_from_cache()

        if sectors:

            logger.info("Sector heatmap from cache: %d sectors", len(sectors))

            return {

                "sectors": sectors,

                "real_data": True,

                "source": "cache",

                "updated_at": datetime.now().isoformat(),

            }



    # Cache empty — try yFinance (with short timeout)

    try:

        sectors = await asyncio.wait_for(

            asyncio.to_thread(fetch_sector_performance),

            timeout=8.0,

        )

    except asyncio.TimeoutError:

        logger.warning("Sector fetch timed out (8s)")

        sectors = []

    except Exception as e:

        logger.error("Sector fetch failed: %s", e)

        sectors = []

    return {

        "sectors": sectors or [],

        "real_data": bool(sectors),

        "updated_at": datetime.now().isoformat(),

    }





@router.get("/api/analytics/scorecard")

async def get_scorecard():

    """

    Multi-dimensional win-rate scorecard from the trade journal.

    Dimensions: regime, symbol, hour, confidence bucket, quality grade.

    Also lists false signal reason distribution.

    """

    scorecard = await asyncio.to_thread(get_journal().get_scorecard)

    return scorecard





@router.get("/api/analytics/signals")

async def get_journal_signals(limit: int = Query(50, le=500)):

    """Most recent N recorded signals with their outcomes."""

    signals = await asyncio.to_thread(get_journal().get_recent_signals, limit)

    return {"signals": signals, "total": get_journal().total_count()}





@router.get("/api/tradeflow")

async def get_trade_flow():

    """FII/DII institutional trade flow — real NSE data with rolling history.

    Server-side 8s timeout so the frontend's 10s AbortController never fires first."""

    try:

        data = await asyncio.wait_for(

            asyncio.to_thread(fetch_fii_dii_flow),

            timeout=8.0,

        )

    except asyncio.TimeoutError:

        logger.warning("Trade flow fetch timed out (8s)")

        data = {

            "flows": [],

            "date": datetime.now().strftime("%d-%b-%Y"),

            "sentiment": "UNAVAILABLE",

            "combined_net": None,

            "history": [],

            "source": "timeout",

            "real_data": False,

            "stale": True,

        }

    except Exception as e:

        logger.error("Trade flow fetch failed: %s", e)

        data = {

            "flows": [],

            "date": datetime.now().strftime("%d-%b-%Y"),

            "sentiment": "UNAVAILABLE",

            "combined_net": None,

            "history": [],

            "source": "error",

            "real_data": False,

            "stale": True,

        }

    data["updated_at"] = datetime.now().isoformat()

    return data





@router.get("/api/market/mood")

async def get_market_mood():

    """Get computed market mood from breadth, signals, regimes, and sectors."""

    return svc._compute_market_mood()





@router.get("/api/nlp/summary")

async def get_ai_summary(symbol: str | None = Query(default=None)):

    """Get a concise AI-style summary for market or a specific symbol."""

    if symbol:

        clean = symbol.replace("-", "/")

        sig = svc._get_signal_for_symbol(clean)

        if not sig:

            return {"error": f"Symbol {clean} not found"}



        direction = sig.get("signal", "HOLD")

        conf = float(sig.get("confidence", 0.0))

        trend = str(sig.get("trend", "NEUTRAL"))

        regime = str(sig.get("regime", "RANGING"))

        lines = [

            f"{clean} signal is {direction} with confidence {conf:.0f}%.",

            f"Trend is {trend} and regime is {regime}.",

            f"Risk frame: entry {sig.get('entry')}, stop {sig.get('stop_loss')}, target {sig.get('target')}.",

        ]

        return {

            "symbol": clean,

            "lines": lines,

            "summary": " ".join(lines),

            "generated_at": datetime.now().isoformat(),

        }



    mood = svc._compute_market_mood()

    return svc._build_ai_market_summary(mood)





@router.get("/api/overview/intelligence")

async def get_overview_intelligence():

    """

    Aggregated decision-cockpit payload for the Overview tab.



    Returns top ranked setups, high-quality-but-blocked setups, market guardrails,

    system/risk state, and an advisory learning overlay — all in one fast call.

    """

    try:

        return svc._compute_overview_intelligence()

    except Exception as exc:

        logger.error("Overview intelligence failed: %s", exc)

        return {

            "top_setups": [],

            "avoid_list": [],

            "ranking_meta": {

                "total_tracked": 0,

                "actionable": 0,

                "blocked_high_quality": 0,

            },

            "market_guardrails": {},

            "system_state": {},

            "learning_overlay": None,

            "error": str(exc),

            "updated_at": datetime.now().isoformat(),

        }





# ── Edge Engine Endpoints ───────────────────────────────────────────────────

@router.get("/api/edge/score/{symbol:path}")

async def get_edge_score(

    symbol: str,

    force_refresh: bool = Query(

        False, description="Bypass edge cache and compute fresh"

    ),

):

    """

    Get EdgeScore breakdown for a symbol.

    Uses {symbol:path} so crypto symbols like SOL/USDT survive URL routing.

    """

    try:

        from agents.macro.edge_score_engine import EdgeScoreEngine



        symbol_clean = symbol.replace("-", "/").upper()

        if "/" in symbol_clean and "USDT" in symbol_clean:

            asset_class = "crypto"

        elif "/" in symbol_clean:

            asset_class = "forex"

        elif symbol_clean in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:

            asset_class = "index"

        else:

            asset_class = "nse"



        # Run blocking EdgeScoreEngine in a thread — it makes live HTTP calls

        # and takes several seconds on cache miss. Never block the event loop.

        def _compute():

            engine = (

                EdgeScoreEngine(cache_ttl_seconds=0)

                if force_refresh

                else EdgeScoreEngine()

            )

            return engine.get_edge_score(symbol_clean, asset_class)



        breakdown = await asyncio.wait_for(

            asyncio.to_thread(_compute),

            timeout=15.0,

        )



        cache_age_seconds = max(

            0,

            int((datetime.now(timezone.utc) - breakdown.timestamp).total_seconds()),

        )



        return {

            "symbol": symbol_clean,

            "asset_class": asset_class,

            "edge_score": breakdown.edge_score,

            "macro_score": breakdown.macro_score,

            "cot_score": breakdown.cot_score,

            "sentiment_score": breakdown.sentiment_score,

            "risk_score": breakdown.risk_score,

            "momentum_score": breakdown.momentum_score,

            "seasonal_bonus": breakdown.seasonal_bonus,

            "carry_bonus": breakdown.carry_bonus,

            "timestamp": breakdown.timestamp.isoformat(),

            "cache_hit": breakdown.cache_hit,

            "cache_age_seconds": cache_age_seconds,

            "force_refresh": force_refresh,

        }

    except asyncio.TimeoutError:

        logger.warning("EdgeScore timed out for %s", symbol)

        return {

            "error": "EdgeScore calculation timed out",

            "symbol": symbol,

            "edge_score": None,

        }

    except Exception as e:

        logger.error("Failed to get EdgeScore for %s: %s", symbol, e)

        return {"error": str(e), "symbol": symbol, "edge_score": None}





@router.get("/api/edge/top-setups")

async def get_edge_top_setups(

    asset_class: str = Query(

        "forex", description="Asset class: forex, crypto, nse, index"

    ),

    min_score: float = Query(50.0, description="Minimum EdgeScore threshold"),

    limit: int = Query(10, le=50, description="Maximum results to return"),

    force_refresh: bool = Query(

        False, description="Bypass edge cache and compute fresh"

    ),

):

    """

    Get top-ranked symbols by EdgeScore.



    Returns symbols with the highest EdgeScore for the specified asset class.

    Useful for identifying the best macro setups across the market.

    """

    try:

        from agents.macro.edge_score_engine import EdgeScoreEngine



        def _compute_top():

            engine = (

                EdgeScoreEngine(cache_ttl_seconds=0)

                if force_refresh

                else EdgeScoreEngine()

            )

            ranked = engine.get_top_setups(asset_class, min_score, limit)

            rows = []

            now_utc = datetime.now(timezone.utc)

            for symbol, score in ranked:

                cached = engine._load_cache(symbol)

                cache_age_seconds = None

                if cached:

                    cache_age_seconds = max(

                        0, int((now_utc - cached.timestamp).total_seconds())

                    )

                rows.append(

                    {

                        "symbol": symbol,

                        "edge_score": score,

                        "macro": getattr(cached, "macro_score", None),

                        "cot": getattr(cached, "cot_score", None),

                        "sentiment": getattr(cached, "sentiment_score", None),

                        "risk": getattr(cached, "risk_score", None),

                        "momentum": getattr(cached, "momentum_score", None),

                        "seasonal": getattr(cached, "seasonal_bonus", None),

                        "carry": getattr(cached, "carry_bonus", None),

                        "cache_age_seconds": cache_age_seconds,

                    }

                )

            return rows



        setups = await asyncio.wait_for(asyncio.to_thread(_compute_top), timeout=30.0)



        return {

            "asset_class": asset_class,

            "min_score": min_score,

            "count": len(setups),

            "setups": setups,

            "force_refresh": force_refresh,

            "timestamp": datetime.now(timezone.utc).isoformat(),

        }

    except asyncio.TimeoutError:

        return {

            "error": "Top setups timed out",

            "asset_class": asset_class,

            "setups": [],

        }

    except Exception as e:

        logger.error("Failed to get top EdgeScore setups: %s", e)

        return {"error": str(e), "asset_class": asset_class, "setups": []}





@router.get("/api/edge/macro-heatmap")

async def get_edge_macro_heatmap(

    force_refresh: bool = Query(

        False, description="Bypass edge cache and compute fresh"

    ),

):

    """

    Get macro heatmap showing EdgeScore components across all asset classes.



    Returns a grid of scores (macro, COT, sentiment, risk, EdgeScore) for

    each asset class (forex, crypto, nse, index). Useful for dashboard

    visualization showing which markets have the best macro edge.

    """

    try:

        from agents.macro.edge_score_engine import EdgeScoreEngine

        from config.settings import CRYPTO_SYMBOLS, FOREX_SYMBOLS, NSE_SYMBOLS



        engine = (

            EdgeScoreEngine(cache_ttl_seconds=0) if force_refresh else EdgeScoreEngine()

        )

        heatmap = {}



        # Sample one symbol from each asset class to get representative scores

        asset_samples = {

            "forex": FOREX_SYMBOLS[0] if FOREX_SYMBOLS else "EUR/USD",

            "crypto": CRYPTO_SYMBOLS[0] if CRYPTO_SYMBOLS else "BTC/USDT",

            "nse": NSE_SYMBOLS[0] if NSE_SYMBOLS else "RELIANCE",

            "index": "NIFTY",

        }



        for asset_class, symbol in asset_samples.items():

            try:

                breakdown = engine.get_edge_score(symbol, asset_class)

                cache_age_seconds = max(

                    0,

                    int(

                        (

                            datetime.now(timezone.utc) - breakdown.timestamp

                        ).total_seconds()

                    ),

                )

                heatmap[asset_class] = {

                    "macro_score": breakdown.macro_score,

                    "cot_score": breakdown.cot_score,

                    "sentiment_score": breakdown.sentiment_score,

                    "risk_score": breakdown.risk_score,

                    "momentum_score": breakdown.momentum_score,

                    "edge_score": breakdown.edge_score,

                    "sample_symbol": symbol,

                    "cache_hit": breakdown.cache_hit,

                    "cache_age_seconds": cache_age_seconds,

                }

            except Exception as e:

                logger.warning(f"Failed to get EdgeScore for {asset_class}: {e}")

                heatmap[asset_class] = {

                    "macro_score": 0.0,

                    "cot_score": 0.0,

                    "sentiment_score": 0.0,

                    "risk_score": 0.0,

                    "momentum_score": 0.0,

                    "edge_score": 0.0,

                    "sample_symbol": symbol,

                    "error": str(e),

                }



        return {

            "heatmap": heatmap,

            "force_refresh": force_refresh,

            "timestamp": datetime.now(timezone.utc).isoformat(),

        }

    except Exception as e:

        logger.error(f"Failed to generate macro heatmap: {e}")

        return {"error": str(e)}





@router.get("/api/edge/risk-gauge")

async def get_edge_risk_gauge(

    force_refresh: bool = Query(

        False, description="Bypass risk cache and compute fresh"

    ),

):

    """

    Get current risk regime from the Edge Engine.



    Returns risk indicators (VIX, BTC, Nifty, USD/JPY, Gold) and overall

    risk score. Useful for understanding whether the market is in risk-on

    or risk-off mode.

    """

    try:

        from agents.macro.risk_gauge import RiskGauge



        def _vix_trend(change_pct) -> str:

            if change_pct is None:

                return "FLAT"

            if change_pct > 0:

                return "RISING"

            if change_pct < 0:

                return "FALLING"

            return "FLAT"



        gauge = RiskGauge(cache_ttl_seconds=0) if force_refresh else RiskGauge()

        indicators = gauge.fetch_risk_indicators()

        risk_score = gauge.get_risk_score()



        updated_at = indicators.timestamp.isoformat()

        regime = indicators.risk_regime

        vix_trend = _vix_trend(indicators.vix_change_pct)



        indicators_payload = {

            "vix": indicators.vix,

            "vix_change_pct": indicators.vix_change_pct,

            "vix_trend": vix_trend,

            "btc_trend": indicators.btc_trend,

            "nifty_trend": indicators.nifty_trend,

            "usdjpy_level": indicators.usdjpy_level,

            "usdjpy_trend": indicators.usdjpy_trend,

            "gold_trend": indicators.gold_trend,

        }



        cache_age_seconds = max(

            0,

            int((datetime.now(timezone.utc) - indicators.timestamp).total_seconds()),

        )



        return {

            "risk_score": risk_score,

            "regime": regime,

            "indicators": indicators_payload,

            "updated_at": updated_at,

            "cache_age_seconds": cache_age_seconds,

            "force_refresh": force_refresh,

            # Backward-compatible aliases for existing clients.

            "risk_regime": regime,

            "vix_level": indicators.vix,

            "vix_trend": vix_trend,

            "btc_trend": indicators.btc_trend,

            "nifty_trend": indicators.nifty_trend,

            "usdjpy_trend": indicators.usdjpy_trend,

            "gold_trend": indicators.gold_trend,

            "timestamp": updated_at,

        }

    except Exception as e:

        logger.error(f"Failed to get risk gauge: {e}")

        return {"error": str(e)}





@router.get("/api/nse/dashboard")

async def get_nse_dashboard_snapshot():

    """Aggregate NSE dashboard snapshot for faster UI bootstrap."""

    if not svc._movers_cache.get("gainers") and svc._market_data:

        svc._refresh_movers_cache()



    mood = svc._compute_market_mood()

    summary = svc._build_ai_market_summary(mood)

    movers_updated_at = (

        svc._movers_cache.get("updated_at") or datetime.now().isoformat()

    )



    gainers = list((svc._movers_cache.get("gainers") or [])[:7])

    losers = list((svc._movers_cache.get("losers") or [])[:7])



    return {

        "timestamp": datetime.now().isoformat(),

        "last_refresh": svc._last_refresh.isoformat()

        if svc._last_refresh != datetime.min

        else None,

        "loading": svc._loading_status,

        "mood": mood,

        "ai_summary": summary,

        "movers": {

            "gainers": gainers,

            "losers": losers,

            "updated_at": movers_updated_at,

        },

    }





# ── Swing Scanner Endpoint ────────────────────────────────────





@router.get("/api/swing/shortlist")

async def get_swing_shortlist():

    """Returns the pre-computed morning shortlist of A/B grade setups."""

    import json

    import os



    shortlist_path = os.path.abspath(

        os.path.join(

            os.path.dirname(__file__), "..", "data", "swing_shortlist_latest.json"

        )

    )

    if os.path.exists(shortlist_path):

        try:

            with open(shortlist_path, "r", encoding="utf-8") as f:

                raw = json.load(f)

            # JSON stores setups under 'shortlist' key; UI expects 'setups'

            setups = raw.get("shortlist") or raw.get("raw_setups") or []

            return {

                "setups": setups,

                "scan_date": raw.get("scan_date"),

                "generated_at": raw.get("scan_timestamp"),

                "symbols_scanned": raw.get("symbols_scanned", 0),

                "setups_found": raw.get("setups_found", len(setups)),

                "grade_summary": raw.get("grade_summary", {}),

                "message": f"{len(setups)} setups from scan on {raw.get('scan_date', 'unknown')}",

            }

        except Exception as e:

            logger.error("Failed to load swing shortlist: %s", e)

            return {"setups": [], "error": str(e)}

    return {

        "setups": [],

        "message": "Shortlist not generated yet. Run: python scripts/nightly_swing_scan.py",

    }





@router.get("/api/swing/history/{symbol}")

async def get_swing_history(symbol: str):

    """Returns the 30-day trailing swing score history for a specific symbol."""

    import json

    import os



    symbol_upper = symbol.upper()

    hist_path = os.path.abspath(

        os.path.join(

            os.path.dirname(__file__),

            "..",

            "data",

            "swing_history",

            f"{symbol_upper}.json",

        )

    )

    if os.path.exists(hist_path):

        try:

            with open(hist_path, "r", encoding="utf-8") as f:

                return json.load(f)

        except Exception as e:

            logger.error(f"Failed to load swing history for {symbol_upper}: {e}")

            return []

    return []





@router.get("/api/swing-scanner")

async def get_swing_setups():

    """

    SwingSpectrum — serves pre-computed swing trade setups from cache.

    Cache is populated during the background refresh cycle (every few minutes).

    The API never blocks — it always returns immediately from the last known result.

    """

    if svc._swing_cache["symbols_scanned"] == 0 and svc._daily_data_cache:

        await svc._recompute_swing_cache(timeout_sec=30.0)



    # Fallback path: if background refresh did not populate daily cache yet,

    # fetch a small daily subset on demand so Swing tab doesn't stay empty.

    if not svc._swing_cache["real_data"] and not svc._daily_data_cache:

        try:

            daily_sem = asyncio.Semaphore(4)



            async def _fetch_daily_bootstrap(name: str, sem: asyncio.Semaphore):

                async with sem:

                    try:

                        df = await asyncio.wait_for(

                            asyncio.to_thread(fetch_ohlcv, name, "1y", "1d"),

                            timeout=20.0,

                        )

                        if df is not None and not df.empty and len(df) >= 30:

                            svc._daily_data_cache[name] = calculate_indicators(df)

                    except Exception:

                        return



            bootstrap_symbols = svc.TOP_NSE_SYMBOLS[:20]

            await asyncio.gather(

                *[_fetch_daily_bootstrap(sym, daily_sem) for sym in bootstrap_symbols]

            )

            if svc._daily_data_cache:

                await svc._recompute_swing_cache(timeout_sec=45.0)

        except Exception as exc:

            logger.warning("Swing on-demand bootstrap failed: %s", exc)



    if not svc._swing_cache["real_data"]:

        return {

            "setups": [],

            "total_found": 0,

            "symbols_scanned": len(svc._daily_data_cache),

            "message": "Swing data is still warming up. Daily candles are being fetched now; retry in 20-40 seconds.",

            "real_data": False,

            "updated_at": datetime.now().isoformat(),

            "market_context": svc._swing_cache.get("market_context"),

        }



    # Normalize field names: nightly scan JSON uses short keys (entry, sl, t1, rr)

    # but the dashboard grid expects the real-time scanner names (entry_zone, stop_loss, etc.)

    def _normalize_setup(s: dict) -> dict:

        out = dict(s)

        # ── Short key → long key mapping (backwards-compat shim) ──────────────

        # These short aliases come from rank_summary() in ranking_engine.py.

        # The full-key names are what SwingSetup.asdict() and the dashboard expect.

        if "entry" in out and "entry_zone" not in out:

            out["entry_zone"] = out["entry"]

        if "sl" in out and "stop_loss" not in out:

            out["stop_loss"] = out["sl"]

        if "t1" in out and "target_1" not in out:

            out["target_1"] = out["t1"]

        if "t2" in out and "target_2" not in out:

            out["target_2"] = out["t2"]

        if "rr" in out and "risk_reward" not in out:

            out["risk_reward"] = out["rr"]

        # composite_score is the ranking engine's output; strength is the raw scanner value.

        # Prefer explicit strength; fall back to composite_score so STRENGTH column is never blank.

        if "strength" not in out or out["strength"] is None:

            out["strength"] = out.get("composite_score") or 0

        # description is the human-readable note shown at the bottom of each card.

        if not out.get("description"):

            out["description"] = out.get("entry_note") or ""

        # Ensure fib targets default to 0 (swingPickTarget() treats 0 as "no fib").

        if "fib_tp1" not in out:

            out["fib_tp1"] = 0

        if "fib_tp2" not in out:

            out["fib_tp2"] = 0

        # target_2 default (swingPickTarget() falls back to "—" when 0).

        if "target_2" not in out:

            out["target_2"] = 0

        # indicators dict used for vol badge and watchlist logic.

        if "indicators" not in out or out["indicators"] is None:

            out["indicators"] = {}

        return out



    raw_setups = svc._swing_cache["setups"]

    normalized = [_normalize_setup(s) for s in raw_setups]



    return {

        "setups": normalized,

        "total_found": svc._swing_cache["total_found"],

        "symbols_scanned": svc._swing_cache["symbols_scanned"],

        "real_data": True,

        "updated_at": svc._swing_cache["updated_at"],

        "market_context": svc._swing_cache.get("market_context"),

    }





@router.get("/api/swing/history")

async def swing_scan_history(limit: int = Query(30, ge=1, le=100)):

    """Recent SwingSpectrum scan rows (summary, no full payload)."""

    rows = await asyncio.to_thread(

        swing_store.list_recent_scans, max(1, min(int(limit), 100))

    )

    return {"scans": rows, "updated_at": datetime.now().isoformat()}





@router.get("/api/swing/history/{scan_id}")

async def swing_scan_history_detail(scan_id: int):

    """Single persisted scan including full setups payload."""

    row = await asyncio.to_thread(swing_store.load_scan_by_id, int(scan_id))

    if not row:

        return JSONResponse({"detail": "Scan not found"}, status_code=404)

    return row





@router.get("/api/swing/stats")

async def swing_outcome_stats(days: int = Query(30, ge=7, le=365)):

    """Hit-rate style aggregates from SwingSetupOutcome rows."""

    stats = await asyncio.to_thread(swing_store.compute_stats, int(days))

    return {"stats": stats, "updated_at": datetime.now().isoformat()}





@router.post("/api/swing/scan-now")

async def swing_scan_now():

    """Force a full SwingSpectrum recompute (same worker as periodic refresh)."""

    ok = await svc._recompute_swing_cache(timeout_sec=120.0, trigger="manual")

    return {

        "ok": bool(ok),

        "updated_at": svc._swing_cache.get("updated_at"),

        "total_found": svc._swing_cache.get("total_found"),

        "symbols_scanned": svc._swing_cache.get("symbols_scanned"),

    }





@router.post("/api/intraday/scan")

async def intraday_run_scan():

    """Paper-only intraday heuristics on tracked NSE symbols (5m delayed yFinance)."""

    from agents.intraday_engine.scanner import (
        get_nifty_market_context,
        scan_intraday_setups,
    )
    from agents.intraday_engine.risk_manager import should_square_off_now



    symbols = list(svc.TOP_NSE_SYMBOLS or [])



    engine = svc._get_engine()

    stats = engine.get_stats()

    capital = float(

        stats.get("capital")

        or getattr(settings, "INTRADAY_VIRTUAL_CAPITAL", 100000.0)

        or 100000.0

    )

    daily_pnl = float(stats.get("daily_pnl") or 0.0)

    daily_pnl_pct = daily_pnl / max(capital, 1.0)

    portfolio_state = {

        "open_trades": len(engine.get_open_trades()),

        "daily_pnl_pct": daily_pnl_pct,

        "capital": capital,

    }



    sigs = await asyncio.to_thread(scan_intraday_setups, symbols, portfolio_state)

    scan_id = await asyncio.to_thread(

        lambda: intraday_store.save_scan(

            sigs,

            symbols_scanned=len(symbols),

            trigger="api",

        ),

    )

    return {

        "scan_id": scan_id,

        "signals": sigs[:40],

        "count": len(sigs),

        "symbols_scanned": len(symbols),
        "nifty_market_context": await asyncio.to_thread(get_nifty_market_context),
        "should_square_off": should_square_off_now(),

        "updated_at": datetime.now().isoformat(),

        "disclaimer": "Paper / educational — 5m delayed data, ATR-sized risk, and mandatory intraday square-off; not live broker execution.",

    }





@router.get("/api/intraday/latest")

async def intraday_latest():

    row = await asyncio.to_thread(intraday_store.load_latest_scan)
    from agents.intraday_engine.scanner import get_nifty_market_context
    from agents.intraday_engine.risk_manager import should_square_off_now

    nifty_ctx = await asyncio.to_thread(get_nifty_market_context)
    sq_off = should_square_off_now()

    if not row:

        return {
            "signals": [],
            "signal_count": 0,
            "scan_time": None,
            "nifty_market_context": nifty_ctx,
            "should_square_off": sq_off,
        }
    if isinstance(row, dict):
        row["nifty_market_context"] = nifty_ctx
        row["should_square_off"] = sq_off

    return row





@router.get("/api/intraday/live-quote/{symbol}")
async def intraday_live_quote_endpoint(symbol: str):
    """0-delay live quote snapshot (LTP, Day VWAP, RVOL, High, Low) for an NSE symbol."""
    from agents.data_ingestion.nse_live_quote import get_nse_live_quote

    clean_sym = symbol.strip().upper().replace(".NS", "")
    quote = await asyncio.to_thread(get_nse_live_quote, clean_sym)
    if quote is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Live quote unavailable for {symbol}"},
        )
    return quote.to_dict()


@router.get("/api/intraday/stats")

async def intraday_stats_endpoint(days: int = Query(14, ge=1, le=90)):

    return await asyncio.to_thread(intraday_store.compute_stats, int(days))





def _swing_symbol_lookup_message(sym: str, stages: dict) -> str:

    """Explain zero-setup swing symbol lookup using scan stage counters."""

    blocked = stages.get("blocked")

    if blocked == "insufficient_bars":

        return f"Not enough daily history (need 30+ bars) for {sym}."

    if blocked == "no_timestamp":

        return f"Missing last-bar timestamp for {sym}; cannot evaluate bar age."

    if blocked == "stale_bars":

        age = stages.get("bar_age_days")

        mx = stages.get("max_bar_age_days")

        return (

            f"Last daily bar is too old ({age} d > {mx} d) for {sym}; "

            "data may be delayed or the symbol inactive."

        )

    if blocked == "missing_columns":

        return f"Required indicator columns are missing for {sym}."

    if blocked == "bad_atr":

        return f"Invalid ATR or close for {sym}; swing scan skipped."

    if blocked == "scan_error":

        return f"Swing scan failed for {sym}: {stages.get('error', 'unknown')}"

    raw_n = int(stages.get("raw_pattern_count") or 0)

    after_rr = int(stages.get("after_rr_count") or 0)

    after_reg = int(stages.get("after_regime_count") or 0)

    if raw_n == 0:

        return (

            f"No swing pattern matched for {sym} right now (pullback, range break, RSI divergence, "

            "EMA cross, inside bar, BB squeeze, 52w proximity, support reversal, post-result gap). "

            "Daily data is OK; the scanner is strict and may show nothing until rules align."

        )

    if after_rr == 0:

        # Fix 1c: show exact numbers if rr_fail_details available

        fail_details = stages.get("rr_fail_details") or []

        if fail_details:

            d = fail_details[0]

            return (

                f"Pattern mila lekin R:R gate fail hua (minimum {settings.SWING_MIN_RISK_REWARD}x chahiye). "

                f"Pattern: {d.get('pattern','?')} | "

                f"Entry ₹{d.get('entry','?')} → SL ₹{d.get('sl','?')} → TP1 ₹{d.get('tp1','?')} | "

                f"Risk ₹{d.get('risk','?')} | Reward ₹{d.get('reward','?')} | "

                f"Actual R:R {d.get('rr','?')}x (need {d.get('required','?')}x). "

                f"{d.get('reason', '')}"

            )

        return (

            f"Patterns fired for {sym} but none met minimum risk:reward "

            f"({settings.SWING_MIN_RISK_REWARD}x). Enable DEBUG logging for per-setup detail."

        )

    if after_reg == 0:

        return (

            f"Setups for {sym} were removed by the regime gate (benchmark trend vs "

            "LONG/SHORT direction)."

        )

    return (

        "No swing setups passed filters (R:R, regime gate, bar age, pattern rules) "

        f"for {sym}."

    )





@router.get("/api/swing/symbol/{symbol}")

async def get_swing_for_symbol(symbol: str):

    """

    Swing setups for one NSE symbol (same rules as SwingSpectrum).



    Merges the symbol into a copy of the daily cache so sector momentum still

    uses the full tracked universe; fetches 1y daily OHLCV if missing from cache.

    """

    raw = (symbol or "").strip().upper().replace(".NS", "")

    if not raw or len(raw) > 20 or "/" in raw or ".." in raw:

        return JSONResponse(

            status_code=400,

            content={"error": "Invalid symbol", "symbol": raw, "setups": []},

        )



    try:

        ctx = svc._build_swing_market_context()

        merged: dict = dict(svc._daily_data_cache)

        df = merged.get(raw)



        if df is None or getattr(df, "empty", True):

            df_fetched = await asyncio.wait_for(

                asyncio.to_thread(fetch_ohlcv, raw, "1y", "1d"),

                timeout=25.0,

            )

            # fetch_arbitrary_symbol(raw) re-calls fetch_ohlcv when raw is in NSE_SYMBOLS — no real

            # fallback. Use .NS ticker to hit the raw yFinance path and bypass cache/throttle quirks.

            if df_fetched is None or df_fetched.empty or len(df_fetched) < 30:

                if " " not in raw and "/" not in raw:

                    df_fetched = await asyncio.wait_for(

                        asyncio.to_thread(

                            fetch_arbitrary_symbol, f"{raw}.NS", "1y", "1d"

                        ),

                        timeout=30.0,

                    )

            if df_fetched is None or df_fetched.empty or len(df_fetched) < 30:

                df_fetched = await asyncio.wait_for(

                    asyncio.to_thread(fetch_arbitrary_symbol, raw, "1y", "1d"),

                    timeout=30.0,

                )

            if df_fetched is None or df_fetched.empty or len(df_fetched) < 30:

                return JSONResponse(

                    status_code=404,

                    content={

                        "error": (

                            f"No or insufficient daily data for {raw}. "

                            "Check symbol spelling (NSE equity); delisted or very illiquid names may fail."

                        ),

                        "symbol": raw,

                        "setups": [],

                    },

                )

            df = calculate_indicators(df_fetched)

            merged[raw] = df

        elif len(df) < 30:

            df_refetch = None

            if " " not in raw and "/" not in raw:

                df_refetch = await asyncio.wait_for(

                    asyncio.to_thread(fetch_arbitrary_symbol, f"{raw}.NS", "1y", "1d"),

                    timeout=30.0,

                )

            if df_refetch is None or df_refetch.empty or len(df_refetch) < 30:

                df_refetch = await asyncio.wait_for(

                    asyncio.to_thread(fetch_arbitrary_symbol, raw, "1y", "1d"),

                    timeout=30.0,

                )

            if (

                df_refetch is not None

                and not df_refetch.empty

                and len(df_refetch) >= 30

            ):

                df = calculate_indicators(df_refetch)

                merged[raw] = df

            else:

                return JSONResponse(

                    status_code=404,

                    content={

                        "error": f"Insufficient bars in cache for {raw}",

                        "symbol": raw,

                        "setups": [],

                    },

                )



        def _run_one_symbol_scan():

            stages_local: dict = {}

            sector = settings.NSE_SECTOR_MAP.get(raw, "Other")

            momentum = compute_sector_momentum_map(merged).get(sector, 0.0)

            objs = scan_swing_setups(

                df,

                raw,

                sector_momentum=momentum,

                sector=sector,

                market_context=ctx,

                stages_out=stages_local,

                allow_fallback=True,

            )

            return [asdict(s) for s in objs], stages_local



        setups, swing_stages = await asyncio.wait_for(

            asyncio.to_thread(_run_one_symbol_scan),

            timeout=90.0,

        )



        for setup in setups:

            sym_key = setup.get("symbol", "")

            df_live = svc._market_data.get(sym_key)

            if df_live is not None and not df_live.empty:

                try:

                    ltp = float(df_live.iloc[-1]["close"])

                    entry = float(setup.get("entry_zone", 0))

                    setup["current_price"] = round(ltp, 2)

                    if entry > 0:

                        setup["price_vs_entry_pct"] = round(

                            (ltp - entry) / entry * 100, 2

                        )

                    else:

                        setup["price_vs_entry_pct"] = 0.0

                except Exception:

                    setup["current_price"] = None

                    setup["price_vs_entry_pct"] = None

            else:

                setup["current_price"] = None

                setup["price_vs_entry_pct"] = None



        msg = None

        if not setups:

            msg = _swing_symbol_lookup_message(raw, swing_stages)



        # Include a quick technical summary even when no setup fires

        tech_summary = None

        if df is not None and not df.empty and len(df) >= 30:

            try:

                _last = df.iloc[-1]

                _close = float(_last["close"])

                _rsi = (

                    float(_last.get("RSI_14", 0))

                    if pd.notna(_last.get("RSI_14"))

                    else None

                )

                _ema20 = (

                    float(_last.get("EMA_20", 0))

                    if pd.notna(_last.get("EMA_20"))

                    else None

                )

                _ema50 = (

                    float(_last.get("EMA_50", 0))

                    if pd.notna(_last.get("EMA_50"))

                    else None

                )

                _atr = (

                    float(_last.get("ATR_14", 0))

                    if pd.notna(_last.get("ATR_14"))

                    else None

                )

                _trend = (

                    "UPTREND"

                    if _ema20 and _ema50 and _ema20 > _ema50

                    else "DOWNTREND"

                    if _ema20 and _ema50

                    else "UNKNOWN"

                )

                tech_summary = {

                    "close": round(_close, 2),

                    "rsi": round(_rsi, 1) if _rsi else None,

                    "ema20": round(_ema20, 2) if _ema20 else None,

                    "ema50": round(_ema50, 2) if _ema50 else None,

                    "atr": round(_atr, 2) if _atr else None,

                    "trend": _trend,

                    "bars": len(df),

                }

            except Exception:

                pass



        # Provide active symbols from cached full scan for "try these" UX

        active_symbols = []

        if not setups and svc._swing_cache.get("setups"):

            seen = set()

            for cs in svc._swing_cache["setups"]:

                sym = cs.get("symbol", "")

                if sym and sym not in seen:

                    seen.add(sym)

                    active_symbols.append(

                        {

                            "symbol": sym,

                            "direction": cs.get("direction", ""),

                            "pattern": cs.get("pattern", ""),

                            "strength": cs.get("strength", 0),

                        }

                    )

                if len(active_symbols) >= 8:

                    break



        return {

            "symbol": raw,

            "setups": setups,

            "total_found": len(setups),

            "symbols_scanned": len(merged),

            "real_data": True,

            "lookup": True,

            "updated_at": datetime.now(timezone.utc).isoformat(),

            "market_context": ctx,

            "message": msg,

            "swing_scan": swing_stages,

            "tech_summary": tech_summary,

            "active_symbols": active_symbols,

        }

    except asyncio.TimeoutError:

        logger.warning("Swing symbol lookup timed out: %s", raw)

        return JSONResponse(

            status_code=504,

            content={

                "error": "Scan timed out — try again",

                "symbol": raw,

                "setups": [],

            },

        )

    except Exception as exc:

        logger.error("Swing symbol lookup failed for %s: %s", raw, exc)

        return JSONResponse(

            status_code=500,

            content={"error": str(exc), "symbol": raw, "setups": []},

        )





# ── Swing Scanner Debug Endpoint ──────────────────────────────





@router.get("/api/swing-debug")

async def get_swing_debug(symbol: str = None):

    """

    Debug endpoint — shows per-symbol scan results before/after filtering.

    Use ?symbol=RELIANCE to debug one stock, or omit for a summary of all.

    """

    from agents.feature_engine.indicators import calculate_indicators

    from agents.market_intelligence.swing_scanner import scan_swing_setups



    ctx = svc._build_swing_market_context()

    results = []



    data = dict(svc._daily_data_cache)

    if symbol:

        sym_upper = symbol.upper()

        data = {k: v for k, v in data.items() if k.upper() == sym_upper}

        if not data:

            # Try fetching it on-demand

            try:

                df = await asyncio.wait_for(

                    asyncio.to_thread(fetch_ohlcv, sym_upper, "1y", "1d"), timeout=15.0

                )

                if df is not None and not df.empty:

                    data = {sym_upper: calculate_indicators(df)}

            except Exception as exc:

                return {"error": str(exc)}



    for sym, df in data.items():

        if df is None or df.empty:

            results.append({"symbol": sym, "status": "empty_df", "bars": 0})

            continue



        last = df.iloc[-1]

        atr = float(last.get("ATR_14", 0) or 0)

        close = float(last.get("close", 0) or 0)

        rsi = float(last.get("RSI_14", 0) or 0)

        ema20 = float(last.get("EMA_20", 0) or 0)

        ema50 = float(last.get("EMA_50", 0) or 0)



        # Run scan WITHOUT regime gate so we see raw results

        raw = scan_swing_setups(

            df, sym, sector_momentum=0.0, sector="Other", market_context=None

        )

        # Run scan WITH market context

        gated = scan_swing_setups(

            df, sym, sector_momentum=0.0, sector="Other", market_context=ctx

        )



        results.append(

            {

                "symbol": sym,

                "bars": len(df),

                "close": round(close, 2),

                "atr": round(atr, 2),

                "rsi": round(rsi, 1),

                "ema20": round(ema20, 2),

                "ema50": round(ema50, 2),

                "raw_setups_before_gate": len(raw),

                "setups_after_regime_gate": len(gated),

                "raw_patterns": [

                    {

                        "pattern": s.pattern,

                        "direction": s.direction,

                        "rr": s.risk_reward,

                        "strength": s.strength,

                    }

                    for s in raw

                ],

                "gated_patterns": [

                    {

                        "pattern": s.pattern,

                        "direction": s.direction,

                        "rr": s.risk_reward,

                    }

                    for s in gated

                ],

                "dropped_by_regime_gate": len(raw) - len(gated),

            }

        )



    results.sort(key=lambda x: x.get("raw_setups_before_gate", 0), reverse=True)

    return {

        "market_context": ctx,

        "symbols_checked": len(results),

        "total_raw_setups": sum(r.get("raw_setups_before_gate", 0) for r in results),

        "total_after_gate": sum(r.get("setups_after_regime_gate", 0) for r in results),

        "settings": {

            "SWING_MIN_RISK_REWARD": settings.SWING_MIN_RISK_REWARD,

            "SWING_REGIME_GATE_ENABLED": settings.SWING_REGIME_GATE_ENABLED,

            "SWING_BLOCK_LONG_IN_BEARISH_TREND": settings.SWING_BLOCK_LONG_IN_BEARISH_TREND,

            "daily_cache_size": len(svc._daily_data_cache),

        },

        "results": results[:20] if not symbol else results,

    }





# ── Index Mover Endpoint ─────────────────────────────────────





@router.get("/api/index-mover/{index_name}")

async def get_index_movers(index_name: str):

    """

    IndexMover — shows which stocks are contributing the most to

    Nifty 50 / BankNifty movement (up or down).

    """

    # Normalize input

    index_map = {

        "nifty": "NIFTY 50",

        "nifty50": "NIFTY 50",

        "nifty-50": "NIFTY 50",

        "nifty_50": "NIFTY 50",

        "NIFTY 50": "NIFTY 50",

        "banknifty": "BANKNIFTY",

        "bank-nifty": "BANKNIFTY",

        "bank_nifty": "BANKNIFTY",

        "BANKNIFTY": "BANKNIFTY",

    }

    clean_name = index_map.get(index_name.replace(" ", "").lower(), index_name)

    if clean_name not in ("NIFTY 50", "BANKNIFTY"):

        return {"error": f"Unknown index: {index_name}. Use 'nifty50' or 'banknifty'."}



    try:

        data = await asyncio.to_thread(

            calculate_index_movers, clean_name, svc._market_data

        )

        # Remove internal cache key before sending to client

        data.pop("_fetched_at", None)

    except Exception as e:

        logger.error("Index mover failed for %s: %s", clean_name, e)

        data = {"error": str(e)}



    return data





# ── Breakout Scanner Endpoint ────────────────────────────────





@router.get("/api/breakouts")

async def get_breakouts(

    min_strength: int = Query(30, ge=0, le=100),

):

    """

    BreakoutScanner — scans all tracked symbols for volume-confirmed

    10-day, 50-day, and 52-week breakouts.

    """

    if not svc._daily_data_cache:

        return {

            "all": [],

            "message": "Daily data not yet loaded.",

            "real_data": False,

            "updated_at": datetime.now().isoformat(),

        }



    try:

        data = await asyncio.to_thread(

            breakout_scan_all, svc._daily_data_cache, min_strength

        )

    except Exception as e:

        logger.error("Breakout scanner failed: %s", e)

        data = {"all": [], "total_breakouts": 0, "real_data": False}



    data["updated_at"] = datetime.now().isoformat()

    return data





# ── WebSocket Endpoint for Real-time Updates ──────────────





@router.websocket("/ws/prices")

async def websocket_prices(websocket: WebSocket):

    """

    WebSocket endpoint for real-time price and signal updates.



    Sends payload under key "prices" to match the frontend handler:

      if (data.prices) { updateLivePrices(data.prices) }



    Pushes price updates every 5 seconds and a lightweight ping every 20 seconds

    to keep the connection alive through proxies and Render's load balancer.

    """

    await websocket.accept()

    svc._ws_clients.append(websocket)

    logger.info("WebSocket client connected (%d total)", len(svc._ws_clients))



    last_ping_at = time.time()

    # Slightly aggressive ping helps some proxies / Render LB paths that idle-out WS.

    _PING_INTERVAL = 15  # seconds between server-side pings



    try:

        while True:

            now_ts = time.time()



            # Server-side ping to keep connection alive through proxies / Render LB.

            if now_ts - last_ping_at >= _PING_INTERVAL:

                try:

                    await websocket.send_json(

                        {"type": "ping", "ts": datetime.now().isoformat()}

                    )

                    last_ping_at = now_ts

                except Exception:

                    break  # Connection gone — exit loop cleanly



            update = {

                "type": "price_update",

                "timestamp": datetime.now().isoformat(),

                "prices": {},

            }



            for symbol in svc._market_data:

                df = svc._market_data[symbol]

                if df.empty:

                    continue

                last = df.iloc[-1]

                update["prices"][symbol] = {

                    "close": round(float(last["close"]), 2),

                    "high": round(float(last["high"]), 2),

                    "low": round(float(last["low"]), 2),

                    "rsi": round(float(last["RSI_14"]), 1)

                    if pd.notna(last.get("RSI_14"))

                    else None,

                    "signal": str(last.get("signal", "HOLD")),

                    "regime": svc._regime_cache.get(symbol, "RANGING"),

                }



            engine = svc._get_engine()

            stats = engine.get_stats()

            update["stats"] = {

                "capital": stats.get("capital", 100000),
        "open_trades": stats.get("open_trades", len(engine.get_open_trades())),

                "total_pnl": stats.get("total_pnl", 0),

                "open_trades": len(engine.get_open_trades()),

            }



            # Mood computation can be CPU-heavy; keep it off the event loop so HTTP

            # health checks and other clients stay responsive on small Render instances.

            update["market_mood"] = await asyncio.to_thread(svc._compute_market_mood)

            update["feed_status"] = svc._get_feed_status()



            await websocket.send_json(update)

            await asyncio.sleep(5)



    except WebSocketDisconnect:

        pass

    except Exception as e:

        logger.error("WebSocket error: %s", e)

    finally:

        if websocket in svc._ws_clients:

            svc._ws_clients.remove(websocket)

        logger.info(

            "WebSocket client disconnected (%d remaining)", len(svc._ws_clients)

        )





@router.post("/api/refresh")

async def refresh_data():

    """Force refresh of all market data."""

    await svc._refresh_market_data()

    return {

        "status": "ok",

        "symbols": len(svc._market_data),

        "updated_at": datetime.now().isoformat(),

    }





# ── Search Endpoints ─────────────────────────────────────





@router.get("/api/search/symbols")

async def search_symbol_list(q: str = Query("", min_length=0, max_length=30)):

    """

    Search available symbols by name/ticker (NSE + Crypto).



    Phase 1: Instant match against pre-loaded NSE_SYMBOLS + crypto list.

    Phase 2: If fewer than 3 pre-loaded results, dynamically validate

             '{QUERY}.NS' via yFinance fast_info (~0.3s) for any NSE stock

             not in the pre-loaded map (e.g. PRECWIRE, NAUKRI, etc.).

    """

    results = search_symbols(q)

    for r in results:

        sym = r["symbol"]

        r["tracked"] = sym in svc._market_data

        if not r["tracked"] and r.get("type") == "CRYPTO":

            r["tracked"] = (

                sym in svc._market_data or sym.replace("/", "-") in svc._market_data

            )



    # ── Dynamic NSE validation ────────────────────────────────────────────

    # Only run if the query looks like a stock ticker (not crypto) and we

    # have fewer than 3 pre-loaded matches — avoids unnecessary yf calls.

    q_clean = q.upper().strip()

    already_found = {

        r["ticker"].replace(".NS", "") for r in results if r.get("type") == "NSE"

    }

    # Only resolve q_clean as a raw yFinance NSE ticker when the static map had

    # zero NSE hits. Otherwise partial queries like "INF" could add INF.NS while

    # INFY already matched — wrong instrument for SwingSpectrum lookup.

    nse_hits = sum(1 for r in results if r.get("type") == "NSE")

    is_likely_nse = (

        nse_hits == 0

        and "/" not in q_clean

        and len(q_clean) >= 3

        and not any(c in q_clean for c in ["BTC", "ETH", "SOL", "BNB", "USDT"])

    )



    if is_likely_nse and q_clean not in already_found:

        nse_ticker = f"{q_clean}.NS"

        try:

            info = await asyncio.to_thread(validate_nse_symbol, nse_ticker)

            if info:

                # Symbol is valid — add as dynamic suggestion (not pre-tracked)

                results.insert(

                    0,

                    {

                        "symbol": q_clean,

                        "ticker": nse_ticker,

                        "type": "NSE",

                        "tracked": q_clean in svc._market_data,

                        "dynamic": True,

                        "price": info["price"],

                        "exchange": info.get("exchange", "NSE"),

                    },

                )

        except Exception as e:

            logger.debug("Dynamic NSE validate failed for %s: %s", nse_ticker, e)



    # ── Inject Opportunity Stocks ──────────────────────────────────────

    if q_clean:

        # Prepend any Opportunity Stocks that match the query

        for opp in reversed(opportunity_stocks):

            if q_clean in opp["symbol"].upper():

                # Avoid duplicates

                if not any(r["symbol"] == opp["symbol"] for r in results):

                    results.insert(

                        0,

                        {

                            "symbol": opp["symbol"],

                            "ticker": f"{opp['symbol']}.NS",

                            "type": "NSE",

                            "tracked": opp["symbol"] in svc._market_data,

                            "dynamic": True,

                            "opportunity": True,

                            "signal": opp["signal"],

                        },

                    )



    return {"results": results[:50], "query": q}





@router.get("/api/screener/opportunities")

async def get_screener_opportunities():

    """Return top 5 stock opportunities found by the screener."""

    return {"opportunities": opportunity_stocks}





@router.get("/api/morning-scan")

async def get_morning_scan():

    """Return the latest persisted morning scan results (max 10)."""

    session = SessionLocal()

    try:

        latest = (

            session.query(MorningScan.scan_time)

            .order_by(MorningScan.scan_time.desc())

            .first()

        )



        if not latest:

            return {

                "scan_time": None,

                "count": 0,

                "results": [],

            }



        latest_scan_time = latest[0]

        rows = (

            session.query(MorningScan)

            .filter(MorningScan.scan_time == latest_scan_time)

            .order_by(MorningScan.confidence_score.desc())

            .limit(10)

            .all()

        )



        results = []

        for row in rows:

            results.append(

                {

                    "symbol": row.symbol,

                    "entry_price": float(row.entry_price),

                    "stop_loss": float(row.stop_loss),

                    "target": float(row.target_price),

                    "confidence_score": float(row.confidence_score),

                    "reason": row.reason,

                    "close": float(row.close),

                    "rsi": float(row.rsi),

                    "macd": float(row.macd),

                    "macd_signal": float(row.macd_signal),

                    "ema_20": float(row.ema_20),

                    "ema_50": float(row.ema_50),

                    "ema_200": float(row.ema_200),

                    "volume": float(row.volume),

                    "avg_volume_20": float(row.avg_volume_20),

                    "volume_ratio": float(row.volume_ratio),

                    "scan_time": row.scan_time.isoformat(),

                    "scan_date": row.scan_date.isoformat(),

                }

            )



        return {

            "scan_time": latest_scan_time.isoformat() if latest_scan_time else None,

            "count": len(results),

            "results": results,

        }

    except Exception as e:

        logger.error("Morning scan API error: %s", e)

        return JSONResponse(

            status_code=500,

            content={

                "scan_time": None,

                "count": 0,

                "results": [],

                "error": "Failed to fetch morning scan results",

            },

        )

    finally:

        session.close()





@router.get("/api/search/quote/{symbol}")

async def search_quote(symbol: str):

    """

    Get full quote (OHLCV + indicators + signal) for any symbol.

    Supports NSE stocks (yFinance), crypto pairs (CCXT/Binance),

    and any global ticker.



    Auto-appends ".NS" suffix for unrecognized Indian stock symbols.

    """

    clean = symbol.replace("-", "/").replace("_", " ")

    # Strip any .NS suffix the caller may have included (normalize to display name format)

    clean_base = clean.upper().replace(".NS", "").strip()



    # Check if already in cache (try both the clean name and the original symbol)

    if clean in svc._market_data:

        df = svc._market_data[clean]

    elif clean_base in svc._market_data:

        df = svc._market_data[clean_base]

        clean = clean_base

    else:

        df = None



        # ── Try crypto via CCXT first ─────────────────

        is_crypto = "/" in clean and any(

            clean.upper().endswith(f"/{q}")

            for q in ("USDT", "BTC", "ETH", "BUSD", "USDC")

        )

        if is_crypto:

            try:

                from agents.data_ingestion.binance_fetcher import fetch_historical



                df = fetch_historical(clean.upper(), "1h", since_days=7)

                if df is None or df.empty:

                    df = None

                else:

                    htf_df = fetch_historical(clean.upper(), "1d", since_days=60)

                    df = calculate_indicators(df)

                    if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:

                        htf_df = calculate_indicators(htf_df)

                        htf_df = classify_trend(htf_df)

                        df = propagate_trend_to_ltf(df, htf_df)

                    else:

                        df = classify_trend(df)

                    regime = detect_regime(df)

                    svc._regime_cache[clean] = regime.value

                    df = generate_signals(df, regime=regime)

                    svc._market_data[clean] = df

                    svc._signal_cache[clean] = df

            except Exception as e:

                logger.warning("Crypto fetch failed for %s: %s", clean, e)

                df = None



        # ── Fallback to yFinance (NSE + global) ──────

        if df is None:

            # 1st attempt: raw symbol (handles known tickers, AAPL, etc.)

            df = await asyncio.to_thread(fetch_arbitrary_symbol, clean)



            # 2nd attempt: try as NSE stock with .NS suffix (e.g. PRECWIRE → PRECWIRE.NS)

            if (df is None or df.empty) and "/" not in clean:

                nse_ticker = f"{clean_base}.NS"

                logger.info("Trying NSE suffix: %s", nse_ticker)

                df = await asyncio.to_thread(fetch_arbitrary_symbol, nse_ticker)

                if df is not None and not df.empty:

                    clean = clean_base  # Use clean display name for cache key



            if df is None or df.empty:

                return JSONResponse(

                    status_code=404,

                    content={

                        "error": f"Symbol '{symbol}' not found on NSE or yFinance. "

                        f"Tried '{clean_base}.NS'. Check the symbol and try again."

                    },

                )

            df = calculate_indicators(df)

            htf_df = await asyncio.to_thread(fetch_arbitrary_symbol, clean, "60d", "1d")

            if htf_df is None or htf_df.empty:

                # For dynamic NSE symbols, also try with .NS for HTF

                nse_htf = f"{clean_base}.NS"

                htf_df = await asyncio.to_thread(

                    fetch_arbitrary_symbol, nse_htf, "60d", "1d"

                )

            if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:

                htf_df = calculate_indicators(htf_df)

                htf_df = classify_trend(htf_df)

                df = propagate_trend_to_ltf(df, htf_df)

            else:

                df = classify_trend(df)

            regime = detect_regime(df)

            svc._regime_cache[clean] = regime.value

            df = generate_signals(df, regime=regime)

            svc._market_data[clean] = df

            svc._signal_cache[clean] = df



    last = df.iloc[-1]



    # Build quote response

    sig = svc._get_signal_for_symbol(clean)



    # Recent OHLCV data (last 100 bars)

    tail = df.tail(100)

    chart_data = []

    for _, row in tail.iterrows():

        chart_data.append(

            {

                "timestamp": row["timestamp"].isoformat(),

                "open": round(float(row["open"]), 2),

                "high": round(float(row["high"]), 2),

                "low": round(float(row["low"]), 2),

                "close": round(float(row["close"]), 2),

                "volume": round(float(row["volume"]), 0),

                "rsi": round(float(row["RSI_14"]), 1)

                if pd.notna(row.get("RSI_14"))

                else None,

                "ema_20": round(float(row["EMA_20"]), 2)

                if pd.notna(row.get("EMA_20"))

                else None,

                "ema_50": round(float(row["EMA_50"]), 2)

                if pd.notna(row.get("EMA_50"))

                else None,

                "vwap": round(float(row["VWAP"]), 2)

                if pd.notna(row.get("VWAP"))

                else None,

                "macd_hist": round(float(row["MACD_hist"]), 4)

                if pd.notna(row.get("MACD_hist"))

                else None,

                "signal": str(row.get("signal", "HOLD"))

                if "signal" in row.index

                else "HOLD",

            }

        )



    # Compute MACD histogram for current bar

    macd_hist = (

        round(float(last["MACD_hist"]), 4) if pd.notna(last.get("MACD_hist")) else None

    )



    return {

        "symbol": clean,

        "close": round(float(last["close"]), 2),

        "high": round(float(last["high"]), 2),

        "low": round(float(last["low"]), 2),

        "rsi": round(float(last["RSI_14"]), 1)

        if pd.notna(last.get("RSI_14"))

        else None,

        "macd_hist": macd_hist,

        "regime": svc._regime_cache.get(clean, "RANGING"),

        "signal": sig,

        "chart": chart_data,

    }





# ── AI / Ensemble Endpoints ──────────────────────────────





@router.get("/api/ensemble/validate")

async def ensemble_validate(

    symbol: str = Query(default="BTC/USDT"),

    timeframe: str = Query(default="4h"),

    days: int = Query(default=365),

    n_folds: int = Query(default=5),

):

    """

    Run walk-forward validation of the ensemble predictor.



    Tests prediction accuracy on historical data without lookahead bias.

    Each fold trains on older data and tests on newer data.



    Returns per-fold accuracy, precision, high-confidence accuracy,

    estimated profit factor, and calibration score.



    Note: Takes 2-5 minutes depending on dataset size.

    """

    import asyncio

    from concurrent.futures import ThreadPoolExecutor



    async def _run():

        loop = asyncio.get_event_loop()

        with ThreadPoolExecutor(max_workers=1) as ex:

            from agents.backtester.ensemble_validator import (

                result_to_dict,

                run_walk_forward_validation,

            )



            result = await loop.run_in_executor(

                ex,

                lambda: run_walk_forward_validation(

                    symbol=symbol,

                    timeframe=timeframe,

                    days=days,

                    n_folds=n_folds,

                ),

            )

        return result_to_dict(result)



    try:

        return await _run()

    except Exception as e:

        return JSONResponse(status_code=500, content={"error": str(e)})





@router.get("/api/ensemble/status")

async def ensemble_status():

    """

    Return status of all AI models in the ensemble.

    Shows which models are active, their last training time, and current

    Fear & Greed sentiment reading.

    """

    status = {

        "statistical": {"active": True, "note": "Always active — no training required"},

        "xgboost": {"active": False, "model_path": "models/xgb_model.pkl"},

        "lstm": {"active": False, "model_path": "models/best_model.pt"},

        "sentiment": {"active": False, "fear_greed": None},

    }



    # XGBoost status

    try:

        import os

        import pickle



        xgb_path = "models/xgb_model.pkl"

        if os.path.exists(xgb_path):

            mtime = os.path.getmtime(xgb_path)

            size_kb = round(os.path.getsize(xgb_path) / 1024, 1)

            with open(xgb_path, "rb") as f:

                meta = pickle.load(f)

            status["xgboost"] = {

                "active": True,

                "model_path": xgb_path,

                "n_samples": meta.get("n_samples"),

                "symbols": meta.get("symbols", ["BTC/USDT"]),

                "timeframe": meta.get("timeframe", "4h"),

                "size_kb": size_kb,

                "trained_at": datetime.fromtimestamp(mtime).isoformat(),

            }

    except Exception as e:

        status["xgboost"]["error"] = str(e)



    # LSTM status

    try:

        import json as _json

        import os



        lstm_path = "models/best_model.pt"

        meta_path = "models/model_meta.json"

        if os.path.exists(lstm_path) and os.path.exists(meta_path):

            with open(meta_path) as f:

                lstm_meta = _json.load(f)

            status["lstm"] = {

                "active": True,

                "model_path": lstm_path,

                "val_accuracy": lstm_meta.get("val_accuracy"),

                "input_size": lstm_meta.get("input_size"),

                "trained_at": lstm_meta.get("trained_at"),

            }

    except Exception as e:

        status["lstm"]["error"] = str(e)



    # Sentiment (Fear & Greed)

    try:

        from agents.ai_engine.crypto_sentiment import get_fear_greed_index



        fg = get_fear_greed_index(timeout=3)

        status["sentiment"] = {

            "active": True,

            "fear_greed_value": fg["value"],

            "classification": fg["classification"],

            "sentiment_bias": fg["sentiment_bias"],

            "contrarian_conf": fg["contrarian_conf"],

            "source": fg.get("source", "api"),

        }

    except Exception as e:

        status["sentiment"] = {"active": False, "error": str(e)}



    active_models = sum(

        1 for k, v in status.items() if isinstance(v, dict) and v.get("active")

    )

    return {

        "ensemble_active": True,

        "active_model_count": active_models,

        "models": status,

        "timestamp": datetime.now(timezone.utc).isoformat(),

    }





@router.post("/api/retrain")

async def retrain_xgboost(

    symbols: str = Query(

        default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT",

        description="Comma-separated crypto symbols to train on",

    ),

    timeframe: str = Query(default="4h", description="Candle timeframe: 1h, 4h, 1d"),

    days: int = Query(default=730, description="Days of history to use"),

    forward_bars: int = Query(

        default=12, description="Forward bars for label generation"

    ),

    min_return_pct: float = Query(

        default=0.5, description="Minimum % move to count as signal"

    ),

):

    """

    Retrain the XGBoost model on fresh crypto data.



    Runs asynchronously in a background thread.

    Returns a job_id that can be polled at /api/retrain/status.



    Usage from dashboard:

        POST /api/retrain

        POST /api/retrain?symbols=BTC/USDT,ETH/USDT&timeframe=4h&days=730

    """

    import asyncio

    from concurrent.futures import ThreadPoolExecutor



    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]



    async def _run_training():

        loop = asyncio.get_event_loop()

        with ThreadPoolExecutor(max_workers=1) as executor:

            result = await loop.run_in_executor(

                executor,

                svc._train_xgboost_sync,

                symbol_list,

                timeframe,

                days,

                forward_bars,

                min_return_pct,

            )

        return result



    # Store job status

    job_id = f"retrain_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    svc._retrain_jobs[job_id] = {

        "status": "running",

        "started_at": datetime.now(timezone.utc).isoformat(),

    }



    async def _background():

        try:

            result = await _run_training()

            svc._retrain_jobs[job_id] = {

                "status": "completed",

                "result": result,

                "completed_at": datetime.now(timezone.utc).isoformat(),

            }

            logger.info("XGBoost retrain complete: %s", result)

        except Exception as e:

            svc._retrain_jobs[job_id] = {

                "status": "failed",

                "error": str(e),

                "completed_at": datetime.now(timezone.utc).isoformat(),

            }

            logger.error("XGBoost retrain failed: %s", e)



    asyncio.create_task(_background())

    return {

        "job_id": job_id,

        "status": "started",

        "symbols": symbol_list,

        "timeframe": timeframe,

    }





@router.get("/api/retrain/status")

async def retrain_status():

    """Return status of all retrain jobs."""

    return {"jobs": svc._retrain_jobs}





@router.get("/api/forex/rates")

async def get_forex_rates():

    """Return live rates for all configured forex pairs."""

    try:

        rates = await asyncio.to_thread(get_all_forex_rates)

        return {

            "rates": rates,

            "updated_at": datetime.now().isoformat(),

            "source": "twelve_data",

        }

    except Exception as e:

        logger.error("Forex rates error: %s", e)

        return JSONResponse(status_code=503, content={"error": str(e), "rates": {}})





@router.get("/api/forex/signals")

async def get_forex_signals():

    """Return cached forex swing setups sorted by strength score."""

    try:

        setups = await asyncio.to_thread(scan_all_forex)

        return {

            "setups": [s.to_dict() for s in setups],

            "count": len(setups),

            "scanning_in_background": is_forex_scanning(),

            "updated_at": datetime.now().isoformat(),

        }

    except Exception as e:

        logger.error("Forex signals error: %s", e)

        return JSONResponse(status_code=503, content={"error": str(e), "setups": []})





@router.get("/api/forex/signal/{symbol}")

async def get_forex_signal(symbol: str):

    """

    Return a fresh single-pair forex setup.



    symbol format in URL: EUR_USD  (underscore; converted to EUR/USD internally)

    """

    try:

        display_symbol = symbol.replace("_", "/")

        df_4h = await asyncio.to_thread(get_forex_ohlcv, display_symbol, "4h", 200)

        df_daily = await asyncio.to_thread(get_forex_ohlcv, display_symbol, "1day", 100)

        setup = await asyncio.to_thread(

            scan_forex_setup, display_symbol, df_4h, df_daily

        )

        if setup is None:

            return {

                "symbol": display_symbol,

                "setup": None,

                "message": "No valid setup found",

            }

        return {"symbol": display_symbol, "setup": setup.to_dict()}

    except Exception as e:

        logger.error("Forex single signal error for %s: %s", symbol, e)

        return JSONResponse(status_code=503, content={"error": str(e), "setup": None})





@router.get("/api/forex/sessions")

async def get_forex_sessions():

    """

    Return current session status for all forex sessions.



    Includes: active/inactive flag, best pairs, and minutes until next session open.

    """

    try:

        from zoneinfo import ZoneInfo



        from agents.data_ingestion.forex_fetcher import is_session_active



        _IST = ZoneInfo("Asia/Kolkata")

        now_ist = datetime.now(_IST)

        now_time = now_ist.strftime("%H:%M")

        result = {}



        from config import settings as _s



        def _minutes_until(target_time: str) -> int:

            th, tm = map(int, target_time.split(":"))

            nh, nm = map(int, now_time.split(":"))

            target_mins = th * 60 + tm

            now_mins = nh * 60 + nm

            diff = target_mins - now_mins

            if diff <= 0:

                diff += 1440  # next day

            return diff



        def _in_session(start: str, end: str, current: str) -> bool:

            if start <= end:

                return start <= current < end

            return current >= start or current < end



        for sess_name, sess_data in _s.FOREX_SESSIONS.items():

            start = sess_data["start"]

            end = sess_data["end"]

            active = _in_session(start, end, now_time)

            minutes_to_open = 0 if active else _minutes_until(start)

            result[sess_name] = {

                "active": active,

                "start_ist": start,

                "end_ist": end,

                "best_pairs": sess_data.get("best_pairs", []),

                "minutes_to_open": minutes_to_open,

            }



        return {

            "sessions": result,

            "current_ist": now_time,

            "updated_at": now_ist.isoformat(),

        }

    except Exception as e:

        logger.error("Forex sessions error: %s", e)

        return JSONResponse(status_code=503, content={"error": str(e), "sessions": {}})





@router.get("/api/forex/ohlcv/{symbol}")

async def get_forex_ohlcv_endpoint(

    symbol: str,

    interval: str = Query("4h", description="Candle interval: 1h, 4h, 1day"),

    bars: int = Query(100, ge=10, le=500, description="Number of candles to return"),

):

    """

    Return OHLCV candles for a forex pair (for charting).



    symbol format in URL: EUR_USD  (underscore; converted to EUR/USD internally)

    """

    try:

        display_symbol = symbol.replace("_", "/")

        df = await asyncio.to_thread(get_forex_ohlcv, display_symbol, interval, bars)

        if df.empty:

            return {"symbol": display_symbol, "interval": interval, "candles": []}

        records = df.assign(

            timestamp=df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        ).to_dict(orient="records")

        return {

            "symbol": display_symbol,

            "interval": interval,

            "candles": records,

            "count": len(records),

        }

    except Exception as e:

        logger.error("Forex OHLCV error for %s: %s", symbol, e)

        return JSONResponse(status_code=503, content={"error": str(e), "candles": []})





@router.get("/")

async def serve_dashboard():

    """Serve the main dashboard page."""

    index = os.path.join(STATIC_DIR, "index.html")

    if os.path.exists(index):

        return FileResponse(index, headers=_DASH_HTML_HEADERS)

    return {"message": "Dashboard not found. Place files in /dashboard/"}





@router.get("/landing")

async def serve_landing():

    """Serve the marketing landing page."""

    landing = os.path.join(STATIC_DIR, "landing.html")

    if os.path.exists(landing):

        return FileResponse(landing, headers=_DASH_HTML_HEADERS)

    return FileResponse(

        os.path.join(STATIC_DIR, "index.html"), headers=_DASH_HTML_HEADERS

    )





@router.get("/crypto")

@router.get("/crypto/")

async def serve_crypto():

    """Serve the live crypto dashboard page."""

    crypto = os.path.join(STATIC_DIR, "crypto.html")

    if os.path.exists(crypto):

        return FileResponse(crypto, headers=_DASH_HTML_HEADERS)

    return FileResponse(

        os.path.join(STATIC_DIR, "index.html"), headers=_DASH_HTML_HEADERS

    )





@router.get("/api/crypto/news")

async def get_crypto_news(limit: int = Query(12, ge=3, le=40)):

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

    Phase 6 – Aggregated crypto cockpit intelligence payload.

    Mirrors /api/overview/intelligence contract for frontend reuse.

    Includes: top_setups, avoid_list, market_guardrails, futures_context, theme_leadership.

    """

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





@router.get("/api/crypto/prediction")

async def get_crypto_prediction(timeframe: str = Query(default="1h")):

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

            return 100.0

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





@router.get("/api/nse/news")

async def get_nse_news(limit: int = Query(12, ge=3, le=40)):

    """Fetch latest Indian market headlines from free RSS sources."""

    feeds = [

        (

            "Google News NSE",

            "https://news.google.com/rss/search?q=NSE+stock+market+India&hl=en-IN&gl=IN&ceid=IN:en",

        ),

        (

            "Google News Nifty",

            "https://news.google.com/rss/search?q=NIFTY+50+India+markets&hl=en-IN&gl=IN&ceid=IN:en",

        ),

        (

            "Economic Times Markets",

            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",

        ),

        (

            "Moneycontrol Markets",

            "https://www.moneycontrol.com/rss/marketreports.xml",

        ),

    ]



    def _norm_text(node: ET.Element | None) -> str:

        if node is None:

            return ""

        txt = (node.text or "").strip()

        return " ".join(txt.split())



    def _fetch_feed(source: str, url: str) -> list[dict]:

        last_exc: Exception | None = None

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

        key = f"NSE:{source}"

        backoff_until = _NEWS_SOURCE_BACKOFF_UNTIL.get(key, 0.0)

        if now_ts < backoff_until:

            failed_sources.append(source)

            continue

        try:

            all_news.extend(await asyncio.to_thread(_fetch_feed, source, url))

            sources_ok.append(source)

            _NEWS_SOURCE_FAIL_COUNT.pop(key, None)

            _NEWS_SOURCE_BACKOFF_UNTIL.pop(key, None)

        except Exception as exc:

            fails = _NEWS_SOURCE_FAIL_COUNT.get(key, 0) + 1

            _NEWS_SOURCE_FAIL_COUNT[key] = fails

            backoff_seconds = min(1800, 30 * (2 ** min(fails, 6)))

            _NEWS_SOURCE_BACKOFF_UNTIL[key] = now_ts + backoff_seconds

            last_warn = _NEWS_SOURCE_LAST_WARN_TS.get(key, 0.0)

            if now_ts - last_warn >= 300:

                logger.warning(

                    "NSE news feed failed (%s): %s (backoff %ss)",

                    source,

                    exc,

                    int(backoff_seconds),

                )

                _NEWS_SOURCE_LAST_WARN_TS[key] = now_ts

            failed_sources.append(source)



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





@router.get("/api/nse/prediction")

async def get_nse_prediction():

    """Calculated Indian market prediction with NSE news + top BUY/SELL results."""



    def _sentiment_from_titles(items: list[dict]) -> tuple[float, int, int]:

        titles = [

            str(row.get("title") or "").strip() for row in items if row.get("title")

        ]

        if not titles:

            return 0.0, 0, 0



        try:

            from transformers import pipeline



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

            pos_words = {

                "rally",

                "surge",

                "gain",

                "bullish",

                "breakout",

                "upbeat",

                "record high",

                "strong",

                "buy",

                "upgrade",

                "inflow",

                "beats",

                "jumps",

            }

            neg_words = {

                "falls",

                "drop",

                "selloff",

                "bearish",

                "crash",

                "downgrade",

                "outflow",

                "weak",

                "misses",

                "warning",

                "decline",

                "slides",

                "slump",

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



    def _pct_change(df: pd.DataFrame) -> float:

        if df is None or df.empty:

            return 0.0

        first = float(df.iloc[0].get("open", 0.0) or 0.0)

        last = float(df.iloc[-1].get("close", 0.0) or 0.0)

        if first == 0:

            return 0.0

        return ((last - first) / first) * 100.0



    try:

        news_payload = await get_nse_news(limit=24)

        news_items = list(news_payload.get("items") or [])

        news_score, pos_hits, neg_hits = _sentiment_from_titles(news_items)



        all_rows: list[dict] = []

        buy_rows: list[dict] = []

        sell_rows: list[dict] = []



        for symbol, df in svc._signal_cache.items():

            # NSE/universe only (exclude crypto symbols like BTC/USDT).

            if "/" in symbol or df is None or df.empty:

                continue



            last = df.iloc[-1]

            raw_signal = str(last.get("signal", "HOLD") or "HOLD").upper()

            signal = (

                "BUY"

                if raw_signal in ("LONG", "BUY")

                else "SELL"

                if raw_signal in ("SHORT", "SELL")

                else "HOLD"

            )

            confidence = float(

                last.get("confidence", last.get("ai_confidence", 0.0)) or 0.0

            )

            quality = float(last.get("quality_score", 0.0) or 0.0)

            price = float(last.get("close", 0.0) or 0.0)



            row = {

                "symbol": symbol,

                "signal": signal,

                "confidence": round(confidence, 1),

                "quality_score": int(round(quality)),

                "price": round(price, 2),

            }

            all_rows.append(row)

            if signal == "BUY":

                buy_rows.append(row)

            elif signal == "SELL":

                sell_rows.append(row)



        buy_rows.sort(key=lambda r: (r["confidence"], r["quality_score"]), reverse=True)

        sell_rows.sort(

            key=lambda r: (r["confidence"], r["quality_score"]), reverse=True

        )



        buy_count = len(buy_rows)

        sell_count = len(sell_rows)

        hold_count = max(0, len(all_rows) - buy_count - sell_count)

        breadth = (buy_count - sell_count) / max(buy_count + sell_count, 1)



        nifty_df = svc._signal_cache.get("NIFTY 50")

        bank_df = svc._signal_cache.get("BANKNIFTY")

        nifty_pct = _pct_change(nifty_df) if nifty_df is not None else 0.0

        bank_pct = _pct_change(bank_df) if bank_df is not None else 0.0



        mood_payload = await get_market_mood_api()

        market_mood = str(mood_payload.get("market_mood") or "NEUTRAL").upper()



        score = 0.0

        reasons: list[str] = []



        if nifty_pct >= 0.8:

            score += 1.1

            reasons.append(f"NIFTY +{nifty_pct:.2f}%")

        elif nifty_pct <= -0.8:

            score -= 1.1

            reasons.append(f"NIFTY {nifty_pct:.2f}%")



        if bank_pct >= 0.8:

            score += 0.8

            reasons.append(f"BANKNIFTY +{bank_pct:.2f}%")

        elif bank_pct <= -0.8:

            score -= 0.8

            reasons.append(f"BANKNIFTY {bank_pct:.2f}%")



        score += breadth * 2.2

        reasons.append(f"signal breadth {(breadth * 100):.0f}%")



        if market_mood == "BULLISH":

            score += 0.7

            reasons.append("market mood bullish")

        elif market_mood == "BEARISH":

            score -= 0.7

            reasons.append("market mood bearish")



        if news_score > 0.10:

            score += 0.7

            reasons.append(f"news positive ({pos_hits}/{len(news_items)})")

        elif news_score < -0.10:

            score -= 0.7

            reasons.append(f"news negative ({neg_hits}/{len(news_items)})")



        direction = "NEUTRAL"

        if score >= 1.0:

            direction = "BULLISH_BIAS"

        elif score <= -1.0:

            direction = "BEARISH_BIAS"



        confidence = int(max(35, min(93, round(38 + abs(score) * 18))))



        if _NSE_PREDICTION_HISTORY:

            prev = _NSE_PREDICTION_HISTORY[-1]

            if prev.get("actual") is None:

                actual = (

                    "UP"

                    if nifty_pct > 0.15

                    else "DOWN"

                    if nifty_pct < -0.15

                    else "FLAT"

                )

                prev_dir = str(prev.get("direction") or "")

                hit: bool | None = None

                if prev_dir == "BULLISH_BIAS":

                    hit = actual == "UP"

                elif prev_dir == "BEARISH_BIAS":

                    hit = actual == "DOWN"

                prev["actual"] = actual

                prev["hit"] = hit



        _NSE_PREDICTION_HISTORY.append(

            {

                "ts": datetime.now(timezone.utc).isoformat(),

                "direction": direction,

                "confidence": confidence,

                "score": round(score, 3),

                "nifty_pct": round(nifty_pct, 3),

                "buy_count": buy_count,

                "sell_count": sell_count,

                "hold_count": hold_count,

                "breadth": round(breadth, 4),

                "actual": None,

                "hit": None,

            }

        )



        resolved = [h for h in _NSE_PREDICTION_HISTORY if h.get("hit") is not None]

        hit_rate = (

            round(100.0 * sum(1 for h in resolved if h.get("hit")) / len(resolved), 1)

            if resolved

            else None

        )

        accuracy_series: list[dict] = []

        wins = 0

        total = 0

        for row in resolved:

            total += 1

            if row.get("hit"):

                wins += 1

            accuracy_series.append(

                {

                    "ts": row.get("ts"),

                    "resolved": total,

                    "hit_rate_pct": round((wins / total) * 100.0, 1),

                }

            )



        return {

            "prediction": {

                "direction": direction,

                "confidence": confidence,

                "score": round(score, 3),

                "horizon": "1-3 trading days",

                "reasons": reasons[:6],

                "inputs": {

                    "nifty_change_pct": round(nifty_pct, 3),

                    "banknifty_change_pct": round(bank_pct, 3),

                    "market_mood": market_mood,

                    "signal_breadth": round(breadth, 4),

                    "external_news_sentiment": round(news_score, 4),

                },

            },

            "signals": {

                "buy_count": buy_count,

                "sell_count": sell_count,

                "hold_count": hold_count,

                "top_buy": buy_rows[:5],

                "top_sell": sell_rows[:5],

            },

            "news": {

                "items": news_items[:8],

                "positive_hits": pos_hits,

                "negative_hits": neg_hits,

                "sentiment_score": round(news_score, 4),

                "sources_ok": news_payload.get("sources_ok") or [],

                "sources_failed": news_payload.get("failed_sources") or [],

            },

            "prediction_history": list(_NSE_PREDICTION_HISTORY)[-12:],

            "accuracy_tracker": {

                "resolved": len(resolved),

                "hit_rate_pct": hit_rate,

                "series": accuracy_series[-20:],

            },

            "calculated_at": datetime.now(timezone.utc).isoformat(),

            "real_data": True,

        }

    except Exception as exc:

        logger.error("NSE prediction failed: %s", exc)

        return {

            "prediction": {

                "direction": "NEUTRAL",

                "confidence": 20,

                "score": 0.0,

                "horizon": "1-3 trading days",

                "reasons": ["Prediction unavailable due to upstream fetch failure."],

                "inputs": {},

            },

            "signals": {

                "buy_count": 0,

                "sell_count": 0,

                "hold_count": 0,

                "top_buy": [],

                "top_sell": [],

            },

            "news": {

                "items": [],

                "positive_hits": 0,

                "negative_hits": 0,

                "sentiment_score": 0.0,

                "sources_ok": [],

                "sources_failed": [],

            },

            "prediction_history": list(_NSE_PREDICTION_HISTORY)[-12:],

            "accuracy_tracker": {"resolved": 0, "hit_rate_pct": None, "series": []},

            "calculated_at": datetime.now(timezone.utc).isoformat(),

            "real_data": False,

            "error": str(exc),

        }





@router.get("/api/context/events")

async def get_context_events():

    """Return upcoming high-impact events in next 7 days."""

    try:

        fetcher = build_context_fetcher()

        events = await asyncio.to_thread(fetcher.get_upcoming_events)

        return {"events": events}

    except Exception as exc:

        logger.error("Context events fetch failed: %s", exc)

        return {"events": [], "error": str(exc)}





@router.get("/api/context/market-mood")

async def get_market_mood_api():

    """Return composite market mood payload for dashboard context strip.



    Returns a neutral cached response immediately during server startup so the

    frontend never receives a 503 — the real data populates once warmup is done.

    """

    # During startup warmup return neutral immediately — avoids 503 on first hit.

    if not svc._loading_status.get("ready"):

        return {

            "crypto_mood": "NEUTRAL",

            "btc_dominance": None,

            "btc_dominance_source": "warming_up",

            "btc_24hr_change": 0.0,

            "nifty_24hr_change": 0.0,

            "avg_news_sentiment": 0.0,

            "active_event_risk": False,

            "next_event": None,

        }



    try:

        fetcher = build_context_fetcher()

        btc_snapshot = await asyncio.to_thread(fetcher.get_snapshot_sync, "BTCUSDT")



        # Nifty from cached 5m stream if available.

        nifty_change = 0.0

        if "NIFTY 50" in svc._market_data and len(svc._market_data["NIFTY 50"]) >= 2:

            nifty_df = svc._market_data["NIFTY 50"]

            latest = float(nifty_df.iloc[-1]["close"])

            first = (

                float(nifty_df.iloc[0]["open"])

                if float(nifty_df.iloc[0]["open"]) != 0

                else latest

            )

            nifty_change = ((latest - first) / first) * 100.0 if first else 0.0



        # Average context sentiment over active symbols (cache-first, cheap after warmup).

        sentiments: list[float] = []

        active_event_risk = bool(btc_snapshot.event_risk_active)

        tracked = list(svc._signal_cache.keys())[:8]

        for sym in tracked:

            try:

                snap = await asyncio.to_thread(fetcher.get_snapshot_sync, sym)

                sentiments.append(float(snap.news_sentiment_24hr or 0.0))

                active_event_risk = active_event_risk or bool(snap.event_risk_active)

            except Exception:

                continue



        avg_sent = (

            float(np.mean(sentiments))

            if sentiments

            else float(btc_snapshot.news_sentiment_24hr or 0.0)

        )

        events = await asyncio.to_thread(fetcher.get_upcoming_events)

        next_event = events[0] if events else None



        if active_event_risk or btc_snapshot.price_change_24hr_pct <= -1.0:

            crypto_mood = "RISK_OFF"

        elif btc_snapshot.price_change_24hr_pct >= 1.0 and avg_sent > 0.1:

            crypto_mood = "RISK_ON"

        else:

            crypto_mood = "NEUTRAL"



        dominance_source = "cache"

        # Read BTC dominance from context fetcher global cache if available.

        btc_dominance = float(

            fetcher._global_cache.get("coingecko_global", {})

            .get("payload", {})

            .get("btc_dominance", 0.0)  # type: ignore[attr-defined]

        )



        # Fallback to direct CoinGecko global endpoint when cache is empty/stale.

        global _BTC_DOM_BACKOFF_UNTIL, _BTC_DOM_LAST_WARN_TS

        now_ts = time.time()

        if btc_dominance <= 0 and now_ts >= _BTC_DOM_BACKOFF_UNTIL:

            try:

                req = Request(

                    "https://api.coingecko.com/api/v3/global",

                    headers={

                        "User-Agent": (

                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "

                            "AppleWebKit/537.36 (KHTML, like Gecko) "

                            "Chrome/124.0.0.0 Safari/537.36"

                        )

                    },

                )

                with urlopen(req, timeout=8) as resp:

                    payload = json.loads(resp.read())

                btc_dominance = float(

                    ((payload or {}).get("data") or {})

                    .get("market_cap_percentage", {})

                    .get("btc", 0.0)

                )

                if btc_dominance > 0:

                    dominance_source = "coingecko_live"

            except Exception as exc:

                msg = str(exc)

                if "429" in msg or "Too Many Requests" in msg:

                    _BTC_DOM_BACKOFF_UNTIL = now_ts + 300

                else:

                    _BTC_DOM_BACKOFF_UNTIL = now_ts + 120



                if now_ts - _BTC_DOM_LAST_WARN_TS >= 300:

                    logger.warning("BTC dominance fallback fetch failed: %s", exc)

                    _BTC_DOM_LAST_WARN_TS = now_ts

                dominance_source = "unavailable"

        elif btc_dominance <= 0:

            dominance_source = "backoff"



        return {

            "crypto_mood": crypto_mood,

            "btc_dominance": round(btc_dominance, 2) if btc_dominance > 0 else None,

            "btc_dominance_source": dominance_source,

            "btc_24hr_change": round(

                float(btc_snapshot.price_change_24hr_pct or 0.0), 2

            ),

            "nifty_24hr_change": round(float(nifty_change), 2),

            "avg_news_sentiment": round(avg_sent, 3),

            "active_event_risk": active_event_risk,

            "next_event": next_event,

        }

    except Exception as e:

        logger.error("Error fetching market mood: %s", e)

        return {

            "crypto_mood": "NEUTRAL",

            "btc_dominance": None,

            "btc_dominance_source": "error",

            "btc_24hr_change": 0.0,

            "nifty_24hr_change": 0.0,

            "avg_news_sentiment": 0.0,

            "active_event_risk": False,

            "next_event": None,

        }





@router.get("/api/context/{symbol}")

async def get_context_snapshot(symbol: str):

    """Return latest context snapshot for a symbol."""

    try:

        fetcher = build_context_fetcher()

        snapshot = await asyncio.to_thread(fetcher.get_snapshot_sync, symbol)

        return {

            "symbol": snapshot.symbol,

            "timestamp_utc": snapshot.timestamp_utc.isoformat(),

            "price_change_24hr_pct": snapshot.price_change_24hr_pct,

            "volume_change_24hr_pct": snapshot.volume_change_24hr_pct,

            "news_sentiment_24hr": snapshot.news_sentiment_24hr,

            "news_sentiment_7d": snapshot.news_sentiment_7d,

            "news_volume_spike": snapshot.news_volume_spike,

            "weekly_trend_bias": snapshot.weekly_trend_bias,

            "oi_change_7d_pct": snapshot.oi_change_7d_pct,

            "btc_dominance_7d_trend": snapshot.btc_dominance_7d_trend,

            "days_to_next_event": snapshot.days_to_next_event,

            "event_risk_active": snapshot.event_risk_active,

            "context_score": snapshot.context_score,

        }

    except Exception as exc:

        logger.error("Context snapshot failed for %s: %s", symbol, exc)

        return {"error": str(exc), "symbol": symbol}

