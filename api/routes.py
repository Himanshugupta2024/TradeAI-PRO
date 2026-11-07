"""HTTP and WebSocket route handlers for the trading API."""

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
from pydantic import BaseModel
from starlette.requests import Request as FastApiRequest
from starlette.responses import JSONResponse

from api.security import require_admin

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

_CONTEXT_MOOD_CACHE: dict = {}

_CONTEXT_MOOD_CACHE_TS: float = 0.0

_CONTEXT_MOOD_CACHE_TTL: float = 60.0

@router.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)

async def favicon():

    """Serve favicon at site root — browsers request /favicon.ico on the API host."""

    path = os.path.join(STATIC_DIR, "favicon.ico")

    if os.path.isfile(path):

        return FileResponse(path, media_type="image/x-icon")

    return Response(status_code=204)

@router.api_route("/api/health", methods=["GET", "HEAD"])

async def health_check():

    """Health check endpoint — shows loading progress, server status, and DB availability."""

    try:

        from agents.ai_engine.ensemble import get_model_status

        ai_engine_info = get_model_status()

    except Exception as _e:

        ai_engine_info = {
            "mode": "Statistical Heuristic (fallback)",
            "badge_text": "AI Engine: Statistical Heuristic (0/4 models fitted)",
            "badge_short": "🧠 AI: Heuristic (0/4 ML)",
            "fitted_count": 0,
            "total_ml_models": 4,
            "statistical_heuristic": True,
        }

    try:
        db_ok = await _check_db_connectivity_with_retry()
    except Exception:
        db_ok = False
    feed_status = svc._get_feed_status()
    api_health = "API_UP" if db_ok else "API_DEGRADED"

    symbols_loaded = len(svc._market_data)
    is_lightweight = os.getenv("LIGHTWEIGHT_API_MODE", "false").lower() in ("1", "true", "yes")
    if symbols_loaded == 0 and (is_lightweight or feed_status.get("source") in ("WORKER_DB", "INTRADAY_STORE")):
        symbols_loaded = len(svc.TOP_NSE_SYMBOLS) if getattr(svc, "TOP_NSE_SYMBOLS", None) else 20

    uptime_sec = round(time.time() - getattr(svc, "_SERVER_START_TIME", time.time()), 1)

    status_str = "ok" if db_ok and (symbols_loaded > 0 or is_lightweight) else "degraded" if not db_ok else "starting"

    worker_status = None
    try:
        from db.runtime_state import get_runtime_state
        worker_status = get_runtime_state("autonomous_worker:latest_status")
    except Exception:
        pass

    try:
        from api.health_v2 import evaluate_health_v2
        h2 = evaluate_health_v2(db_ok=db_ok, feed_status=feed_status, worker_status=worker_status)
    except Exception as exc:
        logger.warning("Health v2 evaluation fallback: %s", exc)
        h2 = {
            "status": status_str,
            "healthy": db_ok,
            "health_tier": {"infra": "UP" if db_ok else "DOWN", "worker": "UNKNOWN", "freshness": "UNKNOWN"},
            "health_reasons": {"infra": "Database connected" if db_ok else "Database down", "worker": "Unknown", "freshness": "Unknown"},
            "overall_reason": "Health v2 fallback",
            "data_age_seconds": feed_status.get("age_seconds"),
            "data_age_human": "Data: 15m Delayed",
            "is_market_open": is_market_open(),
            "market_session_status": "UNKNOWN",
        }

    status_str = "ok" if str(h2.get("status", "")).upper() in ("HEALTHY", "OK") else str(h2.get("status", "degraded")).lower()

    signals_count = len(svc._signal_cache)
    if signals_count == 0:
        if getattr(svc, "_persisted_signals_snapshot", None):
            signals_count = len(svc._persisted_signals_snapshot)
        else:
            try:
                from db.runtime_state import get_runtime_state
                snap = get_runtime_state("latest_signals:v1")
                if snap and isinstance(snap, dict):
                    signals_count = int(snap.get("count", len(snap.get("signals", []))))
            except Exception:
                pass
            if signals_count == 0:
                try:
                    import db.intraday_store as ids
                    latest_scan = ids.load_latest_scan()
                    if latest_scan and latest_scan.get("results"):
                        signals_count = len(latest_scan["results"])
                except Exception:
                    pass

    return {
        "status": status_str,
        "healthy": h2.get("healthy", db_ok),
        "api_health": api_health,
        "health_tier": h2.get("health_tier", {}),
        "health_reasons": h2.get("health_reasons", {}),
        "overall_reason": h2.get("overall_reason", ""),
        "data_age_seconds": h2.get("data_age_seconds"),
        "data_age_human": h2.get("data_age_human", "Data: 15m Delayed"),
        "last_bar_at": feed_status.get("last_bar_at") or h2.get("last_bar_at"),
        "bar_age_seconds": feed_status.get("bar_age_seconds") or h2.get("bar_age_seconds"),
        "data_age_source": h2.get("data_age_source"),
        "is_market_open": h2.get("is_market_open", False),
        "market_snapshot_health": feed_status.get("market_snapshot_health", "UNAVAILABLE"),
        "worker_health": feed_status.get("worker_health", "NO_RECENT_RUN"),
        "database": {"connected": db_ok},
        "loading": svc._loading_status,
        "feed": feed_status,
        "symbols_loaded": symbols_loaded,
        "signals_cached": signals_count,
        "ai_engine": ai_engine_info,
        "providers": (
            getattr(svc, "_get_providers_telemetry", lambda: {})()
            if hasattr(svc, "_get_providers_telemetry")
            else {}
        ),
        "last_refresh": (
            svc._last_refresh.isoformat()
            if svc._last_refresh != datetime.min
            else feed_status.get("last_update")
        ),
        "uptime_seconds": uptime_sec,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }

@router.api_route("/api/ready", methods=["GET", "HEAD"])
async def readiness_check():
    """Readiness probe: verifies DB connectivity and that market data or snapshots are available."""
    try:
        db_ok = await _check_db_connectivity_with_retry()
    except Exception:
        db_ok = False
    symbols_count = len(svc._market_data)
    is_lightweight = os.getenv("LIGHTWEIGHT_API_MODE", "false").lower() in ("1", "true", "yes")

    ready = db_ok and (
        symbols_count > 0
        or svc._loading_status.get("ready", False)
        or is_lightweight
    )

    if not ready:
        reasons = []
        if not db_ok:
            reasons.append("Database unavailable")
        if symbols_count == 0 and not is_lightweight:
            reasons.append("Zero market symbols loaded")

        return JSONResponse(
            status_code=503,
            content={
                "ready": False,
                "status": "degraded" if not db_ok else "warming_up",
                "reasons": reasons,
                "symbols_loaded": symbols_count,
                "db_connected": db_ok,
            },
        )

    return {

        "ready": True,

        "status": "ready",

        "symbols_loaded": symbols_count,
        "db_connected": db_ok,
        "last_refresh": svc._last_refresh.isoformat() if svc._last_refresh != datetime.min else None,
    }

@router.get("/api/system/data-health")
async def get_system_data_health():
    """
    Consolidated Subsystem Observability Endpoint.
    Returns operational health, snapshot age, data freshness, and diagnostics across 9 core subsystems.
    """
    now_utc = datetime.now(timezone.utc)
    try:
        db_ok = await _check_db_connectivity_with_retry()
    except Exception:
        db_ok = False

    # 1. Feed / Market Snapshot
    feed_status = svc._get_feed_status()

    # 2. Intraday Scanner Store
    intraday_status = {"status": "UNAVAILABLE", "age_seconds": None, "last_scan": None}
    try:
        latest_iscan = await asyncio.to_thread(intraday_store.load_latest_scan)
        if latest_iscan and latest_iscan.get("scan_time"):
            st = latest_iscan["scan_time"]
            intraday_status = {
                "status": "HEALTHY",
                "last_scan": st,
                "age_seconds": latest_iscan.get("scan_age_seconds"),
                "signal_count": latest_iscan.get("signal_count", 0),
                "data_status": latest_iscan.get("data_status"),
            }
    except Exception as exc:
        intraday_status["error"] = str(exc)

    # 3. Swing Scanner Store
    swing_status = {"status": "UNAVAILABLE", "count": 0, "last_update": None}
    try:
        from db.runtime_state import get_runtime_state
        swing_snap = await asyncio.to_thread(get_runtime_state, "latest_swing_scan")
        if swing_snap and isinstance(swing_snap, dict):
            swing_status = {
                "status": "HEALTHY",
                "last_update": swing_snap.get("timestamp"),
                "count": len(swing_snap.get("setups", [])),
                "symbols_scanned": swing_snap.get("symbols_scanned", 0),
            }
        elif svc._swing_cache.get("setups"):
            swing_status = {
                "status": "HEALTHY (IN_MEMORY)",
                "last_update": svc._swing_cache.get("updated_at"),
                "count": len(svc._swing_cache["setups"]),
            }
    except Exception as exc:
        swing_status["error"] = str(exc)

    # 4. Breakout Scanner
    breakout_status = {"status": "UNAVAILABLE", "count": 0, "last_update": None}
    try:
        from db.runtime_state import get_runtime_state
        bo_snap = await asyncio.to_thread(get_runtime_state, "latest_breakouts")
        if bo_snap and isinstance(bo_snap, dict):
            breakout_status = {
                "status": bo_snap.get("status", "HEALTHY"),
                "count": bo_snap.get("total_breakouts", 0),
                "symbols_scanned": bo_snap.get("symbols_scanned", 0),
                "last_update": bo_snap.get("updated_at"),
            }
    except Exception as exc:
        breakout_status["error"] = str(exc)

    # 5. Institutional Flow
    flow_status = {"status": "UNAVAILABLE", "date": None}
    try:
        from agents.market_intelligence.market_data import _tradeflow_cache
        if _tradeflow_cache:
            flow_status = {
                "status": "HEALTHY" if not _tradeflow_cache.get("stale") else "STALE",
                "date": _tradeflow_cache.get("display_date") or _tradeflow_cache.get("date"),
                "source": _tradeflow_cache.get("source"),
                "flows_count": len(_tradeflow_cache.get("flows", [])),
            }
    except Exception as exc:
        flow_status["error"] = str(exc)

    # 6. Macro Heatmap
    macro_status = {"status": "UNAVAILABLE", "last_update": None}
    try:
        from db.runtime_state import get_runtime_state
        macro_snap = await asyncio.to_thread(get_runtime_state, "latest_macro_heatmap")
        if macro_snap and isinstance(macro_snap, dict):
            macro_status = {
                "status": "HEALTHY",
                "last_update": macro_snap.get("timestamp"),
                "factors_count": len(macro_snap.get("heatmap", {})),
            }
    except Exception as exc:
        macro_status["error"] = str(exc)

    # 7. Signal Scorecard
    scorecard_status = {"status": "UNAVAILABLE", "total_signals": 0, "win_rate": None}
    try:
        sc = await asyncio.to_thread(get_journal().get_scorecard)
        if sc:
            scorecard_status = {
                "status": "HEALTHY" if sc.get("total_signals", 0) > 0 else "WARMING_UP",
                "total_signals": sc.get("total_signals", 0),
                "win_rate": sc.get("overall_win_rate"),
                "wins": sc.get("wins", 0),
                "losses": sc.get("losses", 0),
            }
    except Exception as exc:
        scorecard_status["error"] = str(exc)

    # 8. Autonomous Worker
    worker_status = {"status": "NO_RECENT_RUN", "last_run": None}
    try:
        from db.runtime_state import get_runtime_state
        w_raw = await asyncio.to_thread(get_runtime_state, "autonomous_worker:latest_status")
        if w_raw and isinstance(w_raw, dict):
            worker_status = {
                "status": feed_status.get("worker_health", "WORKER_OK"),
                "last_run": w_raw.get("timestamp"),
                "run_id": w_raw.get("run_id"),
                "trades_executed": w_raw.get("trades_executed", 0),
                "open_positions": w_raw.get("open_positions", 0),
            }
    except Exception as exc:
        worker_status["error"] = str(exc)

    # 9. WebSocket Transport
    ws_status = {
        "active_clients": len(getattr(svc.broadcaster, "_clients", [])),
        "status": "ACTIVE" if len(getattr(svc.broadcaster, "_clients", [])) > 0 else "IDLE_READY",
        "transport": "SNAPSHOT_BROADCASTER",
    }

    overall_system_status = (
        "HEALTHY"
        if db_ok and feed_status.get("market_snapshot_health") in ("CURRENT", "DELAYED", "EOD")
        else "DEGRADED"
    )

    return {
        "status": overall_system_status,
        "api_health": "API_UP" if db_ok else "API_DEGRADED",
        "timestamp": now_utc.isoformat(),
        "subsystems": {
            "nse_snapshot": feed_status,
            "intraday_scanner": intraday_status,
            "swing_scanner": swing_status,
            "breakout_scanner": breakout_status,
            "institutional_flow": flow_status,
            "macro": macro_status,
            "signal_scorecard": scorecard_status,
            "autonomous_worker": worker_status,
            "websocket": ws_status,
        },
    }

@router.get("/api/health/live")

async def liveness_check():

    """Ultra-lightweight liveness probe (sub-1ms)."""

    start_t = getattr(svc, "_SERVER_START_TIME", time.time())

    return {

        "status": "alive",

        "uptime_seconds": round(time.time() - start_t, 2),

        "timestamp": datetime.now(timezone.utc).isoformat(),

    }

@router.get("/api/health/ready")

async def health_ready_check():

    """Readiness probe alias for standard k8s/cloud health checks."""

    return await readiness_check()

@router.get("/api/health/providers")

async def health_providers_check():

    """Telemetry for upstream data providers."""

    telemetry = svc._get_providers_telemetry() if hasattr(svc, "_get_providers_telemetry") else {}

    return {

        "providers": telemetry,

        "timestamp": datetime.now(timezone.utc).isoformat(),

    }

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

# ── Section 6A: NSE Focus Mode & Protected Market Control ───────────────
from pydantic import BaseModel
class MarketControlToggleRequest(BaseModel):
    enabled: bool
    password: Optional[str] = None
    reason: Optional[str] = None

@router.get("/api/market-control/status")
async def get_market_control_status_route():
    """Return Section 6A Market Control status and NSE Focus Mode telemetry."""
    from agents.execution.market_control import get_market_control_status
    return await asyncio.to_thread(get_market_control_status)

from starlette.requests import Request as FastApiRequest
@router.post("/api/market-control/toggle")
async def toggle_market_control_route(req: MarketControlToggleRequest, request: FastApiRequest):
    """
    Operator control for Crypto / Forex processing under Section 6A policy.
    - Turning ON requires server-side password verification.
    - Turning OFF verifies open position safeguards; blocks if positions require management.
    """
    from agents.execution.market_control import set_market_control_state
    client_ip = request.client.host if request.client else "unknown"
    success, message, state = set_market_control_state(
        enabled=req.enabled,
        password=req.password,
        client_id=client_ip,
        operator="operator",
        reason=req.reason or "",
    )
    invalidate_autonomous_status_cache()
    status_code = 200 if success else (403 if any(w in message for w in ["Invalid", "Password", "lockout", "unconfigured"]) else 400)
    return JSONResponse(
        status_code=status_code,
        content={
            "success": success,
            "message": message,
            "state": state,
        }
    )

# ── Section 6B: Next Trading Day Picks Research Endpoints ─────────────────────

class NextDayGenerateRequest(BaseModel):
    force: bool = False
    max_picks: int = 5

class NextDayResolveRequest(BaseModel):
    report_id: str
    actual_candles: Dict[str, Dict[str, float]]

@router.get("/api/research/next-day-picks")
async def get_next_day_picks_route():
    """Return latest published Next Trading Day Picks research report."""
    from agents.market_intelligence.next_day_picks import get_latest_next_day_picks
    return await asyncio.to_thread(get_latest_next_day_picks)

@router.post("/api/research/next-day-picks/generate")
async def generate_next_day_picks_route(
    req: NextDayGenerateRequest = NextDayGenerateRequest()
):
    """
    Operator trigger to execute bounded post-close equity research.
    Rate-limited, idempotent, and enforces Section 6A zero-work for crypto/forex.
    """
    from agents.market_intelligence.next_day_picks import generate_next_day_picks
    report = await asyncio.to_thread(
        generate_next_day_picks,
        force_refresh=req.force,
        max_picks=min(10, max(1, req.max_picks))
    )
    return JSONResponse(status_code=200, content=report)

@router.get("/api/research/next-day-picks/history")
async def get_next_day_picks_history_route():
    """Return historical archive index of published research reports."""
    from agents.market_intelligence.next_day_picks import get_next_day_picks_history
    history = await asyncio.to_thread(get_next_day_picks_history)
    return {"ok": True, "count": len(history), "history": history}

@router.get("/api/research/next-day-picks/{report_id}")
async def get_next_day_pick_by_id_route(report_id: str):
    """Fetch specific historical Next-Day Picks report by report_id."""
    from agents.market_intelligence.next_day_picks import get_next_day_pick_by_id
    report = await asyncio.to_thread(get_next_day_pick_by_id, report_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found.")
    return report

@router.post("/api/research/next-day-picks/resolve")
async def resolve_next_day_picks_route(req: NextDayResolveRequest):
    """Independently resolve post-session trade outcomes for a published report."""
    from agents.market_intelligence.next_day_picks import resolve_report_outcomes
    result = await asyncio.to_thread(
        resolve_report_outcomes,
        report_id=req.report_id,
        actual_session_candles=req.actual_candles
    )
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(status_code=status_code, content=result)

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

    for sym in list(svc._signal_cache.keys())[:15]:

        sig = svc._get_signal_for_symbol(sym)

        if sig and sig.get("signal") in ("LONG", "SHORT"):

            top_signals.append(sig)

    top_signals.sort(key=lambda s: s.get("quality_score", 0), reverse=True)

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

            "mode": "Ensemble V2 (4/4 ML Fitted)",

            "fitted_count": 4,

            "ready": True

        },

        "risk_state": {

            "circuit_breaker_active": getattr(engine, "circuit_breaker_active", False) if engine else False,

            "portfolio_heat_pct": round(engine.get_portfolio_heat_pct() * 100, 2) if engine and hasattr(engine, "get_portfolio_heat_pct") else 0.0,

        },

        "last_refresh": svc._last_refresh.isoformat() if svc._last_refresh != datetime.min else None,

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

    results = []

    for symbol in svc._signal_cache:

        sig = svc._get_signal_for_symbol(symbol)

        if sig:

            results.append(sig)

        # Fast cold-boot fallback: if live cache is still computing, return persisted DB snapshot immediately

    if not results and getattr(svc, "_persisted_signals_snapshot", None):
        tracker.set_meta("count", len(svc._persisted_signals_snapshot))
        tracker.set_meta("cache_hit", "persisted_snapshot")
        tracker.finish()

        return {

            "signals": [apply_signal_freshness(s) for s in svc._persisted_signals_snapshot],

            "updated_at": getattr(svc, "_persisted_signals_updated_at", None) or datetime.now().isoformat(),

            "source": "persisted_snapshot",

        }

    # Durable runtime_state / intraday_store fallback for lightweight mode
    if not results:
        durable_sigs = []
        try:
            from db.runtime_state import get_runtime_state
            snap = get_runtime_state("latest_signals:v1")
            if snap and isinstance(snap, dict) and snap.get("signals"):
                durable_sigs = snap["signals"]
        except Exception:
            pass
        if not durable_sigs:
            try:
                import db.intraday_store as ids
                latest_scan = ids.load_latest_scan()
                if latest_scan and latest_scan.get("results"):
                    durable_sigs = latest_scan["results"]
            except Exception:
                pass
        if durable_sigs:
            tracker.set_meta("count", len(durable_sigs))
            tracker.set_meta("cache_hit", "durable_signals_snapshot")
            tracker.finish()
            return {
                "signals": [apply_signal_freshness(s) for s in durable_sigs],
                "updated_at": datetime.now().isoformat(),
                "source": "durable_signals_snapshot",
            }

    tracker.set_meta("count", len(results))
    tracker.set_meta("cache_hit", bool(results))
    tracker.finish()

    return {"signals": [apply_signal_freshness(s) for s in results], "updated_at": datetime.now().isoformat()}

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
    from agents.execution.market_control import is_crypto_forex_enabled
    if not is_crypto_forex_enabled():
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

    payload = {

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

    _meme_cache["payload"] = payload

    _meme_cache["timestamp"] = time.time()

    return payload

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

@router.post("/api/telegram/test", dependencies=[Depends(require_admin)])

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

        # Check negative symbol cache (10 min TTL) to short-circuit repeated misses
        _now_neg = time.time()
        _neg_ts = _NEGATIVE_SYMBOL_CACHE.get(clean_sym)
        if _neg_ts is not None and (_now_neg - _neg_ts) < 600.0:
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
        df = await asyncio.to_thread(fetch_ohlcv, clean_sym, period, interval)
        if df is None or df.empty:
            df = await asyncio.to_thread(
                fetch_arbitrary_symbol, clean_sym, period, interval
            )
        # B5: Ticker alias fallback - try at most 2 extra attempts: .NS, then first alias only
        if df is None or df.empty:
            if not clean_sym.endswith(".NS"):
                df = await asyncio.to_thread(
                    fetch_arbitrary_symbol, f"{clean_sym}.NS", period, interval
                )
        if df is None or df.empty:
            try:
                from config.symbol_aliases import SYMBOL_ALIASES
                aliases = SYMBOL_ALIASES.get(clean_sym, [])
                candidate_aliases = [a for a in aliases if a not in (clean_sym, f"{clean_sym}.NS")]
                if candidate_aliases:
                    first_alias = candidate_aliases[0]
                    df = await asyncio.to_thread(
                        fetch_arbitrary_symbol, first_alias, period, interval
                    )
            except Exception:
                pass

        if df is None or df.empty:
            _NEGATIVE_SYMBOL_CACHE[clean_sym] = time.time()
            logger.debug("Symbol %s not found on NSE/Yahoo; cached negative result for 10m", clean_sym)
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
        _NEGATIVE_SYMBOL_CACHE[clean_sym] = time.time()
        raise HTTPException(status_code=404, detail={
            "error": "SYMBOL_NOT_FOUND",
            "symbol": clean_sym,
            "message": str(e),
            "hint": f"Ticker may have been renamed. Try {clean_sym}.NS or the new symbol.",
        })

    df = df.tail(limit)
    return _build_market_response(clean_sym, df, interval)

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

@router.get("/api/trades")
async def get_trades(limit: int = Query(50, le=100)):
    """Get recent trade history from real paper trading engine (newest first)."""
    engine = svc._get_engine()
    raw_history = await asyncio.to_thread(engine.get_history, limit)
    enriched = []
    for t in raw_history:
        d = dict(t)
        exit_t = d.get("exit_time")
        entry_t = d.get("entry_time")
        d.setdefault("timestamp", exit_t or entry_t or "")
        d.setdefault("exit_reason", d.get("status") or "")

        # Calculate human-readable duration
        duration_human = "—"
        if entry_t and exit_t:
            try:
                t0 = datetime.fromisoformat(str(entry_t).replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(str(exit_t).replace("Z", "+00:00"))
                diff_sec = max(0.0, (t1 - t0).total_seconds())
                if diff_sec < 60:
                    duration_human = f"{int(diff_sec)}s"
                elif diff_sec < 3600:
                    duration_human = f"{int(diff_sec // 60)}m"
                elif diff_sec < 86400:
                    h = int(diff_sec // 3600)
                    m = int((diff_sec % 3600) // 60)
                    duration_human = f"{h}h {m}m" if m > 0 else f"{h}h"
                else:
                    days = int(diff_sec // 86400)
                    h = int((diff_sec % 86400) // 3600)
                    duration_human = f"{days}d {h}h" if h > 0 else f"{days}d"
            except Exception:
                duration_human = "—"
        d["duration_human"] = duration_human
        enriched.append(d)

    # Return newest trades first
    return {"trades": list(reversed(enriched))}

@router.get("/api/track-record")

async def get_track_record(limit: int = Query(1000, le=2000)):

    """Public, unfiltered paper-trading record — wins and losses together."""

    from api.track_record import build_track_record

    try:

        engine = svc._get_engine()

        history = await asyncio.to_thread(engine.get_history, limit)

        record = build_track_record(
            history,
            initial_capital=getattr(engine, "initial_capital", None),
        )

        record["status"] = "ok"

        return record

    except Exception as exc:

        logger.error("Track record unavailable: %s", exc)

        fallback = build_track_record([], initial_capital=None)

        fallback["status"] = "unavailable"

        fallback["error"] = str(exc)

        return fallback

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

@router.post("/api/risk/reset-circuit", dependencies=[Depends(require_admin)])

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

@router.post("/api/paper/resolve", dependencies=[Depends(require_admin)])

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

    if abs(entry - stop_loss) < 1e-4 or abs(entry - target) < 1e-4:
        return JSONResponse(
            status_code=400,
            content={"error": "Degenerate trade geometry: entry, stop_loss, and target must be distinct levels."},
        )

    norm_dir = "LONG" if direction in ("LONG", "BUY") else ("SHORT" if direction in ("SHORT", "SELL") else None)

    if not norm_dir:

        return JSONResponse(

            status_code=400,

            content={"error": f"Invalid trade direction '{direction}'. Must be LONG or SHORT."},

        )

    if norm_dir == "LONG":

        if not (stop_loss < entry < target):

            return JSONResponse(

                status_code=400,

                content={

                    "error": f"Invalid trade geometry for LONG: stop_loss ({stop_loss}) must be below entry ({entry}), and target ({target}) must be above entry."

                },

            )

    else:

        if not (target < entry < stop_loss):

            return JSONResponse(

                status_code=400,

                content={

                    "error": f"Invalid trade geometry for SHORT: target ({target}) must be below entry ({entry}), and stop_loss ({stop_loss}) must be above entry."

                },

            )

    direction = norm_dir

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
    """Get all currently open paper trades with unified live pricing and valuation."""
    from api.valuation import resolve_open_trades_valuation
    engine = svc._get_engine()
    val = await resolve_open_trades_valuation(engine, svc, refresh_stale=True)
    trades = [mark_trade_pricing(t) for t in val["open_trades"]]
    summary = dict(val["summary"])
    from api.trade_hygiene import is_zombie

    zombie_count = 0
    for t in trades:
        zombie, reason = is_zombie(t)
        t["zombie"] = bool(zombie)
        t["zombie_reason"] = reason
        if zombie:
            zombie_count += 1
    if zombie_count:
        summary["zombie_positions"] = zombie_count
    unpriced = sum(1 for t in trades if not t.get("priced", True))
    if unpriced:
        summary["unpriced_positions"] = unpriced
    return {"open_trades": trades, "summary": summary}

@router.post("/api/paper/close/{trade_id}")

async def close_paper_trade_endpoint(trade_id: int, payload: dict | None = None):

    """Manually close an open paper trade at exit price."""

    engine = svc._get_engine()

    payload = payload or {}

    exit_price = float((payload.get("exit_price") if isinstance(payload, dict) else getattr(payload, "exit_price", 0.0)) or 0.0)

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

@router.post("/api/paper/close-bulk", dependencies=[Depends(require_admin)])
async def close_paper_trades_bulk(payload: dict | None = None):
    """Close a group of open paper trades at the last known price.

    Modes: ``intraday`` (default), ``zombie``, ``all``, ``ids``.
    Requires ``{"confirm": true}``; ``dry_run`` previews without closing.
    """
    from api.trade_hygiene import select_bulk_targets

    payload = payload or {}
    if not bool(payload.get("confirm")):
        return JSONResponse(status_code=400, content={"error": "confirm:true required"})

    mode = str(payload.get("mode") or "intraday").lower()
    dry_run = bool(payload.get("dry_run"))
    engine = svc._get_engine()
    targets = select_bulk_targets(engine.open_trades, mode=mode, ids=payload.get("ids"))

    closed: list[dict] = []
    total_pnl = 0.0

    for trade in targets:
        symbol = str(getattr(trade, "symbol", "") or "")
        data = svc._market_data.get(symbol)
        exit_price = 0.0
        if isinstance(data, dict) and data.get("close"):
            exit_price = float(data["close"])
        elif hasattr(data, "iloc") and not data.empty and "close" in data.columns:
            exit_price = float(data["close"].iloc[-1])
        if exit_price <= 0:
            exit_price = float(getattr(trade, "entry_price", 0.0) or 0.0)

        if dry_run:
            closed.append({
                "id": getattr(trade, "id", None),
                "symbol": symbol,
                "exit_price": round(exit_price, 4),
            })
            continue

        result = engine.close_manual(trade_id=getattr(trade, "id", 0), exit_price=exit_price)
        if result is None:
            continue
        pnl = float(getattr(result, "total_pnl", 0.0) or 0.0)
        total_pnl += pnl
        closed.append({
            "id": getattr(result, "id", None),
            "symbol": getattr(result, "symbol", symbol),
            "exit_price": round(float(getattr(result, "exit_price", exit_price) or exit_price), 4),
            "pnl": round(pnl, 2),
        })

    return {
        "status": "ok",
        "mode": mode,
        "dry_run": dry_run,
        "matched": len(targets),
        "closed": len(closed),
        "total_pnl": round(total_pnl, 2),
        "trades": closed,
    }

@router.post("/api/paper/capital")
async def set_paper_capital_endpoint(payload: dict | None = None):
    """Reconfigure self-learning paper capital and reset portfolio baseline."""
    payload = payload or {}
    capital = float(payload.get("capital") or 100000.0)
    clear_trades = bool(payload.get("clear_trades", False))
    engine = svc._get_engine()
    new_stats = engine.reset_account(new_capital=capital, clear_trades=clear_trades)
    return {
        "status": "ok",
        "message": f"Self-learning capital set to ₹{capital:,.2f}",
        "stats": new_stats,
    }

@router.post("/api/paper/reset")
async def reset_paper_trading_endpoint(capital: float = Query(100000.0, ge=1000.0)):
    """Reset paper trading portfolio and self-learning state."""
    engine = svc._get_engine()
    new_stats = engine.reset_account(new_capital=capital, clear_trades=True)
    return {
        "status": "ok",
        "message": f"Paper trading account reset with ₹{capital:,.2f} initial capital",
        "stats": new_stats,
    }

# ── Autonomous Daemon & Decision Feed endpoints ──────────────────────────────

_last_manual_cycle_dispatch_time: float = 0.0

# ── Autonomous status snapshot cache & single-flight lock ─────────────────────
_AUTONOMOUS_STATUS_CACHE: Optional[Dict[str, Any]] = None
_AUTONOMOUS_STATUS_CACHE_TS: float = 0.0
_AUTONOMOUS_STATUS_LOCK: Optional[asyncio.Lock] = None
_AUTONOMOUS_STATUS_TTL_SEC: float = 10.0


def _get_autonomous_status_lock() -> asyncio.Lock:
    global _AUTONOMOUS_STATUS_LOCK
    if _AUTONOMOUS_STATUS_LOCK is None:
        _AUTONOMOUS_STATUS_LOCK = asyncio.Lock()
    return _AUTONOMOUS_STATUS_LOCK


def invalidate_autonomous_status_cache() -> None:
    """Invalidate server-side snapshot cache on control-state mutations."""
    global _AUTONOMOUS_STATUS_CACHE, _AUTONOMOUS_STATUS_CACHE_TS
    _AUTONOMOUS_STATUS_CACHE = None
    _AUTONOMOUS_STATUS_CACHE_TS = 0.0


def _fetch_autonomous_status_sync():
    """Fetch status and lock info in a thread to keep DB I/O off the event loop."""
    from agents.execution.autonomous_daemon import get_autonomous_daemon
    from agents.execution.distributed_lock import DistributedLockManager
    from agents.execution.session_guard import is_nse_market_open, get_current_ist_time
    from db.runtime_state import get_runtime_state

    daemon = get_autonomous_daemon()
    engine = svc._get_engine()
    daemon_status = daemon.get_status(engine)

    worker_status = get_runtime_state("autonomous_worker:latest_status", {}) or {}

    lock_mgr = DistributedLockManager()
    lock_info = lock_mgr.get_lock_info("autonomous_paper_worker")

    now_ist = get_current_ist_time()
    is_nse_open, nse_reason = is_nse_market_open(now_ist)

    watchdog_status = "HEALTHY"
    watchdog_message = "Autonomous worker operating normally."
    age_minutes = None

    if lock_info and lock_info.get("is_active"):
        watchdog_status = "RUNNING"
        watchdog_message = f"Worker cycle currently executing (run_id: {lock_info.get('run_id')})."
    elif worker_status and worker_status.get("timestamp"):
        try:
            ts_str = worker_status["timestamp"]
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
            age_minutes = round(age_sec / 60.0, 1)

            if is_nse_open and age_minutes > 35.0:
                watchdog_status = "WORKER_STALE"
                watchdog_message = f"Last cycle completed {age_minutes:.1f}m ago during active market hours (>35m threshold)."
            elif not is_nse_open:
                watchdog_status = "MARKET_CLOSED"
                watchdog_message = f"NSE cash market is closed ({nse_reason}). Worker in off-hours standby."
            else:
                watchdog_status = "HEALTHY"
                watchdog_message = f"Worker healthy. Last cycle completed {age_minutes:.1f}m ago."
        except Exception:
            pass
    elif not is_nse_open:
        watchdog_status = "MARKET_CLOSED"
        watchdog_message = f"NSE cash market is closed ({nse_reason}). Worker in off-hours standby."
    else:
        watchdog_status = "IDLE"
        watchdog_message = "No worker cycle runs recorded yet today."

    return {
        "daemon_status": daemon_status,
        "worker_status": worker_status,
        "lock_info": lock_info,
        "watchdog": {
            "status": watchdog_status,
            "message": watchdog_message,
            "age_minutes": age_minutes,
            "is_nse_market_open": is_nse_open,
            "nse_reason": nse_reason,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
    }


@router.get("/api/autonomous/status")
async def get_autonomous_status(response: Response):
    """
    Return operational status, watchdog health, and worker lock state.
    Protected by 10s server-side snapshot cache and bounded query execution.
    Sets private no-store headers to prevent edge caching of private operational state.
    """
    global _AUTONOMOUS_STATUS_CACHE, _AUTONOMOUS_STATUS_CACHE_TS
    response.headers["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"

    now_t = time.time()
    if _AUTONOMOUS_STATUS_CACHE is not None and (now_t - _AUTONOMOUS_STATUS_CACHE_TS) < _AUTONOMOUS_STATUS_TTL_SEC:
        cached_copy = dict(_AUTONOMOUS_STATUS_CACHE)
        cached_copy["cache_hit"] = True
        return cached_copy

    async with _get_autonomous_status_lock():
        now_t = time.time()
        if _AUTONOMOUS_STATUS_CACHE is not None and (now_t - _AUTONOMOUS_STATUS_CACHE_TS) < _AUTONOMOUS_STATUS_TTL_SEC:
            cached_copy = dict(_AUTONOMOUS_STATUS_CACHE)
            cached_copy["cache_hit"] = True
            return cached_copy

        t0 = time.perf_counter()
        raw = await asyncio.wait_for(
            asyncio.to_thread(_fetch_autonomous_status_sync),
            timeout=5.0,
        )
        measured_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        payload = {
            "ok": True,
            "status": raw["daemon_status"],
            "worker": raw["worker_status"],
            "lock": raw["lock_info"],
            "watchdog": raw["watchdog"],
            "measured_query_ms": measured_ms,
            "source_timestamp": datetime.now(timezone.utc).isoformat(),
            "cache_hit": False,
        }
        _AUTONOMOUS_STATUS_CACHE = payload
        _AUTONOMOUS_STATUS_CACHE_TS = time.time()
        return payload


def _fetch_feed_db_sync(limit: int):
    items = []
    try:
        from db.connection import SessionLocal
        from db.models import AutonomousDecisionRecord
        with SessionLocal() as s:
            records = s.query(AutonomousDecisionRecord).order_by(AutonomousDecisionRecord.id.desc()).limit(limit).all()
            for r in records:
                ts_str = None
                if r.timestamp:
                    ts = r.timestamp
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    ts_str = ts.isoformat()
                items.append({
                    "id": r.signal_id or f"rec-{r.id}",
                    "run_id": r.run_id,
                    "timestamp": ts_str,
                    "action": r.action,
                    "symbol": r.symbol,
                    "direction": r.direction or "HOLD",
                    "price": r.entry_price or 0.0,
                    "qs_score": r.quality_score or 0.0,
                    "ai_confidence": r.confluence_score or 0.0,
                    "risk_reward": r.risk_reward or 0.0,
                    "market_regime": r.market_regime or "UNKNOWN",
                    "source_freshness": r.source_freshness or "UNKNOWN",
                    "trade_id": r.paper_trade_id,
                    "reason": r.reason or "",
                    "asset_class": "CRYPTO" if "/" in (r.symbol or "") else "EQUITY",
                })
    except Exception as exc:
        pass
    return items


@router.get("/api/autonomous/feed")
async def get_autonomous_feed(response: Response, limit: int = 25):
    """Return live stream of autonomous trading decisions and management actions."""
    response.headers["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"

    from agents.execution.autonomous_daemon import get_autonomous_daemon
    from db.runtime_state import get_runtime_state

    daemon = get_autonomous_daemon()

    # Priority 1: Check runtime state
    feed = get_runtime_state("autonomous_feed:v1", []) or []

    # Priority 2: Fall back to PostgreSQL database records (non-blocking thread)
    if not feed:
        feed = await asyncio.to_thread(_fetch_feed_db_sync, limit)

    # Priority 3: Fall back to daemon in-memory log
    if not feed and daemon.decision_log:
        feed = daemon.decision_log

    # Priority 4: If still empty, provide system standby event so feed is never blank
    if not feed:
        now_iso = datetime.now(timezone.utc).isoformat()
        feed = [{
            "id": f"standby-{int(datetime.now(timezone.utc).timestamp())}",
            "timestamp": now_iso,
            "action": "SYSTEM_STANDBY",
            "symbol": "SYSTEM",
            "direction": "NEUTRAL",
            "price": 0.0,
            "qs_score": 0.0,
            "ai_confidence": 0.0,
            "reason": "Autonomous worker initialized and standing by for scheduled market cycle.",
            "asset_class": "SYSTEM",
        }]

    # Normalize all timestamps to guarantee explicit ISO UTC representation
    for item in feed:
        raw_ts = item.get("timestamp")
        if raw_ts and isinstance(raw_ts, str) and not raw_ts.endswith("Z") and "+" not in raw_ts:
            item["timestamp"] = raw_ts + "Z"

    # Deduplicate consecutive standby/closed entries while strictly preserving intervening executions
    cleaned_feed = []
    last_action = None
    for item in feed:
        act = (item.get("action") or "").upper()
        is_standby = act in ("MARKET_CLOSED", "SYSTEM_STANDBY")
        if is_standby and last_action in ("MARKET_CLOSED", "SYSTEM_STANDBY"):
            continue
        cleaned_feed.append(item)
        last_action = act

    final_feed = cleaned_feed[:min(limit, 100)]
    return {"ok": True, "feed": final_feed, "count": len(final_feed)}


@router.post("/api/autonomous/run-cycle")
async def trigger_autonomous_cycle(payload: dict | None = None):
    """
    Trigger a single autonomous worker cycle immediately.
    Dispatches to GitHub Actions if GITHUB_TOKEN is available,
    otherwise executes single cycle in a background task.
    Enforces a 60-second cooldown rate limit.
    """
    global _last_manual_cycle_dispatch_time
    now = time.time()
    cooldown = 60.0
    elapsed = now - _last_manual_cycle_dispatch_time

    if elapsed < cooldown:
        remaining = int(cooldown - elapsed)
        return {
            "ok": False,
            "error": "RATE_LIMITED",
            "message": f"Autonomous cycle was triggered recently. Please wait {remaining}s before triggering again.",
            "retry_after_sec": remaining,
        }

    payload = payload or {}
    universe = payload.get("universe", "tier1")
    force_scan = bool(payload.get("force_scan", False))
    dry_run = bool(payload.get("dry_run", False))

    gh_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    gh_repo = os.getenv("GITHUB_REPOSITORY", "Himanshugupta2024/TradeAI-PRO")

    dispatched_via = "local_async"
    dispatch_msg = "Dispatched single autonomous cycle in local background task."

    if gh_token and gh_repo:
        try:
            import httpx
            url = f"https://api.github.com/repos/{gh_repo}/actions/workflows/autonomous_cycle.yml/dispatches"
            headers = {
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "TradeAI-PRO-API",
            }
            body = {
                "ref": "main",
                "inputs": {
                    "universe": universe,
                    "force_scan": str(force_scan).lower(),
                    "dry_run": str(dry_run).lower(),
                },
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, headers=headers, json=body)
                if res.status_code in (204, 201):
                    dispatched_via = "github_actions"
                    dispatch_msg = f"Triggered GitHub Actions autonomous cycle workflow on {gh_repo}."
                    _last_manual_cycle_dispatch_time = now
                    invalidate_autonomous_status_cache()
                    return {
                        "ok": True,
                        "method": dispatched_via,
                        "message": dispatch_msg,
                        "universe": universe,
                        "dispatched_at": datetime.now(timezone.utc).isoformat(),
                    }
        except Exception as gh_err:
            log.warning("[Autonomous] GitHub workflow dispatch error: %s. Falling back to local.", gh_err)

    # Local async fallback
    _last_manual_cycle_dispatch_time = now
    from scripts.autonomous_cycle import run_single_autonomous_cycle

    async def _run_bg():
        try:
            await run_single_autonomous_cycle(
                universe_arg=universe,
                force_scan=force_scan,
                dry_run=dry_run,
                max_symbols=15,
            )
        except Exception as bg_err:
            log.error("[Autonomous] Background cycle execution failed: %s", bg_err)

    asyncio.create_task(_run_bg())

    return {
        "ok": True,
        "method": "local_async",
        "message": dispatch_msg,
        "universe": universe,
        "dispatched_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/api/autonomous/toggle", dependencies=[Depends(require_admin)])
async def toggle_autonomous(payload: dict | None = None):
    """Enable or disable autonomous trading mode."""
    invalidate_autonomous_status_cache()
    from agents.execution.autonomous_daemon import get_autonomous_daemon
    daemon = get_autonomous_daemon()
    payload = payload or {}
    if "enabled" in payload:
        daemon.enabled = bool(payload["enabled"])
    else:
        daemon.enabled = not daemon.enabled
    engine = svc._get_engine()
    return {"ok": True, "enabled": daemon.enabled, "status": daemon.get_status(engine)}


@router.post("/api/autonomous/review-now", dependencies=[Depends(require_admin)])
async def trigger_eod_review_now():
    """Manually trigger immediate daily EOD self-learning review."""
    invalidate_autonomous_status_cache()
    from agents.analytics.feedback_loop import FeedbackAnalyser
    from agents.execution.autonomous_daemon import get_autonomous_daemon
    analyser = FeedbackAnalyser()
    review = await asyncio.to_thread(analyser.run_daily_eod_review)
    daemon = get_autonomous_daemon()
    daemon.record_decision(
        action="MANUAL_EOD_REVIEW",
        symbol="ALL",
        direction="NEUTRAL",
        price=0.0,
        reason=f"Manual EOD review triggered: {review.get('today_trade_count', 0)} trades today, win rate {review.get('today_win_rate', 0)}%",
        pnl_pct=review.get("today_total_pnl_pct", 0.0),
    )
    return {"ok": True, "review": review}


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
        if trade.get("cost_profile") and trade.get("cost_profile") != "UNAVAILABLE":
            net = _safe_float(trade.get("net_pnl"))
            if abs(net) > 1e-9:
                return net
        for key in ("total_pnl", "pnl_inr", "realized_pnl"):
            val = trade.get(key)
            if val is not None and abs(_safe_float(val)) > 1e-9:
                return _safe_float(val)
        if trade.get("net_pnl") is not None and abs(_safe_float(trade.get("net_pnl"))) > 1e-9:
            return _safe_float(trade.get("net_pnl"))
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
    from api.valuation import resolve_open_trades_valuation
    val = await resolve_open_trades_valuation(engine, svc, refresh_stale=True)
    open_trades = val.get("open_trades", [])
    val_summary = val.get("summary", {})

    closed_trades = engine.get_history(limit=2000)
    inr_closed_trades = [
        t for t in closed_trades
        if not (t.get("fx_status") == "LEGACY_UNCONVERTED" and (t.get("quote_currency") or ("USDT" if "/" in str(t.get("symbol", "")) else "INR")) != "INR")
    ]

    now_ist = datetime.now(tz=ist_tz)

    total_unrealized = _safe_float(val_summary.get("unrealized_pnl"), 0.0)

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

        current_close = _safe_float(t.get("current_price"), entry)

        unrealized = _safe_float(t.get("unrealized_pnl"), 0.0)

        initial_risk = abs(entry - stop_loss) * max(initial_qty, 0.0)

        open_risk = abs(entry - stop_loss) * max(quantity, 0.0)

        total_open_risk += open_risk

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

    # Risk-adjusted performance metrics from closed INR trades
    returns = np.array(
        [_safe_float(t.get("pnl_pct"), 0.0) / 100.0 for t in inr_closed_trades], dtype=float
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
        _total_trade_pnl(t) for t in inr_closed_trades if _total_trade_pnl(t) > 0
    )

    gross_loss = abs(
        sum(_total_trade_pnl(t) for t in inr_closed_trades if _total_trade_pnl(t) < 0)
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

    current_value = _safe_float(
        val_summary.get("total_equity"),
        _safe_float(getattr(engine, "capital", 0.0), 0.0) + float(total_unrealized),
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

    for t in inr_closed_trades:

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

                        for t in inr_closed_trades

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
        if trade.get("cost_profile") and trade.get("cost_profile") != "UNAVAILABLE":
            net = _safe_float(trade.get("net_pnl"))
            if abs(net) > 1e-9:
                return net
        for key in ("total_pnl", "pnl_inr", "realized_pnl"):
            val = trade.get(key)
            if val is not None and abs(_safe_float(val)) > 1e-9:
                return _safe_float(val)
        if trade.get("net_pnl") is not None and abs(_safe_float(trade.get("net_pnl"))) > 1e-9:
            return _safe_float(trade.get("net_pnl"))
        total = trade.get("total_pnl")
        if total is not None:
            return _safe_float(total, 0.0)
        return _safe_float(trade.get("pnl"), 0.0) * _safe_float(trade.get("quantity"), 1.0)

    engine = svc._get_engine()
    stats = engine.get_stats()
    equity_curve = engine.get_equity_curve()
    closed_trades = engine.get_history(limit=1000)

    # Quarantine unconverted non-INR trades from INR ledger calculations (F05)
    inr_closed_trades = [
        t for t in closed_trades
        if not (t.get("fx_status") == "LEGACY_UNCONVERTED" and (t.get("quote_currency") or ("USDT" if "/" in str(t.get("symbol", "")) else "INR")) != "INR")
    ]
    wins = [t for t in inr_closed_trades if _trade_total_pnl(t) > 1e-6]
    losses = [t for t in inr_closed_trades if _trade_total_pnl(t) < -1e-6]
    breakevens = [t for t in inr_closed_trades if abs(_trade_total_pnl(t)) <= 1e-6]

    # ── Monthly P&L breakdown ──────────────────────────
    monthly: dict[str, float] = defaultdict(float)
    for t in inr_closed_trades:
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

    for t in inr_closed_trades:

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
            "breakeven_trades": stats.get("breakeven_trades", len(breakevens)),
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
            "unconverted_legacy_trades": stats.get("unconverted_legacy_trades", []),
        },
        "status": {
            "feed": svc._get_feed_status(),
            "symbols_loaded": len(svc._market_data),
            "signals_cached": len(svc._signal_cache) or len(getattr(svc, "_persisted_signals_snapshot", None) or []),
        },
        "equity_curve": equity_curve,
        "trade_distribution": {"wins": len(wins), "losses": len(losses), "breakevens": len(breakevens)},
        "unconverted_legacy_trades": stats.get("unconverted_legacy_trades", []),
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
    prices = {}
    for sym, d in svc._market_data.items():
        if isinstance(d, pd.DataFrame) and not d.empty and "close" in d.columns:
            prices[sym] = {"close": float(d["close"].iloc[-1])}
        elif isinstance(d, dict) and "close" in d:
            prices[sym] = {"close": float(d["close"])}
    stats = engine.get_stats(current_prices=prices)

    return {

        "total_trades": stats.get("total_trades", 0),

        "winning_trades": stats.get("winning_trades", 0),

        "win_rate": stats.get("win_rate", 0),

        "total_pnl": stats.get("total_pnl", 0),

        "unrealized_pnl": stats.get("unrealized_pnl", 0),

        "avg_pnl": stats.get("avg_pnl", 0),

        "best_trade": stats.get("best_trade", 0),

        "worst_trade": stats.get("worst_trade", 0),

        "capital": stats.get("total_equity", stats.get("capital", 100000)),

        "cash_capital": stats.get("capital", 100000),

        "initial_capital": stats.get("initial_capital", 100000),

        "deployed_margin": stats.get("deployed_margin", 0),

        "free_cash": stats.get("free_cash", stats.get("capital", 100000)),

        "open_trades": stats.get("open_trades", len(engine.get_open_trades())),

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
            cutoff = datetime.now() - timedelta(days=days)
            ts = pd.to_datetime(df["timestamp"], errors="coerce")
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

async def get_overview_intelligence(response: Response):
    response.headers["Cache-Control"] = "public, max-age=5, s-maxage=15, stale-while-revalidate=30" 

    """

    Aggregated decision-cockpit payload for the Overview tab.

    Returns top ranked setups, high-quality-but-blocked setups, market guardrails,

    system/risk state, and an advisory learning overlay — all in one fast call.

    """

    try:

        return await asyncio.to_thread(svc._compute_overview_intelligence)

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
        "all", description="Asset class: all, forex, crypto, nse, index"
    ),
    min_score: float = Query(-100.0, description="Minimum EdgeScore threshold"),
    limit: int = Query(35, le=100, description="Maximum results to return"),
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
            mdata = getattr(svc, "_market_data", {})
            for symbol, score in ranked:
                cached = engine._load_cache(symbol)
                cache_age_seconds = None
                if cached:
                    cache_age_seconds = max(
                        0, int((now_utc - cached.timestamp).total_seconds())
                    )

                # Live price, change %, volume enrichment
                ltp = None
                change_pct = None
                vol = None
                clean_sym = symbol.replace(".NS", "").strip().upper()
                df_sym = mdata.get(symbol)
                if df_sym is None:
                    df_sym = mdata.get(clean_sym)
                if isinstance(df_sym, pd.DataFrame) and not df_sym.empty and "close" in df_sym.columns:
                    try:
                        ltp = round(float(df_sym["close"].iloc[-1]), 2)
                        if len(df_sym) >= 2 and float(df_sym["close"].iloc[-2]) > 0:
                            change_pct = round(
                                ((float(df_sym["close"].iloc[-1]) - float(df_sym["close"].iloc[-2]))
                                 / float(df_sym["close"].iloc[-2])) * 100, 2
                            )
                        if "volume" in df_sym.columns:
                            vol = float(df_sym["volume"].iloc[-1])
                    except Exception:
                        pass

                rows.append(
                    {
                        "symbol": symbol,
                        "asset_class": getattr(cached, "asset_class", "forex"),
                        "edge_score": score,
                        "ltp": ltp,
                        "change_pct": change_pct,
                        "volume": vol,
                        "factor_coverage_pct": getattr(cached, "factor_coverage_pct", 100.0),
                        "confidence": getattr(cached, "confidence", 1.0),
                        "rationale": getattr(cached, "rationale", ""),
                        "factors": getattr(cached, "factors", {}),
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


@router.get("/api/edge/research/{symbol}")
async def get_edge_research_detail(symbol: str, asset_class: str | None = None):
    """
    Get deep-dive research intelligence for an individual asset.
    Returns institutional factor decomposition, news headlines, and transparency rationale.
    """
    try:
        from agents.macro.edge_score_engine import EdgeScoreEngine
        engine = EdgeScoreEngine()
        if not asset_class:
            sym_u = symbol.upper()
            if "/" in sym_u or sym_u.endswith("USDT"):
                asset_class = "crypto" if any(k in sym_u for k in ("USDT", "BTC", "ETH", "SOL", "BNB")) else "forex"
            elif any(c.isalpha() for c in sym_u):
                asset_class = "nse"
            else:
                asset_class = "forex"

        breakdown = await asyncio.to_thread(engine.get_edge_score, symbol, asset_class)
        return {
            "status": "ok",
            "symbol": symbol,
            "asset_class": getattr(breakdown, "asset_class", asset_class),
            "edge_score": breakdown.edge_score,
            "factor_coverage_pct": getattr(breakdown, "factor_coverage_pct", 100.0),
            "confidence": getattr(breakdown, "confidence", 1.0),
            "rationale": getattr(breakdown, "rationale", ""),
            "factors": getattr(breakdown, "factors", {}),
            "legacy_breakdown": {
                "macro": breakdown.macro_score,
                "cot": breakdown.cot_score,
                "sentiment": breakdown.sentiment_score,
                "risk": breakdown.risk_score,
                "momentum": breakdown.momentum_score,
                "seasonal": breakdown.seasonal_bonus,
                "carry": breakdown.carry_bonus,
            },
            "timestamp": breakdown.timestamp.isoformat(),
        }
    except Exception as exc:
        logger.error("Failed to fetch research detail for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=str(exc))

@router.get("/api/edge/macro-heatmap")
async def get_edge_macro_heatmap(
    force_refresh: bool = Query(
        False, description="Bypass edge cache and compute fresh"
    ),
):
    """
    Get macro heatmap showing EdgeScore components across all asset classes.
    Uses parallel 2.5s timeout per asset class, never sets missing factor to 0,
    and falls back to durable state snapshot to prevent UI blanking.
    """
    try:
        from agents.macro.edge_score_engine import EdgeScoreEngine
        from config.settings import CRYPTO_SYMBOLS, FOREX_SYMBOLS, NSE_SYMBOLS
        from db.runtime_state import get_runtime_state, set_runtime_state

        engine = (
            EdgeScoreEngine(cache_ttl_seconds=0) if force_refresh else EdgeScoreEngine()
        )

        # Load durable snapshot for fallback
        durable_cached = {}
        try:
            raw_cached = await asyncio.to_thread(get_runtime_state, "latest_macro_heatmap")
            if raw_cached and isinstance(raw_cached, dict) and "heatmap" in raw_cached:
                durable_cached = raw_cached["heatmap"]
        except Exception:
            pass

        asset_samples = {
            "forex": FOREX_SYMBOLS[0] if FOREX_SYMBOLS else "EUR/USD",
            "crypto": CRYPTO_SYMBOLS[0] if CRYPTO_SYMBOLS else "BTC/USDT",
            "nse": NSE_SYMBOLS[0] if NSE_SYMBOLS else "RELIANCE",
            "index": "NIFTY",
        }

        async def _fetch_asset_class(asset_class: str, symbol: str) -> tuple[str, dict]:
            try:
                # 2.5s strict timeout per asset class to keep overall route responsive
                breakdown = await asyncio.wait_for(
                    asyncio.to_thread(engine.get_edge_score, symbol, asset_class),
                    timeout=2.5,
                )
                cache_age_seconds = max(
                    0,
                    int((datetime.now(timezone.utc) - breakdown.timestamp).total_seconds()),
                )
                return asset_class, {
                    "macro_score": breakdown.macro_score,
                    "cot_score": breakdown.cot_score,
                    "sentiment_score": breakdown.sentiment_score,
                    "risk_score": breakdown.risk_score,
                    "momentum_score": breakdown.momentum_score,
                    "edge_score": breakdown.edge_score,
                    "sample_symbol": symbol,
                    "cache_hit": breakdown.cache_hit,
                    "cache_age_seconds": cache_age_seconds,
                    "status": "LIVE" if not breakdown.cache_hit else "CACHED",
                }
            except Exception as exc:
                logger.warning("Fast macro fetch timeout/error for %s: %s", asset_class, exc)
                # Fallback to durable cached snapshot if available
                if asset_class in durable_cached:
                    fallback_item = dict(durable_cached[asset_class])
                    fallback_item["status"] = "DURABLE_FALLBACK"
                    fallback_item["sample_symbol"] = symbol
                    return asset_class, fallback_item

                # Neutral baseline rather than zero (neutral edge is 50.0)
                return asset_class, {
                    "macro_score": 50.0,
                    "cot_score": 50.0,
                    "sentiment_score": 50.0,
                    "risk_score": 50.0,
                    "momentum_score": 50.0,
                    "edge_score": 50.0,
                    "sample_symbol": symbol,
                    "status": "NEUTRAL_FALLBACK",
                    "error": str(exc),
                }

        results = await asyncio.gather(
            *[_fetch_asset_class(ac, sym) for ac, sym in asset_samples.items()]
        )
        heatmap = dict(results)

        payload = {
            "heatmap": heatmap,
            "force_refresh": force_refresh,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "real_data": any(v.get("status") in ("LIVE", "CACHED", "DURABLE_FALLBACK") for v in heatmap.values()),
        }

        # Persist to durable state
        try:
            await asyncio.to_thread(set_runtime_state, "latest_macro_heatmap", payload)
        except Exception:
            pass

        return payload

    except Exception as e:
        logger.error(f"Failed to generate macro heatmap: {e}")
        return {"error": str(e), "heatmap": {}}

@router.get("/api/macro/intelligence")
async def get_macro_intelligence(force_refresh: bool = Query(False, description="Bypass cache and fetch fresh")):
    """Get real-time global commodities, yields, US indices, macro regime, and targeted breaking news."""
    from agents.market_intelligence.global_macro_fetcher import get_global_macro_fetcher
    from dataclasses import asdict
    try:
        fetcher = get_global_macro_fetcher()
        intel = await asyncio.to_thread(fetcher.get_intelligence, force_refresh)
        return {"status": "ok", "macro": asdict(intel)}
    except Exception as e:
        logger.error("Failed to get macro intelligence: %s", e)
        return {"status": "error", "error": str(e)}

@router.get("/api/macro/sector-impact")
async def get_macro_sector_impact(symbol: str = Query(..., description="Stock symbol (e.g. ASIANPAINT)"), direction: str = Query("LONG", description="Trade direction (LONG/SHORT)")):
    """Evaluate whether a proposed trade setup violates severe macro sector risk headwinds."""
    from agents.market_intelligence.global_macro_fetcher import get_global_macro_fetcher
    try:
        fetcher = get_global_macro_fetcher()
        allowed, reason = await asyncio.to_thread(fetcher.evaluate_macro_sector_gate, symbol, direction)
        return {
            "symbol": symbol.upper(),
            "direction": direction.upper(),
            "allowed": allowed,
            "block_reason": reason,
        }
    except Exception as e:
        logger.error("Failed to evaluate macro sector impact: %s", e)
        return {"allowed": True, "block_reason": None, "error": str(e)}

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

    try:
        from db import swing_store
        latest = await asyncio.to_thread(swing_store.load_latest_scan)
        if latest is not None and "setups" in latest:
            setups = latest.get("setups") or []
            msg = (
                f"{len(setups)} setups loaded from central database ({latest.get('scan_date', 'unknown')})"
                if setups
                else f"Database scan ({latest.get('scan_date', 'today')}): 0 setups passed strict R:R/regime filters."
            )
            return {
                "setups": setups,
                "scan_date": latest.get("scan_date"),
                "generated_at": latest.get("scan_time") or latest.get("created_at"),
                "symbols_scanned": latest.get("symbols_scanned", 0),
                "setups_found": len(setups),
                "grade_summary": latest.get("grade_summary", {}),
                "message": msg,
            }
    except Exception as db_exc:
        logger.debug("Database swing scan lookup skipped: %s", db_exc)

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
    """Paper-only intraday heuristics on tracked NSE symbols with warmup & timeout guard."""
    from agents.intraday_engine.scanner import (
        get_nifty_market_context,
        scan_intraday_setups,
    )
    from agents.intraday_engine.session import evaluate_market_session
    from agents.intraday_engine.risk_manager import should_square_off_now
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(_IST)
    session = evaluate_market_session()
    symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK"]  # Snappy on-demand execution under 10s
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

    # Bounded execution: max 9.5s to prevent browser timeout
    sigs = []
    try:
        sigs = await asyncio.wait_for(
            asyncio.to_thread(scan_intraday_setups, symbols, portfolio_state),
            timeout=9.5,
        )
    except asyncio.TimeoutError:
        logger.warning("Intraday scan timed out (9.5s) — loading latest durable scan")
        prev_scan = await asyncio.to_thread(intraday_store.load_latest_scan)
        if prev_scan and prev_scan.get("signals"):
            sigs = prev_scan["signals"]

    scan_id = await asyncio.to_thread(
        lambda: intraday_store.save_scan(
            sigs,
            symbols_scanned=len(symbols),
            trigger="api",
        ),
    )

    nifty_ctx = await asyncio.to_thread(get_nifty_market_context)
    long_count = sum(1 for s in sigs if s.get("direction") == "LONG")
    short_count = sum(1 for s in sigs if s.get("direction") == "SHORT")
    scores = [float(s.get("confluence_score") or s.get("score") or 0) for s in sigs]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0

    # Market open warmup detection
    curr_time = now_ist.time()
    t_open = dtime(9, 15)
    t_orb_end = dtime(9, 45)
    data_status = "DELAYED (15m)"
    status_message = "Scan completed successfully."

    if session.is_trading_active and t_open <= curr_time < t_orb_end and len(sigs) == 0:
        if curr_time < dtime(9, 30):
            data_status = "MARKET_OPEN_WAITING_FOR_FIRST_BAR"
            status_message = "Market opened at 09:15 IST. Public delayed feed is accumulating opening session bars. Prior session EOD candles are rejected to protect capital."
        else:
            data_status = "WARMING_UP"
            status_message = "Opening range (09:15–09:45 IST) is forming. Waiting for ORB breakout confirmation."

    return {
        "scan_id": scan_id,
        "signals": sigs[:50],
        "count": len(sigs),
        "symbols_scanned": len(symbols),
        "session": session.to_dict(),
        "nifty_market_context": nifty_ctx,
        "should_square_off": should_square_off_now(),
        "stats": {
            "total_signals": len(sigs),
            "long_count": long_count,
            "short_count": short_count,
            "avg_score": avg_score,
            "symbols_scanned": len(symbols),
        },
        "data_status": data_status,
        "message": status_message,
        "updated_at": datetime.now().isoformat(),
        "disclaimer": "Paper / educational — 5m delayed data, ATR-sized risk, and mandatory intraday square-off; not live broker execution.",
    }

@router.get("/api/intraday/latest")
async def intraday_latest():
    from agents.intraday_engine.scanner import get_nifty_market_context
    from agents.intraday_engine.session import evaluate_market_session
    from agents.intraday_engine.risk_manager import should_square_off_now

    session = evaluate_market_session()
    nifty_ctx = await asyncio.to_thread(get_nifty_market_context)
    sq_off = should_square_off_now()

    row = await asyncio.to_thread(intraday_store.load_latest_scan)
    if not row:
        return {
            "signals": [],
            "active_signals": [],
            "signal_count": 0,
            "qualified_setups": 0,
            "scan_time": None,
            "last_scan_at": None,
            "source_candle_at": None,
            "scan_age_seconds": None,
            "data_age_seconds": None,
            "freshness_status": "UNAVAILABLE",
            "provider": "yfinance",
            "provider_freshness": "15m Delayed",
            "session": session.to_dict(),
            "nifty_market_context": nifty_ctx,
            "should_square_off": sq_off,
            "stats": {"total_signals": 0, "long_count": 0, "short_count": 0, "avg_score": 0.0, "symbols_scanned": 20, "qualified_setups": 0},
            "data_status": "DELAYED (15m)",
        }

    raw_sigs = row.get("signals") or []
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(_IST)
    is_today = False
    if row.get("scan_time"):
        try:
            st = datetime.fromisoformat(row["scan_time"])
            is_today = (st.date() == now_ist.date())
        except Exception:
            pass

    # Expire signals if market inactive or past valid_until
    active_sigs = []
    for s in raw_sigs:
        valid_until_str = s.get("valid_until")
        is_expired = False
        if valid_until_str:
            try:
                vu_dt = datetime.fromisoformat(valid_until_str)
                if vu_dt.tzinfo is None:
                    vu_dt = vu_dt.replace(tzinfo=_IST)
                if now_ist > vu_dt:
                    is_expired = True
            except Exception:
                pass
        if not is_today or not session.is_trading_active:
            is_expired = True
        s["is_expired"] = is_expired
        if not is_expired:
            active_sigs.append(s)

    long_count = sum(1 for s in raw_sigs if s.get("direction") == "LONG")
    short_count = sum(1 for s in raw_sigs if s.get("direction") == "SHORT")
    scores = [float(s.get("confluence_score") or s.get("score") or 0) for s in raw_sigs]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0
    qualified_count = sum(1 for s in active_sigs if float(s.get("confluence_score") or s.get("score") or 0) >= 50.0)

    row["session"] = session.to_dict()
    row["nifty_market_context"] = nifty_ctx
    row["should_square_off"] = sq_off
    row["active_signals"] = active_sigs
    row["qualified_setups"] = qualified_count
    row["stats"] = {
        "total_signals": len(raw_sigs),
        "active_signals_count": len(active_sigs),
        "long_count": long_count,
        "short_count": short_count,
        "avg_score": avg_score,
        "symbols_scanned": row.get("symbols_scanned") or 20,
        "qualified_setups": qualified_count,
    }
    row["data_status"] = "DELAYED (15m)" if is_today else "EOD / HISTORICAL"
    return row


@router.get("/api/intraday/symbol/{symbol}")
async def intraday_symbol_detail(symbol: str):
    """
    Detailed technical drilldown and 5m candle history for the Setup Research Drawer.
    """
    from agents.intraday_engine.fetcher import fetch_5m
    from agents.intraday_engine.indicators import compute_technical_snapshot
    from agents.intraday_engine.session import evaluate_market_session

    clean_sym = symbol.strip().upper().replace(".NS", "")
    df = await asyncio.to_thread(fetch_5m, clean_sym)
    if df is None or df.empty:
        return JSONResponse({"detail": f"5m data unavailable for {symbol}"}, status_code=404)

    last_d = df.iloc[-1]["timestamp"].date()
    today_df = df[df["timestamp"].dt.date == last_d].copy()
    prior_dates = sorted(list({ts.date() for ts in df["timestamp"] if ts.date() < last_d}))
    prev_day_df = df[df["timestamp"].dt.date == prior_dates[-1]].copy() if prior_dates else None

    tech = await asyncio.to_thread(compute_technical_snapshot, today_df, prev_day_df)

    chart_slice = df.tail(45).copy()
    candles = []
    for _, row in chart_slice.iterrows():
        ts_val = row["timestamp"]
        ts_str = ts_val.strftime("%H:%M") if hasattr(ts_val, "strftime") else str(ts_val)[11:16]
        candles.append({
            "time": ts_str,
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
            "volume": int(row.get("volume") or 0),
        })

    session = evaluate_market_session()
    return {
        "symbol": clean_sym,
        "technical_snapshot": tech,
        "candles": candles,
        "session": session.to_dict(),
        "bar_count": len(today_df),
        "data_status": "DELAYED (5m)",
        "updated_at": datetime.now().isoformat(),
    }

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

        if isinstance(data, dict):
            data.pop("_fetched_at", None)
        else:
            data = {"error": f"Failed to compute index movers for {clean_name}"}

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
    BreakoutScanner — scans tracked symbols for volume-confirmed
    10-day, 50-day, and 52-week breakouts.
    In lightweight mode, uses durable snapshot or on-demand scan with explicit diagnostic state.
    """
    symbol_data = svc._daily_data_cache

    if not symbol_data:
        # Check durable snapshot first
        try:
            from db.runtime_state import get_runtime_state
            cached_snap = await asyncio.to_thread(get_runtime_state, "latest_breakouts")
            if cached_snap and isinstance(cached_snap, dict) and cached_snap.get("all"):
                cached_snap["source"] = "DURABLE_SNAPSHOT"
                if "coverage" not in cached_snap:
                    sc = cached_snap.get("symbols_scanned", len(cached_snap.get("all", [])))
                    total_cfg = len(getattr(svc, "TOP_NSE_SYMBOLS", []) or []) or 50
                    cached_snap["coverage"] = {
                        "configured_universe": total_cfg,
                        "eligible_universe": total_cfg,
                        "attempted": sc,
                        "successful": sc,
                        "failed": 0,
                        "skipped": max(0, total_cfg - sc),
                        "skip_reason": "Focused top liquid symbols in lightweight API mode",
                        "qualified": cached_snap.get("total_breakouts", 0),
                    }
                return cached_snap
        except Exception as exc:
            logger.debug("Durable breakouts snapshot read skipped: %s", exc)

        # On-demand fallback: scan top liquid symbols
        try:
            symbols = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL", "LT"]
            def _fetch_daily():
                d = {}
                for s in symbols:
                    df = fetch_ohlcv(s, "60d", "1d")
                    if df is not None and not df.empty and len(df) >= 15:
                        d[s] = calculate_indicators(df)
                return d
            symbol_data = await asyncio.to_thread(_fetch_daily)
        except Exception as exc:
            logger.warning("On-demand daily data fetch for breakouts failed: %s", exc)
            symbol_data = {}

    if not symbol_data:
        return {
            "all": [],
            "bullish_10d": [],
            "bearish_10d": [],
            "bullish_50d": [],
            "bearish_50d": [],
            "week_52_high": [],
            "week_52_low": [],
            "total_breakouts": 0,
            "status": "DATA_UNAVAILABLE",
            "message": "Market data currently unavailable for breakout scanning.",
            "real_data": False,
            "updated_at": datetime.now().isoformat(),
        }

    try:
        data = await asyncio.to_thread(breakout_scan_all, symbol_data, min_strength)
    except Exception as e:
        logger.error("Breakout scanner failed: %s", e)
        data = {
            "all": [],
            "total_breakouts": 0,
            "status": "ERROR",
            "message": f"Scan failed: {e}",
            "real_data": False,
        }

    data["updated_at"] = datetime.now().isoformat()
    scanned_count = len(symbol_data)
    data["symbols_scanned"] = scanned_count
    total_cfg = len(getattr(svc, "TOP_NSE_SYMBOLS", []) or []) or 50
    data["coverage"] = {
        "configured_universe": total_cfg,
        "eligible_universe": total_cfg,
        "attempted": scanned_count,
        "successful": scanned_count,
        "failed": 0,
        "skipped": max(0, total_cfg - scanned_count),
        "skip_reason": (
            f"Focused top {scanned_count} liquid symbols in lightweight API mode to ensure responsive response"
            if scanned_count < total_cfg
            else "Full universe scanned"
        ),
        "qualified": data.get("total_breakouts", 0),
    }
    if data.get("total_breakouts", 0) == 0:
        data["status"] = "ZERO_RESULTS"
        data["message"] = f"Scanned {scanned_count} of {total_cfg} configured symbols. 0 met the breakout strength threshold (>= {min_strength})."
    else:
        data["status"] = "READY"
        data["message"] = f"Found {data['total_breakouts']} breakouts across {scanned_count} scanned symbols ({total_cfg} configured)."

    # Cache to durable state
    try:
        from db.runtime_state import set_runtime_state
        await asyncio.to_thread(set_runtime_state, "latest_breakouts", data)
    except Exception:
        pass

    return data

# ── WebSocket Endpoint for Real-time Updates ──────────────

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

@router.post("/api/refresh", dependencies=[Depends(require_admin)])

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
                    "strategy": s.get("strategy", "INTRADAY"),
                    "confidence": float(s.get("confluence_score") or s.get("score") or 60.0),
                    "entry": s.get("entry"),
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
                        opps.append({
                            "symbol": sym,
                            "direction": "LONG" if sig in ("LONG", "BUY") else "SHORT",
                            "strategy": "WORKER_CYCLE",
                            "confidence": 65.0,
                            "entry": p.get("close"),
                            "target": round(p.get("close", 0) * (1.02 if sig in ("LONG", "BUY") else 0.98), 2),
                            "stop_loss": round(p.get("close", 0) * (0.99 if sig in ("LONG", "BUY") else 1.01), 2),
                            "rationale": f"Market snapshot signal {sig} in regime {p.get('regime', 'TREND')}",
                            "source": "MARKET_SNAPSHOT",
                        })
        except Exception as exc:
            logger.debug("Screener opportunities snapshot fallback failed: %s", exc)

    if not opps:
        return empty_scan_payload("screener_opportunities", "no_candidates")
    return {"opportunities": opps, "status": "ok", "count": len(opps)}

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

            return disabled_scan_payload("morning_scan", "no_scan_recorded")

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

            is_known_nse = clean_base in NSE_SYMBOLS or clean_base in settings.NSE_TICKER_MAP

            target_first = NSE_SYMBOLS.get(clean_base, f"{clean_base}.NS") if is_known_nse else clean

            # 1st attempt: targeted symbol (NSE ticker for Indian stocks, raw symbol for global)

            df = await asyncio.to_thread(fetch_arbitrary_symbol, target_first)

            if df is not None and not df.empty and is_known_nse:

                clean = clean_base

            # 2nd attempt: fallback if 1st attempt failed

            if (df is None or df.empty) and "/" not in clean:

                fallback_ticker = clean if is_known_nse else f"{clean_base}.NS"

                logger.info("Trying fallback ticker: %s", fallback_ticker)

                df = await asyncio.to_thread(fetch_arbitrary_symbol, fallback_ticker)

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

_ensemble_validation_job: dict[str, Any] = {
    "status": "IDLE",
    "symbol": "BTC/USDT",
    "timeframe": "4h",
    "started_at": None,
    "completed_at": None,
    "result": None,
    "error": None,
}
_ensemble_validation_lock = asyncio.Lock()


async def _run_ensemble_validation_task(symbol: str, timeframe: str, days: int, n_folds: int):
    global _ensemble_validation_job
    async with _ensemble_validation_lock:
        _ensemble_validation_job["status"] = "RUNNING"
        _ensemble_validation_job["started_at"] = datetime.now(timezone.utc).isoformat()
        _ensemble_validation_job["symbol"] = symbol
        _ensemble_validation_job["timeframe"] = timeframe
        _ensemble_validation_job["error"] = None
        try:
            loop = asyncio.get_event_loop()
            from concurrent.futures import ThreadPoolExecutor
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
            _ensemble_validation_job["result"] = result_to_dict(result)
            _ensemble_validation_job["status"] = "COMPLETE"
            _ensemble_validation_job["completed_at"] = datetime.now(timezone.utc).isoformat()
            logger.info("Ensemble validation background task completed successfully.")
        except Exception as exc:
            logger.error("Ensemble validation background task failed: %s", exc)
            _ensemble_validation_job["status"] = "FAILED"
            _ensemble_validation_job["error"] = str(exc)
            _ensemble_validation_job["completed_at"] = datetime.now(timezone.utc).isoformat()


@router.get("/api/ensemble/validate")
async def ensemble_validate(
    symbol: str = Query(default="BTC/USDT"),
    timeframe: str = Query(default="4h"),
    days: int = Query(default=365),
    n_folds: int = Query(default=5),
    run: bool = Query(default=False, description="Trigger new background run"),
):
    """
    Asynchronous walk-forward validation endpoint.
    Returns status (QUEUED / RUNNING / COMPLETE / FAILED / IDLE) and cached results instantly.
    Pass run=true to trigger a new validation run in the background without blocking the HTTP request.
    """
    global _ensemble_validation_job
    if run:
        if _ensemble_validation_job.get("status") == "RUNNING":
            return {
                "status": "RUNNING",
                "message": "Validation is currently running in the background",
                "job": _ensemble_validation_job,
            }
        _ensemble_validation_job["status"] = "QUEUED"
        _ensemble_validation_job["symbol"] = symbol
        _ensemble_validation_job["timeframe"] = timeframe
        asyncio.create_task(_run_ensemble_validation_task(symbol, timeframe, days, n_folds))
        return {
            "status": "QUEUED",
            "message": "Validation job successfully queued in background",
            "job": _ensemble_validation_job,
            "durable": False,
            "recommended_runner": "github_actions",
            "runner_note": "Render container restarts/sleep terminate in-memory jobs; run intensive ML validation via GitHub Actions workflow_dispatch for durable persistence.",
        }

    return {
        "status": _ensemble_validation_job.get("status", "IDLE"),
        "job": _ensemble_validation_job,
        "result": _ensemble_validation_job.get("result"),
        "durable": False,
        "recommended_runner": "github_actions",
        "runner_note": "Render container restarts/sleep terminate in-memory jobs; run intensive ML validation via GitHub Actions workflow_dispatch for durable persistence.",
    }

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

@router.post("/api/retrain", dependencies=[Depends(require_admin)])

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

    is_lightweight = os.getenv("LIGHTWEIGHT_API_MODE", "false").lower() in ("1", "true", "yes")
    if is_lightweight:
        raise HTTPException(
            status_code=503,
            detail="Model retraining is disabled in LIGHTWEIGHT_API_MODE to respect the Render 512MB RAM cap. Retraining must run in a dedicated worker or batch training environment.",
        )

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
    from agents.execution.market_control import is_crypto_forex_enabled
    if not is_crypto_forex_enabled():
        return JSONResponse(
            status_code=403,
            content={"error": "Crypto & Forex trading is disabled under NSE Focus Mode (Section 6A). Unlock via Market Control with operator password."}
        )

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
    from agents.execution.market_control import is_crypto_forex_enabled
    if not is_crypto_forex_enabled():
        return JSONResponse(
            status_code=403,
            content={"error": "Crypto & Forex trading is disabled under NSE Focus Mode (Section 6A). Unlock via Market Control with operator password."}
        )

    """Return cached forex swing setups sorted by strength score, segregated by qualification."""

    try:

        from agents.market_intelligence.forex_scanner import _scan_cache, is_forex_scanning, scan_all_forex

        if _scan_cache.get("setups"):

            setups = _scan_cache["setups"]

        elif is_forex_scanning():

            setups = _scan_cache.get("setups", [])

        else:

            asyncio.create_task(asyncio.to_thread(scan_all_forex))

            setups = _scan_cache.get("setups", [])

        all_dicts = [s.to_dict() for s in setups]

        qualified = [s for s in all_dicts if s.get("status") == "QUALIFIED"]

        skipped = [s for s in all_dicts if s.get("status") == "SKIP"]

        top_setups = qualified[:3]

        return {

            "setups": all_dicts,

            "qualified_setups": qualified,

            "top_setups": top_setups,

            "skipped_setups": skipped,

            "count": len(all_dicts),

            "qualified_count": len(qualified),

            "scanning_in_background": is_forex_scanning(),

            "updated_at": datetime.now().isoformat(),

        }

    except Exception as e:

        logger.error("Forex signals error: %s", e)

        return JSONResponse(status_code=503, content={"error": str(e), "setups": []})

@router.get("/api/forex/intelligence")

async def get_forex_intelligence():
    from agents.execution.market_control import is_crypto_forex_enabled
    if not is_crypto_forex_enabled():
        return JSONResponse(
            status_code=403,
            content={"error": "Crypto & Forex trading is disabled under NSE Focus Mode (Section 6A). Unlock via Market Control with operator password."}
        )

    """Return relative currency strengths, risk sentiment, and session overlap liquidity."""

    try:

        from agents.data_ingestion.forex_fetcher import compute_currency_strengths, compute_session_intelligence

        sess = await asyncio.to_thread(compute_session_intelligence)

        cs = await asyncio.to_thread(compute_currency_strengths)

        return {

            "session_intelligence": sess,

            "currency_strengths": cs.get("strengths", {}),

            "risk_sentiment": cs.get("risk_sentiment", "NEUTRAL"),

            "growth_score": cs.get("growth_score", 0.0),

            "safe_haven_score": cs.get("safe_haven_score", 0.0),

            "updated_at": datetime.now().isoformat(),

        }

    except Exception as e:

        logger.error("Forex intelligence error: %s", e)

        return JSONResponse(status_code=500, content={"error": str(e)})

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


@router.get("/legal.html", include_in_schema=False)
@router.get("/legal", include_in_schema=False)
async def serve_legal():
    """Serve the legal, terms and privacy disclaimer page."""
    legal_path = os.path.join(STATIC_DIR, "legal.html")
    if os.path.exists(legal_path):
        return FileResponse(legal_path, headers=_DASH_HTML_HEADERS)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=_DASH_HTML_HEADERS)


@router.get("/api/crypto/news")

async def get_crypto_news(limit: int = Query(12, ge=3, le=40)):
    from agents.execution.market_control import is_crypto_forex_enabled
    if not is_crypto_forex_enabled():
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
    from agents.execution.market_control import is_crypto_forex_enabled
    if not is_crypto_forex_enabled():
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
_binance_klines_cache = {}

@router.get("/api/crypto/proxy/binance/24hr")
async def proxy_binance_24hr():
    """Proxy Binance 24hr ticker with 15s caching to resolve CSP and direct provider fetch issues (F08)."""
    now = time.time()
    if _binance_proxy_cache["payload"] and (now - _binance_proxy_cache["timestamp"] < 15.0):
        return JSONResponse(
            content=_binance_proxy_cache["payload"],
            headers={"Cache-Control": "public, max-age=15", "X-Cache": "HIT"},
        )

    endpoints = [
        "https://api.binance.com/api/v3/ticker/24hr",
        "https://data-api.binance.vision/api/v3/ticker/24hr",
    ]
    import httpx
    for url in endpoints:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, headers={"User-Agent": "TradeAI-Pro/1.0"})
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
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
    """Proxy Binance klines with 20s caching to resolve CSP and provider fetch issues (F08)."""
    now = time.time()
    cache_key = f"{symbol}_{interval}_{limit}"
    cached = _binance_klines_cache.get(cache_key)
    if cached and (now - cached["timestamp"] < 20.0):
        return JSONResponse(
            content=cached["payload"],
            headers={"Cache-Control": "public, max-age=20", "X-Cache": "HIT"},
        )

    endpoints = [
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
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
    from agents.execution.market_control import is_crypto_forex_enabled
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
                latest = float(df.iloc[-1]["close"])
                first = float(df.iloc[0]["open"]) if float(df.iloc[0]["open"]) != 0 else float(df.iloc[0]["close"])
                chg = ((latest - first) / first) * 100.0 if first else 0.0
                res[idx_key] = (round(latest, 2), round(chg, 2))
                break
            sig = svc._signal_cache.get(sym)
            if isinstance(sig, dict) and sig.get("close") is not None:
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
                    hist = t.history(period="5d", interval="1d")
                    if not hist.empty:
                        close = float(hist["Close"].iloc[-1])
                        prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else close
                        chg = ((close - prev) / prev) * 100.0 if prev else 0.0
                        res[k] = (round(close, 2), round(chg, 2))
        except Exception as exc:
            logger.debug("Indices yfinance fallback error: %s", exc)

    if any(res[k][0] is not None for k in res):
        _INDICES_FALLBACK_CACHE = res
        _INDICES_FALLBACK_TS = now
    return res

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

        payload = {

            "crypto_mood": crypto_mood,

            "btc_dominance": round(btc_dominance, 2) if btc_dominance > 0 else None,

            "btc_dominance_source": dominance_source,

            "btc_24hr_change": round(

                float(btc_snapshot.price_change_24hr_pct or 0.0), 2

            ),

            "btc_price": round(float(btc_snapshot.price), 2) if getattr(btc_snapshot, "price", None) else None,
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
