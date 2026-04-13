"""HTTP and WebSocket route handlers for the trading API."""
import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from config import settings
from db.connection import SessionLocal
from db.models import MorningScan
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.trend import classify_trend, propagate_trend_to_ltf
from agents.feature_engine.regime_detector import detect_regime
from agents.signal_engine.signal_generator import generate_signals
from agents.backtester.engine import Backtester
from agents.market_intelligence.market_data import fetch_fii_dii_flow
from agents.market_intelligence.swing_scanner import scan_all_symbols as swing_scan_all
from agents.market_intelligence.index_mover import calculate_index_movers
from agents.market_intelligence.breakout_scanner import scan_all_symbols as breakout_scan_all
from agents.market_intelligence.nse_fetcher import (
    fetch_ohlcv,
    fetch_nse_gainers_losers,
    fetch_sector_performance,
    search_symbols,
    fetch_arbitrary_symbol,
    validate_nse_symbol,
)
from agents.stock_screener import opportunity_stocks
from agents.data_ingestion.context_fetcher import build_context_fetcher
from agents.analytics.trade_journal import get_journal
try:
    from agents.analytics.signal_tracker import get_win_rate, get_leaderboard, score_outcomes, log_signal
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
        send_signal_alert, send_whale_alert, is_configured as tg_is_configured,
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

router = APIRouter()

# Dashboard HTML is edited often during dev; avoid stale cached documents masking UI changes.
_DASH_HTML_HEADERS = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}

_CRYPTO_PREDICTION_HISTORY: deque[dict] = deque(maxlen=60)
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
    return {
        "status": "ok",
        "loading": svc._loading_status,
        "feed": svc._get_feed_status(),
        "symbols_loaded": len(svc._market_data),
        "signals_cached": len(svc._signal_cache),
        "last_refresh": svc._last_refresh.isoformat() if svc._last_refresh != datetime.min else None,
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
    Fetches live Binance klines for each meme coin and computes
    RSI-based BUY/SELL/HOLD signals independently (not from NSE cache).

    Signal logic (meme-tuned thresholds):
      BUY  → RSI < 35 (oversold) + price above EMA20
      SELL → RSI > 70 (overbought) OR price 8%+ below EMA20
      HOLD → everything else
    """
    import statistics
    import math

    COIN_EMOJIS = {
        "DOGE": "🐶", "SHIB": "🐱", "BONK": "🐶", "WIF": "🐶",
        "PEPE": "🐸", "FLOKI": "🐕", "PNUT": "🐿️", "BOME": "📚",
        "MEW": "🐱", "POPCAT": "😸", "BRETT": "🧢", "TURBO": "🤖",
        "NEIRO": "🐶", "GOAT": "🐐", "CHILLGUY": "😎", "DEGEN": "🎰",
        "TRUMP": "🎩", "FARTCOIN": "💨", "MOODENG": "🦛", "SPX": "📈",
        "GIGA": "💪", "PONKE": "🐒",
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
        avg = statistics.mean(volumes[-22:-1]) if len(volumes) >= 22 else statistics.mean(volumes[:-1])
        return round(recent / avg, 2) if avg > 0 else 1.0

    def _meme_signal(
        closes: list[float],
        rsi: float | None,
        ema20: float | None,
        change_24h: float = 0.0,
        vol_spike: float = 1.0,
    ) -> tuple[str, float]:
        """
        Multi-factor meme signal — combines RSI + EMA + momentum + volume.

        BUY conditions (any one enough):
          A. RSI < 40 (oversold dip)
          B. RSI 40-55 + positive 24h momentum (>+2%) + price above EMA20
          C. Volume spike >2x + RSI < 60 + positive momentum

        SELL conditions (any one enough):
          A. RSI > 65 (overbought)
          B. RSI 55-65 + negative 24h momentum (<-3%) + price below EMA20
          C. Price >20% above EMA20 (extended)

        HOLD: everything else
        """
        if rsi is None or not closes:
            return "HOLD", 0.38

        price = closes[-1]
        price_vs_ema = ((price - ema20) / ema20 * 100) if ema20 else 0.0
        above_ema = price_vs_ema > 0

        # ── BUY Logic ───────────────────────────────────────────
        # A: Oversold RSI
        if rsi < 40:
            conf = 0.58 + (40 - rsi) / 40 * 0.28       # 0.58–0.86
            if price_vs_ema < -20:                        # extreme dump — reduce
                conf *= 0.80
            return "BUY", round(min(conf, 0.90), 2)

        # B: Momentum BUY — positive trend in healthy RSI range
        if 40 <= rsi <= 55 and change_24h >= 2.0 and above_ema:
            conf = 0.52 + min(change_24h, 15) / 100      # 0.52–0.67
            if vol_spike >= 1.5:
                conf = min(conf + 0.08, 0.75)             # volume boost
            return "BUY", round(conf, 2)

        # C: Volume pump signal
        if vol_spike >= 2.5 and rsi < 62 and change_24h > 0:
            conf = 0.50 + (vol_spike - 2.5) / 10         # 0.50–0.65
            return "BUY", round(min(conf, 0.68), 2)

        # ── SELL Logic ───────────────────────────────────────────
        # A: Classic overbought
        if rsi > 65:
            conf = 0.55 + (rsi - 65) / 35 * 0.30        # 0.55–0.85
            if price_vs_ema > 20:
                conf = min(conf + 0.07, 0.88)
            return "SELL", round(conf, 2)

        # B: Momentum reversal
        if 55 <= rsi <= 65 and change_24h <= -3.0 and not above_ema:
            conf = 0.52 + min(abs(change_24h), 15) / 100
            return "SELL", round(conf, 2)

        # C: Extended far above EMA — take profit zone
        if price_vs_ema > 22 and rsi > 58:
            return "SELL", 0.56

        # ── HOLD ─────────────────────────────────────────────────
        mid_dist = abs(rsi - 52)
        conf = max(0.35, 0.50 - mid_dist / 120)
        return "HOLD", round(conf, 2)

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
            closes  = [float(c[4]) for c in raw]
            volumes = [float(c[5]) for c in raw]
            price   = closes[-1] if closes else 0.0
            change  = ((closes[-1] - closes[0]) / closes[0] * 100) if closes[0] else 0.0
            return {"closes": closes, "volumes": volumes, "price": price, "change_24h": round(change, 2)}
        except Exception:
            return None

    # ── Fetch all meme coins concurrently ──────────────────────
    meme_bases = [sym.split("/")[0] for sym in settings.MEME_COIN_SYMBOLS]

    kline_tasks = {base: asyncio.create_task(_fetch_binance_klines(base)) for base in meme_bases}
    await asyncio.gather(*kline_tasks.values(), return_exceptions=True)

    results = []
    for base in meme_bases:
        data = None
        try:
            data = kline_tasks[base].result()
        except Exception:
            pass

        # Fallback to service market_data cache if Binance fetch failed
        if data is None:
            sym_key = f"{base}/USDT"
            df = svc._market_data.get(sym_key)
            if df is None:
                df = svc._market_data.get(f"{base}USDT")
            if df is not None and not df.empty and "close" in df.columns:
                closes  = df["close"].dropna().tolist()
                volumes = df["volume"].dropna().tolist() if "volume" in df.columns else []
                price   = closes[-1] if closes else 0.0
                pct     = ((closes[-1] - closes[0]) / closes[0] * 100) if closes and closes[0] else 0.0
                data = {"closes": closes, "volumes": volumes, "price": price, "change_24h": round(pct, 2)}

        if not data or not data.get("closes"):
            continue  # skip coin — no data at all

        closes  = data["closes"]
        volumes = data["volumes"]
        price   = data["price"]

        rsi    = _calc_rsi(closes)
        ema20  = _calc_ema(closes, 20)
        spike  = _vol_spike(volumes)
        change_24h = data.get("change_24h", 0.0)
        signal, confidence = _meme_signal(closes, rsi, ema20, change_24h, spike)

        # Pull context score if available
        sym_key = f"{base}/USDT"
        cached_sig = svc._signal_cache.get(sym_key)
        if cached_sig is None:
            cached_sig = svc._signal_cache.get(f"{base}USDT")
        ctx_score = 0.0
        if cached_sig is not None and not cached_sig.empty:
            raw_ctx = cached_sig.iloc[-1].get("context_score", 0.0)
            ctx_score = float(raw_ctx) if raw_ctx and not math.isnan(float(raw_ctx)) else 0.0

        news_sentiment = (
            "bullish" if ctx_score > 0.15
            else "bearish" if ctx_score < -0.15
            else "neutral"
        )

        results.append({
            "symbol":         sym_key,
            "base":           base,
            "emoji":          COIN_EMOJIS.get(base, "🪙"),
            "signal":         signal,
            "confidence":     confidence,
            "price":          price,
            "change_24h":     data.get("change_24h", 0.0),
            "rsi":            rsi,
            "ema20":          ema20,
            "trend":          "BULLISH" if (ema20 and price > ema20) else "BEARISH",
            "volume_spike":   spike,
            "is_hot":         spike >= 2.0,
            "news_sentiment": news_sentiment,
            "context_score":  round(ctx_score, 3),
            "risk_level":     "HIGH",
            "risk_note":      "Meme coin — extreme volatility. Use small position sizes only.",
        })

    # Sort: BUY first → highest confidence → alphabetical
    signal_order = {"BUY": 0, "HOLD": 1, "SELL": 2}
    results.sort(key=lambda x: (signal_order.get(x["signal"], 1), -x["confidence"]))

    return {
        "meme_coins":  results,
        "total":       len(results),
        "buy_count":   sum(1 for r in results if r["signal"] == "BUY"),
        "sell_count":  sum(1 for r in results if r["signal"] == "SELL"),
        "hold_count":  sum(1 for r in results if r["signal"] == "HOLD"),
        "hot_count":   sum(1 for r in results if r["is_hot"]),
        "updated_at":  datetime.now().isoformat(),
    }



# ── Signal Win Rate Endpoints ─────────────────────────────────────

@router.get("/api/signals/winrate")
async def get_signal_winrate(symbol: str = Query(None, description="Filter by symbol e.g. BONK/USDT")):
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
        "configured":  configured,
        "enabled":     os.getenv("TELEGRAM_ALERTS_ENABLED", "false").lower() == "true",
        "token_set":   bool(os.getenv("TELEGRAM_BOT_TOKEN")),
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
        "TEST/USDT", "BUY", 0.001234, 0.75,
        rsi=28.5, change_24h=12.3, volume_spike=3.2,
        news_sentiment="bullish", reason="Test alert from TradeAI PRO dashboard",
    )
    return {
        "success": sent,
        "message": "Test alert sent! Check your Telegram." if sent else "Send failed — check bot token and chat ID",
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
        return {"symbol": symbol, "data": svc._build_market_records(df), "interval": interval}

    # On-demand fetch for non-default intervals (or symbol not yet in cache)
    period_map = {"5m": "5d", "15m": "10d", "1h": "30d", "1d": "90d"}
    period = period_map.get(interval, "5d")

    try:
        df = await asyncio.to_thread(fetch_ohlcv, symbol, period, interval)
        if df is None or df.empty:
            df = await asyncio.to_thread(fetch_arbitrary_symbol, symbol, period, interval)
        if df is None or df.empty:
            return {"error": f"Symbol {symbol} not found"}
        df = calculate_indicators(df)
    except Exception as e:
        logger.error("On-demand market fetch error for %s@%s: %s", symbol, interval, e)
        return {"error": str(e)}

    df = df.tail(limit)
    return {"symbol": symbol, "data": svc._build_market_records(df), "interval": interval}


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
    return {"trades": engine.get_history(limit=limit)}


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
                "overall": {"total_trades": 0, "win_rate": 0, "avg_pnl_pct": 0, "model_accuracy": 0},
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
    engine = svc._get_engine()
    open_trades = engine.get_open_trades()

    total_unrealized = 0
    for t in open_trades:
        symbol = t["symbol"]
        if symbol in svc._market_data:
            current_close = float(svc._market_data[symbol].iloc[-1]["close"])
            if t["direction"] == "BUY":
                unrealized = (current_close - t["entry_price"]) * t.get("quantity", 1)
            else:
                unrealized = (t["entry_price"] - current_close) * t.get("quantity", 1)
            t["current_price"] = round(current_close, 2)
            t["unrealized_pnl"] = round(unrealized, 2)
            total_unrealized += unrealized
        else:
            t["current_price"] = t["entry_price"]
            t["unrealized_pnl"] = 0

    closed_count = engine.get_stats().get("total_trades", 0)

    return {
        "open_positions": open_trades,
        "total_open": len(open_trades),
        "total_unrealized_pnl": round(total_unrealized, 2),
        "total_closed": closed_count,
    }


@router.get("/api/performance")
async def get_performance():
    """Get detailed performance metrics and equity curve from real trading engine."""
    from collections import defaultdict, Counter

    engine = svc._get_engine()
    stats = engine.get_stats()
    equity_curve = engine.get_equity_curve()

    closed_trades = engine.get_history(limit=1000)
    wins   = [t for t in closed_trades if t.get("pnl", 0) > 0]
    losses = [t for t in closed_trades if t.get("pnl", 0) <= 0]

    # ── Monthly P&L breakdown ──────────────────────────
    monthly: dict[str, float] = defaultdict(float)
    for t in closed_trades:
        key = (t.get("exit_time") or "")[:7]   # "2026-03"
        if key:
            monthly[key] += t.get("pnl", 0) * t.get("quantity", 1)
    monthly_pnl = [{"month": k, "pnl": round(v, 2)} for k, v in sorted(monthly.items())]

    # ── Exit reason breakdown ──────────────────────────
    exit_counts = Counter(t.get("status", "UNKNOWN") for t in closed_trades)
    exit_reasons = [{"reason": r, "count": c} for r, c in exit_counts.most_common()]

    # ── P&L by symbol (top 10) ─────────────────────────
    sym_pnl:    dict[str, float] = defaultdict(float)
    sym_trades: dict[str, int]   = defaultdict(int)
    for t in closed_trades:
        sym = t.get("symbol", "?")
        sym_pnl[sym]    += t.get("pnl", 0) * t.get("quantity", 1)
        sym_trades[sym] += 1
    symbol_breakdown = sorted(
        [{"symbol": k, "pnl": round(v, 2), "trades": sym_trades[k]} for k, v in sym_pnl.items()],
        key=lambda x: abs(x["pnl"]), reverse=True,
    )[:10]

    # ── Return % ───────────────────────────────────────
    initial = engine.initial_capital
    current = stats.get("capital", initial)
    total_return_pct = round((current - initial) / initial * 100, 2) if initial else 0

    return {
        "metrics": {
            "total_trades":     stats.get("total_trades", 0),
            "winning_trades":   stats.get("winning_trades", 0),
            "losing_trades":    stats.get("losing_trades", 0),
            "win_rate":         stats.get("win_rate", 0),
            "total_pnl":        stats.get("total_pnl", 0),
            "total_return_pct": total_return_pct,
            "avg_pnl":          stats.get("avg_pnl", 0),
            "avg_win":          stats.get("avg_win", 0),
            "avg_loss":         stats.get("avg_loss", 0),
            "best_trade":       stats.get("best_trade", 0),
            "worst_trade":      stats.get("worst_trade", 0),
            "sharpe_ratio":     stats.get("sharpe_ratio", 0),
            "max_drawdown":     stats.get("max_drawdown", 0),
            "max_drawdown_pct": stats.get("max_drawdown_pct", 0),
            "profit_factor":    stats.get("profit_factor", 0),
            "capital":          current,
            "initial_capital":  initial,
            "daily_pnl":        stats.get("daily_pnl", 0),
        },
        "status": {
            "feed": svc._get_feed_status(),
            "symbols_loaded": len(svc._market_data),
            "signals_cached": len(svc._signal_cache),
        },
        "equity_curve":       equity_curve,
        "trade_distribution": {"wins": len(wins), "losses": len(losses)},
        "monthly_pnl":        monthly_pnl,
        "exit_reasons":       exit_reasons,
        "symbol_breakdown":   symbol_breakdown,
        "recent_trades":      list(reversed(closed_trades[-50:])),
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
    }


@router.get("/api/backtest/{symbol}")
async def run_backtest(symbol: str):
    """Run backtest on cached data for a symbol."""
    symbol = symbol.replace("-", "/")
    if symbol not in svc._signal_cache:
        return {"error": f"Symbol {symbol} not found"}

    df = svc._signal_cache[symbol]
    bt = Backtester(initial_capital=100000, slippage_pct=0.05)
    result = bt.run(df)

    return {
        "symbol": symbol,
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
            return {"data": data, "real_data": True, "source": "cache", "updated_at": datetime.now().isoformat()}

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

    return {"data": data or [], "real_data": bool(data), "updated_at": datetime.now().isoformat()}


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
            return {"data": data, "real_data": True, "source": "cache", "updated_at": datetime.now().isoformat()}

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

    return {"data": data or [], "real_data": bool(data), "updated_at": datetime.now().isoformat()}


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
            return {"sectors": sectors, "real_data": True, "source": "cache", "updated_at": datetime.now().isoformat()}

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
    return {"sectors": sectors or [], "real_data": bool(sectors), "updated_at": datetime.now().isoformat()}


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
            "ranking_meta": {"total_tracked": 0, "actionable": 0, "blocked_high_quality": 0},
            "market_guardrails": {},
            "system_state": {},
            "learning_overlay": None,
            "error": str(exc),
            "updated_at": datetime.now().isoformat(),
        }


@router.get("/api/nse/dashboard")
async def get_nse_dashboard_snapshot():
    """Aggregate NSE dashboard snapshot for faster UI bootstrap."""
    if not svc._movers_cache.get("gainers") and svc._market_data:
        svc._refresh_movers_cache()

    mood = svc._compute_market_mood()
    summary = svc._build_ai_market_summary(mood)
    movers_updated_at = svc._movers_cache.get("updated_at") or datetime.now().isoformat()

    gainers = list((svc._movers_cache.get("gainers") or [])[:7])
    losers = list((svc._movers_cache.get("losers") or [])[:7])

    return {
        "timestamp": datetime.now().isoformat(),
        "last_refresh": svc._last_refresh.isoformat() if svc._last_refresh != datetime.min else None,
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
            await asyncio.gather(*[_fetch_daily_bootstrap(sym, daily_sem) for sym in bootstrap_symbols])
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

    return {
        "setups": svc._swing_cache["setups"],
        "total_found": svc._swing_cache["total_found"],
        "symbols_scanned": svc._swing_cache["symbols_scanned"],
        "real_data": True,
        "updated_at": svc._swing_cache["updated_at"],
        "market_context": svc._swing_cache.get("market_context"),
    }


# ── Swing Scanner Debug Endpoint ──────────────────────────────

@router.get("/api/swing-debug")
async def get_swing_debug(symbol: str = None):
    """
    Debug endpoint — shows per-symbol scan results before/after filtering.
    Use ?symbol=RELIANCE to debug one stock, or omit for a summary of all.
    """
    from agents.market_intelligence.swing_scanner import scan_swing_setups
    from agents.feature_engine.indicators import calculate_indicators
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
        raw = scan_swing_setups(df, sym, sector_momentum=0.0, sector="Other", market_context=None)
        # Run scan WITH market context
        gated = scan_swing_setups(df, sym, sector_momentum=0.0, sector="Other", market_context=ctx)

        results.append({
            "symbol": sym,
            "bars": len(df),
            "close": round(close, 2),
            "atr": round(atr, 2),
            "rsi": round(rsi, 1),
            "ema20": round(ema20, 2),
            "ema50": round(ema50, 2),
            "raw_setups_before_gate": len(raw),
            "setups_after_regime_gate": len(gated),
            "raw_patterns": [{"pattern": s.pattern, "direction": s.direction, "rr": s.risk_reward, "strength": s.strength} for s in raw],
            "gated_patterns": [{"pattern": s.pattern, "direction": s.direction, "rr": s.risk_reward} for s in gated],
            "dropped_by_regime_gate": len(raw) - len(gated),
        })

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
        "nifty": "NIFTY 50", "nifty50": "NIFTY 50", "nifty-50": "NIFTY 50",
        "nifty_50": "NIFTY 50", "NIFTY 50": "NIFTY 50",
        "banknifty": "BANKNIFTY", "bank-nifty": "BANKNIFTY",
        "bank_nifty": "BANKNIFTY", "BANKNIFTY": "BANKNIFTY",
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
        return {"all": [], "message": "Daily data not yet loaded.",
                "real_data": False, "updated_at": datetime.now().isoformat()}

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
                    await websocket.send_json({"type": "ping", "ts": datetime.now().isoformat()})
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
                    "rsi": round(float(last["RSI_14"]), 1) if pd.notna(last.get("RSI_14")) else None,
                    "signal": str(last.get("signal", "HOLD")),
                    "regime": svc._regime_cache.get(symbol, "RANGING"),
                }

            engine = svc._get_engine()
            stats = engine.get_stats()
            update["stats"] = {
                "capital": stats.get("capital", 100000),
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
        logger.info("WebSocket client disconnected (%d remaining)", len(svc._ws_clients))


@router.post("/api/refresh")
async def refresh_data():
    """Force refresh of all market data."""
    await svc._refresh_market_data()
    return {"status": "ok", "symbols": len(svc._market_data), "updated_at": datetime.now().isoformat()}


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
            r["tracked"] = sym in svc._market_data or sym.replace("/", "-") in svc._market_data

    # ── Dynamic NSE validation ────────────────────────────────────────────
    # Only run if the query looks like a stock ticker (not crypto) and we
    # have fewer than 3 pre-loaded matches — avoids unnecessary yf calls.
    q_clean = q.upper().strip()
    already_found = {r["ticker"].replace(".NS", "") for r in results if r.get("type") == "NSE"}
    is_likely_nse = (
        len(results) < 3
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
                results.insert(0, {
                    "symbol": q_clean,
                    "ticker": nse_ticker,
                    "type": "NSE",
                    "tracked": q_clean in svc._market_data,
                    "dynamic": True,
                    "price": info["price"],
                    "exchange": info.get("exchange", "NSE"),
                })
        except Exception as e:
            logger.debug("Dynamic NSE validate failed for %s: %s", nse_ticker, e)

    # ── Inject Opportunity Stocks ──────────────────────────────────────
    if q_clean:
        # Prepend any Opportunity Stocks that match the query
        for opp in reversed(opportunity_stocks):
            if q_clean in opp["symbol"].upper():
                # Avoid duplicates
                if not any(r["symbol"] == opp["symbol"] for r in results):
                    results.insert(0, {
                        "symbol": opp["symbol"],
                        "ticker": f"{opp['symbol']}.NS",
                        "type": "NSE",
                        "tracked": opp["symbol"] in svc._market_data,
                        "dynamic": True,
                        "opportunity": True,
                        "signal": opp["signal"]
                    })

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
            clean.upper().endswith(f"/{q}") for q in ("USDT", "BTC", "ETH", "BUSD", "USDC")
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
                    content={"error": f"Symbol '{symbol}' not found on NSE or yFinance. "
                                      f"Tried '{clean_base}.NS'. Check the symbol and try again."},
                )
            df = calculate_indicators(df)
            htf_df = await asyncio.to_thread(fetch_arbitrary_symbol, clean, "60d", "1d")
            if htf_df is None or htf_df.empty:
                # For dynamic NSE symbols, also try with .NS for HTF
                nse_htf = f"{clean_base}.NS"
                htf_df = await asyncio.to_thread(fetch_arbitrary_symbol, nse_htf, "60d", "1d")
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
        chart_data.append({
            "timestamp": row["timestamp"].isoformat(),
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
            "volume": round(float(row["volume"]), 0),
            "rsi": round(float(row["RSI_14"]), 1) if pd.notna(row.get("RSI_14")) else None,
            "ema_20": round(float(row["EMA_20"]), 2) if pd.notna(row.get("EMA_20")) else None,
            "ema_50": round(float(row["EMA_50"]), 2) if pd.notna(row.get("EMA_50")) else None,
            "vwap": round(float(row["VWAP"]), 2) if pd.notna(row.get("VWAP")) else None,
            "macd_hist": round(float(row["MACD_hist"]), 4) if pd.notna(row.get("MACD_hist")) else None,
            "signal": str(row.get("signal", "HOLD")) if "signal" in row.index else "HOLD",
        })

    # Compute MACD histogram for current bar
    macd_hist = round(float(last["MACD_hist"]), 4) if pd.notna(last.get("MACD_hist")) else None

    return {
        "symbol": clean,
        "close": round(float(last["close"]), 2),
        "high": round(float(last["high"]), 2),
        "low": round(float(last["low"]), 2),
        "rsi": round(float(last["RSI_14"]), 1) if pd.notna(last.get("RSI_14")) else None,
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
    from concurrent.futures import ThreadPoolExecutor
    import asyncio

    async def _run():
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=1) as ex:
            from agents.backtester.ensemble_validator import (
                run_walk_forward_validation, result_to_dict
            )
            result = await loop.run_in_executor(
                ex,
                lambda: run_walk_forward_validation(
                    symbol=symbol, timeframe=timeframe,
                    days=days, n_folds=n_folds,
                )
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
        import os, pickle
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
        import os, json as _json
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

    active_models = sum(1 for k, v in status.items() if isinstance(v, dict) and v.get("active"))
    return {
        "ensemble_active": True,
        "active_model_count": active_models,
        "models": status,
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.post("/api/retrain")
async def retrain_xgboost(
    symbols: str = Query(default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT",
                         description="Comma-separated crypto symbols to train on"),
    timeframe: str = Query(default="4h", description="Candle timeframe: 1h, 4h, 1d"),
    days: int = Query(default=730, description="Days of history to use"),
    forward_bars: int = Query(default=12, description="Forward bars for label generation"),
    min_return_pct: float = Query(default=0.5, description="Minimum % move to count as signal"),
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
                symbol_list, timeframe, days, forward_bars, min_return_pct,
            )
        return result

    # Store job status
    job_id = f"retrain_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    svc._retrain_jobs[job_id] = {"status": "running", "started_at": datetime.utcnow().isoformat()}

    async def _background():
        try:
            result = await _run_training()
            svc._retrain_jobs[job_id] = {
                "status": "completed",
                "result": result,
                "completed_at": datetime.utcnow().isoformat(),
            }
            logger.info("XGBoost retrain complete: %s", result)
        except Exception as e:
            svc._retrain_jobs[job_id] = {
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.utcnow().isoformat(),
            }
            logger.error("XGBoost retrain failed: %s", e)

    asyncio.create_task(_background())
    return {"job_id": job_id, "status": "started", "symbols": symbol_list, "timeframe": timeframe}


@router.get("/api/retrain/status")
async def retrain_status():
    """Return status of all retrain jobs."""
    return {"jobs": svc._retrain_jobs}

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
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=_DASH_HTML_HEADERS)


@router.get("/crypto")
@router.get("/crypto/")
async def serve_crypto():
    """Serve the live crypto dashboard page."""
    crypto = os.path.join(STATIC_DIR, "crypto.html")
    if os.path.exists(crypto):
        return FileResponse(crypto, headers=_DASH_HTML_HEADERS)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=_DASH_HTML_HEADERS)


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
                    title = _norm_text(it.find("title")) or _norm_text(it.find("{http://www.w3.org/2005/Atom}title"))
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
        "updated_at": datetime.utcnow().isoformat(),
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
            "market_guardrails": {"mood": "UNKNOWN", "mood_score": 50, "why_market_hint": "Intelligence fetch failed."},
            "futures_context": {"per_symbol": [], "updated_at": None, "source": "unavailable"},
            "theme_leadership": [],
            "ranking_meta": {"total_tracked": 0, "actionable": 0, "blocked_high_quality": 0},
            "updated_at": datetime.utcnow().isoformat(),
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
        pos_words = {
            "surge", "rally", "breakout", "adoption", "approval", "launch", "partnership", "bull", "bullish",
            "upgrade", "rebound", "inflow", "record", "gain", "recovery", "accumulate"
        }
        neg_words = {
            "crash", "dump", "selloff", "hack", "ban", "lawsuit", "liquidation", "bear", "bearish",
            "outflow", "downgrade", "risk", "warning", "fraud", "decline", "drop"
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

    def _hero_signal(sym: str, ticker: dict, closes: list[float], volumes: list[float]) -> dict:
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
            reasons.append("MACD hist positive" if macd_h >= 0 else "MACD hist negative")

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

        conf = int(max(35, min(95, round(42 + abs(score) * 13 + min(abs(pct), 9.0) * 1.2))))
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
        raw_tickers = await asyncio.to_thread(_fetch_json, "https://api.binance.com/api/v3/ticker/24hr")
        if not isinstance(raw_tickers, list):
            raise ValueError("Unexpected ticker payload")

        usdt = [
            t for t in raw_tickers
            if isinstance(t, dict)
            and str(t.get("symbol", "")).endswith("USDT")
            and float(t.get("quoteVolume") or 0.0) > 1_000_000
            and "DOWN" not in str(t.get("symbol"))
            and "UP" not in str(t.get("symbol"))
            and "BEAR" not in str(t.get("symbol"))
            and "BULL" not in str(t.get("symbol"))
        ]
        ticker_map = {str(t.get("symbol")): t for t in usdt}

        btc_pct = float((ticker_map.get("BTCUSDT") or {}).get("priceChangePercent") or 0.0)
        eth_pct = float((ticker_map.get("ETHUSDT") or {}).get("priceChangePercent") or 0.0)

        gainers_n = sum(1 for t in usdt if float(t.get("priceChangePercent") or 0.0) > 0)
        losers_n = sum(1 for t in usdt if float(t.get("priceChangePercent") or 0.0) < 0)
        breadth = (gainers_n - losers_n) / max(gainers_n + losers_n, 1)

        # 2) Fear & Greed
        fg_value: int | None = None
        try:
            fg_raw = await asyncio.to_thread(_fetch_json, "https://api.alternative.me/fng/?limit=1")
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
                closes = [float(r[4]) for r in kraw if isinstance(r, list) and len(r) > 5]
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
                actual = "UP" if btc_pct > 0.15 else "DOWN" if btc_pct < -0.15 else "FLAT"
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
                "ts": datetime.utcnow().isoformat(),
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
        hit_rate = round(100.0 * sum(1 for h in resolved if h.get("hit")) / len(resolved), 1) if resolved else None

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
                "sentiment": ["Alternative.me Fear&Greed", "Internal context mood", "Crypto RSS headlines"],
                "news_ok": news_payload.get("sources_ok") or [],
                "news_failed": news_payload.get("failed_sources") or [],
            },
            "calculated_at": datetime.utcnow().isoformat(),
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
                "reasons": ["Prediction unavailable due to upstream market/news fetch failure."],
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
            "calculated_at": datetime.utcnow().isoformat(),
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
            first = float(nifty_df.iloc[0]["open"]) if float(nifty_df.iloc[0]["open"]) != 0 else latest
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

        avg_sent = float(np.mean(sentiments)) if sentiments else float(btc_snapshot.news_sentiment_24hr or 0.0)
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
            fetcher._global_cache.get("coingecko_global", {}).get("payload", {}).get("btc_dominance", 0.0)  # type: ignore[attr-defined]
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
                    ((payload or {}).get("data") or {}).get("market_cap_percentage", {}).get("btc", 0.0)
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
            "btc_24hr_change": round(float(btc_snapshot.price_change_24hr_pct or 0.0), 2),
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
