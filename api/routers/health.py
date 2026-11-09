"""
TradeAI PRO — Health & System Inspection Domain Router.
=======================================================
Routes:
  - GET|HEAD /favicon.ico
  - GET|HEAD /api/health
  - GET|HEAD /api/ready
  - GET|HEAD /api/system/memory
  - GET /api/system/data-health
  - GET /api/health/live
  - GET /api/health/ready
  - GET /api/health/providers
"""

from __future__ import annotations

import os
import sys
import time
import math
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any

from fastapi import APIRouter, Response, HTTPException, status
from fastapi.responses import JSONResponse, FileResponse

from config import settings
import api.service as svc
from api.service import (
    _loading_status,
    _SERVER_START_TIME,
    _force_free_memory,
    _get_providers_telemetry,
    _get_feed_status,
    is_market_open,
)

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dashboard")

router = APIRouter(tags=["health"])

import asyncio
from db.connection import check_db_connectivity

async def _check_db_connectivity_with_retry() -> bool:
    """Retry the DB probe so a Render cold start is not reported as an outage."""
    for timeout in (2.5, 6.0):
        try:
            if await asyncio.wait_for(
                asyncio.to_thread(check_db_connectivity), timeout=timeout
            ):
                return True
        except Exception:
            continue
    return False


@router.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)

async def favicon():

    """Serve favicon at site root — browsers request /favicon.ico on the API host."""

    path = os.path.join(STATIC_DIR, "favicon.ico")

    if os.path.isfile(path):

        return FileResponse(path, media_type="image/x-icon")

    return Response(status_code=204)

@router.api_route("/api/health", methods=["GET", "HEAD"])
async def health_check(response: Response = None):
    """Health check endpoint — shows loading progress, server status, and DB availability."""
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"

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
    initial_load_disabled = os.getenv("INITIAL_LOAD_ENABLED", "true").lower() in ("0", "false", "no")
    is_ready_on_demand = svc._loading_status.get("ready", False) or initial_load_disabled
    if symbols_loaded == 0 and (is_lightweight or is_ready_on_demand or feed_status.get("source") in ("WORKER_DB", "INTRADAY_STORE")):
        symbols_loaded = len(svc.TOP_NSE_SYMBOLS) if getattr(svc, "TOP_NSE_SYMBOLS", None) else 20

    uptime_sec = round(time.time() - getattr(svc, "_SERVER_START_TIME", time.time()), 1)

    status_str = "ok" if db_ok and (symbols_loaded > 0 or is_lightweight or is_ready_on_demand) else "degraded" if not db_ok else "starting"

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

    h2_status = str(h2.get("status", "degraded")).upper()
    if h2_status in ("HEALTHY", "OK"):
        status_str = "ok"
    elif h2_status in ("STARTING",):
        status_str = "starting"
    else:
        status_str = "degraded" 

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

    memory_rss_mb = None
    try:
        from utils.memory_profiler import get_process_rss_mb
        memory_rss_mb = get_process_rss_mb(collect_gc=False)
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
        "memory_rss_mb": memory_rss_mb,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }

@router.api_route("/api/ready", methods=["GET", "HEAD"])
async def readiness_check(response: Response = None):
    """Readiness probe: verifies DB connectivity and that market data or snapshots are available."""
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
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
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
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


@router.api_route("/api/system/memory", methods=["GET", "HEAD"])
async def get_system_memory_telemetry(response: Response = None):
    """Real-time process RSS memory and Render 512MB RAM guardrail telemetry."""
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"

    import platform
    from utils.memory_profiler import check_memory_guardrail

    guard = check_memory_guardrail()
    rss_mb = float(guard.get("current_rss_mb", 0.0))
    limit_mb = 512.0
    usage_pct = round((rss_mb / limit_mb) * 100.0, 1)

    status = "HEALTHY"
    if rss_mb >= guard.get("max_peak_mb", 430.0):
        status = "CRITICAL"
    elif rss_mb >= guard.get("max_steady_mb", 380.0):
        status = "WARNING"

    return {
        "status": status,
        "rss_mb": rss_mb,
        "unit": "MB",
        "limit_mb": limit_mb,
        "headroom_mb": guard.get("headroom_mb", round(max(0.0, limit_mb - rss_mb), 2)),
        "usage_pct": usage_pct,
        "max_steady_mb": guard.get("max_steady_mb", 380.0),
        "max_peak_mb": guard.get("max_peak_mb", 430.0),
        "is_safe": guard.get("is_safe", True),
        "warning": guard.get("warning", False),
        "platform": platform.system(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
        from agents.market_intelligence import market_data as tradeflow_module
        if not tradeflow_module._tradeflow_cache:
            try:
                from db.runtime_state import get_runtime_state
                persisted_flow = await asyncio.to_thread(
                    get_runtime_state, "tradeflow_snapshot:v1"
                )
                if persisted_flow:
                    tradeflow_module.restore_tradeflow_cache(persisted_flow)
            except Exception as restore_exc:
                flow_status["error"] = str(restore_exc)

        cached_flow = tradeflow_module._tradeflow_cache
        if cached_flow:
            flow_status = {
                "status": "STALE" if cached_flow.get("stale") else "HEALTHY",
                "date": cached_flow.get("display_date") or cached_flow.get("date"),
                "source": cached_flow.get("source"),
                "flows_count": len(cached_flow.get("flows", [])),
                "age_minutes": cached_flow.get("age_minutes"),
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
async def liveness_check(response: Response = None):
    """Ultra-lightweight liveness probe (sub-1ms)."""
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    start_t = getattr(svc, "_SERVER_START_TIME", time.time())
    return {
        "status": "alive",
        "uptime_seconds": round(time.time() - start_t, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@router.get("/api/health/ready")
async def health_ready_check(response: Response = None):
    """Readiness probe alias for standard k8s/cloud health checks."""
    return await readiness_check(response=response)

@router.get("/api/health/providers")

async def health_providers_check():

    """Telemetry for upstream data providers."""

    telemetry = svc._get_providers_telemetry() if hasattr(svc, "_get_providers_telemetry") else {}

    return {

        "providers": telemetry,

        "timestamp": datetime.now(timezone.utc).isoformat(),

    }

