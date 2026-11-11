"""
TradeAI PRO — Research, Edge Engine & Macro Intelligence Domain Router.
======================================================================
Routes:
  - GET|POST /api/research/next-day-picks*
  - GET /api/edge/score/{symbol:path}
  - GET /api/edge/top-setups
  - GET /api/edge/research/{symbol}
  - GET /api/edge/macro-heatmap
  - GET /api/edge/risk-gauge
  - GET /api/macro/intelligence
  - GET /api/macro/sector-impact
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

import pandas as pd
from pydantic import BaseModel
from fastapi import APIRouter, Query, HTTPException, Depends
from fastapi.responses import JSONResponse

from config import settings
import api.service as svc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["research"])

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

@router.get("/api/research/next-day-picks/latest")
async def get_latest_next_day_picks_alias_route():
    """Alias for /api/research/next-day-picks to ensure /latest probes succeed."""
    from agents.market_intelligence.next_day_picks import get_latest_next_day_picks
    return await asyncio.to_thread(get_latest_next_day_picks)

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
        from agents.execution.market_control import is_crypto_forex_enabled

        crypto_forex_on = is_crypto_forex_enabled()
        if not crypto_forex_on and asset_class.lower() in ("crypto", "forex"):
            return {
                "asset_class": asset_class,
                "min_score": min_score,
                "count": 0,
                "setups": [],
                "force_refresh": force_refresh,
                "status": "DISABLED_BY_MARKET_CONTROL",
                "message": f"{asset_class.upper()} is disabled while NSE Focus Mode is active.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

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
                        "asset_class": getattr(cached, "asset_class", "nse"),
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

        setups = []
        try:
            setups = await asyncio.wait_for(asyncio.to_thread(_compute_top), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Top setups calculation timed out (5s) — loading fast cached setups without recalculation")
            try:
                engine = EdgeScoreEngine()
                for sym in ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN"]:
                    cached = engine._load_cache(sym)
                    if cached:
                        setups.append({
                            "symbol": sym,
                            "asset_class": "nse",
                            "edge_score": cached.edge_score,
                            "confidence": cached.confidence,
                            "rationale": cached.rationale or "Cached NSE Quant Score",
                        })
            except Exception:
                setups = []

        return {
            "asset_class": asset_class,
            "min_score": min_score,
            "count": len(setups),
            "setups": setups,
            "force_refresh": force_refresh,
            "timestamp": datetime.now(timezone.utc).isoformat(),
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

