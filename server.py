"""
FastAPI Application — Trading Dashboard Backend.

Serves API endpoints for signals, trades, market data, dashboard metrics,
portfolio view, performance stats, and regime classification.
Also serves the static frontend dashboard.

Data Sources:
  - Real trades from paper_trades.json (PaperTradingEngine)
  - Real signals from DB (signals table)
  - Live market data from yFinance / Binance (with caching)
  - WebSocket endpoint for real-time price streaming

Fixes applied:
  - [Fix 1] WebSocket key: sends "prices" (was "data") to match frontend
  - [Fix 2] Background periodic refresh every 60 seconds
  - [Fix 3] Screener/Sector endpoints use real yfinance data
  - [Fix 4] Fetch top 20 NSE symbols (was 10)
  - [Fix 5] HTF (1d) trend propagation in _refresh_market_data
"""

import sys
import os
import logging
import asyncio
from contextlib import asynccontextmanager
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import numpy as np
import pandas as pd

from config import settings
from db.connection import init_db, SessionLocal
from db.models import Signal, Trade, TradeStatus, Candle
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.trend import classify_trend, propagate_trend_to_ltf
from agents.feature_engine.regime_detector import detect_regime, MarketRegime
from agents.signal_engine.signal_generator import generate_signals
from agents.backtester.engine import Backtester
from agents.ai_engine.fallback_predictor import predict_batch
from agents.feature_engine.mtf_scorer import calculate_mtf_score
from agents.execution.paper_trading import PaperTradingEngine
from agents.market_intelligence.market_data import (
    generate_pcr_data,
    fetch_fii_dii_flow,
)
from agents.market_intelligence.swing_scanner import (
    scan_all_symbols as swing_scan_all,
)
from agents.market_intelligence.index_mover import (
    calculate_index_movers,
)
from agents.market_intelligence.breakout_scanner import (
    scan_all_symbols as breakout_scan_all,
)
try:
    from agents.alerts.telegram_bot import send_signal_alert as _tg_signal_alert
except ImportError:
    _tg_signal_alert = None  # type: ignore
from agents.market_intelligence.nse_fetcher import (
    fetch_ohlcv,
    fetch_nse_gainers_losers,
    fetch_sector_performance,
    search_symbols,
    fetch_arbitrary_symbol,
    NSE_SYMBOLS,
)
from agents.data_ingestion.validator import validate as validate_ohlcv
from agents.analytics.trade_journal import get_journal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Modern FastAPI lifespan — replaces deprecated @app.on_event.
    
    Non-blocking startup: server accepts connections immediately,
    market data loads progressively in the background.
    """
    # ── Startup ──────────────────────────────────────────
    init_db()
    logger.info("Starting server — market data will load in background...")
    # Start data loading as a background task (non-blocking)
    asyncio.create_task(_initial_load())
    asyncio.create_task(_background_refresh())
    logger.info("Server ready — accepting connections. Data loading in background.")
    yield
    # ── Shutdown (nothing special needed) ────────────────


app = FastAPI(title="AI Trading System — Live", version="4.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Real data stores ──────────────────────────────────────
_market_data: dict[str, pd.DataFrame] = {}
_signal_cache: dict[str, pd.DataFrame] = {}
_regime_cache: dict[str, str] = {}
_last_refresh: datetime = datetime.min

# Daily data cache for swing scanner & breakout scanner
_daily_data_cache: dict[str, pd.DataFrame] = {}

# Prevents concurrent refresh cycles from producing inconsistent partial snapshots
_refresh_lock: asyncio.Lock | None = None


def _get_refresh_lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock

# Paper trading engine (loads real trade data from paper_trades.json)
_engine: PaperTradingEngine | None = None

# WebSocket clients for real-time updates
_ws_clients: list[WebSocket] = []

# ── Symbols to track ─────────────────────────────────────
# Priority symbols loaded first (indices + top stocks), rest loaded progressively
_PRIORITY_SYMBOLS = list(NSE_SYMBOLS.keys())[:15]   # Fast initial load
_REMAINING_SYMBOLS = list(NSE_SYMBOLS.keys())[15:50]  # Loaded in background after priority
TOP_NSE_SYMBOLS = list(NSE_SYMBOLS.keys())[:50]

# Track loading progress for health endpoint
_loading_status = {"phase": "starting", "loaded": 0, "total": 50, "ready": False}

# Previous signal state per symbol — used to detect NEW signals for Telegram alerts
_prev_signal_cache: dict[str, str] = {}

# Data quality scores per symbol (updated each refresh cycle)
_data_quality_cache: dict[str, float] = {}

# Precomputed snapshots for fast dashboard responses
_movers_cache: dict[str, object] = {"gainers": [], "losers": [], "updated_at": None}
_market_mood_cache: dict[str, object] = {"score": 50, "mood": "NEUTRAL", "updated_at": None}

# Regime hysteresis: (confirmed_regime, tentative_regime, consecutive_count)
_regime_hysteresis: dict[str, tuple[str, str, int]] = {}
REGIME_HYSTERESIS_MIN = 3  # Must see new regime N times before switching


def _stable_regime(symbol: str, new_regime: MarketRegime) -> MarketRegime:
    """Only switch regime after it's been detected N consecutive times."""
    state = _regime_hysteresis.get(symbol)
    nv = new_regime.value

    if state is None:
        _regime_hysteresis[symbol] = (nv, nv, 1)
        return new_regime

    confirmed, tentative, count = state

    if nv == confirmed:
        # Same as confirmed — reset any tentative
        _regime_hysteresis[symbol] = (confirmed, confirmed, 0)
        return MarketRegime(confirmed)
    elif nv == tentative:
        # Continuing tentative
        count += 1
        if count >= REGIME_HYSTERESIS_MIN:
            _regime_hysteresis[symbol] = (nv, nv, 0)
            return new_regime  # Confirmed switch
        else:
            _regime_hysteresis[symbol] = (confirmed, tentative, count)
            return MarketRegime(confirmed)  # Keep old
    else:
        # New tentative regime
        _regime_hysteresis[symbol] = (confirmed, nv, 1)
        return MarketRegime(confirmed)


def _get_engine() -> PaperTradingEngine:
    """Get or create the paper trading engine (loads real trade history)."""
    global _engine
    if _engine is None:
        _engine = PaperTradingEngine(initial_capital=100_000)
    return _engine


# Startup logic is now handled in the lifespan context manager above.


async def _initial_load():
    """Non-blocking progressive data load — runs as a background task after server starts.
    
    Phase 1: Load priority symbols (15) — fast, gets dashboard functional
    Phase 2: Load remaining symbols (35) in batches of 10
    Phase 3: Load daily data for swing/breakout scanners
    """
    global _loading_status
    try:
        _loading_status = {"phase": "priority", "loaded": 0, "total": len(_PRIORITY_SYMBOLS), "ready": False}
        logger.info("Phase 1: Loading %d priority symbols...", len(_PRIORITY_SYMBOLS))
        
        sem = asyncio.Semaphore(3)
        # Phase 1: Priority symbols
        tasks = [_fetch_nse_symbol_safe(name, sem) for name in _PRIORITY_SYMBOLS]
        await asyncio.gather(*tasks)
        _loading_status["loaded"] = len(_market_data)
        _loading_status["ready"] = True  # Dashboard is now functional
        logger.info("Phase 1 complete: %d priority symbols loaded. Dashboard ready.", len(_market_data))
        
        # Phase 2: Remaining symbols in batches of 10
        _loading_status["phase"] = "remaining"
        _loading_status["total"] = len(TOP_NSE_SYMBOLS)
        for i in range(0, len(_REMAINING_SYMBOLS), 10):
            batch = _REMAINING_SYMBOLS[i:i+10]
            logger.info("Phase 2: Loading batch %d-%d (%d symbols)...", i, i+len(batch), len(batch))
            batch_tasks = [_fetch_nse_symbol_safe(name, sem) for name in batch]
            await asyncio.gather(*batch_tasks)
            _loading_status["loaded"] = len(_market_data)
            # Small pause between batches to avoid rate limiting
            await asyncio.sleep(2)
        
        logger.info("Phase 2 complete: %d total symbols loaded.", len(_market_data))
        
        # Phase 3: Daily data for scanners (only top 20)
        _loading_status["phase"] = "daily"
        try:
            daily_sem = asyncio.Semaphore(3)
            async def _fetch_daily_safe(name: str, sem: asyncio.Semaphore):
                async with sem:
                    try:
                        df = await asyncio.wait_for(
                            asyncio.to_thread(fetch_ohlcv, name, "60d", "1d"),
                            timeout=30.0,
                        )
                        if df is not None and not df.empty and len(df) >= 15:
                            df = calculate_indicators(df)
                            _daily_data_cache[name] = df
                    except Exception as e:
                        logger.debug("Daily fetch failed for %s: %s", name, e)

            daily_tasks = [_fetch_daily_safe(n, daily_sem) for n in _PRIORITY_SYMBOLS]
            await asyncio.gather(*daily_tasks)
            logger.info("Phase 3 complete: %d daily symbols for scanners.", len(_daily_data_cache))
        except Exception as e:
            logger.error("Daily data fetch failed: %s", e)
        
        _loading_status["phase"] = "complete"
        _loading_status["loaded"] = len(_market_data)
        global _last_refresh
        _last_refresh = datetime.now()
        logger.info("Initial load complete. Tracking %d symbols.", len(_market_data))
        
    except Exception as e:
        logger.error("Initial load failed: %s — will retry on next background refresh", e)
        _loading_status["phase"] = "error"


async def _fetch_nse_symbol_safe(display_name: str, sem: asyncio.Semaphore) -> None:
    """Wrapper around _fetch_nse_symbol with a 90-second per-symbol timeout."""
    try:
        await asyncio.wait_for(
            _fetch_nse_symbol(display_name, sem),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Symbol %s timed out after 90s — skipping", display_name)
    except Exception as e:
        logger.error("Symbol %s failed: %s", display_name, e)


async def _background_refresh():
    """Background task: refresh market data every 60 seconds.
    Waits for initial load to complete before starting refresh cycle."""
    # Wait until initial load is done
    while not _loading_status.get("ready"):
        await asyncio.sleep(5)
    
    while True:
        await asyncio.sleep(60)
        lock = _get_refresh_lock()
        if lock.locked():
            logger.debug("Background refresh skipped — previous refresh still running")
            continue
        try:
            logger.info("Background refresh triggered...")
            await _refresh_market_data()
            logger.info("Background refresh complete. %d symbols loaded.", len(_market_data))
        except Exception as e:
            logger.error("Background refresh failed: %s", e)


def _fire_telegram_signal(display_name: str, df: pd.DataFrame) -> None:
    """
    Fire a Telegram alert if the latest signal is new (changed since last refresh).
    Also records the signal in the trade journal.
    Called from a thread pool — must not use async primitives.
    """
    try:
        last = df.iloc[-1]
        latest_signal = str(last.get("signal", "HOLD"))
        prev = _prev_signal_cache.get(display_name)

        if latest_signal in ("LONG", "SHORT") and latest_signal != prev:
            sl = last.get("stop_loss")
            tp = last.get("target")

            # ── Telegram alert ────────────────────────────────────
            if _tg_signal_alert:
                _tg_signal_alert(
                    symbol=display_name,
                    direction=latest_signal,
                    entry=float(last["close"]),
                    stop_loss=float(sl) if pd.notna(sl) else float(last["close"]),
                    target=float(tp) if pd.notna(tp) else float(last["close"]),
                    confidence=float(last.get("confidence", last.get("ai_confidence", 0))),
                    trend=str(last.get("htf_trend", "")),
                    rsi=float(last["RSI_14"]) if pd.notna(last.get("RSI_14")) else 0.0,
                    regime=_regime_cache.get(display_name, ""),
                )

            # ── Trade journal recording ───────────────────────────
            try:
                signal_data = {
                    "symbol":        display_name,
                    "signal":        latest_signal,
                    "timestamp":     last["timestamp"].isoformat() if hasattr(last.get("timestamp"), "isoformat") else str(last.get("timestamp", "")),
                    "confidence":    float(last.get("confidence", last.get("ai_confidence", 0))),
                    "regime":        _regime_cache.get(display_name, "RANGING"),
                    "quality_score": int(last.get("quality_score", 0)),
                    "quality_grade": str(last.get("quality_grade", "D")),
                }
                data_quality = _data_quality_cache.get(display_name, 100.0)
                get_journal().record_signal(signal_data, data_quality)
                # Try to resolve any pending outcomes while df is available
                get_journal().resolve_pending_outcomes(df, display_name)
            except Exception as jexc:
                logger.debug("Journal record failed for %s: %s", display_name, jexc)

        _prev_signal_cache[display_name] = latest_signal
    except Exception as exc:
        logger.warning("Telegram/journal fire failed for %s: %s", display_name, exc)


async def _fetch_nse_symbol(display_name: str, sem: asyncio.Semaphore) -> None:
    """Fetch, validate, process, and cache a single NSE symbol inside a semaphore gate."""
    async with sem:
        try:
            df = await asyncio.to_thread(fetch_ohlcv, display_name, "5d", "5m")
            if df is None or df.empty:
                logger.warning("No 5m data for %s — skipping", display_name)
                return

            # ── Data validation (run before indicator calculation) ────────────
            df, report = await asyncio.to_thread(validate_ohlcv, df, display_name, "5m")
            _data_quality_cache[display_name] = report.quality_pct
            if not report.is_safe:
                logger.warning("Skipping %s: data quality too low (%.1f%%)", display_name, report.quality_pct)
                return

            df = calculate_indicators(df)

            # ── Real AI confidence from ensemble predictor ────────────────────
            ai_confidence = await asyncio.to_thread(predict_batch, df)

            htf_df = await asyncio.to_thread(fetch_ohlcv, display_name, "60d", "1d")
            if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:
                htf_df = calculate_indicators(htf_df)
                htf_df = classify_trend(htf_df)
                df = propagate_trend_to_ltf(df, htf_df)
            else:
                htf_df = None
                df = classify_trend(df)

            # ── MTF score from resampled timeframes ───────────────────────────
            mtf_data = {"5m": df}
            try:
                _agg = {"open": "first", "high": "max", "low": "min",
                        "close": "last", "volume": "sum"}
                df_15m = (df.set_index("timestamp")
                          .resample("15min").agg(_agg)
                          .dropna(subset=["close"]).reset_index())
                if len(df_15m) >= 30:
                    df_15m = calculate_indicators(df_15m)
                    mtf_data["15m"] = df_15m
            except Exception as e:
                logger.debug("15m resample failed for %s: %s", display_name, e)
            if htf_df is not None and not htf_df.empty:
                mtf_data["1d"] = htf_df
            mtf_score = calculate_mtf_score(mtf_data)

            raw_regime = detect_regime(df)
            regime = _stable_regime(display_name, raw_regime)
            _regime_cache[display_name] = regime.value

            # ── Sector / Market Correlation: read Nifty 50 trend ──────────────
            index_symbols = {"NIFTY 50", "BANKNIFTY", "SENSEX", "NIFTY IT", "NIFTY BANK"}
            if display_name not in index_symbols:
                market_trend = _regime_cache.get("NIFTY 50")
                nifty_df = _signal_cache.get("NIFTY 50")
                if nifty_df is not None and not nifty_df.empty:
                    nifty_last_trend = str(nifty_df.iloc[-1].get("htf_trend", ""))
                    if "BULLISH" in nifty_last_trend.upper():
                        market_trend = "BULLISH"
                    elif "BEARISH" in nifty_last_trend.upper():
                        market_trend = "BEARISH"
                    else:
                        market_trend = None
                else:
                    market_trend = None
            else:
                market_trend = None

            df = generate_signals(
                df, ai_confidence=ai_confidence, regime=regime,
                symbol=display_name, market_trend=market_trend,
                mtf_score=mtf_score,
            )
            _market_data[display_name] = df
            _signal_cache[display_name] = df

            # Fire Telegram alert + journal for new LONG/SHORT signals (non-blocking)
            await asyncio.to_thread(_fire_telegram_signal, display_name, df)

        except Exception as e:
            logger.error("Failed to fetch %s: %s", display_name, e)


async def _refresh_market_data():
    """
    Fetch real market data from yFinance/Binance and generate signals.

    Concurrent NSE symbol fetching via semaphore-gated asyncio.gather
    (max 5 concurrent yfinance calls) to keep startup fast even with 50 symbols.
    HTF (1d) trend propagated to 5m data.

    Protected by _refresh_lock so that concurrent callers (background task +
    manual /api/refresh) never produce a partial/interleaved cache snapshot.
    """
    global _last_refresh
    async with _get_refresh_lock():
        # ── Concurrent NSE symbol fetching (with per-symbol timeout) ──
        sem = asyncio.Semaphore(3)
        nse_tasks = [_fetch_nse_symbol_safe(name, sem) for name in TOP_NSE_SYMBOLS]
        await asyncio.gather(*nse_tasks)

        # ── Fetch crypto data via CCXT ────────────────────────
        try:
            from agents.data_ingestion.binance_fetcher import fetch_historical
            for symbol in settings.CRYPTO_SYMBOLS:
                try:
                    df = await asyncio.to_thread(fetch_historical, symbol, "1h", 7)
                    if df is None or df.empty:
                        continue

                    # Data validation
                    df, report = await asyncio.to_thread(validate_ohlcv, df, symbol, "1h")
                    _data_quality_cache[symbol] = report.quality_pct
                    if not report.is_safe:
                        logger.warning("Skipping %s: data quality too low (%.1f%%)", symbol, report.quality_pct)
                        continue

                    df = calculate_indicators(df)

                    htf_df = await asyncio.to_thread(fetch_historical, symbol, "1d", 60)
                    if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:
                        htf_df = calculate_indicators(htf_df)
                        htf_df = classify_trend(htf_df)
                        df = propagate_trend_to_ltf(df, htf_df)
                    else:
                        df = classify_trend(df)

                    raw_regime = detect_regime(df)
                    regime = _stable_regime(symbol, raw_regime)
                    _regime_cache[symbol] = regime.value
                    ai_conf_crypto = await asyncio.to_thread(predict_batch, df)
                    df = generate_signals(df, ai_confidence=ai_conf_crypto, regime=regime, symbol=symbol)
                    _market_data[symbol] = df
                    _signal_cache[symbol] = df

                    await asyncio.to_thread(_fire_telegram_signal, symbol, df)

                except Exception as e:
                    logger.error("Failed to fetch %s: %s", symbol, e)
        except ImportError:
            logger.warning("CCXT not available for crypto data")

        # ── Fetch daily data for swing/breakout scanners ─────────
        try:
            daily_sem = asyncio.Semaphore(3)
            async def _fetch_daily(name: str, sem: asyncio.Semaphore):
                async with sem:
                    try:
                        df = await asyncio.wait_for(
                            asyncio.to_thread(fetch_ohlcv, name, "60d", "1d"),
                            timeout=30.0,
                        )
                        if df is not None and not df.empty and len(df) >= 15:
                            df = calculate_indicators(df)
                            _daily_data_cache[name] = df
                    except asyncio.TimeoutError:
                        logger.debug("Daily fetch timed out for %s", name)
                    except Exception as e:
                        logger.debug("Daily fetch failed for %s: %s", name, e)

            daily_tasks = [_fetch_daily(n, daily_sem) for n in TOP_NSE_SYMBOLS[:20]]
            await asyncio.gather(*daily_tasks)
            logger.info("Daily data fetched: %d symbols for swing/breakout.", len(_daily_data_cache))
        except Exception as e:
            logger.error("Daily data batch fetch failed: %s", e)

        # Precompute dashboard snapshots from latest cache state
        try:
            _refresh_movers_cache()
            _compute_market_mood()
        except Exception as e:
            logger.debug("Dashboard precompute failed: %s", e)

        _last_refresh = datetime.now()
        logger.info("Market data refreshed: %d symbols loaded", len(_market_data))


# ── API Endpoints ────────────────────────────────────────


def _clean_trend(raw) -> str:
    """Return plain string trend value, stripping any enum class prefix."""
    s = str(raw) if raw is not None else "NEUTRAL"
    return s.split(".")[-1] if "." in s else s


def _get_signal_for_symbol(symbol: str) -> dict:
    """Extract the latest signal data for a symbol (shared helper)."""
    if symbol not in _signal_cache:
        return None

    df = _signal_cache[symbol]
    last = df.iloc[-1]

    # Always use current-bar values for these live fields
    current_close = round(float(last["close"]), 2)
    current_rsi = round(float(last["RSI_14"]), 1) if pd.notna(last.get("RSI_14")) else None
    current_trend = _clean_trend(last.get("htf_trend", "NEUTRAL"))
    current_regime = _regime_cache.get(symbol, "RANGING")
    current_ts = last["timestamp"].isoformat()
    current_conf = round(float(last.get("confidence", last.get("ai_confidence", 0))), 0)

    # Look for non-HOLD signal within the LAST 100 bars (recent window)
    recent = df.tail(100)
    signals = recent[recent["signal"] != "HOLD"]

    if signals.empty:
        return {
            "symbol": symbol,
            "signal": "HOLD",
            "confidence": current_conf,
            "entry": current_close,
            "close": current_close,
            "stop_loss": None,
            "target": None,
            "rsi": current_rsi,
            "trend": current_trend,
            "regime": current_regime,
            "timestamp": current_ts,
            "stale": False,
        }

    last_signal = signals.iloc[-1]
    stale = False

    # Pull live indicator values from current bar (last row of df)
    def _safe_float(row, col):
        v = row.get(col)
        return round(float(v), 4) if v is not None and pd.notna(v) else None

    def _safe_int(row, col, default=0):
        v = row.get(col)
        return int(v) if v is not None and pd.notna(v) else default

    # Quality info from the signal bar
    quality_score = _safe_int(last_signal, "quality_score", 0)
    quality_grade = str(last_signal.get("quality_grade", "D") or "D")
    quality_breakdown_raw = last_signal.get("quality_breakdown", "{}")
    try:
        quality_breakdown = json.loads(str(quality_breakdown_raw)) if quality_breakdown_raw else {}
    except Exception:
        quality_breakdown = {}

    # Determine underlying direction for SKIP signals
    sig_label = str(last_signal["signal"])
    raw_dir = str(last_signal.get("_raw_direction", sig_label))

    # Entry price = signal bar's close
    entry_price = round(float(last_signal["close"]), 2)

    # SL/Target from signal bar — validate they make sense relative to entry
    sl_raw = last_signal.get("stop_loss")
    tp_raw = last_signal.get("target")
    sl = round(float(sl_raw), 2) if pd.notna(sl_raw) else None
    tp = round(float(tp_raw), 2) if pd.notna(tp_raw) else None

    # Sanity check: SL must be on the correct side of entry
    effective_dir = sig_label if sig_label in ("LONG", "SHORT") else raw_dir
    if sl is not None and effective_dir == "LONG" and sl >= entry_price:
        # SL above entry for LONG = wrong, recalculate using current ATR
        atr_val = _safe_float(last, f"ATR_{settings.ATR_PERIOD}")
        if atr_val:
            sl = round(entry_price - atr_val * settings.ATR_SL_MULTIPLIER, 2)
            tp = round(entry_price + atr_val * settings.ATR_SL_MULTIPLIER * settings.MIN_RISK_REWARD, 2)
    elif sl is not None and effective_dir == "SHORT" and sl <= entry_price:
        atr_val = _safe_float(last, f"ATR_{settings.ATR_PERIOD}")
        if atr_val:
            sl = round(entry_price + atr_val * settings.ATR_SL_MULTIPLIER, 2)
            tp = round(entry_price - atr_val * settings.ATR_SL_MULTIPLIER * settings.MIN_RISK_REWARD, 2)

    return {
        "symbol":    symbol,
        "signal":    sig_label,                     # LONG / SHORT / SKIP / HOLD
        "direction": effective_dir,                 # Underlying direction for SKIPs
        "confidence": round(float(last_signal.get("confidence", last_signal.get("ai_confidence", 0))), 0),
        "entry":     entry_price,
        "close":     current_close,                # always current price
        "stop_loss": sl,
        "target":    tp,
        "rsi":       current_rsi,                  # always current RSI
        "trend":     current_trend,                # always current trend
        "regime":    current_regime,
        "timestamp": last_signal["timestamp"].isoformat(),
        "stale":     stale,
        # Signal quality
        "quality_score":     quality_score,
        "quality_grade":     quality_grade,
        "quality_breakdown": quality_breakdown,
        # Data quality
        "data_quality": round(_data_quality_cache.get(symbol, 100.0), 1),
        # Live indicator values for signal card display
        "macd_hist": _safe_float(last, "MACD_hist"),
        "ema_20":    _safe_float(last, "EMA_20"),
        "ema_50":    _safe_float(last, "EMA_50"),
    }


@app.get("/api/health")
async def health_check():
    """Health check endpoint — shows loading progress and server status."""
    return {
        "status": "ok",
        "loading": _loading_status,
        "symbols_loaded": len(_market_data),
        "signals_cached": len(_signal_cache),
        "last_refresh": _last_refresh.isoformat() if _last_refresh != datetime.min else None,
        "uptime": datetime.now().isoformat(),
    }


@app.get("/api/signals/all")
async def get_all_signals():
    """Get the latest signal for every tracked symbol."""
    results = []
    for symbol in _signal_cache:
        sig = _get_signal_for_symbol(symbol)
        if sig:
            results.append(sig)
    return {"signals": results, "updated_at": datetime.now().isoformat()}


@app.get("/api/symbols")
async def get_symbols():
    """List all available symbols."""
    return {"symbols": list(_market_data.keys())}


def _build_market_records(df: pd.DataFrame) -> list[dict]:
    """Convert a market DataFrame to a list of API-friendly record dicts."""
    records = []
    for _, row in df.iterrows():
        records.append({
            "timestamp": row["timestamp"].isoformat(),
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
            "volume": round(float(row["volume"]), 0),
            "rsi": round(float(row["RSI_14"]), 1) if pd.notna(row.get("RSI_14")) else None,
            "ema_20": round(float(row["EMA_20"]), 2) if pd.notna(row.get("EMA_20")) else None,
            "ema_50": round(float(row["EMA_50"]), 2) if pd.notna(row.get("EMA_50")) else None,
            "ema_200": round(float(row["EMA_200"]), 2) if pd.notna(row.get("EMA_200")) else None,
            "vwap": round(float(row["VWAP"]), 2) if pd.notna(row.get("VWAP")) else None,
            "bb_upper": round(float(row["BB_upper"]), 2) if pd.notna(row.get("BB_upper")) else None,
            "bb_lower": round(float(row["BB_lower"]), 2) if pd.notna(row.get("BB_lower")) else None,
            "adx": round(float(row[f"ADX_{settings.ADX_PERIOD}"]), 1) if pd.notna(row.get(f"ADX_{settings.ADX_PERIOD}")) else None,
            "signal": str(row.get("signal", "HOLD")) if "signal" in row.index else "HOLD",
        })
    return records


@app.get("/api/market/{symbol}")
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
    symbol = symbol.replace("-", "/")
    if interval not in ("5m", "15m", "1h", "1d"):
        interval = "5m"

    if interval == "5m" and symbol in _market_data:
        df = _market_data[symbol].tail(limit)
        return {"symbol": symbol, "data": _build_market_records(df), "interval": interval}

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
    return {"symbol": symbol, "data": _build_market_records(df), "interval": interval}


@app.get("/api/signal/{symbol}")
async def get_current_signal(symbol: str):
    """Get the latest signal for a symbol."""
    symbol = symbol.replace("-", "/")
    result = _get_signal_for_symbol(symbol)
    if result is None:
        return {"error": f"Symbol {symbol} not found"}
    return result


@app.get("/api/trades")
async def get_trades(limit: int = Query(20, le=100)):
    """Get recent trade history from real paper trading engine."""
    engine = _get_engine()
    return {"trades": engine.get_history(limit=limit)}


@app.get("/api/portfolio")
async def get_portfolio():
    """Get open positions and unrealized P&L from real trading engine."""
    engine = _get_engine()
    open_trades = engine.get_open_trades()

    total_unrealized = 0
    for t in open_trades:
        symbol = t["symbol"]
        if symbol in _market_data:
            current_close = float(_market_data[symbol].iloc[-1]["close"])
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


@app.get("/api/performance")
async def get_performance():
    """Get detailed performance metrics and equity curve from real trading engine."""
    from collections import defaultdict, Counter

    engine = _get_engine()
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
        "equity_curve":       equity_curve,
        "trade_distribution": {"wins": len(wins), "losses": len(losses)},
        "monthly_pnl":        monthly_pnl,
        "exit_reasons":       exit_reasons,
        "symbol_breakdown":   symbol_breakdown,
        "recent_trades":      list(reversed(closed_trades[-50:])),
    }


@app.get("/api/regime/{symbol}")
async def get_regime(symbol: str):
    """Get current market regime for a symbol."""
    symbol = symbol.replace("-", "/")
    if symbol not in _regime_cache:
        return {"error": f"Symbol {symbol} not found"}

    return {
        "symbol": symbol,
        "regime": _regime_cache[symbol],
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/stats")
async def get_stats():
    """Get dashboard summary statistics from real trading engine."""
    engine = _get_engine()
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


@app.get("/api/backtest/{symbol}")
async def run_backtest(symbol: str):
    """Run backtest on cached data for a symbol."""
    symbol = symbol.replace("-", "/")
    if symbol not in _signal_cache:
        return {"error": f"Symbol {symbol} not found"}

    df = _signal_cache[symbol]
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

def _compute_gainers_losers_from_cache(mode: str) -> list[dict]:
    """
    Fallback: compute gainers/losers from the in-memory _market_data cache
    when the NSE screener API is unavailable or times out.
    Compares current close vs day's first bar (open) for meaningful daily % change.
    """
    results = []
    for symbol, df in _market_data.items():
        if df is None or len(df) < 5:
            continue
        # Skip indices for screener (only stocks)
        if symbol.startswith(("NIFTY", "BANKNIFTY", "SENSEX")):
            continue
        try:
            close = float(df.iloc[-1]["close"])

            # Find today's open: first bar of the current trading day
            today = df["timestamp"].iloc[-1].date() if hasattr(df["timestamp"].iloc[-1], "date") else None
            if today:
                today_bars = df[df["timestamp"].dt.date == today]
                if len(today_bars) >= 2:
                    day_open = float(today_bars.iloc[0]["open"])
                else:
                    # Weekend/holiday: use first bar's open from available data
                    day_open = float(df.iloc[0]["open"])
            else:
                day_open = float(df.iloc[0]["open"])

            if day_open == 0:
                continue

            pct_change = ((close - day_open) / day_open) * 100

            # Volume: sum of today's bars (total daily volume so far)
            if today and len(today_bars) > 0:
                total_vol = float(today_bars["volume"].sum())
            else:
                total_vol = float(df["volume"].tail(20).sum())
            vol_cr = round(total_vol * close / 1e7, 1) if total_vol > 0 else 0.1

            # X-Factor: abs(pct_change) normalized later
            results.append({
                "symbol": symbol,
                "company": symbol,
                "sector": _get_sector_for_symbol(symbol),
                "ltp": round(close, 2),
                "pct_change": round(pct_change, 2),
                "volume_cr": max(vol_cr, 0.1),
                "x_factor": 1.0,  # normalized below
                "oi_change": None,
            })
        except Exception:
            continue

    if not results:
        return []

    # Normalize X-Factor relative to avg % change
    avg_abs = np.mean([abs(r["pct_change"]) for r in results]) or 1.0
    for r in results:
        r["x_factor"] = round(abs(r["pct_change"]) / avg_abs, 2)

    # Sort: gainers = highest pct_change, losers = lowest pct_change
    if mode == "gainers":
        results.sort(key=lambda x: x["pct_change"], reverse=True)
        results = [r for r in results if r["pct_change"] > 0]
    else:
        results.sort(key=lambda x: x["pct_change"])
        results = [r for r in results if r["pct_change"] < 0]

    return results[:20]


# Sector mapping for cache-based screener/heatmap
_SYMBOL_SECTOR_MAP = {
    "RELIANCE": "Energy", "TCS": "IT", "HDFCBANK": "Finance",
    "INFY": "IT", "ICICIBANK": "Finance", "SBIN": "Finance",
    "BHARTIARTL": "Telecom", "KOTAKBANK": "Finance", "LT": "Infra",
    "ITC": "FMCG", "AXISBANK": "Finance", "HINDUNILVR": "FMCG",
    "BAJFINANCE": "Finance", "MARUTI": "Auto", "TITAN": "Consumer",
    "WIPRO": "IT", "TATAMOTORS": "Auto", "SUNPHARMA": "Pharma",
    "ULTRACEMCO": "Infra", "ADANIENT": "Metals", "ADANIPORTS": "Infra",
    "ADANIPOWER": "Energy", "ADANIGREEN": "Energy", "TATASTEEL": "Metals",
    "JSWSTEEL": "Metals", "HINDALCO": "Metals", "COALINDIA": "Energy",
    "NTPC": "Energy", "POWERGRID": "Energy", "ONGC": "Energy",
    "BPCL": "Energy", "IOC": "Energy", "GAIL": "Energy",
    "TECHM": "IT", "HCLTECH": "IT", "DRREDDY": "Pharma",
    "CIPLA": "Pharma", "DIVISLAB": "Pharma", "APOLLOHOSP": "Pharma",
    "ASIANPAINT": "Consumer", "NESTLEIND": "FMCG", "BRITANNIA": "FMCG",
    "DABUR": "FMCG", "GODREJCP": "FMCG", "PIDILITIND": "Consumer",
    "BERGEPAINT": "Consumer", "HAVELLS": "Consumer", "VOLTAS": "Consumer",
    "SIEMENS": "Infra", "ABB": "Infra", "BHEL": "Infra",
    "M&M": "Auto", "EICHERMOT": "Auto", "HEROMOTOCO": "Auto",
    "BAJAJ-AUTO": "Auto", "TVSMOTOR": "Auto", "INDUSINDBK": "Finance",
    "BANDHANBNK": "Finance", "PNB": "Finance", "BANKBARODA": "Finance",
    "HAL": "Defence", "BEL": "Defence", "IRCTC": "Consumer",
    "ZOMATO": "Consumer", "TATAPOWER": "Energy", "DLF": "Realty",
    "GODREJPROP": "Realty", "OBEROIRLTY": "Realty", "PRESTIGE": "Realty",
    "TRENT": "Consumer", "DMART": "Consumer",
}

_SECTOR_ICONS = {
    "IT": "💻", "Finance": "🏦", "FMCG": "🛒", "Auto": "🚗",
    "Pharma": "💊", "Energy": "⚡", "Metals": "⚙️", "Infra": "🏗️",
    "Telecom": "📡", "Consumer": "🛍️", "Realty": "🏢", "Defence": "🛡️",
}


def _get_sector_for_symbol(symbol: str) -> str:
    """Get sector for a symbol from the mapping."""
    return _SYMBOL_SECTOR_MAP.get(symbol, "Others")


def _compute_sectors_from_cache() -> list[dict]:
    """
    Compute sector performance from _market_data cache.
    Groups symbols by sector, averages their daily % change.
    """
    sector_changes: dict[str, list[float]] = {}

    for symbol, df in _market_data.items():
        if df is None or len(df) < 5:
            continue
        if symbol.startswith(("NIFTY", "BANKNIFTY", "SENSEX")):
            continue

        sector = _get_sector_for_symbol(symbol)
        if sector == "Others":
            continue

        try:
            close = float(df.iloc[-1]["close"])
            today = df["timestamp"].iloc[-1].date() if hasattr(df["timestamp"].iloc[-1], "date") else None
            if today:
                today_bars = df[df["timestamp"].dt.date == today]
                day_open = float(today_bars.iloc[0]["open"]) if len(today_bars) >= 2 else float(df.iloc[0]["open"])
            else:
                day_open = float(df.iloc[0]["open"])

            if day_open > 0:
                pct = ((close - day_open) / day_open) * 100
                sector_changes.setdefault(sector, []).append(pct)
        except Exception:
            continue

    sectors = []
    for name, changes in sector_changes.items():
        if not changes:
            continue
        avg_pct = round(float(np.mean(changes)), 2)
        total = len(changes) * 3  # approximate total stocks in sector
        adv = int(total * max(0.1, (avg_pct + 3.5) / 7.0))
        dec = total - adv

        sectors.append({
            "name": name,
            "pct_change": avg_pct,
            "stocks": total,
            "adv": adv,
            "dec": dec,
            "icons": _SECTOR_ICONS.get(name, "📊"),
            "real_data": True,
        })

    # Sort by absolute change (most active sectors first)
    sectors.sort(key=lambda x: abs(x["pct_change"]), reverse=True)
    return sectors


def _refresh_movers_cache() -> None:
    """Precompute gainers/losers from the in-memory market snapshot."""
    gainers = _compute_gainers_losers_from_cache("gainers")
    losers = _compute_gainers_losers_from_cache("losers")
    _movers_cache["gainers"] = gainers
    _movers_cache["losers"] = losers
    _movers_cache["updated_at"] = datetime.now().isoformat()


def _compute_market_mood() -> dict:
    """Compute market mood score from breadth, signals, regimes, and sectors."""
    gainers = _movers_cache.get("gainers") or []
    losers = _movers_cache.get("losers") or []
    if not gainers and not losers:
        gainers = _compute_gainers_losers_from_cache("gainers")
        losers = _compute_gainers_losers_from_cache("losers")

    total_movers = len(gainers) + len(losers)
    breadth_pct = ((len(gainers) - len(losers)) / total_movers * 100.0) if total_movers else 0.0

    long_count = 0
    short_count = 0
    hold_count = 0
    for symbol in _signal_cache:
        sig = _get_signal_for_symbol(symbol)
        if not sig:
            continue
        sig_label = str(sig.get("signal", "HOLD")).upper()
        if sig_label == "LONG":
            long_count += 1
        elif sig_label == "SHORT":
            short_count += 1
        else:
            hold_count += 1

    signal_total = long_count + short_count + hold_count
    signal_bias_pct = ((long_count - short_count) / signal_total * 100.0) if signal_total else 0.0

    regime_counts = {"TRENDING": 0, "RANGING": 0, "HIGH_VOLATILITY": 0}
    for regime in _regime_cache.values():
        key = str(regime).upper()
        if key in regime_counts:
            regime_counts[key] += 1

    regime_total = sum(regime_counts.values())
    regime_bias = 0.0
    if regime_total:
        regime_bias = (
            (regime_counts["TRENDING"] * 1.0) +
            (regime_counts["RANGING"] * 0.0) +
            (regime_counts["HIGH_VOLATILITY"] * -1.0)
        ) / regime_total * 100.0

    sectors = _compute_sectors_from_cache()
    avg_sector_move = float(np.mean([s["pct_change"] for s in sectors])) if sectors else 0.0
    sector_bias = max(min(avg_sector_move * 15.0, 100.0), -100.0)

    score = 50.0
    score += breadth_pct * 0.25
    score += signal_bias_pct * 0.35
    score += regime_bias * 0.25
    score += sector_bias * 0.15
    score = max(0.0, min(100.0, score))

    if score >= 68:
        mood = "BULLISH"
    elif score >= 52:
        mood = "NEUTRAL"
    elif score >= 40:
        mood = "CAUTIOUS"
    else:
        mood = "BEARISH"

    payload = {
        "mood": mood,
        "score": round(score, 1),
        "breadth": {
            "gainers": len(gainers),
            "losers": len(losers),
            "breadth_pct": round(breadth_pct, 1),
        },
        "signals": {
            "long": long_count,
            "short": short_count,
            "hold": hold_count,
            "bias_pct": round(signal_bias_pct, 1),
        },
        "regimes": regime_counts,
        "sector_avg_pct": round(avg_sector_move, 2),
        "updated_at": datetime.now().isoformat(),
    }

    _market_mood_cache.update(payload)
    return payload


def _build_ai_market_summary(mood_payload: dict | None = None) -> dict:
    """Generate a concise 2-3 line narrative summary from current market state."""
    mood = mood_payload or _compute_market_mood()
    gainers = list((_movers_cache.get("gainers") or [])[:2])
    losers = list((_movers_cache.get("losers") or [])[:2])

    top_gainer = gainers[0]["symbol"] if gainers else "NA"
    top_gainer_pct = gainers[0].get("pct_change", 0.0) if gainers else 0.0
    top_loser = losers[0]["symbol"] if losers else "NA"
    top_loser_pct = losers[0].get("pct_change", 0.0) if losers else 0.0

    lines = [
        (
            f"Market mood is {mood.get('mood', 'NEUTRAL')} "
            f"with score {mood.get('score', 50):.1f}/100."
        ),
        (
            f"Breadth: {mood.get('breadth', {}).get('gainers', 0)} gainers vs "
            f"{mood.get('breadth', {}).get('losers', 0)} losers; "
            f"signal bias {mood.get('signals', {}).get('bias_pct', 0):+.1f}%."
        ),
        (
            f"Top movers: {top_gainer} ({top_gainer_pct:+.2f}%) and "
            f"{top_loser} ({top_loser_pct:+.2f}%)."
        ),
    ]

    return {
        "lines": lines,
        "summary": " ".join(lines),
        "generated_at": datetime.now().isoformat(),
    }


@app.get("/api/screener/gainers")
async def screener_gainers():
    """
    Top NSE gainers with X-Factor multiplier.
    Primary: fetch from NSE screener API.
    Fallback: compute from _market_data cache if API fails/times out.
    """
    # Try precomputed cache first for instant response
    if _movers_cache.get("gainers"):
        data = _movers_cache.get("gainers")
        if data:
            logger.info("Screener gainers from precomputed cache: %d stocks", len(data))
            return {
                "data": data,
                "real_data": True,
                "source": "cache",
                "updated_at": _movers_cache.get("updated_at"),
            }

    # Fallback to direct cache computation
    if _market_data:
        data = _compute_gainers_losers_from_cache("gainers")
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


@app.get("/api/screener/losers")
async def screener_losers():
    """
    Top NSE losers with X-Factor multiplier.
    Primary: fetch from NSE screener API.
    Fallback: compute from _market_data cache if API fails/times out.
    """
    # Try precomputed cache first for instant response
    if _movers_cache.get("losers"):
        data = _movers_cache.get("losers")
        if data:
            logger.info("Screener losers from precomputed cache: %d stocks", len(data))
            return {
                "data": data,
                "real_data": True,
                "source": "cache",
                "updated_at": _movers_cache.get("updated_at"),
            }

    # Fallback to direct cache computation
    if _market_data:
        data = _compute_gainers_losers_from_cache("losers")
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


@app.get("/api/sectors")
async def sector_heatmap():
    """
    Sectoral performance heatmap data.
    Cache-first: compute from _market_data for instant response.
    Fallback: fetch from yFinance if cache is empty.
    """
    # Try cache first for instant response
    if _market_data:
        sectors = _compute_sectors_from_cache()
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


@app.get("/api/analytics/scorecard")
async def get_scorecard():
    """
    Multi-dimensional win-rate scorecard from the trade journal.
    Dimensions: regime, symbol, hour, confidence bucket, quality grade.
    Also lists false signal reason distribution.
    """
    scorecard = await asyncio.to_thread(get_journal().get_scorecard)
    return scorecard


@app.get("/api/analytics/signals")
async def get_journal_signals(limit: int = Query(50, le=500)):
    """Most recent N recorded signals with their outcomes."""
    signals = await asyncio.to_thread(get_journal().get_recent_signals, limit)
    return {"signals": signals, "total": get_journal().total_count()}


@app.get("/api/broker/status")
async def broker_status():
    """Return which broker APIs are configured for real option chain data."""
    try:
        from agents.market_intelligence.broker_api import get_broker_status
        status = get_broker_status()
    except Exception:
        status = {"zerodha": False, "angel": False, "any": False}
    return {
        "status": status,
        "message": (
            "Real option chain data active"
            if status.get("any") else
            "No broker configured — option chain shows OFFLINE. "
            "Add ZERODHA_API_KEY or ANGEL_API_KEY to .env for real OI data."
        ),
    }


@app.get("/api/pcr/{symbol}")
async def get_pcr(symbol: str):
    """Put-Call Ratio and option chain data for a symbol."""
    symbol = symbol.replace("-", " ").replace("_", " ")
    data = await asyncio.to_thread(generate_pcr_data, symbol)
    return data


@app.get("/api/tradeflow")
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


@app.get("/api/market/mood")
async def get_market_mood():
    """Get computed market mood from breadth, signals, regimes, and sectors."""
    return _compute_market_mood()


@app.get("/api/nlp/summary")
async def get_ai_summary(symbol: str | None = Query(default=None)):
    """Get a concise AI-style summary for market or a specific symbol."""
    if symbol:
        clean = symbol.replace("-", "/")
        sig = _get_signal_for_symbol(clean)
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

    mood = _compute_market_mood()
    return _build_ai_market_summary(mood)


@app.get("/api/nse/dashboard")
async def get_nse_dashboard_snapshot():
    """Aggregate NSE dashboard snapshot for faster UI bootstrap."""
    if not _movers_cache.get("gainers") and _market_data:
        _refresh_movers_cache()

    mood = _compute_market_mood()
    summary = _build_ai_market_summary(mood)
    movers_updated_at = _movers_cache.get("updated_at") or datetime.now().isoformat()

    gainers = list((_movers_cache.get("gainers") or [])[:7])
    losers = list((_movers_cache.get("losers") or [])[:7])

    return {
        "timestamp": datetime.now().isoformat(),
        "last_refresh": _last_refresh.isoformat() if _last_refresh != datetime.min else None,
        "loading": _loading_status,
        "mood": mood,
        "ai_summary": summary,
        "movers": {
            "gainers": gainers,
            "losers": losers,
            "updated_at": movers_updated_at,
        },
    }


# ── Swing Scanner Endpoint ────────────────────────────────────

@app.get("/api/swing-scanner")
async def get_swing_setups():
    """
    SwingSpectrum — scans all tracked symbols for multi-day swing trade setups.
    Uses daily OHLCV data from _daily_data_cache (populated during background refresh).
    Patterns: pullback-to-EMA, range breakout, RSI divergence, golden/death cross,
    inside bar breakout, BB squeeze.
    """
    if not _daily_data_cache:
        return {"setups": [], "message": "Daily data not yet loaded. Wait for next refresh cycle.",
                "real_data": False, "updated_at": datetime.now().isoformat()}

    try:
        setups = await asyncio.to_thread(swing_scan_all, _daily_data_cache)
    except Exception as e:
        logger.error("Swing scanner failed: %s", e)
        setups = []

    return {
        "setups": setups[:30],
        "total_found": len(setups),
        "symbols_scanned": len(_daily_data_cache),
        "real_data": True,
        "updated_at": datetime.now().isoformat(),
    }


# ── Index Mover Endpoint ─────────────────────────────────────

@app.get("/api/index-mover/{index_name}")
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
            calculate_index_movers, clean_name, _market_data
        )
        # Remove internal cache key before sending to client
        data.pop("_fetched_at", None)
    except Exception as e:
        logger.error("Index mover failed for %s: %s", clean_name, e)
        data = {"error": str(e)}

    return data


# ── Breakout Scanner Endpoint ────────────────────────────────

@app.get("/api/breakouts")
async def get_breakouts(
    min_strength: int = Query(30, ge=0, le=100),
):
    """
    BreakoutScanner — scans all tracked symbols for volume-confirmed
    10-day, 50-day, and 52-week breakouts.
    """
    if not _daily_data_cache:
        return {"all": [], "message": "Daily data not yet loaded.",
                "real_data": False, "updated_at": datetime.now().isoformat()}

    try:
        data = await asyncio.to_thread(
            breakout_scan_all, _daily_data_cache, min_strength
        )
    except Exception as e:
        logger.error("Breakout scanner failed: %s", e)
        data = {"all": [], "total_breakouts": 0, "real_data": False}

    data["updated_at"] = datetime.now().isoformat()
    return data


# ── WebSocket Endpoint for Real-time Updates ──────────────

@app.websocket("/ws/prices")
async def websocket_prices(websocket: WebSocket):
    """
    WebSocket endpoint for real-time price and signal updates.

    Fix 1: Sends payload under key "prices" (was "data") to match
           the frontend handler: if (data.prices) { updateLivePrices(data.prices) }

    Pushes updates every 5 seconds with latest prices and signals
    for all tracked symbols.
    """
    await websocket.accept()
    _ws_clients.append(websocket)
    logger.info("WebSocket client connected (%d total)", len(_ws_clients))

    try:
        while True:
            # Fix 1: Use "prices" key to match frontend expectation
            update = {
                "type": "price_update",
                "timestamp": datetime.now().isoformat(),
                "prices": {},   # ← was "data", now "prices"
            }

            for symbol in _market_data:
                df = _market_data[symbol]
                if df.empty:
                    continue
                last = df.iloc[-1]
                update["prices"][symbol] = {
                    "close": round(float(last["close"]), 2),
                    "high": round(float(last["high"]), 2),
                    "low": round(float(last["low"]), 2),
                    "rsi": round(float(last["RSI_14"]), 1) if pd.notna(last.get("RSI_14")) else None,
                    "signal": str(last.get("signal", "HOLD")),
                    "regime": _regime_cache.get(symbol, "RANGING"),
                }

            # Add live trade stats
            engine = _get_engine()
            stats = engine.get_stats()
            update["stats"] = {
                "capital": stats.get("capital", 100000),
                "total_pnl": stats.get("total_pnl", 0),
                "open_trades": len(engine.get_open_trades()),
            }

            update["market_mood"] = _compute_market_mood()

            await websocket.send_json(update)
            await asyncio.sleep(5)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WebSocket error: %s", e)
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)
        logger.info("WebSocket client disconnected (%d remaining)", len(_ws_clients))


@app.post("/api/refresh")
async def refresh_data():
    """Force refresh of all market data."""
    await _refresh_market_data()
    return {"status": "ok", "symbols": len(_market_data), "updated_at": datetime.now().isoformat()}


# ── Search Endpoints ─────────────────────────────────────

@app.get("/api/search/symbols")
async def search_symbol_list(q: str = Query("", min_length=1, max_length=30)):
    """Search available symbols by name/ticker (NSE + Crypto)."""
    results = search_symbols(q)
    for r in results:
        sym = r["symbol"]
        r["tracked"] = sym in _market_data
        # For crypto: also check with CCXT symbol format (BTC/USDT)
        if not r["tracked"] and r.get("type") == "CRYPTO":
            r["tracked"] = sym in _market_data or sym.replace("/", "-") in _market_data
    return {"results": results, "query": q}


@app.get("/api/search/quote/{symbol}")
async def search_quote(symbol: str):
    """
    Get full quote (OHLCV + indicators + signal) for any symbol.
    Supports NSE stocks (yFinance), crypto pairs (CCXT/Binance),
    and any global ticker.
    """
    clean = symbol.replace("-", "/").replace("_", " ")

    # Check if already in cache
    if clean in _market_data:
        df = _market_data[clean]
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
                    _regime_cache[clean] = regime.value
                    df = generate_signals(df, regime=regime)
                    _market_data[clean] = df
                    _signal_cache[clean] = df
            except Exception as e:
                logger.warning("Crypto fetch failed for %s: %s", clean, e)
                df = None

        # ── Fallback to yFinance (NSE + global) ──────
        if df is None:
            df = fetch_arbitrary_symbol(clean)
            if df is None or df.empty:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"Symbol '{symbol}' not found. For crypto use format: BTC-USDT"},
                )
            df = calculate_indicators(df)
            htf_df = fetch_arbitrary_symbol(clean, period="60d", interval="1d")
            if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:
                htf_df = calculate_indicators(htf_df)
                htf_df = classify_trend(htf_df)
                df = propagate_trend_to_ltf(df, htf_df)
            else:
                df = classify_trend(df)
            regime = detect_regime(df)
            _regime_cache[clean] = regime.value
            df = generate_signals(df, regime=regime)
            _market_data[clean] = df
            _signal_cache[clean] = df

    last = df.iloc[-1]

    # Build quote response
    sig = _get_signal_for_symbol(clean)

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
        })

    return {
        "symbol": clean,
        "close": round(float(last["close"]), 2),
        "high": round(float(last["high"]), 2),
        "low": round(float(last["low"]), 2),
        "rsi": round(float(last["RSI_14"]), 1) if pd.notna(last.get("RSI_14")) else None,
        "regime": _regime_cache.get(clean, "RANGING"),
        "signal": sig,
        "chart": chart_data,
    }


# ── AI / Ensemble Endpoints ──────────────────────────────

@app.get("/api/ensemble/validate")
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


@app.get("/api/ensemble/status")
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


@app.post("/api/retrain")
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
                _train_xgboost_sync,
                symbol_list, timeframe, days, forward_bars, min_return_pct,
            )
        return result

    # Store job status
    job_id = f"retrain_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    _retrain_jobs[job_id] = {"status": "running", "started_at": datetime.utcnow().isoformat()}

    async def _background():
        try:
            result = await _run_training()
            _retrain_jobs[job_id] = {
                "status": "completed",
                "result": result,
                "completed_at": datetime.utcnow().isoformat(),
            }
            logger.info("XGBoost retrain complete: %s", result)
        except Exception as e:
            _retrain_jobs[job_id] = {
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.utcnow().isoformat(),
            }
            logger.error("XGBoost retrain failed: %s", e)

    asyncio.create_task(_background())
    return {"job_id": job_id, "status": "started", "symbols": symbol_list, "timeframe": timeframe}


@app.get("/api/retrain/status")
async def retrain_status():
    """Return status of all retrain jobs."""
    return {"jobs": _retrain_jobs}


# In-memory job tracker
_retrain_jobs: dict = {}


def _train_xgboost_sync(
    symbols: list[str],
    timeframe: str,
    days: int,
    forward_bars: int,
    min_return_pct: float,
) -> dict:
    """Blocking XGBoost training — runs in a thread executor."""
    import numpy as np
    import pickle
    import os

    from agents.data_ingestion.binance_fetcher import fetch_historical
    from agents.feature_engine.indicators import calculate_indicators
    from agents.feature_engine.trend import classify_trend
    from agents.ai_engine.xgboost_model import (
        prepare_training_data, FEATURE_COLS_XGB, get_xgb_predictor,
        XGB_AVAILABLE, SKLEARN_AVAILABLE,
    )
    import agents.ai_engine.xgboost_model as xm

    if not (XGB_AVAILABLE or SKLEARN_AVAILABLE):
        raise RuntimeError("Neither xgboost nor scikit-learn is installed. Run: pip install xgboost")

    all_X, all_y = [], []
    symbol_stats = {}

    for sym in symbols:
        try:
            df = fetch_historical(sym, timeframe=timeframe, since_days=days)
            if df is None or len(df) < 100:
                symbol_stats[sym] = {"error": "insufficient data"}
                continue
            df = calculate_indicators(df)
            df = classify_trend(df)
            X, y = prepare_training_data(df, forward_bars=forward_bars, min_return_pct=min_return_pct)
            if len(X) >= 50:
                all_X.append(X)
                all_y.append(y)
                symbol_stats[sym] = {"samples": len(X), "balance": round(float(y.mean()) * 100, 1)}
            else:
                symbol_stats[sym] = {"error": f"only {len(X)} samples"}
        except Exception as e:
            symbol_stats[sym] = {"error": str(e)}

    if not all_X:
        raise RuntimeError("No valid training data collected from any symbol")

    X_all = np.vstack(all_X)
    y_all = np.concatenate(all_y)

    # Clean inf/NaN
    X_all = np.where(np.isinf(X_all), np.nan, X_all)
    col_med = np.nanmedian(X_all, axis=0)
    nan_mask = np.isnan(X_all)
    if nan_mask.any():
        X_all[nan_mask] = np.take(col_med, np.where(nan_mask)[1])

    # Train
    if XGB_AVAILABLE:
        import xgboost as xgb_lib
        model = xgb_lib.XGBClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.03,
            subsample=0.8, colsample_bytree=0.8, min_child_weight=3,
            gamma=0.1, eval_metric="logloss", random_state=42,
            n_jobs=-1, verbosity=0,
        )
    else:
        from sklearn.ensemble import GradientBoostingClassifier
        model = GradientBoostingClassifier(n_estimators=200, max_depth=4, learning_rate=0.05, random_state=42)

    split = int(len(X_all) * 0.8)
    model.fit(X_all[:split], y_all[:split])

    val_acc = float((model.predict(X_all[split:]) == y_all[split:]).mean() * 100)
    fi = dict(zip(FEATURE_COLS_XGB, model.feature_importances_.round(4).tolist()))

    # Update singleton
    xm._predictor = None
    p = get_xgb_predictor()
    p.model = model
    p.is_fitted = True
    p._feature_importance = fi

    os.makedirs("models", exist_ok=True)
    with open("models/xgb_model.pkl", "wb") as f:
        pickle.dump({
            "model": model,
            "feature_importance": fi,
            "feature_cols": FEATURE_COLS_XGB,
            "n_samples": len(X_all),
            "timeframe": timeframe,
            "symbols": symbols,
        }, f)

    return {
        "val_accuracy": round(val_acc, 2),
        "n_samples": len(X_all),
        "symbol_stats": symbol_stats,
        "top_features": dict(sorted(fi.items(), key=lambda x: -x[1])[:5]),
    }


# ── Serve Frontend ───────────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "dashboard")

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def serve_dashboard():
    """Serve the main dashboard page."""
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "Dashboard not found. Place files in /dashboard/"}


@app.get("/landing")
async def serve_landing():
    """Serve the marketing landing page."""
    landing = os.path.join(STATIC_DIR, "landing.html")
    if os.path.exists(landing):
        return FileResponse(landing)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
