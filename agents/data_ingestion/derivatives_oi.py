"""
Derivatives Open Interest & PCR Intelligence Engine — TradeAI-PRO Data Brain V3
Calculates Put-Call Ratio (PCR), Open Interest changes, and 4-quadrant OI Buildup
classification (Long Buildup, Short Buildup, Short Covering, Long Unwinding) for Nifty & BankNifty.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Bounded in-memory derivatives cache
_oi_cache: dict[str, Any] = {}
_last_oi_fetch_ts: float = 0.0
_CACHE_TTL_SECONDS: float = 180.0  # 3 minutes
_prev_total_oi_by_symbol: dict[str, int] = {}


def classify_oi_buildup(price_change_pct: float, oi_change_pct: float) -> str:
    """
    Classify 4-quadrant Open Interest buildup:
      Price UP   + OI UP   -> LONG_BUILDUP (Bullish conviction)
      Price DOWN + OI UP   -> SHORT_BUILDUP (Aggressive selling)
      Price UP   + OI DOWN -> SHORT_COVERING (Fragile rally)
      Price DOWN + OI DOWN -> LONG_UNWINDING (Exhaustion / Breakdown)
    """
    if price_change_pct >= 0.10:
        if oi_change_pct >= 0.25:
            return "LONG_BUILDUP"
        elif oi_change_pct <= -0.25:
            return "SHORT_COVERING"
        else:
            return "MILD_UPWARD_BIAS"
    elif price_change_pct <= -0.10:
        if oi_change_pct >= 0.25:
            return "SHORT_BUILDUP"
        elif oi_change_pct <= -0.25:
            return "LONG_UNWINDING"
        else:
            return "MILD_DOWNWARD_BIAS"
    else:
        return "CONSOLIDATION"


def interpret_pcr(pcr: float) -> tuple[str, int]:
    """
    Interpret Put-Call Ratio and assign a sentiment score (-5 to +5):
      PCR > 1.25: Put writing dominance -> Bullish support (+3 to +5)
      0.85-1.25:  Balanced range -> Neutral (0)
      PCR < 0.75: Call writing dominance -> Bearish ceiling (-3 to -5)
      Contrarian extremes (>1.60 or <0.50): Reversal alert
    """
    if pcr >= 1.50:
        return "EXTREME_OVERBOUGHT_REVERSAL_RISK", -2
    elif pcr >= 1.20:
        return "BULLISH_SUPPORT", 4
    elif pcr >= 0.85:
        return "NEUTRAL_BALANCED", 0
    elif pcr >= 0.65:
        return "BEARISH_RESISTANCE", -3
    else:
        return "EXTREME_OVERSOLD_REVERSAL_RISK", 2


def _fetch_derivatives_sync(index_symbol: str = "NIFTY") -> dict[str, Any]:
    """Fetch live/cached derivatives OI and PCR from Box C (never fabricate baseline constants)."""
    price_change = 0.0
    spot_close = 0.0
    try:
        from agents.market_intelligence.nse_fetcher import fetch_arbitrary_symbol
        bench = "^NSEBANK" if "BANK" in index_symbol.upper() else "^NSEI"
        df = fetch_arbitrary_symbol(bench, period="2d", interval="5m")
        if df is not None and not df.empty and len(df) >= 2:
            c0 = float(df["close"].iloc[-2])
            c1 = float(df["close"].iloc[-1])
            spot_close = c1
            if c0 > 0:
                price_change = round(((c1 - c0) / c0) * 100.0, 2)
    except Exception as exc:
        log.debug("[DerivativesOI] Index price check error: %s", exc)

    fno_sym = "NIFTY BANK" if "BANK" in index_symbol.upper() else "NIFTY 50"
    fno_data: dict[str, Any] = {}
    try:
        from api.fno_client import fetch_fno_analytics_sync
        fno_data = fetch_fno_analytics_sync(fno_sym, spot=spot_close)
    except Exception as exc:
        log.debug("[DerivativesOI] FNO client lookup failed: %s", exc)

    envelope_src = str(fno_data.get("envelope_source") or "ATR_FALLBACK")
    backed_sources = {"BOX_C_STREAM", "BOX_C_STALE", "BOX_C_CACHED", "SHOONYA_OPTIONS_CHAIN"}
    raw_pcr = fno_data.get("pcr_oi")
    if envelope_src not in backed_sources or raw_pcr is None or float(raw_pcr) <= 0:
        return {
            "status": "unavailable",
            "oi_available": False,
            "source": envelope_src,
            "reason": "ATR_FALLBACK (no live NFO OI feed configured)",
            "index_symbol": index_symbol,
            "last": None,
            "put_call_ratio": None,
            "pcr": None,
            "interpretation": "OI_UNAVAILABLE",
            "pcr_sentiment": "OI_UNAVAILABLE",
            "pcr_score_modifier": 0,
            "index_price_change_pct": price_change,
            "index_oi_change_pct": None,
            "oi_buildup_classification": "OI_UNAVAILABLE",
            "is_short_buildup": False,
            "is_long_buildup": False,
            "ce_wall": fno_data.get("ce_wall"),
            "pe_wall": fno_data.get("pe_wall"),
            "max_pain": fno_data.get("max_pain"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    pcr = float(raw_pcr)
    total_oi = int(fno_data.get("total_oi") or 0)
    if total_oi <= 0:
        total_oi = int(fno_data.get("total_ce_oi") or 0) + int(fno_data.get("total_pe_oi") or 0)

    prev_oi = _prev_total_oi_by_symbol.get(fno_sym, 0)
    if total_oi > 0 and prev_oi > 0:
        oi_change = round(((total_oi - prev_oi) / prev_oi) * 100.0, 2)
    else:
        oi_change = 0.0
    if total_oi > 0:
        _prev_total_oi_by_symbol[fno_sym] = total_oi

    buildup = classify_oi_buildup(price_change, oi_change)
    pcr_interp, pcr_score = interpret_pcr(pcr)

    return {
        "status": "ok",
        "oi_available": True,
        "source": envelope_src,
        "index_symbol": index_symbol,
        "last": round(pcr, 2),
        "put_call_ratio": round(pcr, 2),
        "pcr": round(pcr, 2),
        "interpretation": pcr_interp,
        "pcr_sentiment": pcr_interp,
        "pcr_score_modifier": pcr_score,
        "index_price_change_pct": price_change,
        "index_oi_change_pct": oi_change,
        "oi_buildup_classification": buildup,
        "is_short_buildup": buildup == "SHORT_BUILDUP",
        "is_long_buildup": buildup == "LONG_BUILDUP",
        "ce_wall": fno_data.get("ce_wall"),
        "pe_wall": fno_data.get("pe_wall"),
        "max_pain": fno_data.get("max_pain"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_derivatives_intelligence(index_symbol: str = "NIFTY", force_refresh: bool = False) -> dict[str, Any]:
    """Get derivatives open interest and PCR intelligence asynchronously with TTL cache."""
    global _oi_cache, _last_oi_fetch_ts
    now = time.time()
    if not force_refresh and _oi_cache and (now - _last_oi_fetch_ts < _CACHE_TTL_SECONDS):
        return dict(_oi_cache)

    try:
        data = await asyncio.to_thread(_fetch_derivatives_sync, index_symbol)
        _oi_cache = data
        _last_oi_fetch_ts = now
        return dict(_oi_cache)
    except Exception as exc:
        log.warning("[DerivativesOI] Async fetch error: %s", exc)
        if _oi_cache:
            stale = dict(_oi_cache)
            stale["stale"] = True
            return stale
        return {
            "status": "unavailable",
            "oi_available": False,
            "source": "UNAVAILABLE",
            "reason": str(exc)[:100],
            "index_symbol": index_symbol,
            "last": None,
            "put_call_ratio": None,
            "pcr": None,
            "interpretation": "OI_UNAVAILABLE",
            "pcr_sentiment": "OI_UNAVAILABLE",
            "pcr_score_modifier": 0,
            "index_price_change_pct": 0.0,
            "index_oi_change_pct": None,
            "oi_buildup_classification": "OI_UNAVAILABLE",
            "is_short_buildup": False,
            "is_long_buildup": False,
            "stale": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

