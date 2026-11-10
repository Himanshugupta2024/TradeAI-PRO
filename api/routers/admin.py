"""
TradeAI PRO — Admin, Operations & Autonomous Control Domain Router.
===================================================================
Routes:
  - GET  /api/market-control/status
  - POST /api/market-control/toggle
  - GET  /api/telegram/status
  - POST /api/telegram/test
  - GET  /api/autonomous/status
  - GET  /api/autonomous/feed
  - POST /api/autonomous/run-cycle
  - POST /api/autonomous/toggle
  - POST /api/autonomous/review-now
  - GET  /api/models/status
  - POST /api/models/sync
  - GET  /api/learning/stats
  - GET  /api/learning/outcomes
  - GET  /api/ensemble/validate
  - GET  /api/ensemble/status
  - POST /api/retrain
  - GET  /api/retrain/status
  - POST /api/refresh
  - GET  /admin, /admin/, /admin.html
"""

from __future__ import annotations

import os
import sys
import time
import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from pydantic import BaseModel
from fastapi import APIRouter, Query, HTTPException, Depends, Response, Header
from fastapi.responses import JSONResponse, FileResponse
from starlette.requests import Request as FastApiRequest

from config import settings
import api.service as svc
from api.security import require_admin

logger = logging.getLogger(__name__)
log = logger

router = APIRouter(tags=["Admin & Control"])

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(ROOT_DIR, "dashboard")
_DASH_HTML_HEADERS = {
    "Cache-Control": "no-store, must-revalidate",
    "Pragma": "no-cache",
}

# ── Telegram helper wrappers ─────────────────────────────────────────────────
try:
    from agents.notifications.telegram import (
        is_configured as tg_is_configured,
        send_signal_alert,
    )
except ImportError:
    def send_signal_alert(*args, **kwargs):
        return False

    def tg_is_configured():
        return False


# ── Market Control Section 6A ────────────────────────────────────────────────
class MarketControlToggleRequest(BaseModel):
    enabled: bool
    password: Optional[str] = None
    reason: Optional[str] = None


@router.get("/api/market-control/status")
async def get_market_control_status_route():
    """Return Section 6A Market Control status and NSE Focus Mode telemetry."""
    from agents.execution.market_control import get_market_control_status
    return await asyncio.to_thread(get_market_control_status)


@router.post("/api/market-control/toggle")
async def toggle_market_control_route(
    req: MarketControlToggleRequest,
    request: FastApiRequest,
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
):
    """
    Operator control for Crypto / Forex processing under Section 6A policy.
    - Turning ON requires server-side password verification.
    - Turning OFF verifies operator authorization and open position safeguards.
    """
    from agents.execution.market_control import set_market_control_state, verify_operator_password
    client_ip = request.client.host if request.client else "unknown"

    header_key = x_admin_key or x_api_key
    if not header_key and authorization and authorization.strip().lower().startswith("bearer "):
        header_key = authorization.strip()[7:].strip()

    candidate_key = (req.password or "").strip() or (header_key or "").strip()

    if not req.enabled:
        is_auth = False
        if header_key:
            try:
                require_admin(x_admin_key=x_admin_key, x_api_key=x_api_key, authorization=authorization)
                is_auth = True
            except HTTPException:
                pass
        if not is_auth and candidate_key:
            ok, _ = verify_operator_password(candidate_key, client_id=client_ip)
            if ok:
                is_auth = True

        if not is_auth:
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "message": "Operator authorization required to disable Crypto / Forex processing.",
                    "state": None,
                }
            )

    success, message, state = set_market_control_state(
        enabled=req.enabled,
        password=candidate_key,
        client_id=client_ip,
        operator="operator",
        reason=req.reason or "",
    )
    invalidate_autonomous_status_cache()
    status_code = 200 if success else (403 if any(w in message for w in ["Invalid", "Password", "lockout", "unconfigured", "blocked"]) else 400)
    return JSONResponse(
        status_code=status_code,
        content={
            "success": success,
            "message": message,
            "state": state,
        }
    )


# ── Telegram Alert Endpoints ──────────────────────────────────────────────────
@router.get("/api/telegram/status")
async def telegram_status():
    """Check if Telegram bot is configured and ready."""
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
        "message": "Test alert sent! Check your Telegram." if sent else "Send failed — check bot token and chat ID",
    }


# ── Autonomous status snapshot cache & single-flight lock ─────────────────────
_AUTONOMOUS_STATUS_CACHE: Optional[Dict[str, Any]] = None
_AUTONOMOUS_STATUS_CACHE_TS: float = 0.0
_AUTONOMOUS_STATUS_LOCK: Optional[asyncio.Lock] = None
_AUTONOMOUS_STATUS_TTL_SEC: float = 10.0
_last_manual_cycle_dispatch_time: float = 0.0


def _get_autonomous_status_lock() -> asyncio.Lock:
    global _AUTONOMOUS_STATUS_LOCK
    if _AUTONOMOUS_STATUS_LOCK is None:
        _AUTONOMOUS_STATUS_LOCK = asyncio.Lock()
    return _AUTONOMOUS_STATUS_LOCK


def invalidate_autonomous_status_cache() -> None:
    global _AUTONOMOUS_STATUS_CACHE, _AUTONOMOUS_STATUS_CACHE_TS
    _AUTONOMOUS_STATUS_CACHE = None
    _AUTONOMOUS_STATUS_CACHE_TS = 0.0


def _fetch_autonomous_status_sync():
    """Fetch status and lock info in a thread to keep DB I/O off the event loop."""
    from agents.execution.autonomous_daemon import get_autonomous_daemon
    from agents.execution.distributed_lock import DistributedLockManager
    from agents.execution.session_guard import is_nse_market_open, get_current_ist_time

    daemon = get_autonomous_daemon()
    engine = svc._get_engine()
    daemon_status = daemon.get_status(engine)

    worker_status = {}
    lock_info = None

    try:
        from db.connection import SessionLocal
        from db.runtime_state import get_runtime_states

        session = SessionLocal()
        try:
            states = get_runtime_states(
                ["eod_review:last_run:v1", "autonomous_worker:latest_status"],
                session=session,
            )

            review_marker = states.get("eod_review:last_run:v1", {})
            if not daemon_status.get("last_eod_review") and isinstance(review_marker, dict):
                daemon_status["last_eod_review"] = review_marker.get("last_review_date")

            worker_status = states.get("autonomous_worker:latest_status", {}) or {}

            lock_mgr = DistributedLockManager()
            lock_info = lock_mgr.get_lock_info("autonomous_paper_worker", session=session)
        finally:
            session.close()
    except Exception as exc:
        log.warning("[Autonomous] Database error in _fetch_autonomous_status_sync: %s", exc)

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
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_fetch_autonomous_status_sync),
                timeout=8.0,
            )
        except Exception as fetch_err:
            log.warning("[Autonomous] Status fetch timeout/failure: %s", fetch_err)
            if _AUTONOMOUS_STATUS_CACHE is not None:
                cached_copy = dict(_AUTONOMOUS_STATUS_CACHE)
                cached_copy["cache_hit"] = True
                cached_copy["degraded"] = True
                return cached_copy
            # Fallback in-memory status when database or background worker telemetry times out
            from agents.execution.autonomous_daemon import get_autonomous_daemon
            from agents.execution.session_guard import is_nse_market_open, get_current_ist_time
            daemon = get_autonomous_daemon()
            engine = svc._get_engine()
            now_ist = get_current_ist_time()
            is_nse_open, nse_reason = is_nse_market_open(now_ist)
            raw = {
                "daemon_status": daemon.get_status(engine),
                "worker_status": {},
                "lock_info": None,
                "watchdog": {
                    "status": "MARKET_CLOSED" if not is_nse_open else "HEALTHY",
                    "message": f"NSE cash market closed ({nse_reason}). Worker in standby." if not is_nse_open else "Worker nominal (fallback)",
                    "age_minutes": None,
                    "is_nse_market_open": is_nse_open,
                    "nse_reason": nse_reason,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                },
            }
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
    except Exception:
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

    feed = get_runtime_state("autonomous_feed:v1", []) or []

    if not feed:
        feed = await asyncio.to_thread(_fetch_feed_db_sync, limit)

    if not feed and daemon.decision_log:
        feed = daemon.decision_log

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

    for item in feed:
        raw_ts = item.get("timestamp")
        if raw_ts and isinstance(raw_ts, str) and not raw_ts.endswith("Z") and "+" not in raw_ts:
            item["timestamp"] = raw_ts + "Z"

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


@router.post("/api/autonomous/run-cycle", dependencies=[Depends(require_admin)])
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

    from api.cycle_scheduler import local_cycle_allowed

    _allowed, _refusal = local_cycle_allowed()
    if not _allowed:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "CYCLE_DISPATCH_UNAVAILABLE",
                "message": _refusal,
                "dispatch": "disabled",
            },
        )

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
    try:
        from db.runtime_state import set_runtime_state
        set_runtime_state("autonomous_trading_enabled", daemon.enabled)
    except Exception as state_err:
        log.warning("[Autonomous] Could not persist enabled state to DB: %s", state_err)
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


# ── Model Bundle & Feedback Loop Endpoints ────────────────────────────────────
@router.get("/api/models/status")
async def get_model_bundle_status():
    """Which model bundle is on disk and where it came from (no artifact content)."""
    from api.model_sync import BUNDLE_KEY, current_status

    try:
        from db.runtime_state import get_runtime_state
        published = get_runtime_state(BUNDLE_KEY)
    except Exception:
        published = None
    status = current_status()
    if isinstance(published, dict):
        status["published"] = {
            "version": published.get("version"),
            "trained_at": published.get("trained_at"),
            "source": published.get("source"),
            "metrics": published.get("metrics") or {},
            "files": sorted((published.get("files") or {}).keys()),
        }
    else:
        status["published"] = None
    return {"ok": True, **status}


@router.post("/api/models/sync", dependencies=[Depends(require_admin)])
async def sync_model_bundle(payload: dict | None = None):
    """Apply the published model bundle to disk (admin only, explicit by design)."""
    from api.model_sync import sync_from_db

    payload = payload or {}
    force = bool(payload.get("force", False))
    result = await asyncio.to_thread(sync_from_db, force=force)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(status_code=status_code, content={"ok": bool(result.get("ok")), **result})


@router.get("/api/learning/stats")
async def get_learning_stats_endpoint():
    """Return AI self-learning insights + current learned weights."""
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
    """Return recent raw trade outcomes logged to the feedback loop."""
    try:
        from agents.analytics.feedback_loop import get_recent_outcomes
        return {"outcomes": get_recent_outcomes(limit=limit)}
    except Exception as exc:
        logger.error("Learning outcomes error: %s", exc)
        return {"outcomes": [], "error": str(exc)}


# ── AI / Ensemble Endpoints ───────────────────────────────────────────────────
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
        lstm_path = "models/best_model.pt"
        meta_path = "models/model_meta.json"
        if os.path.exists(lstm_path) and os.path.exists(meta_path):
            with open(meta_path) as f:
                lstm_meta = json.load(f)
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


# ── Retraining & Refresh Ops ──────────────────────────────────────────────────
@router.post("/api/retrain", dependencies=[Depends(require_admin)])
async def retrain_xgboost(
    symbols: str = Query(
        default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT",
        description="Comma-separated symbols to train on",
    ),
    timeframe: str = Query(default="4h", description="Candle timeframe"),
    days: int = Query(default=365, description="Days of historical data"),
):
    """
    Retrain the XGBoost model on fresh crypto data.
    Runs asynchronously in a background thread.
    Returns a job_id that can be polled at /api/retrain/status.
    """
    is_lightweight = os.getenv("LIGHTWEIGHT_API_MODE", "false").lower() in ("1", "true", "yes")
    if is_lightweight:
        raise HTTPException(
            status_code=503,
            detail="Model retraining is disabled in LIGHTWEIGHT_API_MODE to respect the Render 512MB RAM cap. Retraining must run in a dedicated worker or batch training environment.",
        )

    from concurrent.futures import ThreadPoolExecutor

    def _retrain_sync(sym_list, tf, d):
        from agents.ai_engine.train import train_xgboost
        return train_xgboost(symbols=sym_list, timeframe=tf, days=d)

    job_id = f"retrain_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    svc._retrain_jobs[job_id] = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "timeframe": timeframe,
    }

    async def _background():
        loop = asyncio.get_event_loop()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = await loop.run_in_executor(
                    pool,
                    _retrain_sync,
                    symbols.split(","),
                    timeframe,
                    days,
                )
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
        "status": "started",
        "job_id": job_id,
        "message": "Retraining started in background. Poll /api/retrain/status for progress.",
        "symbols": symbols.split(","),
        "timeframe": timeframe,
        "days": days,
    }


@router.get("/api/retrain/status")
async def retrain_status():
    """Return status of all retrain jobs."""
    return {"jobs": svc._retrain_jobs}


@router.post("/api/refresh", dependencies=[Depends(require_admin)])
async def refresh_data():
    """Force refresh of all market data."""
    await svc._refresh_market_data()
    return {
        "status": "ok",
        "symbols": len(svc._market_data),
        "updated_at": datetime.now().isoformat(),
    }


# ── Admin Web Console ─────────────────────────────────────────────────────────
@router.api_route("/admin", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/admin/", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/admin.html", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_admin():
    """Serve the operator / administrative console."""
    admin_path = os.path.join(STATIC_DIR, "admin.html")
    if os.path.exists(admin_path):
        return FileResponse(admin_path, headers={
            "Cache-Control": "private, no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Frame-Options": "DENY",
        })
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=_DASH_HTML_HEADERS)
