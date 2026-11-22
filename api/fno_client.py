"""
TradeAI Pro — Client for F&O Derivatives Analytics (Box A -> Box C).
Fetches option chain structural levels (CE resistance wall, PE support wall),
PCR telemetry, and ATM straddle market reachability envelope from Box C.

Fail-safe: If Box C is unreachable or times out, falls back cleanly to ATR
with envelope_source="ATR_FALLBACK" so signals never halt.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

FEED_SERVICE_URL = os.environ.get("FEED_SERVICE_URL", "").rstrip("/")
FEED_INTERNAL_TOKEN = os.environ.get("FEED_INTERNAL_TOKEN", "")
FNO_TIMEOUT_SEC = float(os.environ.get("FNO_TIMEOUT_SEC", "2.5"))
FNO_CACHE_TTL_SEC = 30.0

# In-memory TTL cache: symbol -> (timestamp, data_dict)
_FNO_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_async_client: Optional[httpx.AsyncClient] = None
_sync_client: Optional[httpx.Client] = None


def _get_async_client(timeout: float) -> httpx.AsyncClient:
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(timeout=timeout)
    return _async_client


def _get_sync_client(timeout: float) -> httpx.Client:
    global _sync_client
    if _sync_client is None or _sync_client.is_closed:
        _sync_client = httpx.Client(timeout=timeout)
    return _sync_client


def _build_atr_fallback(symbol: str, spot: float, atr: float) -> Dict[str, Any]:
    """Graceful fallback when Box C derivatives feed is unavailable."""
    clean_spot = float(spot) if spot > 0 else 100.0
    clean_atr = float(atr) if atr > 0 else (clean_spot * 0.015)
    move_1d = round((clean_atr / clean_spot) * 100.0, 2)
    move_5d = round(move_1d * math.sqrt(5.0), 2)

    return {
        "symbol": symbol.strip().upper().replace(".NS", ""),
        "spot_price": round(clean_spot, 2),
        "ce_wall": None,
        "pe_wall": None,
        "max_pain": None,
        "pcr_oi": None,
        "pcr_volume": None,
        "atm_strike": None,
        "straddle_price": None,
        "straddle_iv": None,
        "implied_move_1d_pct": move_1d,
        "implied_move_5d_pct": move_5d,
        "envelope_source": "ATR_FALLBACK",
        "days_to_expiry": 5.0,
    }


async def fetch_fno_analytics(
    symbol: str,
    spot: float,
    atr: float = 0.0,
    days_to_expiry: float = 5.0,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Fetch live F&O analytics for symbol with 30s TTL caching.
    Falls back to ATR envelope on failure.
    """
    sym = symbol.strip().upper().replace(".NS", "")
    now = time.monotonic()

    # Check cache
    if sym in _FNO_CACHE:
        cached_ts, cached_data = _FNO_CACHE[sym]
        if (now - cached_ts) < FNO_CACHE_TTL_SEC:
            return cached_data

    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).rstrip("/")
    if not url:
        return _build_atr_fallback(sym, spot, atr)

    to = timeout or FNO_TIMEOUT_SEC
    token = os.environ.get("FEED_INTERNAL_TOKEN", FEED_INTERNAL_TOKEN)
    headers = {"X-Internal-Token": token} if token else {}

    try:
        client = _get_async_client(to)
        resp = await client.get(
            f"{url}/fno/analytics",
            params={"symbol": sym, "spot": spot, "days_to_expiry": days_to_expiry},
            headers=headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                _FNO_CACHE[sym] = (now, data)
                return data
    except Exception as exc:
        logger.debug("[FNOClient] Call failed for %s, using ATR fallback: %s", sym, exc)

    fallback = _build_atr_fallback(sym, spot, atr)
    _FNO_CACHE[sym] = (now, fallback)
    return fallback


def fetch_fno_analytics_sync(
    symbol: str,
    spot: float,
    atr: float = 0.0,
    days_to_expiry: float = 5.0,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Synchronous version for threads and sync scanning routines."""
    sym = symbol.strip().upper().replace(".NS", "")
    now = time.monotonic()

    if sym in _FNO_CACHE:
        cached_ts, cached_data = _FNO_CACHE[sym]
        if (now - cached_ts) < FNO_CACHE_TTL_SEC:
            return cached_data

    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).rstrip("/")
    if not url:
        return _build_atr_fallback(sym, spot, atr)

    to = timeout or FNO_TIMEOUT_SEC
    token = os.environ.get("FEED_INTERNAL_TOKEN", FEED_INTERNAL_TOKEN)
    headers = {"X-Internal-Token": token} if token else {}

    try:
        client = _get_sync_client(to)
        resp = client.get(
            f"{url}/fno/analytics",
            params={"symbol": sym, "spot": spot, "days_to_expiry": days_to_expiry},
            headers=headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                _FNO_CACHE[sym] = (now, data)
                return data
    except Exception as exc:
        logger.debug("[FNOClient] Sync call failed for %s, using ATR fallback: %s", sym, exc)

    fallback = _build_atr_fallback(sym, spot, atr)
    _FNO_CACHE[sym] = (now, fallback)
    return fallback
