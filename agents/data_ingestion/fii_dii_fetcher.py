"""
FII / DII Institutional Flow Engine — TradeAI-PRO Data Brain V3
Calculates 1-day net, 5-day rolling momentum sum, and institutional context score (-10 to +10)
from official NSE reports.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_fii_dii_cache: dict[str, Any] = {}
_cache_timestamp: float = 0.0
_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes cache during daytime, updates EOD


def _compute_flow_score(fii_5d: float, dii_5d: float, fii_1d: float, dii_1d: float) -> int:
    """
    Compute normalized institutional flow score between -10 and +10.
    FII flow carries 60% weight, DII carries 40% weight.
    Scale: every +/- 1000 Cr of 5-day flow adds approx +/- 1.5 points.
    """
    # 5-day momentum component
    fii_component = (fii_5d / 1500.0) * 5.0
    dii_component = (dii_5d / 1500.0) * 3.0
    # 1-day shock component
    shock_component = ((fii_1d + dii_1d) / 2000.0) * 2.0

    raw_score = fii_component + dii_component + shock_component
    score = int(max(-10, min(10, round(raw_score))))
    return score


def _fetch_fii_dii_sync() -> dict[str, Any]:
    """Synchronous fetcher for FII/DII data using existing market_data module."""
    try:
        from agents.market_intelligence.market_data import fetch_fii_dii_flow, _load_tf_history

        flow_data = fetch_fii_dii_flow()
        fii_1d = 0.0
        dii_1d = 0.0

        for f in flow_data.get("flows", []):
            cat = f.get("category", "")
            net = float(f.get("net_cr", 0.0) or 0.0)
            if "FII" in cat:
                fii_1d = net
            elif "DII" in cat:
                dii_1d = net

        # Rolling 5-day sum from persisted history
        history = _load_tf_history()
        fii_5d = fii_1d
        dii_5d = dii_1d

        if history and isinstance(history, list):
            recent = history[-5:]
            fii_5d = sum(float(h.get("fii_net_cr", 0.0) or 0.0) for h in recent)
            dii_5d = sum(float(h.get("dii_net_cr", 0.0) or 0.0) for h in recent)

        flow_score = _compute_flow_score(fii_5d, dii_5d, fii_1d, dii_1d)

        return {
            "fii_net_1d": round(fii_1d, 2),
            "dii_net_1d": round(dii_1d, 2),
            "fii_5d_sum": round(fii_5d, 2),
            "dii_5d_sum": round(dii_5d, 2),
            "institutional_flow_score": flow_score,
            "sentiment": flow_data.get("sentiment", "NEUTRAL"),
            "trade_date": flow_data.get("trade_date"),
            "source": flow_data.get("source", "NSE_Official"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        log.warning("[FIIDIIFetcher] Sync fetch failed: %s", exc)
        return {
            "fii_net_1d": 0.0,
            "dii_net_1d": 0.0,
            "fii_5d_sum": 0.0,
            "dii_5d_sum": 0.0,
            "institutional_flow_score": 0,
            "sentiment": "NEUTRAL",
            "source": "fallback",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


async def get_fii_dii_metrics(force_refresh: bool = False) -> dict[str, Any]:
    """Get FII/DII metrics asynchronously with bounded in-memory caching."""
    global _fii_dii_cache, _cache_timestamp
    now = time.time()
    if not force_refresh and _fii_dii_cache and (now - _cache_timestamp < _CACHE_TTL_SECONDS):
        return dict(_fii_dii_cache)

    try:
        data = await asyncio.to_thread(_fetch_fii_dii_sync)
        _fii_dii_cache = data
        _cache_timestamp = now
        return dict(_fii_dii_cache)
    except Exception as exc:
        log.warning("[FIIDIIFetcher] Async fetch failed: %s", exc)
        if _fii_dii_cache:
            stale = dict(_fii_dii_cache)
            stale["stale"] = True
            return stale
        return {
            "fii_net_1d": 0.0,
            "dii_net_1d": 0.0,
            "fii_5d_sum": 0.0,
            "dii_5d_sum": 0.0,
            "institutional_flow_score": 0,
            "sentiment": "NEUTRAL",
            "stale": True,
            "source": "fallback",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
