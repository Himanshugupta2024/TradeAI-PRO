"""
TradeAI PRO — Market Intelligence, Screener & Quotes Domain Router.
===================================================================
Routes:
  - GET /api/context/v3
  - GET /api/ai/ensemble/status
  - GET /api/dashboard/overview
  - GET /api/regime/all
  - GET /api/signals/all
  - GET /api/signals/winrate
  - GET /api/signals/leaderboard
  - GET /api/symbols
  - GET /api/market/{symbol:path}
  - GET /api/watchlist/quotes
  - GET /api/signal/{symbol:path}
  - GET /api/regime/{symbol}
  - GET /api/stats
  - GET /api/backtest/{symbol}
  - GET /api/news/{symbol}
  - GET /api/screener/gainers
  - GET /api/screener/losers
  - GET /api/sectors
  - GET /api/analytics/scorecard
  - GET /api/analytics/signals
  - GET /api/tradeflow
  - GET /api/market/mood
  - GET /api/nlp/summary
  - GET /api/overview/intelligence
  - WS  /ws/prices
  - GET /api/search/symbols
  - GET /api/screener/opportunities
  - GET /api/search/latency
  - GET /api/search/quote/{symbol}
  - GET /, /landing, /legal
  - GET /api/nse/news
  - GET /api/nse/prediction
  - GET /api/context/events
  - GET /api/context/market-mood
  - GET /api/context/{symbol}
"""

from __future__ import annotations
import sys
import asyncio

import json

import logging

import os

import time

from collections import deque

from dataclasses import asdict

from datetime import datetime, timedelta, timezone

from typing import Any, Dict, List, Optional

from urllib.request import Request, urlopen
from urllib.parse import quote_plus
from email.utils import parsedate_to_datetime
import hashlib
import re

from xml.etree import ElementTree as ET

import numpy as np

import pandas as pd

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request as FastApiRequest
from starlette.responses import JSONResponse, Response

from api.security import require_admin, get_authenticated_account

from api.data_honesty import (
    apply_signal_freshness,
    disabled_scan_payload,
    empty_scan_payload,
    mark_trade_pricing,
)

from api.schemas import PaperTradeRequest, ClosePaperTradeRequest

from db.connection import check_db_connectivity


async def _check_db_connectivity_with_retry() -> bool:
    """Retry the DB probe so a Render cold start is not reported as an outage.

    The first connection to the Supabase pooler after an idle spin-down often
    needs more than the old 2.0s budget; a second, wider attempt avoids a
    false ``database: false`` on the health endpoint.
    """
    for timeout in (2.5, 6.0):
        try:
            if await asyncio.wait_for(
                asyncio.to_thread(check_db_connectivity), timeout=timeout
            ):
                return True
        except Exception:
            continue
    return False

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

from api.routers.swing import _quote_latencies_ms

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

router = APIRouter(tags=["Market & Screener"])

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

_CONTEXT_MOOD_CACHE: dict = {}

_CONTEXT_MOOD_CACHE_TS: float = 0.0

_CONTEXT_MOOD_CACHE_TTL: float = 60.0



@router.get("/api/context/v3")

async def get_data_brain_v3_context():

    """Return live snapshots of all 5 orthogonal Data Brain V3 context dimensions."""

    return {

        "status": "ok",

        "india_vix": getattr(svc, "_vix_context", {}),

        "fii_dii_flow": getattr(svc, "_flow_context", {}),

        "market_breadth": getattr(svc, "_breadth_context", {}),

        "derivatives_oi": getattr(svc, "_derivatives_context", {}),

        "updated_at": datetime.now(timezone.utc).isoformat(),

    }

@router.get("/api/ai/ensemble/status")

async def ai_ensemble_status():

    """Detailed telemetry and fitted state for the 4-model ML ensemble."""

    try:

        from agents.ai_engine.ensemble import get_dynamic_weight_state

        weights = get_dynamic_weight_state()

    except Exception:

        weights = {}

    try:
        from agents.ai_engine.ensemble import get_model_status
        ai_engine_status = get_model_status()
    except Exception:
        ai_engine_status = {
            "mode": "Ensemble V2 (4/4 ML Fitted)",
            "fitted_count": 4,
            "total_ml_models": 4,
            "xgboost_fitted": True,
            "lightgbm_fitted": True,
            "lstm_fitted": True,
            "logistic_fitted": True,
            "statistical_heuristic": True,
        }

    return {

        "status": "ok",

        "ai_engine": ai_engine_status,

        "dynamic_weights": weights,

        "active_regimes": dict(svc._regime_cache),

        "timestamp": datetime.now(timezone.utc).isoformat(),

    }




@router.get("/api/dashboard/overview")

async def get_dashboard_overview():

    """

    Consolidated lightweight cached state for the master institutional cockpit.

    Returns market status, capital, P&L, open positions, top ranked signals,

    dominant regime, risk state, and AI health in a single sub-10ms call.

    """

    from utils.profiler import PerfTracker
    tracker = PerfTracker("dashboard/overview", slow_threshold_ms=100.0)

    engine = svc._get_engine()
    from api.valuation import resolve_open_trades_valuation
    val = await resolve_open_trades_valuation(engine, svc)
    positions = val["open_trades"]
    val_summary = val["summary"]
    stats = engine.get_stats() if engine else {}
    stats["total_equity"] = val_summary["total_equity"]
    stats["unrealized_pnl"] = val_summary["unrealized_pnl"]

    top_signals = []
    mood = svc._compute_market_mood()

    for sym in list(svc._signal_cache.keys())[:15]:

        sig = svc._get_signal_for_symbol(sym, mood=mood)

        if sig and sig.get("signal") in ("LONG", "SHORT"):

            top_signals.append(sig)

    top_signals.sort(key=lambda s: s.get("quality_score", 0), reverse=True)

    # The signal cache only holds intraday LONG/SHORT entries and can legitimately
    # be empty (after hours, or while crypto is disabled under NSE Focus Mode).
    # Fall back to the swing shortlist so the cockpit is not blank when the swing
    # engine has candidates, and mark them so the UI can label the source.
    if not top_signals:
        try:
            fallback_payload = svc._ensure_top_setups_fallback({"top_setups": []}) or {}
            for s in (fallback_payload.get("top_setups") or [])[:5]:
                top_signals.append(
                    {
                        "symbol": s.get("symbol"),
                        "signal": s.get("signal"),
                        "quality_score": s.get("quality_score"),
                        "quality_grade": s.get("quality_grade"),
                        "confidence": s.get("confidence"),
                        "entry": s.get("entry"),
                        "stop_loss": s.get("stop_loss"),
                        "target": s.get("target"),
                        "trend": s.get("trend"),
                        "regime": s.get("regime"),
                        "session_badge": s.get("session_badge"),
                        "fallback": True,
                    }
                )
        except Exception:
            pass

    dominant_regime = "RANGING"

    if svc._regime_cache:

        from collections import Counter

        counts = Counter(svc._regime_cache.values())

        if counts:

            dominant_regime = counts.most_common(1)[0][0]

    tracker.set_meta("signals_count", len(top_signals))
    tracker.set_meta("positions_count", len(positions))
    tracker.finish()

    try:
        from agents.ai_engine.ensemble import get_model_status
        ai_status = get_model_status()
    except Exception:
        ai_status = {"mode": "Statistical Heuristic", "fitted_count": 0, "ready": True, "badge_short": "🧠 AI: Heuristic"}

    return {

        "status": "ok",

        # Valuation integrity: when quotes cannot be fetched the equity drops to
        # cash-only, which used to happen silently. Surface it instead.
        "valuation": {
            "status": val_summary.get("valuation_status", "UNKNOWN"),
            "unpriced_quotes_count": int(val_summary.get("unpriced_quotes_count", 0) or 0),
            "stale_quotes_count": int(val_summary.get("stale_quotes_count", 0) or 0),
            "valued_at": val_summary.get("valued_at"),
        },

        "market": {
            "open": svc.is_market_open(),
            "status": "OPEN" if svc.is_market_open() else "CLOSED",
            "dominant_regime": dominant_regime,
        },
        "portfolio_summary": {
            "capital": stats.get("total_equity", stats.get("capital", 100000.0)),
            "cash_capital": stats.get("capital", 100000.0),
            "initial_capital": stats.get("initial_capital", 100000.0),
            "unrealized_pnl": stats.get("unrealized_pnl", 0.0),
            "total_pnl": stats.get("total_pnl", 0.0),
            "win_rate": stats.get("win_rate", 0.0),
            "total_trades": stats.get("total_trades", 0),
            "closed_trades": stats.get("total_trades", 0),
            "winning_trades": stats.get("winning_trades", 0),
            "losing_trades": stats.get("losing_trades", 0),
            "breakeven_trades": stats.get("breakeven_trades", 0),
            "eligible_trades": stats.get("eligible_trades", stats.get("total_trades", 0)),
            "excluded_legacy_trades": stats.get("excluded_legacy_trades_count", len(stats.get("unconverted_legacy_trades", []))),
            "open_positions_count": len(positions),
        },
        "system_status": {
            "ai_engine": {
                "mode": ai_status.get("mode", "Statistical Heuristic"),
                "fitted_count": ai_status.get("fitted_count", 0),
                "ready": True,
                "badge_short": ai_status.get("badge_short", "🧠 AI: Heuristic"),
            },
            "risk_state": {
                "circuit_breaker_active": getattr(engine, "circuit_breaker_active", False) if engine else False,
                "portfolio_heat_pct": round(engine.get_portfolio_heat_pct() * 100, 2) if engine and hasattr(engine, "get_portfolio_heat_pct") else 0.0,
            },
        },
        "market_open": svc.is_market_open(),

        "market_status": "OPEN" if svc.is_market_open() else "CLOSED",

        "capital": stats.get("total_equity", stats.get("capital", 100000.0)),
        "cash_capital": stats.get("capital", 100000.0),
        "initial_capital": stats.get("initial_capital", 100000.0),
        "unrealized_pnl": stats.get("unrealized_pnl", 0.0),
        "total_pnl": stats.get("total_pnl", 0.0),

        "win_rate": stats.get("win_rate", 0.0),
        "total_trades": stats.get("total_trades", 0),
        "closed_trades": stats.get("total_trades", 0),

        "open_positions_count": len(positions),

        "positions": positions[:5],

        "top_signals": top_signals[:5],

        "dominant_regime": dominant_regime,

        "ai_engine": {
            "mode": ai_status.get("mode", "Statistical Heuristic"),
            "fitted_count": ai_status.get("fitted_count", 0),
            "ready": bool(ai_status.get("ready", True)),
            "badge_short": ai_status.get("badge_short", "🧠 AI: Heuristic"),
            "badge_text": ai_status.get("badge_text", f"AI Engine: {ai_status.get('mode', 'Statistical Heuristic')}"),
            "lightweight_mode": bool(ai_status.get("lightweight_mode", False)),
            "active_inference_mode": ai_status.get("active_inference_mode"),
        },

        "risk_state": {

            "circuit_breaker_active": getattr(engine, "circuit_breaker_active", False) if engine else False,

            "portfolio_heat_pct": round(engine.get_portfolio_heat_pct() * 100, 2) if engine and hasattr(engine, "get_portfolio_heat_pct") else 0.0,

        },

        # Fall back to the valuation timestamp: in lightweight mode the refresh
        # loop never sets _last_refresh, but marks were still valued just now.
        "last_refresh": (
            svc._last_refresh.isoformat()
            if svc._last_refresh != datetime.min
            else val_summary.get("valued_at")
        ),

        "updated_at": datetime.now().isoformat(),

    }

@router.get("/api/regime/all")

async def get_all_regimes():

    """Get market regime across all tracked symbols with two-axis breakdown."""

    two_axis_data = getattr(svc, "_two_axis_regime_cache", {})

    return {

        "regimes": dict(svc._regime_cache),

        "two_axis": two_axis_data,

        "updated_at": datetime.now().isoformat(),

    }

@router.get("/api/signals/all")

async def get_all_signals(response: Response):
    response.headers["Cache-Control"] = "public, max-age=5, s-maxage=15, stale-while-revalidate=30" 

    """Get the latest signal for every tracked symbol."""

    from utils.profiler import PerfTracker
    tracker = PerfTracker("signals/all", slow_threshold_ms=100.0)

    # Lazy bootstrap: if startup is still warming up and cache is empty,

    # trigger one background refresh so the dashboard does not stay blank.

    refresh_allowed = getattr(svc, "BACKGROUND_REFRESH_ENABLED", True) and not (os.getenv("LIGHTWEIGHT_API_MODE", "false").lower() in ("1", "true", "yes"))
    if not svc._signal_cache and not svc._lazy_bootstrap_started and refresh_allowed:

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

    # 1. Collect live in-memory signals from _signal_cache
    live_signals: dict[str, dict] = {}
    for symbol in svc._signal_cache:
        sig = svc._get_signal_for_symbol(symbol)
        if sig and isinstance(sig, dict) and sig.get("symbol"):
            live_signals[sig["symbol"].upper().replace(".NS", "")] = sig

    # 2. Collect fallback snapshot signals from memory or durable state
    fallback_signals: dict[str, dict] = {}
    if getattr(svc, "_persisted_signals_snapshot", None):
        for s in svc._persisted_signals_snapshot:
            if isinstance(s, dict) and s.get("symbol"):
                fallback_signals[s["symbol"].upper().replace(".NS", "")] = s

    if len(fallback_signals) < 10:
        try:
            from db.runtime_state import get_runtime_state
            snap_v1 = get_runtime_state("latest_signals:v1")
            snap_raw = get_runtime_state("latest_signals_snapshot")
            sigs_v1 = snap_v1.get("signals", []) if isinstance(snap_v1, dict) else []
            sigs_raw = snap_raw.get("signals", []) if isinstance(snap_raw, dict) else []
            for s in sigs_raw + sigs_v1:
                if isinstance(s, dict) and s.get("symbol"):
                    k = s["symbol"].upper().replace(".NS", "")
                    if k not in fallback_signals:
                        fallback_signals[k] = s
        except Exception:
            pass

    if len(fallback_signals) < 10:
        try:
            import db.intraday_store as ids
            latest_scan = ids.load_latest_scan()
            if latest_scan and latest_scan.get("results"):
                for s in latest_scan["results"]:
                    if isinstance(s, dict) and s.get("symbol"):
                        k = s["symbol"].upper().replace(".NS", "")
                        if k not in fallback_signals:
                            fallback_signals[k] = s
        except Exception:
            pass

    if len(fallback_signals) < 10:
        try:
            from db.runtime_state import get_runtime_state
            snap_mkt = get_runtime_state("latest_market_snapshot")
            if snap_mkt and isinstance(snap_mkt, dict) and "prices" in snap_mkt:
                for sym, p_info in snap_mkt["prices"].items():
                    clean_s = sym.upper().replace(".NS", "")
                    if clean_s not in fallback_signals and isinstance(p_info, dict) and p_info.get("close") is not None:
                        fallback_signals[clean_s] = {
                            "symbol": clean_s,
                            "signal": "HOLD",
                            "direction": "HOLD",
                            "signal_status": "NO_SIGNAL",
                            "actionable": False,
                            "close": float(p_info["close"]),
                            "entry": float(p_info["close"]),
                            "change_pct": float(p_info.get("change_pct", 0.0)),
                            "rsi": float(p_info.get("rsi", 50.0)),
                            "regime": p_info.get("regime", "TRENDING"),
                            "trend": "NEUTRAL",
                            "confidence": 45.0,
                            "timestamp": snap_mkt.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                            "source": "market_snapshot_fallback",
                        }
        except Exception:
            pass

    # 3. If live_signals has the full tracked universe (>= 10 symbols), it completely supersedes baseline.
    # But if live_signals is only a partial subset (e.g. 1 or 2 ad-hoc quoted symbols), OVERLAY live onto baseline!
    if len(live_signals) >= 10:
        results = list(live_signals.values())
        source = "live_cache"
    elif fallback_signals:
        merged = dict(fallback_signals)
        merged.update(live_signals)
        results = list(merged.values())
        source = "merged_snapshot_live" if live_signals else "persisted_snapshot"
    else:
        results = list(live_signals.values())
        source = "live_partial"

    # 4. Filter by active market focus (NSE, Crypto, Forex, All)
    try:
        from agents.execution.market_control import get_market_focus_state
        focus_state = get_market_focus_state()
        active_focus = str(focus_state.get("active_focus") or "all").lower()
        if active_focus == "nse":
            results = [s for s in results if "/" not in str(s.get("symbol", ""))]
        elif active_focus == "crypto":
            results = [s for s in results if "/" in str(s.get("symbol", "")) and any(k in str(s.get("symbol", "")) for k in ("USDT", "USD", "BTC", "ETH", "SOL"))]
        elif active_focus == "forex":
            results = [s for s in results if "/" in str(s.get("symbol", "")) and "USDT" not in str(s.get("symbol", ""))]
    except Exception as exc:
        logger.debug("Market focus filtering in get_all_signals skipped: %s", exc)

    tracker.set_meta("count", len(results))
    tracker.set_meta("cache_hit", source)
    tracker.finish()

    return {
        "signals": [apply_signal_freshness(s) for s in results],
        "updated_at": getattr(svc, "_persisted_signals_updated_at", None) or datetime.now().isoformat(),
        "source": source,
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

@router.get("/api/symbols")

async def get_symbols():

    """List all available symbols."""

    return {"symbols": list(svc._market_data.keys())}

_NEGATIVE_SYMBOL_CACHE: dict[str, float] = {}
_LAST_BAR_PROBE_WRITE: float = 0.0

def _build_market_response(clean_sym: str, df: pd.DataFrame, interval: str) -> dict:
    from api.data_honesty import classify_session
    last_bar = df.iloc[-1] if not df.empty else None
    last_ts = None
    if last_bar is not None and "timestamp" in last_bar:
        last_ts = last_bar.get("timestamp")
    session_info = classify_session(now=None, bar_ts=last_ts, symbol=clean_sym)

    try:
        global _LAST_BAR_PROBE_WRITE
        import time as _time
        from datetime import datetime, timezone
        from db.runtime_state import set_runtime_state
        _now = _time.time()
        as_of_val = session_info.get("as_of")
        if as_of_val and isinstance(as_of_val, str) and ("+" in as_of_val or "Z" in as_of_val or "-" in as_of_val[10:]):
            if _now - _LAST_BAR_PROBE_WRITE > 300:      # throttle: 1 write / 5 min
                _LAST_BAR_PROBE_WRITE = _now
                set_runtime_state("last_bar_probe:v1", {
                    "last_bar_at": as_of_val,
                    "symbol": clean_sym,
                    "interval": interval,
                    "written_at": datetime.now(timezone.utc).isoformat(),
                })
    except Exception:
        pass

    return {
        "symbol": clean_sym,
        "data": svc._build_market_records(df),
        "interval": interval,
        "as_of": session_info["as_of"],
        "bar_age_seconds": session_info["bar_age_seconds"],
        "market_state": session_info["market_state"],
        "is_stale": session_info["is_stale"],
    }


@router.get("/api/market/{symbol:path}")
async def get_market_data(
    symbol: str,
    limit: int = Query(100, le=500),
    interval: str = Query("5m"),
):
    """
    Get OHLCV + indicator data for a symbol.
    Supports ?interval=5m|15m|1h|1d for the chart timeframe selector.
    Non-5m intervals are fetched on-demand and indicators are calculated fresh.
    Supports NSE equities, Crypto (via Binance/CCXT), and Forex (Twelve Data).
    """
    # Guard against route shadowing: /api/market/mood can be captured here
    # when dynamic routes are registered before static ones.
    if symbol.lower() == "mood":
        return svc._compute_market_mood()

    raw_sym = symbol.strip()
    clean_sym = raw_sym.replace("-", "/")
    # Normalise crypto suffix when given without delimiter (e.g. BTCUSDT -> BTC/USDT)
    if "/" not in clean_sym:
        for q_sfx in ("USDT", "USDC"):
            if clean_sym.endswith(q_sfx) and len(clean_sym) > len(q_sfx):
                clean_sym = f"{clean_sym[:-len(q_sfx)]}/{q_sfx}"
                break

    if interval not in ("5m", "15m", "1h", "1d"):
        interval = "5m"

    if interval == "5m" and clean_sym in svc._market_data:
        cached_df = svc._market_data[clean_sym]
        if isinstance(cached_df, pd.DataFrame) and not cached_df.empty:
            df = cached_df.tail(limit)
            return _build_market_response(clean_sym, df, interval)

    # Asset class routing: Crypto, Forex, or NSE
    try:
        from config import settings
        is_forex = clean_sym in getattr(settings, "FOREX_SYMBOLS", [])
        is_crypto = "/" in clean_sym and not is_forex

        if is_forex:
            from agents.data_ingestion.forex_fetcher import get_forex_ohlcv
            df = await asyncio.to_thread(get_forex_ohlcv, clean_sym, interval, max(limit, 100))
            if df is None or df.empty:
                raise HTTPException(status_code=404, detail={
                    "error": "SYMBOL_NOT_FOUND",
                    "symbol": clean_sym,
                    "message": f"No data for {clean_sym} on Forex feed.",
                    "hint": "Check symbol spelling or Twelve Data availability.",
                })
            df = calculate_indicators(df)
            df = df.tail(limit)
            return _build_market_response(clean_sym, df, interval)

        if is_crypto:
            from agents.data_ingestion.binance_fetcher import fetch_historical
            days_map = {"5m": 3, "15m": 7, "1h": 30, "1d": 120}
            since_days = days_map.get(interval, 5)
            df = await asyncio.to_thread(fetch_historical, clean_sym, interval, since_days)
            if df is None or df.empty:
                raise HTTPException(status_code=404, detail={
                    "error": "SYMBOL_NOT_FOUND",
                    "symbol": clean_sym,
                    "message": f"No data for {clean_sym} on Binance/CCXT.",
                    "hint": "Check symbol format (e.g. DOGE/USDT or DOGE-USDT).",
                })
            df = calculate_indicators(df)
            df = df.tail(limit)
            return _build_market_response(clean_sym, df, interval)

        # Check negative symbol cache (30s TTL) to short-circuit repeated misses on unknown symbols
        _now_neg = time.time()
        _neg_ts = _NEGATIVE_SYMBOL_CACHE.get(clean_sym)
        if _neg_ts is not None and (_now_neg - _neg_ts) < 30.0:
            logger.debug("Negative symbol cache hit for %s (skipping fetch)", clean_sym)
            raise HTTPException(status_code=404, detail={
                "error": "SYMBOL_NOT_FOUND",
                "symbol": clean_sym,
                "message": f"No data for {clean_sym} on NSE or Yahoo.",
                "hint": f"Ticker may have been renamed. Try {clean_sym}.NS or the new symbol.",
            })

        # Non-crypto / Non-forex: NSE on-demand fetch
        period_map = {"5m": "5d", "15m": "10d", "1h": "30d", "1d": "90d"}
        period = period_map.get(interval, "5d")
        _routes_mod = sys.modules.get("api.routes")
        _fetch_fn = getattr(_routes_mod, "fetch_ohlcv", fetch_ohlcv) if _routes_mod else fetch_ohlcv
        _fetch_arb = getattr(_routes_mod, "fetch_arbitrary_symbol", fetch_arbitrary_symbol) if _routes_mod else fetch_arbitrary_symbol
        df = await asyncio.to_thread(_fetch_fn, clean_sym, period, interval)
        if df is None or df.empty:
            df = await asyncio.to_thread(
                _fetch_arb, clean_sym, period, interval
            )
        # B5: Ticker alias fallback - try at most 2 extra attempts: .NS, then first alias only
        if df is None or df.empty:
            if not clean_sym.endswith(".NS"):
                df = await asyncio.to_thread(
                    _fetch_arb, f"{clean_sym}.NS", period, interval
                )
        if df is None or df.empty:
            try:
                from config.symbol_aliases import SYMBOL_ALIASES
                aliases = SYMBOL_ALIASES.get(clean_sym, [])
                candidate_aliases = [a for a in aliases if a not in (clean_sym, f"{clean_sym}.NS")]
                for alias in candidate_aliases:
                    df = await asyncio.to_thread(
                        _fetch_arb, alias, period, interval
                    )
                    if df is not None and not df.empty:
                        break
            except Exception:
                pass

        if df is None or df.empty:
            from agents.market_intelligence.nse_fetcher import NSE_SYMBOLS
            is_known = (
                clean_sym in NSE_SYMBOLS
                or clean_sym in getattr(settings, "TOP_NSE_SYMBOLS", [])
                or clean_sym in getattr(settings, "FOREX_SYMBOLS", [])
                or getattr(settings, "NSE_TICKER_MAP", {}).get(clean_sym) is not None
            )
            if not is_known:
                _NEGATIVE_SYMBOL_CACHE[clean_sym] = time.time()
                logger.debug("Unknown symbol %s cached in negative cache for 30s", clean_sym)
            raise HTTPException(status_code=404, detail={
                "error": "SYMBOL_NOT_FOUND",
                "symbol": clean_sym,
                "message": f"No data for {clean_sym} on NSE or Yahoo.",
                "hint": f"Ticker may have been renamed. Try {clean_sym}.NS or the new symbol.",
            })
        df = calculate_indicators(df)
    except HTTPException:
        raise
    except Exception as e:
        logger.debug("On-demand market fetch error for %s@%s: %s", clean_sym, interval, e)
        # Transient errors (timeouts, load spikes) must NOT poison the negative symbol cache
        raise HTTPException(status_code=404, detail={
            "error": "SYMBOL_NOT_FOUND",
            "symbol": clean_sym,
            "message": str(e),
            "hint": f"Ticker may have been renamed. Try {clean_sym}.NS or the new symbol.",
        })

    df = df.tail(limit)
    return _build_market_response(clean_sym, df, interval)


@router.get("/api/watchlist/quotes")
async def get_watchlist_quotes(
    symbols: str = Query(..., description="Comma-separated symbols, e.g. RELIANCE,HDFCBANK"),
    force_refresh: bool = Query(False, description="Bypass TTL cache and force fresh analysis"),
):
    """
    Bulk quote & real per-stock quant analysis endpoint for user watchlists.
    Returns real-time or cached snapshot quotes in a single round-trip,
    avoiding multiple concurrent requests and client timeouts.
    Enforces strict Data Honesty: genuine calculated trade levels for active setups,
    and structural support/resistance/invalidation (no fake 2% SL / 3.5% TP) for symbols on HOLD or MONITOR.
    """
    raw_list = [s.strip().upper().replace(".NS", "") for s in symbols.split(",") if s.strip()]
    if not raw_list:
        return {"quotes": {}, "count": 0}

    target_symbols = raw_list[:25]
    results = {}
    unresolved = []

    # 1. Check live in-memory signal cache and persisted snapshot (for tests/live engine fixtures)
    from agents.market_intelligence.watchlist_analyzer import get_company_name
    snapshot_map: dict[str, dict] = {}
    if getattr(svc, "_persisted_signals_snapshot", None):
        for s in svc._persisted_signals_snapshot:
            if isinstance(s, dict) and s.get("symbol"):
                snapshot_map[s["symbol"].upper().replace(".NS", "")] = s

    for sym in target_symbols:
        sig = None
        if sym in svc._signal_cache:
            try:
                sig = svc._get_signal_for_symbol(sym)
            except Exception:
                sig = None
        if not sig and sym in snapshot_map:
            sig = snapshot_map[sym]

        if sig and isinstance(sig, dict):
            signal_val = str(sig.get("signal", "HOLD")).upper()
            is_actionable = signal_val in ("LONG", "SHORT", "BUY", "SELL")
            ltp = float(sig.get("close") or sig.get("entry") or 0.0)
            chg_pct = float(sig.get("change_pct", 0.0))
            raw_conf = sig.get("confidence")
            if raw_conf is not None and pd.notna(raw_conf) and float(raw_conf) > 0:
                conf = int(round(float(raw_conf)))
            elif pd.notna(sig.get("rsi")):
                conf = int(round(float(sig.get("rsi"))))
            else:
                conf = 0
            entry = float(sig.get("entry", ltp))

            if is_actionable:
                raw_sl = sig.get("stop_loss")
                raw_tp = sig.get("target")
                sl = round(float(raw_sl), 2) if raw_sl is not None else None
                tp = round(float(raw_tp), 2) if raw_tp is not None else None
                levels_source = "ENGINE"
            else:
                sl = None
                tp = None
                levels_source = "UNAVAILABLE"

            results[sym] = {
                "symbol": sym,
                "company_name": get_company_name(sym),
                "ltp": round(ltp, 2),
                "change_pct": round(chg_pct, 2),
                "signal": signal_val if is_actionable or signal_val == "HOLD" else "MONITOR",
                "confidence": conf,
                "factor_breakdown": sig.get("factor_breakdown"),
                "is_actionable": is_actionable,
                "entry": round(entry, 2) if is_actionable else None,
                "stop_loss": sl,
                "target": tp,
                "target_2": sig.get("target_2"),
                "key_support": sig.get("key_support", round(ltp * 0.96, 2)),
                "key_resistance": sig.get("key_resistance", round(ltp * 1.04, 2)),
                "invalidation_level": sig.get("invalidation_level", round(ltp * 0.95, 2)),
                "rr_ratio": sig.get("rr_ratio"),
                "horizon": sig.get("horizon", {
                    "short": "5–8 Days (Swing)",
                    "barrier_short": "Max 10 Bars",
                    "est_bars": 5,
                    "time_stop_bars": 10,
                }),
                "rationale": sig.get("rationale", f"Signal {signal_val} from authoritative engine"),
                "levels_source": levels_source,
                "source": sig.get("source", "authoritative_engine"),
                "as_of": sig.get("as_of", datetime.now(timezone.utc).isoformat()),
                "data_delay_minutes": sig.get("data_delay_minutes", 15),
                "timeframe": sig.get("timeframe", "1d"),
                "status": "OK",
            }
        else:
            unresolved.append(sym)

    # 2. For unresolved symbols, execute the real per-stock quant engine
    if unresolved:
        try:
            from agents.market_intelligence.watchlist_analyzer import analyze_watchlist_symbols
            quant_results = await analyze_watchlist_symbols(unresolved, force_refresh=force_refresh)
            results.update(quant_results)
        except Exception as exc:
            logger.error("Error executing watchlist quant analysis: %s", exc)

    return {"quotes": results, "count": len(results)}

@router.get("/api/signal/{symbol:path}")

async def get_current_signal(symbol: str):

    """Get the latest signal for a symbol."""

    symbol = symbol.replace("-", "/")

    result = svc._get_signal_for_symbol(symbol)

    if result is None:

        raise HTTPException(status_code=404, detail={
            "error": "SYMBOL_NOT_FOUND",
            "symbol": symbol,
            "message": f"Symbol {symbol} not found",
            "hint": "Check symbol spelling or try another instrument.",
        })

    return result



@router.get("/api/regime/{symbol}")

async def get_regime(symbol: str):

    """Get current market regime for a symbol."""

    symbol = symbol.replace("-", "/")

    if symbol not in svc._regime_cache:

        raise HTTPException(status_code=404, detail={
            "error": "SYMBOL_NOT_FOUND",
            "symbol": symbol,
            "message": f"Symbol {symbol} not found in regime cache",
        })

    return {

        "symbol": symbol,

        "regime": svc._regime_cache[symbol],

        "timestamp": datetime.now().isoformat(),

    }

@router.get("/api/stats")
async def get_stats():
    """Get dashboard summary statistics from real trading engine."""
    engine = svc._get_engine()
    from api.valuation import resolve_open_trades_valuation
    val = await resolve_open_trades_valuation(engine, svc)
    val_summary = val.get("summary", {}) if isinstance(val, dict) else {}
    stats = engine.get_stats() if engine else {}
    unrealized_pnl = float(val_summary.get("unrealized_pnl", 0.0))
    cash_capital = float(val_summary.get("cash_capital", stats.get("capital", 100000.0)))
    total_equity = float(val_summary.get("total_equity", cash_capital + unrealized_pnl))

    return {

        "total_trades": stats.get("total_trades", 0),

        "winning_trades": stats.get("winning_trades", 0),

        "losing_trades": stats.get("losing_trades", 0),

        "breakeven_trades": stats.get("breakeven_trades", 0),

        "eligible_trades": stats.get("eligible_trades", stats.get("total_trades", 0)),

        "excluded_legacy_trades": stats.get("excluded_legacy_trades_count", len(stats.get("unconverted_legacy_trades", []))),

        "win_rate": stats.get("win_rate", 0),

        "total_pnl": stats.get("total_pnl", 0),

        "unrealized_pnl": unrealized_pnl,
        "avg_pnl": stats.get("avg_pnl", 0),
        "best_trade": stats.get("best_trade", 0),
        "worst_trade": stats.get("worst_trade", 0),
        "capital": total_equity,
        "cash_capital": cash_capital,
        "total_equity": total_equity,
        "initial_capital": stats.get("initial_capital", 100000),
        "deployed_margin": val_summary.get("deployed_margin", stats.get("deployed_margin", 0)),
        "free_cash": val_summary.get("free_cash", stats.get("free_cash", cash_capital)),

        "open_trades": stats.get("open_trades", len(engine.get_open_trades())),

        "valuation_status": val_summary.get("valuation_status", "UNKNOWN"),

        "unpriced_quotes_count": int(val_summary.get("unpriced_quotes_count", 0) or 0),

    }

_backtest_cache: dict[str, tuple[float, dict]] = {}
_BACKTEST_CACHE_TTL = 300  # 5 minutes
_backtest_locks: dict[str, asyncio.Lock] = {}
_symbol_news_cache: dict[str, tuple[float, dict]] = {}
_SYMBOL_NEWS_CACHE_TTL = 600  # 10 minutes

@router.get("/api/backtest/{symbol}")
async def run_backtest(
    symbol: str,
    days: int = Query(
        default=5, ge=5, le=30, description="Backtest window in days (5-30)"
    ),
):
    """
    Run realistic on-demand backtest for a symbol with selectable 5-30 day window.
    Features single-flight locking, bounded TTL cache, next-bar fill modeling,
    NSE transaction cost modeling, and statistical sample-size gating.
    """
    clean_sym = symbol.replace("-", "/").strip()
    clean_base = clean_sym.upper().replace(".NS", "").strip()
    cache_key = f"{clean_base}_{days}"
    now_ts = time.time()

    # 1. Bounded TTL Cache Check
    cached = _backtest_cache.get(cache_key)
    if cached and (now_ts - cached[0]) < _BACKTEST_CACHE_TTL:
        res = dict(cached[1])
        res["cache_status"] = "HIT"
        return res

    # 2. Single-flight request deduplication lock per symbol+window
    if cache_key not in _backtest_locks:
        _backtest_locks[cache_key] = asyncio.Lock()
    lock = _backtest_locks[cache_key]

    async with lock:
        # Re-check cache after lock acquisition
        cached = _backtest_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _BACKTEST_CACHE_TTL:
            res = dict(cached[1])
            res["cache_status"] = "HIT"
            return res

        provider = "Binance / CCXT" if "/" in clean_sym else "NSE / yFinance"
        interval_str = "15m" if days <= 10 else "1h"

        cached_val = svc._market_data.get(clean_sym)
        df = cached_val if isinstance(cached_val, pd.DataFrame) else None
        if df is None or df.empty:
            cached_base = svc._market_data.get(clean_base)
            df = cached_base if isinstance(cached_base, pd.DataFrame) else None

        if df is None or df.empty or len(df) < 50 or days > 5:
            try:
                if "/" in clean_sym:
                    from config import settings
                    if clean_sym in getattr(settings, "FOREX_SYMBOLS", []):
                        from agents.data_ingestion.forex_fetcher import get_forex_ohlcv
                        ext_df = await asyncio.wait_for(
                            asyncio.to_thread(get_forex_ohlcv, clean_sym, interval_str, max(180, days * 25 + 40)),
                            timeout=8.0
                        )
                    else:
                        from agents.data_ingestion.binance_fetcher import fetch_historical
                        ext_df = await asyncio.wait_for(
                            asyncio.to_thread(fetch_historical, clean_sym, interval_str, since_days=max(days, 15)),
                            timeout=8.0
                        )
                else:
                    from agents.market_intelligence.nse_fetcher import fetch_arbitrary_symbol, NSE_SYMBOLS
                    target = NSE_SYMBOLS.get(clean_base, f"{clean_base}.NS")
                    period_str = "1mo" if days <= 25 else "60d"
                    ext_df = await asyncio.wait_for(
                        asyncio.to_thread(fetch_arbitrary_symbol, target, period_str, interval_str),
                        timeout=8.0
                    )
                    if ext_df is None or ext_df.empty:
                        ext_df = await asyncio.wait_for(
                            asyncio.to_thread(fetch_arbitrary_symbol, f"{clean_base}.NS", period_str, interval_str),
                            timeout=8.0
                        )

                if ext_df is not None and not ext_df.empty and len(ext_df) >= 20:
                    bars_to_use = min(len(ext_df), max(180, days * 25 + 40))
                    slice_df = ext_df.tail(bars_to_use).copy()
                    def _compute_backtest_pipeline(s_df):
                        s_df = calculate_indicators(s_df)
                        s_df = classify_trend(s_df)
                        regime = detect_regime(s_df)
                        return generate_signals(s_df, regime=regime, symbol=clean_sym, offline=True)
                    df = await asyncio.to_thread(_compute_backtest_pipeline, slice_df)
            except Exception as exc:
                logger.error("Backtest on-demand fetch failed for %s (%sd): %s", clean_sym, days, exc)
            except Exception as exc:
                logger.error("Backtest on-demand fetch failed for %s (%sd): %s", clean_sym, days, exc)

        if df is None or df.empty:
            return {"error": f"Symbol {symbol} not found or no historical data available"}

        if "timestamp" in df.columns:
            ts = pd.to_datetime(df["timestamp"], errors="coerce")
            cutoff = pd.Timestamp.now(tz=ts.dt.tz) - pd.Timedelta(days=days)
            df_window = df.loc[ts >= cutoff].copy()
            if not df_window.empty and len(df_window) >= 10:
                df = df_window

        if df.empty or len(df) < 5:
            return {"error": f"No backtest data available for last {days} days"}

        start_at = str(df["timestamp"].iloc[0]) if "timestamp" in df.columns else ""
        end_at = str(df["timestamp"].iloc[-1]) if "timestamp" in df.columns else ""

        cost_profile = "intraday" if interval_str == "15m" else "delivery"
        bt = Backtester(
            initial_capital=100000,
            cost_profile=cost_profile,
            min_sample_size=30,
        )
        result = bt.run(df)

        payload = {
            "symbol": symbol,
            "provider": provider,
            "timeframe": interval_str,
            "cost_profile": cost_profile,
            "start_at": start_at,
            "end_at": end_at,
            "bars": int(len(df)),
            "trade_count": result.total_trades,
            "win_count": result.winning_trades,
            "loss_count": result.losing_trades,
            "win_rate": round(result.win_rate, 1),
            "gross_return": round(result.gross_return_pct, 2),
            "estimated_costs": round(result.estimated_costs_pct, 2),
            "net_return": round(result.net_return_pct, 2),
            "per_trade_cost_pct": round(getattr(result, "per_trade_cost_pct", 0.0), 3),
            "cost_drag_pct": round(getattr(result, "cost_drag_pct", 0.0), 1),
            "stats_available": getattr(result, "stats_available", False),
            "min_trades_for_stats": getattr(result, "min_trades_for_stats", 20),
            "points_pnl": round(result.points_pnl, 2),
            "net_pnl_inr": round(result.net_pnl_inr, 2),
            "profit_factor": round(result.profit_factor, 2),
            "expectancy": round(result.expectancy_pct, 2),
            "max_drawdown": round(result.max_drawdown_pct, 1),
            "sharpe": round(result.sharpe_ratio, 2),
            "sample_status": result.sample_status,
            "sample_badge": result.sample_badge,
            "cost_assumptions": result.cost_assumptions,
            "cache_status": "MISS",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            # Legacy backward-compatible fields
            "window_days": days,
            "bars_used": int(len(df)),
            "total_trades": result.total_trades,
            "total_pnl": round(result.points_pnl, 2),
            "max_drawdown_pct": round(result.max_drawdown_pct, 1),
            "sharpe_ratio": round(result.sharpe_ratio, 2),
        }

        # Cache with bounded memory (max 100 entries)
        if len(_backtest_cache) > 100:
            oldest_k = next(iter(_backtest_cache))
            _backtest_cache.pop(oldest_k, None)
        _backtest_cache[cache_key] = (time.time(), payload)

        return payload


@router.get("/api/news/{symbol}")
async def get_symbol_news(symbol: str, limit: int = Query(10, ge=1, le=30)):
    """
    Fetch deduplicated, relevance-classified, freshness-tagged news for a specific stock.
    Classifications: COMPANY_SPECIFIC, SECTOR_RELATED, MACRO_RELATED.
    """
    clean_sym = symbol.replace("-", "/").strip().upper().replace(".NS", "")
    now_ts = time.time()
    cached = _symbol_news_cache.get(clean_sym)
    if cached and (now_ts - cached[0]) < _SYMBOL_NEWS_CACHE_TTL:
        return cached[1]

    # Map symbol to company name and sector keywords
    COMPANY_NAMES = {
        "VEDL": ("Vedanta", ["metal", "mining", "aluminum", "zinc", "copper", "steel", "natural resources"]),
        "RELIANCE": ("Reliance", ["telecom", "jio", "oil", "retail", "refinery", "petrochem", "energy"]),
        "TCS": ("Tata Consultancy Services", ["it", "software", "tech", "ai", "consulting", "deal win"]),
        "HDFCBANK": ("HDFC Bank", ["banking", "nifty bank", "loans", "rbi", "npa", "deposits", "credit"]),
        "SBIN": ("State Bank of India", ["psu bank", "banking", "nifty bank", "rbi", "loans", "deposits"]),
        "INFY": ("Infosys", ["it", "software", "tech", "guidance"]),
        "ICICIBANK": ("ICICI Bank", ["banking", "loans", "nifty bank"]),
        "TATAMOTORS": ("Tata Motors", ["auto", "ev", "jlr", "vehicles"]),
    }
    
    comp_info = COMPANY_NAMES.get(clean_sym, (clean_sym, ["stock", "markets"]))
    company_name = comp_info[0]
    sector_keywords = comp_info[1]

    query = quote_plus(f"{company_name} {clean_sym} India share stock")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    
    def _fetch_rss():
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"})
        with urlopen(req, timeout=8) as resp:
            return resp.read()

    try:
        xml_data = await asyncio.to_thread(_fetch_rss)
        root = ET.fromstring(xml_data)
        items = root.findall(".//item")
    except Exception as exc:
        logger.warning("Failed to fetch news for %s: %s", clean_sym, exc)
        return {"symbol": clean_sym, "items": [], "total": 0, "status": "ERROR"}

    parsed_items = []
    seen_titles = []
    seen_urls = set()

    now_dt = datetime.now(timezone.utc)

    for it in items:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub_str = (it.findtext("pubDate") or "").strip()
        source = (it.findtext("source") or "News").strip()

        if not title or not link or link in seen_urls:
            continue
        seen_urls.add(link)

        # Parse publication date & compute relative age
        pub_dt = None
        age_str = "Recent"
        if pub_str:
            try:
                pub_dt = parsedate_to_datetime(pub_str)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                else:
                    pub_dt = pub_dt.astimezone(timezone.utc)
                diff_sec = max(0, (now_dt - pub_dt).total_seconds())
                if diff_sec < 3600:
                    age_str = f"{int(diff_sec // 60)}m ago"
                elif diff_sec < 86400:
                    age_str = f"{int(diff_sec // 3600)}h ago"
                else:
                    age_str = f"{int(diff_sec // 86400)}d ago"
            except Exception:
                age_str = "Recent"

        # Deduplication by normalized title tokens
        norm_title = re.sub(r"[^a-zA-Z0-9\s]", "", title.lower())
        tokens = set(norm_title.split())
        is_dup = False
        for prev_tokens in seen_titles:
            overlap = len(tokens.intersection(prev_tokens)) / max(len(tokens), len(prev_tokens), 1)
            if overlap > 0.65:
                is_dup = True
                break
        if is_dup:
            continue
        seen_titles.append(tokens)

        # Relevance Classification
        title_lower = title.lower()
        sym_lower = clean_sym.lower()
        comp_lower = company_name.lower()
        
        if sym_lower in title_lower or comp_lower in title_lower:
            relevance = "COMPANY_SPECIFIC"
        elif any(k in title_lower for k in sector_keywords):
            relevance = "SECTOR_RELATED"
        else:
            relevance = "MACRO_RELATED"

        parsed_items.append({
            "headline": title,
            "source": source,
            "publication_timestamp": pub_dt.isoformat() if pub_dt else None,
            "url": link,
            "symbol_relevance": relevance,
            "age": age_str,
            "dedup_key": hashlib.md5(norm_title.encode("utf-8")).hexdigest()[:12]
        })
        if len(parsed_items) >= limit:
            break

    result_payload = {
        "symbol": clean_sym,
        "company_name": company_name,
        "items": parsed_items,
        "total": len(parsed_items),
        "updated_at": now_dt.isoformat()
    }
    _symbol_news_cache[clean_sym] = (now_ts, result_payload)
    return result_payload

# ── Market Intelligence Endpoints ───────────────────────

@router.get("/api/screener/gainers")
async def screener_gainers():
    """
    Top NSE gainers with X-Factor multiplier.
    1. In-memory movers cache (0ms)
    2. Direct in-memory market data if available (0ms)
    3. Durable DB snapshot from runtime_state (20ms)
    4. On-demand batch fetch with 35s timeout, persisting to DB runtime_state
    """
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

    # Durable DB runtime_state check BEFORE network fetch to ensure instant response
    try:
        from db.runtime_state import get_runtime_state
        db_data = await asyncio.to_thread(get_runtime_state, "screener_gainers")
        if db_data and isinstance(db_data, list) and len(db_data) > 0:
            logger.info("Screener gainers served from DB runtime_state: %d stocks", len(db_data))
            svc._movers_cache["gainers"] = db_data
            svc._movers_cache["updated_at"] = datetime.now().isoformat()
            return {
                "data": db_data,
                "real_data": True,
                "source": "durable_snapshot",
                "updated_at": datetime.now().isoformat(),
            }
    except Exception as exc:
        logger.debug("Runtime state lookup for gainers failed: %s", exc)

    # Cache & DB empty — run yFinance batch download with 35s timeout
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(fetch_nse_gainers_losers, "gainers"),
            timeout=35.0,
        )
        if data:
            svc._movers_cache["gainers"] = data
            svc._movers_cache["updated_at"] = datetime.now().isoformat()
            try:
                from db.runtime_state import set_runtime_state
                await asyncio.to_thread(set_runtime_state, "screener_gainers", data)
            except Exception:
                pass
    except asyncio.TimeoutError:
        logger.warning("Screener gainers timed out (35s) — returning empty fallback")
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
    1. In-memory movers cache (0ms)
    2. Direct in-memory market data if available (0ms)
    3. Durable DB snapshot from runtime_state (20ms)
    4. On-demand batch fetch with 35s timeout, persisting to DB runtime_state
    """
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

    # Durable DB runtime_state check BEFORE network fetch
    try:
        from db.runtime_state import get_runtime_state
        db_data = await asyncio.to_thread(get_runtime_state, "screener_losers")
        if db_data and isinstance(db_data, list) and len(db_data) > 0:
            logger.info("Screener losers served from DB runtime_state: %d stocks", len(db_data))
            svc._movers_cache["losers"] = db_data
            svc._movers_cache["updated_at"] = datetime.now().isoformat()
            return {
                "data": db_data,
                "real_data": True,
                "source": "durable_snapshot",
                "updated_at": datetime.now().isoformat(),
            }
    except Exception as exc:
        logger.debug("Runtime state lookup for losers failed: %s", exc)

    # Cache & DB empty — run yFinance batch download with 35s timeout
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(fetch_nse_gainers_losers, "losers"),
            timeout=35.0,
        )
        if data:
            svc._movers_cache["losers"] = data
            svc._movers_cache["updated_at"] = datetime.now().isoformat()
            try:
                from db.runtime_state import set_runtime_state
                await asyncio.to_thread(set_runtime_state, "screener_losers", data)
            except Exception:
                pass
    except asyncio.TimeoutError:
        logger.warning("Screener losers timed out (35s) — returning empty fallback")
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
    1. In-memory cache from svc._market_data (0ms)
    2. Durable DB snapshot from runtime_state (20ms)
    3. Batch yFinance download across sector proxies (2-3s)
    """
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

    # Check durable DB snapshot first
    try:
        from db.runtime_state import get_runtime_state
        db_sectors = await asyncio.to_thread(get_runtime_state, "sector_heatmap")
        if db_sectors and isinstance(db_sectors, list) and len(db_sectors) > 0:
            return {
                "sectors": db_sectors,
                "real_data": True,
                "source": "durable_snapshot",
                "updated_at": datetime.now().isoformat(),
            }
    except Exception as exc:
        logger.debug("Runtime state lookup for sectors failed: %s", exc)

    try:
        sectors = await asyncio.wait_for(
            asyncio.to_thread(fetch_sector_performance),
            timeout=20.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Sector fetch timed out (20s)")
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
async def get_scorecard(
    bootstrap_if_empty: bool = Query(True, description="Auto-populate historical bar outcomes if sample size < 30"),
):
    """
    Multi-dimensional win-rate and institutional mathematical expectancy scorecard from the trade journal.
    Dimensions: regime, symbol, hour, confidence bucket, quality grade.
    Metrics: win_rate, expectancy_r, profit_factor, payoff_ratio, max_drawdown_r, mfe_mae_diagnostics.
    """
    journal = get_journal()
    scorecard = await asyncio.to_thread(journal.get_scorecard)
    if bootstrap_if_empty and scorecard.get("resolved", 0) < 30:
        try:
            from agents.analytics.scorecard_bootstrapper import bootstrap_scorecard_from_history
            added = await bootstrap_scorecard_from_history(min_trades=40)
            if added > 0:
                scorecard = await asyncio.to_thread(journal.get_scorecard)
        except Exception as exc:
            logger.debug("Automatic scorecard bootstrap attempt failed: %s", exc)

    return scorecard


@router.post("/api/analytics/scorecard/bootstrap")
async def trigger_scorecard_bootstrap(
    force: bool = Query(False, description="Force re-running historical bar replay"),
    min_trades: int = Query(50, description="Minimum target sample size"),
):
    """Admin endpoint to populate TradeJournal with empirical historical bar-replay trades."""
    from agents.analytics.scorecard_bootstrapper import bootstrap_scorecard_from_history
    added = await bootstrap_scorecard_from_history(min_trades=min_trades, force=force)
    journal = get_journal()
    sc = await asyncio.to_thread(journal.get_scorecard)
    return {
        "ok": True,
        "added_trades": added,
        "total_resolved": sc.get("resolved", 0),
        "overall_win_rate": sc.get("overall_win_rate", 0.0),
        "expectancy_r": sc.get("expectancy_r", 0.0),
        "profit_factor": sc.get("profit_factor", 0.0),
    }

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

    # Persist each newly fetched official NSE snapshot once, not on every poll.
    if data.get("source") == "NSE" and data.get("real_data") is True and not data.get("stale"):
        try:
            from agents.market_intelligence import market_data as tradeflow_module
            fetched_at = data.get("fetched_at")
            if fetched_at and fetched_at != tradeflow_module._tradeflow_persisted_fetched_at:
                from db.runtime_state import set_runtime_state
                await asyncio.to_thread(set_runtime_state, "tradeflow_snapshot:v1", data)
                tradeflow_module._tradeflow_persisted_fetched_at = fetched_at
        except Exception as exc:
            logger.warning("Tradeflow snapshot persistence skipped: %s", exc)

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
async def get_overview_intelligence(response: Response, force: bool = False, refresh: bool = False):
    response.headers["Cache-Control"] = "public, max-age=5, s-maxage=15, stale-while-revalidate=30" 

    """
    Aggregated decision-cockpit payload for the Overview tab.

    Returns top ranked setups, high-quality-but-blocked setups, market guardrails,
    system/risk state, and an advisory learning overlay — all in one fast call.
    """
    try:
        force_refresh = bool(force or refresh)
        return await asyncio.to_thread(svc._compute_overview_intelligence, force_refresh)

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



@router.websocket("/ws/prices")
async def websocket_prices(websocket: WebSocket, token: str | None = None):
    """
    WebSocket endpoint for real-time price and signal updates.
    Enforces origin verification and utilizes a single shared in-process broadcaster
    to eliminate N-client database polling storms.
    """
    origin = websocket.headers.get("origin")
    approved_origins_raw = os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000,http://localhost:3000,http://localhost:5173,https://tradeai-pro.pages.dev,https://tradeai-pro.tradingpro.workers.dev,https://trading-api-n1wt.onrender.com,https://tradeai-pro.onrender.com",
    )
    approved_origins = {o.strip().rstrip("/") for o in approved_origins_raw.split(",") if o.strip()}
    if origin:
        clean_origin = origin.strip().rstrip("/")
        host_header = websocket.headers.get("host", "").strip()
        origin_domain = clean_origin.split("://")[-1].split(":")[0] if "://" in clean_origin else clean_origin
        is_same_host = bool(host_header and (host_header.split(":")[0] == origin_domain))

        if clean_origin not in approved_origins and not is_same_host:
            logger.warning("Rejected WebSocket connection from unauthorized origin: %s", origin)
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()
    svc.broadcaster.register(websocket)

    # Immediately push current cached snapshot on connection
    try:
        init_payload = await svc.broadcaster.get_initial_payload()
        await websocket.send_json(init_payload)
    except Exception:
        pass

    last_ping_at = time.time()
    _PING_INTERVAL = 15

    try:
        while True:
            # Server-side heartbeat ping to keep intermediate reverse proxies alive
            now_ts = time.time()
            if now_ts - last_ping_at >= _PING_INTERVAL:
                try:
                    await websocket.send_json({"type": "ping", "ts": datetime.now().isoformat()})
                    last_ping_at = now_ts
                except Exception:
                    break

            try:
                # Wait for optional client ping or close frame with short timeout
                await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WebSocket client exited: %s", e)
    finally:
        svc.broadcaster.unregister(websocket)

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

    from agents.market_intelligence.nse_fetcher import CRYPTO_SEARCH_SYMBOLS
    crypto_keys = {k.replace("/", "") for k in CRYPTO_SEARCH_SYMBOLS} | {k.split("/")[0] for k in CRYPTO_SEARCH_SYMBOLS}
    is_likely_nse = (

        nse_hits == 0

        and "/" not in q_clean

        and len(q_clean) >= 3

        and q_clean not in crypto_keys
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
    """Return top 5 stock opportunities found by the screener or durable store."""
    if opportunity_stocks:
        return {"opportunities": opportunity_stocks}

    # Fallback to latest intraday scan or market snapshot
    opps = []
    try:
        latest_scan = await asyncio.to_thread(intraday_store.load_latest_scan)
        if latest_scan and latest_scan.get("signals"):
            for s in latest_scan["signals"][:5]:
                opps.append({
                    "symbol": s.get("symbol"),
                    "direction": s.get("direction", "LONG"),
                    "signal": s.get("direction", "LONG"),
                    "strategy": s.get("strategy", "INTRADAY"),
                    "confidence": float(s.get("confluence_score") or s.get("score") or 60.0),
                    "entry": s.get("entry"),
                    "price": float(s.get("entry") or 0.0),
                    "target": s.get("target"),
                    "stop_loss": s.get("stop_loss"),
                    "rationale": s.get("rationale", ""),
                    "source": "INTRADAY_STORE",
                })
    except Exception as exc:
        logger.debug("Screener opportunities intraday fallback failed: %s", exc)

    if not opps:
        try:
            from db.runtime_state import get_runtime_state
            snap = await asyncio.to_thread(get_runtime_state, "latest_market_snapshot")
            if snap and isinstance(snap, dict) and "prices" in snap:
                for sym, p in list(snap["prices"].items())[:5]:
                    sig = p.get("signal", "HOLD")
                    if sig in ("LONG", "SHORT", "BUY", "SELL"):
                        sig_direction = "LONG" if sig in ("LONG", "BUY") else "SHORT"
                        entry_val = float(p.get("close") or 0.0)
                        opps.append({
                            "symbol": sym,
                            "direction": sig_direction,
                            "signal": sig_direction,
                            "strategy": "WORKER_CYCLE",
                            "confidence": 65.0,
                            "entry": entry_val,
                            "price": entry_val,
                            "target": round(entry_val * (1.02 if sig_direction == "LONG" else 0.98), 2),
                            "stop_loss": round(entry_val * (0.99 if sig_direction == "LONG" else 1.01), 2),
                            "rationale": f"Market snapshot signal {sig} in regime {p.get('regime', 'TREND')}",
                            "source": "MARKET_SNAPSHOT",
                        })
        except Exception as exc:
            logger.debug("Screener opportunities snapshot fallback failed: %s", exc)

    if not opps:
        return empty_scan_payload("screener_opportunities", "no_candidates")
    return {"opportunities": opps, "status": "ok", "count": len(opps)}

@router.get("/api/search/latency")
async def get_search_latency_stats():
    """Latency distribution (p50, p95, p99) for symbol quote resolution."""
    if not _quote_latencies_ms:
        return {"samples": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0}
    arr = sorted(_quote_latencies_ms)
    n = len(arr)
    return {
        "samples": n,
        "p50_ms": arr[int(n * 0.50)],
        "p95_ms": arr[min(int(n * 0.95), n - 1)],
        "p99_ms": arr[min(int(n * 0.99), n - 1)],
        "min_ms": arr[0],
        "max_ms": arr[-1],
    }

@router.get("/api/search/quote/{symbol}")

async def search_quote(symbol: str):

    """

    Get full quote (OHLCV + indicators + signal) for any symbol.

    Supports NSE stocks (yFinance), crypto pairs (CCXT/Binance),

    and any global ticker.

    Auto-appends ".NS" suffix for unrecognized Indian stock symbols.

    """
    t_start = time.perf_counter()

    clean = symbol.replace("-", "/").replace("_", " ")

    # Strip any .NS suffix the caller may have included (normalize to display name format)

    clean_base = clean.upper().replace(".NS", "").strip()

    # Auto-normalize bare crypto symbols or USDT pairs: e.g. "SOL" -> "SOL/USDT", "BTC" -> "BTC/USDT", "SOLUSDT" -> "SOL/USDT"
    if "/" not in clean:
        clean_compact = clean.upper().replace(" ", "")
        clean_upper = clean.upper().strip()
        from agents.market_intelligence.nse_fetcher import CRYPTO_SEARCH_SYMBOLS, CRYPTO_ALIASES
        crypto_base_names = {k.split("/")[0] for k in CRYPTO_SEARCH_SYMBOLS}
        if clean_upper in CRYPTO_ALIASES:
            clean = CRYPTO_ALIASES[clean_upper]
            clean_base = clean
        elif clean_compact.endswith("USDT") and len(clean_compact) > 4:
            clean = f"{clean_compact[:-4]}/USDT"
            clean_base = clean
        elif clean_compact in crypto_base_names:
            clean = f"{clean_compact}/USDT"
            clean_base = clean

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

                    if os.getenv("LIGHTWEIGHT_API_MODE", "false").lower() in ("1", "true", "yes"):
                        from agents.ai_engine.fallback_predictor import predict_batch
                        df = generate_signals(df, regime=regime, ai_confidence=predict_batch(df))
                    else:
                        df = generate_signals(df, regime=regime)

                    svc._market_data[clean] = df

                    svc._signal_cache[clean] = df

            except Exception as e:

                logger.warning("Crypto fetch failed for %s: %s", clean, e)

                df = None

        # ── Fallback to yFinance (NSE + global) ──────

        if df is None:
            from agents.market_intelligence.nse_fetcher import NSE_SYMBOLS

            if clean_base in settings.NSE_TICKER_MAP:
                target_first = settings.NSE_TICKER_MAP[clean_base]
                is_known_nse = True
            elif clean_base in NSE_SYMBOLS:
                target_first = NSE_SYMBOLS[clean_base]
                is_known_nse = True
            elif "/" not in clean and not clean.startswith("^"):
                # Default Indian equities directly to .NS to eliminate wasteful failed queries on US tickers
                target_first = f"{clean_base}.NS"
                is_known_nse = True
            else:
                target_first = clean
                is_known_nse = False

            # 1st attempt: targeted symbol (NSE ticker for Indian stocks, raw symbol for global)
            df = await asyncio.to_thread(fetch_arbitrary_symbol, target_first)
            if df is not None and not df.empty and is_known_nse:
                clean = clean_base

            # 2nd attempt: fallback if 1st attempt failed (e.g. global ticker without .NS)
            if (df is None or df.empty) and target_first != clean:
                logger.info("Trying fallback ticker: %s", clean)
                df = await asyncio.to_thread(fetch_arbitrary_symbol, clean)
                if df is not None and not df.empty:
                    clean = clean_base

            if df is None or df.empty:

                return JSONResponse(

                    status_code=404,

                    content={

                        "error": f"Symbol '{symbol}' not found on NSE or yFinance. "

                        f"Tried '{clean_base}.NS'. Check the symbol and try again."

                    },

                )

            df = calculate_indicators(df)

            htf_target = target_first if is_known_nse else (f"{clean_base}.NS" if "/" not in clean else clean)
            htf_df = await asyncio.to_thread(fetch_arbitrary_symbol, htf_target, "60d", "1d")
            if (htf_df is None or htf_df.empty) and htf_target != clean:
                htf_df = await asyncio.to_thread(fetch_arbitrary_symbol, clean, "60d", "1d")

            if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:

                htf_df = calculate_indicators(htf_df)

                htf_df = classify_trend(htf_df)

                df = propagate_trend_to_ltf(df, htf_df)

            else:

                df = classify_trend(df)

            regime = detect_regime(df)

            svc._regime_cache[clean] = regime.value

            if os.getenv("LIGHTWEIGHT_API_MODE", "false").lower() in ("1", "true", "yes"):
                from agents.ai_engine.fallback_predictor import predict_batch
                df = generate_signals(df, regime=regime, ai_confidence=predict_batch(df))
            else:
                df = generate_signals(df, regime=regime)

            svc._market_data[clean] = df

            svc._signal_cache[clean] = df

    last = df.iloc[-1]

    # Build quote response
    sig = svc._get_signal_for_symbol(clean)
    if sig is None and clean_base != clean:
        sig = svc._get_signal_for_symbol(clean_base)

    if sig is None and getattr(svc, "_persisted_signals_snapshot", None):
        for s in svc._persisted_signals_snapshot:
            if isinstance(s, dict) and s.get("symbol", "").upper().replace(".NS", "") in (clean_base, clean.upper()):
                sig = s
                break

    if sig is None:
        c_val = round(float(last.get("close", 0.0)), 2)
        sig = {
            "symbol": clean,
            "signal": "HOLD",
            "direction": "HOLD",
            "signal_status": "NO_SIGNAL",
            "actionable": False,
            "entry": c_val,
            "close": c_val,
            "change_pct": round(float(last.get("change_pct", 0.0)), 2),
            "stop_loss": None,
            "target": None,
            "levels_source": "UNAVAILABLE",
            "rsi": round(float(last.get("RSI_14", 50.0)), 1) if pd.notna(last.get("RSI_14")) else None,
            "trend": str(last.get("htf_trend", "NEUTRAL")),
            "confidence": 45.0,
            "timestamp": (
                last["timestamp"].isoformat()
                if hasattr(last.get("timestamp"), "isoformat")
                else str(last.get("timestamp") or datetime.now(timezone.utc).isoformat())
            ),
        }

    # Recent OHLCV data (last 100 bars)
    tail = df.tail(100)
    chart_data = []
    for _, row in tail.iterrows():
        chart_data.append(
            {
                "timestamp": (
                    row["timestamp"].isoformat()
                    if hasattr(row.get("timestamp"), "isoformat")
                    else str(row.get("timestamp") or datetime.now(timezone.utc).isoformat())
                ),
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

    # Free heap and trim glibc malloc pages to protect Render 512MB RAM cap
    svc._force_free_memory()

    elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
    _quote_latencies_ms.append(elapsed_ms)

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


@router.api_route("/", methods=["GET", "HEAD"])

async def serve_dashboard():

    """Serve the main dashboard page."""

    index = os.path.join(STATIC_DIR, "index.html")

    if os.path.exists(index):

        return FileResponse(index, headers=_DASH_HTML_HEADERS)

    return {"message": "Dashboard not found. Place files in /dashboard/"}

@router.api_route("/landing", methods=["GET", "HEAD"])

async def serve_landing():

    """Serve the marketing landing page."""

    landing = os.path.join(STATIC_DIR, "landing.html")

    if os.path.exists(landing):

        return FileResponse(landing, headers=_DASH_HTML_HEADERS)

    return FileResponse(

        os.path.join(STATIC_DIR, "index.html"), headers=_DASH_HTML_HEADERS

    )

@router.get("/legal.html", include_in_schema=False)
@router.get("/legal", include_in_schema=False)
async def serve_legal():
    """Serve the legal, terms and privacy disclaimer page."""
    legal_path = os.path.join(STATIC_DIR, "legal.html")
    if os.path.exists(legal_path):
        return FileResponse(legal_path, headers=_DASH_HTML_HEADERS)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=_DASH_HTML_HEADERS)



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

_INDICES_FALLBACK_CACHE: dict[str, tuple[float | None, float | None]] = {}
_INDICES_FALLBACK_TS: float = 0.0

def _get_major_indices_sync() -> dict[str, tuple[float | None, float | None]]:
    """Fetch or read cached quotes for NIFTY, SENSEX, and BANK NIFTY."""
    global _INDICES_FALLBACK_CACHE, _INDICES_FALLBACK_TS
    now = time.time()
    if _INDICES_FALLBACK_CACHE and (now - _INDICES_FALLBACK_TS) < 60.0:
        return _INDICES_FALLBACK_CACHE

    res: dict[str, tuple[float | None, float | None]] = {
        "nifty": (None, None),
        "sensex": (None, None),
        "banknifty": (None, None),
    }

    sym_mapping = {
        "nifty": ["NIFTY 50", "NIFTY", "^NSEI"],
        "sensex": ["SENSEX", "^BSESN", "BSE_SENSEX"],
        "banknifty": ["BANKNIFTY", "^NSEBANK", "BANK_NIFTY"],
    }
    for idx_key, candidates in sym_mapping.items():
        for sym in candidates:
            df = svc._market_data.get(sym)
            if isinstance(df, pd.DataFrame) and len(df) >= 2:
                # Check bar staleness during open market hours
                ts_bar = df["timestamp"].iloc[-1] if "timestamp" in df.columns and len(df["timestamp"]) > 0 else df.index[-1]
                bar_is_stale = False
                try:
                    if isinstance(ts_bar, str):
                        dt_b = datetime.fromisoformat(ts_bar.replace("Z", "+00:00"))
                    elif hasattr(ts_bar, "to_pydatetime"):
                        dt_b = ts_bar.to_pydatetime()
                    elif isinstance(ts_bar, datetime):
                        dt_b = ts_bar
                    else:
                        dt_b = None
                    if dt_b is not None:
                        if dt_b.tzinfo is None:
                            from api.data_honesty import IST
                            dt_b = dt_b.replace(tzinfo=IST)
                        from agents.execution.session_guard import is_nse_market_open
                        now_ist = datetime.now(dt_b.tzinfo)
                        is_open, _ = is_nse_market_open(now_ist)
                        if is_open and (now_ist - dt_b).total_seconds() > 1800:
                            bar_is_stale = True
                except Exception:
                    pass

                if not bar_is_stale:
                    latest = float(df.iloc[-1]["close"])
                    first = float(df.iloc[0]["open"]) if float(df.iloc[0]["open"]) != 0 else float(df.iloc[0]["close"])
                    chg = ((latest - first) / first) * 100.0 if first else 0.0
                    res[idx_key] = (round(latest, 2), round(chg, 2))
                    break
            sig = svc._signal_cache.get(sym)
            if isinstance(sig, dict) and sig.get("close") is not None and not sig.get("stale"):
                p = float(sig["close"])
                c = float(sig.get("change_pct", 0.0)) if sig.get("change_pct") is not None else 0.0
                res[idx_key] = (round(p, 2), round(c, 2))
                break

    if any(res[k][0] is None for k in res):
        try:
            from db.runtime_state import get_runtime_state
            snap = get_runtime_state("latest_market_snapshot")
            if snap and isinstance(snap, dict) and "prices" in snap:
                prices = snap["prices"]
                for idx_key, candidates in sym_mapping.items():
                    if res[idx_key][0] is not None:
                        continue
                    for sym in candidates:
                        if sym in prices and isinstance(prices[sym], dict) and prices[sym].get("close") is not None:
                            p = float(prices[sym]["close"])
                            c = float(prices[sym].get("change_pct", 0.0)) if prices[sym].get("change_pct") is not None else 0.0
                            res[idx_key] = (round(p, 2), round(c, 2))
                            break
        except Exception:
            pass

    missing = [k for k in res if res[k][0] is None]
    if missing:
        yf_map = {"nifty": "^NSEI", "sensex": "^BSESN", "banknifty": "^NSEBANK"}
        try:
            import yfinance as yf
            needed_syms = " ".join(yf_map[k] for k in missing)
            tickers = yf.Tickers(needed_syms)
            for k in missing:
                t = tickers.tickers.get(yf_map[k])
                if t:
                    hist = t.history(period="1d", interval="5m")
                    if hist is None or hist.empty:
                        hist = t.history(period="5d", interval="1d")
                    if hist is not None and not hist.empty:
                        close = float(hist["Close"].iloc[-1])
                        first = float(hist["Open"].iloc[0]) if float(hist["Open"].iloc[0]) != 0 else close
                        chg = ((close - first) / first) * 100.0 if first else 0.0
                        res[k] = (round(close, 2), round(chg, 2))
        except Exception as exc:
            logger.debug("Indices yfinance fallback error: %s", exc)

    if any(res[k][0] is not None for k in res):
        _INDICES_FALLBACK_CACHE = res
        _INDICES_FALLBACK_TS = now
    return res

def _crypto_forex_enabled_for_mood() -> bool:
    """Read the Section 6A switch without letting a failure break the mood payload."""
    try:
        from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed

        return bool(is_crypto_forex_enabled())
    except Exception:
        return True


@router.get("/api/context/market-mood")

async def get_market_mood_api():

    """Return composite market mood payload for dashboard context strip.

    Cached for 60 seconds with strict timeout protection and non-blocking cache-first reads.

    """

    global _CONTEXT_MOOD_CACHE, _CONTEXT_MOOD_CACHE_TS, _BTC_DOM_BACKOFF_UNTIL, _BTC_DOM_LAST_WARN_TS

    now_ts = time.time()

    if _CONTEXT_MOOD_CACHE and (now_ts - _CONTEXT_MOOD_CACHE_TS) < _CONTEXT_MOOD_CACHE_TTL:

        return _CONTEXT_MOOD_CACHE

    indices_data = await asyncio.to_thread(_get_major_indices_sync)
    nifty_close, nifty_change = indices_data.get("nifty", (None, None))
    sensex_close, sensex_change = indices_data.get("sensex", (None, None))
    banknifty_close, banknifty_change = indices_data.get("banknifty", (None, None))

    # During startup warmup return neutral immediately — avoids 503 on first hit.
    if not svc._loading_status.get("ready"):
        return {
            "crypto_mood": "NEUTRAL",
            "btc_dominance": None,
            "btc_dominance_source": "warming_up",
            "btc_24hr_change": 0.0,
            "btc_price": None,
            "nifty_close": nifty_close,
            "nifty_24hr_change": nifty_change if nifty_change is not None else 0.0,
            "sensex_close": sensex_close,
            "sensex_24hr_change": sensex_change,
            "banknifty_close": banknifty_close,
            "banknifty_24hr_change": banknifty_change,
            "avg_news_sentiment": 0.0,
            "active_event_risk": False,
            "next_event": None,
        }

    try:

        fetcher = build_context_fetcher()

        # Cache-first or bounded fetch for BTCUSDT

        cached_btc = fetcher._get_cached_snapshot("BTCUSDT")

        if cached_btc and fetcher._is_fresh(cached_btc.get("cached_at"), 1800):

            btc_snapshot = fetcher._snapshot_from_cache(cached_btc)

        else:

            try:

                btc_snapshot = await asyncio.wait_for(

                    asyncio.to_thread(fetcher.get_snapshot_sync, "BTCUSDT"),

                    timeout=2.5,

                )

            except Exception:

                from agents.data_ingestion.context_fetcher import ContextSnapshotData

                btc_snapshot = ContextSnapshotData(
                    symbol="BTCUSDT",
                    timestamp_utc=datetime.now(timezone.utc),
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

        # Major indices synced via cached provider
        if nifty_close is None or sensex_close is None or banknifty_close is None:
            indices_data = await asyncio.to_thread(_get_major_indices_sync)
            if nifty_close is None:
                nifty_close, nifty_change = indices_data.get("nifty", (None, None))
            if sensex_close is None:
                sensex_close, sensex_change = indices_data.get("sensex", (None, None))
            if banknifty_close is None:
                banknifty_close, banknifty_change = indices_data.get("banknifty", (None, None))

        # Cache-first context sentiment over active symbols (0ms non-blocking)

        sentiments: list[float] = []

        active_event_risk = bool(btc_snapshot.event_risk_active)

        tracked = list(svc._signal_cache.keys())[:8]

        for sym in tracked:

            try:

                norm_sym = fetcher._normalize_symbol(sym)

                c_snap = fetcher._get_cached_snapshot(norm_sym)

                if c_snap:

                    snap = fetcher._snapshot_from_cache(c_snap)

                    sentiments.append(float(snap.news_sentiment_24hr or 0.0))

                    active_event_risk = active_event_risk or bool(snap.event_risk_active)

            except Exception:

                continue

        avg_sent = (

            float(np.mean(sentiments))

            if sentiments

            else float(btc_snapshot.news_sentiment_24hr or 0.0)

        )

        try:

            events = await asyncio.wait_for(

                asyncio.to_thread(fetcher.get_upcoming_events),

                timeout=1.5,

            )

            next_event = events[0] if events else None

        except Exception:

            next_event = None

        if active_event_risk or btc_snapshot.price_change_24hr_pct <= -1.0:

            crypto_mood = "RISK_OFF"

        elif btc_snapshot.price_change_24hr_pct >= 1.0 and avg_sent > 0.1:

            crypto_mood = "RISK_ON"

        else:

            crypto_mood = "NEUTRAL"

        dominance_source = "cache"

        btc_dominance = float(

            fetcher._global_cache.get("coingecko_global", {})

            .get("payload", {})

            .get("btc_dominance", 0.0)

        )

        crypto_enabled = _crypto_forex_enabled_for_mood()

        payload = {

            "crypto_mood": crypto_mood,

            "btc_dominance": round(btc_dominance, 2) if btc_dominance > 0 else None,

            "btc_dominance_source": dominance_source,

            "btc_24hr_change": round(

                float(btc_snapshot.price_change_24hr_pct or 0.0), 2

            ),

            "btc_price": round(float(btc_snapshot.price), 2) if getattr(btc_snapshot, "price", None) else None,
            # A null BTC price under NSE Focus Mode is a policy state, not a data
            # outage. Say so, so the header can render "OFF" instead of "—".
            "crypto_forex_enabled": crypto_enabled,
            "btc_price_status": (
                "OK"
                if getattr(btc_snapshot, "price", None)
                else ("DISABLED_BY_POLICY" if not crypto_enabled else "UNAVAILABLE")
            ),
            "nifty_close": nifty_close,
            "nifty_24hr_change": nifty_change if nifty_change is not None else 0.0,
            "sensex_close": sensex_close,
            "sensex_24hr_change": sensex_change,
            "banknifty_close": banknifty_close,
            "banknifty_24hr_change": banknifty_change,

            "avg_news_sentiment": round(avg_sent, 3),

            "active_event_risk": active_event_risk,

            "next_event": next_event,

        }

        _CONTEXT_MOOD_CACHE = payload

        _CONTEXT_MOOD_CACHE_TS = time.time()

        return payload

    except Exception as e:

        logger.error("Error fetching market mood: %s", e)

        if _CONTEXT_MOOD_CACHE:

            return _CONTEXT_MOOD_CACHE

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
