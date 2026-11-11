"""
TradeAI PRO — Swing, Intraday, Breakout & Morning Scanner Domain Router.
========================================================================
Routes:
  - GET /api/swing/shortlist
  - GET /api/swing/history/{symbol}
  - GET /api/swing-scanner
  - GET /api/swing/history
  - GET /api/swing/history/{scan_id}
  - GET /api/swing/stats
  - POST /api/swing/scan-now
  - GET /api/swing/symbol/{symbol}
  - GET /api/swing-debug
  - POST /api/intraday/scan
  - GET /api/intraday/latest
  - GET /api/intraday/symbol/{symbol}
  - GET /api/intraday/live-quote/{symbol}
  - GET /api/intraday/stats
  - GET /api/breakouts
  - GET /api/morning-scan
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
from collections import deque

import numpy as np
import pandas as pd
from pydantic import BaseModel
from fastapi import APIRouter, Query, HTTPException, Depends, BackgroundTasks
from fastapi.responses import JSONResponse

from config import settings
import api.service as svc
from db import intraday_store, swing_store
from api.security import require_admin
from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.regime_detector import detect_regime
from agents.feature_engine.trend import classify_trend, propagate_trend_to_ltf
from agents.market_intelligence.breakout_scanner import scan_all_symbols as breakout_scan_all
from agents.market_intelligence.swing_scanner import scan_all_symbols as swing_scan_all
from agents.market_intelligence.nse_fetcher import fetch_arbitrary_symbol, fetch_ohlcv, validate_nse_symbol
from agents.stock_screener import opportunity_stocks, start_morning_scan_task, start_screener_task

logger = logging.getLogger(__name__)

router = APIRouter(tags=["swing", "intraday", "scanners"])

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

    scan_id = None
    try:
        scan_id = await asyncio.to_thread(
            lambda: intraday_store.save_scan(
                sigs,
                symbols_scanned=len(symbols),
                trigger="api",
            ),
        )
    except Exception as exc:
        logger.warning("Intraday save_scan failed: %s", exc)

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

    row = None
    try:
        row = await asyncio.to_thread(intraday_store.load_latest_scan)
    except Exception as exc:
        logger.warning("Intraday load_latest_scan failed: %s", exc)
        row = None
    session_dict = session.to_dict()
    if not row:
        session_dict["allows_new_entries"] = False
        session_dict["data_freshness_mode"] = "DELAYED"
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
            "session": session_dict,
            "nifty_market_context": nifty_ctx,
            "should_square_off": sq_off,
            "allows_new_entries": False,
            "data_freshness_mode": "DELAYED",
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

    feed_is_realtime = bool(
        row.get("freshness_status") == "FRESH"
        and "Delayed" not in str(row.get("provider_freshness") or "")
        and row.get("data_status") == "LIVE"
    )
    if not feed_is_realtime:
        session_dict["allows_new_entries"] = False
        session_dict["data_freshness_mode"] = row.get("freshness_status") or "DELAYED"

    row["session"] = session_dict
    row["allows_new_entries"] = session_dict["allows_new_entries"]
    row["data_freshness_mode"] = session_dict["data_freshness_mode"]
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
    row["data_status"] = "LIVE" if feed_is_realtime else ("DELAYED (15m)" if is_today else "EOD / HISTORICAL")
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

    try:
        return await asyncio.to_thread(intraday_store.compute_stats, int(days))
    except Exception as exc:
        logger.warning("Intraday compute_stats failed: %s", exc)
        return {"total_scans": 0, "total_signals": 0, "win_rate": 0.0, "status": "standby"}

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

_quote_single_flight_locks: dict[str, asyncio.Lock] = {}
_quote_latencies_ms: deque = deque(maxlen=500)

