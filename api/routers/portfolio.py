"""
TradeAI PRO — Portfolio & Paper Trading Domain Router.
======================================================
Routes:
  - GET /api/trades
  - GET /api/track-record
  - GET /api/paper/scorecard
  - POST /api/paper/resolve
  - POST /api/paper/trade
  - GET /api/paper/open
  - POST /api/paper/close/{trade_id}
  - POST /api/paper/close-bulk
  - POST /api/paper/capital
  - POST /api/paper/reset
  - GET /api/portfolio
  - GET /api/performance
"""

from __future__ import annotations

import os
import sys
import time
import json
import math
import logging
import asyncio
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

import numpy as np
import pandas as pd
from pydantic import BaseModel
from fastapi import APIRouter, Query, HTTPException, Depends, Response
from fastapi.responses import JSONResponse

from config import settings
import api.service as svc
from api.security import require_admin, get_authenticated_account
from api.data_honesty import price_decimals, mark_trade_pricing
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade
from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed

logger = logging.getLogger(__name__)

router = APIRouter(tags=["portfolio", "paper"])

@router.get("/api/trades")
async def get_trades(limit: int = Query(50, le=100), account_id: str = Depends(get_authenticated_account)):
    """Get recent trade history from real paper trading engine (newest first)."""
    engine = svc.get_engine_for_account(account_id)
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

@router.post("/api/paper/trade", dependencies=[Depends(require_admin)])

async def place_paper_trade_endpoint(payload: dict, account_id: str = Depends(get_authenticated_account)):

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

    engine = svc.get_engine_for_account(account_id)

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
async def get_open_paper_trades_endpoint(account_id: str = Depends(get_authenticated_account)):
    """Get all currently open paper trades with unified live pricing and valuation."""
    from api.valuation import resolve_open_trades_valuation
    engine = svc.get_engine_for_account(account_id)
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

@router.post("/api/paper/close/{trade_id}", dependencies=[Depends(require_admin)])

async def close_paper_trade_endpoint(trade_id: int, payload: dict | None = None, account_id: str = Depends(get_authenticated_account)):

    """Manually close an open paper trade at exit price."""

    engine = svc.get_engine_for_account(account_id)

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
async def close_paper_trades_bulk(payload: dict | None = None, account_id: str = Depends(get_authenticated_account)):
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
    engine = svc.get_engine_for_account(account_id)
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

@router.post("/api/paper/capital", dependencies=[Depends(require_admin)])
async def set_paper_capital_endpoint(payload: dict | None = None, account_id: str = Depends(get_authenticated_account)):
    """Reconfigure self-learning paper capital and reset portfolio baseline."""
    payload = payload or {}
    capital = float(payload.get("capital") or 100000.0)
    clear_trades = bool(payload.get("clear_trades", False))
    engine = svc.get_engine_for_account(account_id)
    new_stats = engine.reset_account(new_capital=capital, clear_trades=clear_trades)
    return {
        "status": "ok",
        "message": f"Self-learning capital set to ₹{capital:,.2f}",
        "stats": new_stats,
    }

@router.post("/api/paper/reset", dependencies=[Depends(require_admin)])
async def reset_paper_trading_endpoint(capital: float = Query(100000.0, ge=1000.0), account_id: str = Depends(get_authenticated_account)):
    """Reset paper trading portfolio and self-learning state."""
    engine = svc.get_engine_for_account(account_id)
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

    # The daemon keeps the review date in memory only, so a restart made the
    # status look like learning had never run. Prefer the durable marker.
    if not daemon_status.get("last_eod_review"):
        review_marker = get_runtime_state("eod_review:last_run:v1", {}) or {}
        if isinstance(review_marker, dict) and review_marker.get("last_review_date"):
            daemon_status["last_eod_review"] = review_marker["last_review_date"]

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



@router.get("/api/portfolio")

async def get_portfolio(account_id: str = Depends(get_authenticated_account)):

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

    engine = svc.get_engine_for_account(account_id)
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

        # A missing quote silently zeroes unrealized P&L and drops total value to
        # cash. Publish the verdict so the UI can say PARTIAL instead of showing a
        # phantom loss.
        "valuation_status": val_summary.get("valuation_status", "UNKNOWN"),

        "unpriced_quotes_count": int(val_summary.get("unpriced_quotes_count", 0) or 0),

        "stale_quotes_count": int(val_summary.get("stale_quotes_count", 0) or 0),

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
            "eligible_trades": stats.get("eligible_trades", stats.get("total_trades", 0)),
            "excluded_legacy_trades": stats.get("excluded_legacy_trades_count", len(stats.get("unconverted_legacy_trades", []))),
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

