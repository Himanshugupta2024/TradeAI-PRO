"""
TradeAI PRO — Forex Domain Router.
==================================
Routes:
  - GET /api/forex/rates
  - GET /api/forex/signals
  - GET /api/forex/intelligence
  - GET /api/forex/signal/{symbol}
  - GET /api/forex/sessions
  - GET /api/forex/ohlcv/{symbol}
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import JSONResponse

from config import settings
from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed

logger = logging.getLogger(__name__)

router = APIRouter(tags=["forex"])

# Fallback definitions if fetcher is missing
try:
    from agents.data_ingestion.forex_fetcher import get_all_forex_rates, get_forex_ohlcv
    from agents.market_intelligence.forex_scanner import (
        is_forex_scanning,
        scan_all_forex,
        scan_forex_setup,
        _scan_cache,
    )
except ImportError:
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
    _scan_cache = {}

@router.get("/api/forex/rates")

async def get_forex_rates():
    from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed
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
    from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed
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
    from agents.execution.market_control import is_crypto_forex_enabled, is_crypto_research_allowed
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

