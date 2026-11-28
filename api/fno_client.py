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
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

FEED_SERVICE_URL = os.environ.get("FEED_SERVICE_URL", "").rstrip("/")
FEED_INTERNAL_TOKEN = os.environ.get("FEED_INTERNAL_TOKEN", "")
FNO_TIMEOUT_SEC = float(os.environ.get("FNO_TIMEOUT_SEC", "2.5"))
FNO_CACHE_TTL_SEC = 30.0

# In-memory TTL cache: symbol -> (timestamp, data_dict)
_FNO_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_UNDERLYINGS_CACHE: Dict[str, Any] = {"ts": 0.0, "items": []}
_PERSIST_THROTTLE: Dict[str, float] = {}
_async_client: Optional[httpx.AsyncClient] = None
_sync_client: Optional[httpx.Client] = None
_FNO_SNAPSHOT_STATE_KEY = "fno_snapshots:v1"


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


# Fallback spot price anchors for major index & stock underlyings when spot is unprovided
_DEFAULT_SPOT_ANCHORS: Dict[str, float] = {
    "NIFTY 50": 25885.0,
    "NIFTY BANK": 53000.0,
    "RELIANCE": 1396.0,
    "TCS": 2640.0,
    "INFY": 1183.0,
    "HDFCBANK": 862.0,
    "ICICIBANK": 1290.0,
    "SBIN": 840.0,
    "BHARTIARTL": 1620.0,
    "ITC": 480.0,
    "KOTAKBANK": 1760.0,
    "LT": 3600.0,
}


# Fallback strike steps for options strike roundoff
_STRIKE_STEPS: Dict[str, float] = {
    "NIFTY 50": 50.0,
    "NIFTY BANK": 100.0,
    "RELIANCE": 20.0,
    "TCS": 20.0,
    "HDFCBANK": 10.0,
    "INFY": 10.0,
    "ICICIBANK": 10.0,
    "SBIN": 5.0,
    "BHARTIARTL": 10.0,
    "ITC": 5.0,
    "KOTAKBANK": 10.0,
    "LT": 20.0,
}


def _build_atr_fallback(symbol: str, spot: float, atr: float) -> Dict[str, Any]:
    """Graceful fallback when Box C derivatives feed is unavailable."""
    sym_clean = symbol.strip().upper().replace(".NS", "")
    clean_spot = float(spot) if spot > 0 else _DEFAULT_SPOT_ANCHORS.get(sym_clean, 100.0)
    clean_atr = float(atr) if atr > 0 else (clean_spot * 0.015)
    move_1d = round((clean_atr / clean_spot) * 100.0, 2)
    move_5d = round(move_1d * math.sqrt(5.0), 2)
    step = _STRIKE_STEPS.get(sym_clean, 10.0)

    atm_strike = round(clean_spot / step) * step
    reach_val = clean_atr * 2.24
    ce_wall = round((clean_spot + reach_val) / step) * step
    pe_wall = round((clean_spot - reach_val) / step) * step
    straddle = round(clean_spot * (move_1d / 100.0), 2)

    return {
        "symbol": sym_clean,
        "spot_price": round(clean_spot, 2),
        "ce_wall": None,
        "pe_wall": None,
        "max_pain": None,
        "pcr_oi": None,
        "pcr_volume": None,
        "atm_strike": atm_strike,
        "straddle_price": straddle,
        "straddle_iv": round(move_1d * math.sqrt(252.0) / 100.0, 4),
        "implied_move_1d_pct": move_1d,
        "implied_move_5d_pct": move_5d,
        "envelope_source": "ATR_FALLBACK",
        "days_to_expiry": 5.0,
    }


def _persist_fno_snapshot_sync(sym: str, data: Dict[str, Any]) -> None:
    """Persist verified Box C F&O snapshot to Supabase runtime_state (throttled per symbol)."""
    src = str(data.get("envelope_source") or "")
    if src not in ("BOX_C_STREAM", "SHOONYA_OPTIONS_CHAIN") or (data.get("ce_wall") is None and data.get("pe_wall") is None):
        return
    now = time.monotonic()
    if now - _PERSIST_THROTTLE.get(sym, 0.0) < 60.0:
        return
    _PERSIST_THROTTLE[sym] = now
    try:
        from db.runtime_state import get_runtime_state, set_runtime_state
        from datetime import datetime, timezone
        store = get_runtime_state(_FNO_SNAPSHOT_STATE_KEY)
        if not isinstance(store, dict):
            store = {}
        snap_copy = dict(data)
        snap_copy["persisted_at"] = datetime.now(timezone.utc).isoformat()
        store[sym] = snap_copy
        # Keep snapshot dictionary bounded to <= 220 underlyings
        if len(store) > 220:
            oldest_key = next(iter(store))
            if oldest_key != sym:
                store.pop(oldest_key, None)
        set_runtime_state(_FNO_SNAPSHOT_STATE_KEY, store)
    except Exception as exc:
        logger.debug("[FNOClient] Persist snapshot skipped for %s: %s", sym, exc)


def _load_cached_fno_snapshot_sync(sym: str, spot: float, atr: float) -> Optional[Dict[str, Any]]:
    """Return last persisted real Box C F&O snapshot when Box C is sleeping or off-hours."""
    try:
        from db.runtime_state import get_runtime_state
        store = get_runtime_state(_FNO_SNAPSHOT_STATE_KEY)
        if not isinstance(store, dict):
            return None
        cached = store.get(sym)
        if not isinstance(cached, dict):
            return None
        if cached.get("ce_wall") is None and cached.get("pe_wall") is None:
            return None
        out = dict(cached)
        if spot > 0:
            out["spot_price"] = round(float(spot), 2)
        out["envelope_source"] = "BOX_C_CACHED"
        return out
    except Exception:
        return None


def _apply_atr_fallback_if_unbacked(
    data: Dict[str, Any],
    sym: str,
    spot: float,
    atr: float,
) -> Dict[str, Any]:
    """Ensure unbacked Box C responses never claim BOX_C_MODEL provenance."""
    if data.get("envelope_source") == "NO_CHAIN_DATA" or (
        data.get("ce_wall") is None and data.get("pe_wall") is None
    ):
        cached = _load_cached_fno_snapshot_sync(sym, spot, atr)
        if cached is not None:
            data.clear()
            data.update(cached)
            return data
        fb = _build_atr_fallback(sym, spot, atr)
        data.update({
            "ce_wall": fb["ce_wall"],
            "pe_wall": fb["pe_wall"],
            "atm_strike": fb["atm_strike"],
            "straddle_price": fb["straddle_price"],
            "straddle_iv": fb["straddle_iv"],
            "max_pain": fb["max_pain"],
            "pcr_oi": data.get("pcr_oi") or fb["pcr_oi"],
            "pcr_volume": data.get("pcr_volume") or fb["pcr_volume"],
            "implied_move_1d_pct": data.get("implied_move_1d_pct") or fb["implied_move_1d_pct"],
            "implied_move_5d_pct": data.get("implied_move_5d_pct") or fb["implied_move_5d_pct"],
            "envelope_source": "ATR_FALLBACK",
        })
    return data


async def fetch_fno_analytics(
    symbol: str,
    spot: float,
    atr: float = 0.0,
    days_to_expiry: float = 5.0,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Fetch live F&O analytics for symbol with 30s TTL caching.
    Falls back cleanly to persisted BOX_C_CACHED snapshot, then ATR envelope.
    """
    import asyncio
    sym = symbol.strip().upper().replace(".NS", "")
    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).rstrip("/")
    if not url:
        cached = await asyncio.to_thread(_load_cached_fno_snapshot_sync, sym, spot, atr)
        return cached if cached is not None else _build_atr_fallback(sym, spot, atr)

    now = time.monotonic()
    cache_key = f"{sym}|{url}|{round(float(spot or 0.0), 2)}"

    # Check cache
    if cache_key in _FNO_CACHE:
        cached_ts, cached_data = _FNO_CACHE[cache_key]
        if (now - cached_ts) < FNO_CACHE_TTL_SEC:
            return cached_data

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
                _apply_atr_fallback_if_unbacked(data, sym, spot, atr)
                if data.get("envelope_source") in ("BOX_C_STREAM", "SHOONYA_OPTIONS_CHAIN"):
                    await asyncio.to_thread(_persist_fno_snapshot_sync, sym, data)
                _FNO_CACHE[cache_key] = (now, data)
                return data
    except Exception as exc:
        logger.debug("[FNOClient] Call failed for %s, using fallback: %s", sym, exc)

    cached = await asyncio.to_thread(_load_cached_fno_snapshot_sync, sym, spot, atr)
    fallback = cached if cached is not None else _build_atr_fallback(sym, spot, atr)
    _FNO_CACHE[cache_key] = (now, fallback)
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
    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).rstrip("/")
    if not url:
        cached = _load_cached_fno_snapshot_sync(sym, spot, atr)
        return cached if cached is not None else _build_atr_fallback(sym, spot, atr)

    now = time.monotonic()
    cache_key = f"{sym}|{url}|{round(float(spot or 0.0), 2)}"

    if cache_key in _FNO_CACHE:
        cached_ts, cached_data = _FNO_CACHE[cache_key]
        if (now - cached_ts) < FNO_CACHE_TTL_SEC:
            return cached_data

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
                _apply_atr_fallback_if_unbacked(data, sym, spot, atr)
                if data.get("envelope_source") in ("BOX_C_STREAM", "SHOONYA_OPTIONS_CHAIN"):
                    _persist_fno_snapshot_sync(sym, data)
                _FNO_CACHE[cache_key] = (now, data)
                return data
    except Exception as exc:
        logger.debug("[FNOClient] Sync call failed for %s, using fallback: %s", sym, exc)

    cached = _load_cached_fno_snapshot_sync(sym, spot, atr)
    fallback = cached if cached is not None else _build_atr_fallback(sym, spot, atr)
    _FNO_CACHE[cache_key] = (now, fallback)
    return fallback


async def fetch_fno_underlyings() -> list[str]:
    """Return full F&O underlying universe from Box C, with equity fallback."""
    now = time.monotonic()
    if _UNDERLYINGS_CACHE["items"] and (now - float(_UNDERLYINGS_CACHE["ts"])) < 300.0:
        return list(_UNDERLYINGS_CACHE["items"])

    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).rstrip("/")
    token = os.environ.get("FEED_INTERNAL_TOKEN", FEED_INTERNAL_TOKEN)
    headers = {"X-Internal-Token": token} if token else {}
    if url:
        try:
            client = _get_async_client(max(2.5, FNO_TIMEOUT_SEC))
            resp = await client.get(f"{url}/fno/underlyings", headers=headers)
            if resp.status_code == 200:
                payload = resp.json()
                items = payload.get("underlyings") if isinstance(payload, dict) else None
                if isinstance(items, list) and items:
                    _UNDERLYINGS_CACHE["ts"] = now
                    _UNDERLYINGS_CACHE["items"] = items
                    return list(items)
        except Exception as exc:
            logger.debug("[FNOClient] Underlyings lookup skipped: %s", exc)

    core = ["NIFTY 50", "NIFTY BANK"]
    try:
        from config import settings
        nse_keys = [
            str(s).upper()
            for s in (settings.NSE_SYMBOLS.keys() if isinstance(settings.NSE_SYMBOLS, dict) else settings.NSE_SYMBOLS)
            if str(s).upper() not in ("NIFTY 50", "BANKNIFTY", "SENSEX", "INDIA VIX", "NIFTY BANK")
        ]
        fallback = core + nse_keys
    except Exception:
        fallback = core + list(_DEFAULT_SPOT_ANCHORS.keys())[2:]
    return fallback
