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


def compute_as_of_timestamp(trade_date_str: str | None, hour: int = 18, minute: int = 0) -> str:
    """Compute official publication as_of timestamp in ISO format (Asia/Kolkata timezone)."""
    try:
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
    except Exception:
        ist = timezone.utc

    if not trade_date_str:
        return datetime.now(timezone.utc).isoformat()

    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(str(trade_date_str).strip(), fmt)
            return dt.replace(hour=hour, minute=minute, second=0, tzinfo=ist).isoformat()
        except Exception:
            continue
    return datetime.now(timezone.utc).isoformat()


def is_published_as_of(as_of_iso: str | None, current_time: datetime | None = None) -> bool:
    """Return False if current_time is strictly before the publication timestamp (prevents look-ahead)."""
    if not as_of_iso:
        return True
    try:
        as_of_dt = datetime.fromisoformat(as_of_iso)
        if current_time is None:
            current_time = datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        if as_of_dt.tzinfo is None:
            as_of_dt = as_of_dt.replace(tzinfo=timezone.utc)
        return current_time >= as_of_dt
    except Exception:
        return True


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
        as_of_iso = compute_as_of_timestamp(flow_data.get("trade_date") or flow_data.get("date"))

        return {
            "fii_net_1d": round(fii_1d, 2),
            "dii_net_1d": round(dii_1d, 2),
            "fii_5d_sum": round(fii_5d, 2),
            "dii_5d_sum": round(dii_5d, 2),
            "institutional_flow_score": flow_score,
            "sentiment": flow_data.get("sentiment", "NEUTRAL"),
            "trade_date": flow_data.get("trade_date"),
            "as_of": as_of_iso,
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
            "as_of": datetime.now(timezone.utc).isoformat(),
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

