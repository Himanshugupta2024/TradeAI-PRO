"""
TradeAI Pro — Client for Dedicated Market Data Feed Service (Box C).
Fetches real-time LTP snapshots and 5-minute rolling bars from Box C,
allowing Box A to completely bypass heavy Yahoo Finance network scraping.
"""
from __future__ import annotations

import logging
import math
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

FEED_SERVICE_URL = os.environ.get("FEED_SERVICE_URL", "").rstrip("/")
FEED_INTERNAL_TOKEN = os.environ.get("FEED_INTERNAL_TOKEN", "")
FEED_TIMEOUT_SEC = float(os.environ.get("FEED_TIMEOUT_SEC", "5.0"))
MAX_SINGLE_SESSION_TICK_DEVIATION = float(os.environ.get("MAX_SINGLE_SESSION_TICK_DEVIATION", "0.20"))

# Persistent HTTP client with connection pooling
_LIMITS = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=30.0)
_async_client: Optional[httpx.AsyncClient] = None
_sync_client: Optional[httpx.Client] = None
_last_warn_time: float = 0.0
_last_sim_warn_time: float = 0.0
_suppress_shoonya_telemetry: ContextVar[bool] = ContextVar("_suppress_shoonya_telemetry", default=False)


@contextmanager
def suppress_feed_telemetry():
    """Avoid double-counting when MarketDataProvider subclasses record their own health."""
    tok = _suppress_shoonya_telemetry.set(True)
    try:
        yield
    finally:
        _suppress_shoonya_telemetry.reset(tok)


def _record_shoonya_telemetry(ok: bool, latency_ms: float = 0.0) -> None:
    if _suppress_shoonya_telemetry.get():
        return
    try:
        from data.providers.base import get_shared_provider_health
        h = get_shared_provider_health("shoonya_feed")
        if ok:
            h.record_success(latency_ms)
        else:
            h.record_failure(latency_ms=latency_ms if latency_ms > 0 else None)
    except Exception:
        pass


def _get_async_client(timeout: float) -> httpx.AsyncClient:
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(timeout=timeout, limits=_LIMITS)
    return _async_client


def _get_sync_client(timeout: float) -> httpx.Client:
    global _sync_client
    if _sync_client is None or _sync_client.is_closed:
        _sync_client = httpx.Client(timeout=timeout, limits=_LIMITS)
    return _sync_client


def _log_feed_failure(exc: Exception) -> None:
    """Rate-limited warning logging (max once per 60s) so feed outages stay visible."""
    global _last_warn_time
    now = time.monotonic()
    if now - _last_warn_time > 60.0:
        logger.warning("[FeedClient] Feed service call failed (throttled 60s): %s", exc)
        _last_warn_time = now
    else:
        logger.debug("[FeedClient] Feed service call failed: %s", exc)


def is_simulated_snapshot(snap: Any) -> bool:
    """Return True when a feed snapshot originates from Box C's simulator."""
    if not isinstance(snap, dict):
        return False
    for key in ("source", "feed_source", "feed_state"):
        val = str(snap.get(key) or "").upper()
        if "SIMULATED" in val or "SYNTHETIC" in val:
            return True
    return False


def _log_simulated_rejected(source: str) -> None:
    global _last_sim_warn_time
    now = time.monotonic()
    if now - _last_sim_warn_time > 60.0:
        logger.warning(
            "[FeedClient] Rejected simulated snapshot from Box C (source=%s) — failing over to real market feeds",
            source,
        )
        _last_sim_warn_time = now


def validate_live_tick(
    symbol: str,
    ltp: float,
    prev_close: Optional[float] = None,
    *,
    max_deviation_pct: float = MAX_SINGLE_SESSION_TICK_DEVIATION,
) -> bool:
    """
    Sanity-gate an incoming tick price against reference/previous close.
    Rejects non-finite, non-positive, or >20% single-session outlier moves
    (exceeding NSE daily circuit limits) so corrupt ticks never propagate.
    """
    try:
        price = float(ltp)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(price) or price <= 0.0:
        return False

    if prev_close is not None:
        try:
            ref = float(prev_close)
        except (TypeError, ValueError):
            ref = 0.0
        if math.isfinite(ref) and ref > 0.0:
            deviation = abs(price / ref - 1.0)
            if deviation > max_deviation_pct:
                logger.warning(
                    "[TickSanityGate] reject_tick symbol=%s ltp=%.2f ref_close=%.2f move=%+.2f%% (exceeds %.0f%% limit)",
                    symbol,
                    price,
                    ref,
                    (price / ref - 1.0) * 100.0,
                    max_deviation_pct * 100.0,
                )
                return False
    return True


def is_feed_configured() -> bool:
    """Check if FEED_SERVICE_URL is set in environment."""
    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).strip()
    return bool(url)


def _process_snapshot_response(
    resp: httpx.Response,
    latency_ms: float,
    allow_simulated: bool,
) -> Optional[Dict[str, Any]]:
    if resp.status_code == 200:
        data = resp.json()
        if isinstance(data, dict):
            if not allow_simulated and is_simulated_snapshot(data):
                _log_simulated_rejected(
                    str(data.get("source") or data.get("feed_source") or "SIMULATED")
                )
                _record_shoonya_telemetry(False, latency_ms)
                return None
            _record_shoonya_telemetry(True, latency_ms)
            return data
    logger.debug("[FeedClient] Feed returned status %s: %s", resp.status_code, resp.text)
    _record_shoonya_telemetry(False, latency_ms)
    return None


async def fetch_feed_snapshot(
    symbols: List[str],
    timeout: Optional[float] = None,
    *,
    allow_simulated: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Fetch live quote snapshots and 5-minute bars from Box C.
    Returns None on network failure, timeout, missing configuration,
    or when Box C is running in SIMULATED mode (unless allow_simulated=True).
    """
    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).rstrip("/")
    if not url or not symbols:
        return None

    to = timeout or FEED_TIMEOUT_SEC
    token = os.environ.get("FEED_INTERNAL_TOKEN", FEED_INTERNAL_TOKEN)
    headers = {"X-Internal-Token": token} if token else {}

    client = _get_async_client(to)
    last_exc: Optional[Exception] = None
    last_latency_ms = 0.0
    for attempt in range(2):
        t0 = time.perf_counter()
        try:
            resp = await client.get(
                f"{url}/snapshot",
                params={"symbols": ",".join(symbols)},
                headers=headers,
                timeout=to,
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return _process_snapshot_response(resp, latency_ms, allow_simulated)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_latency_ms = (time.perf_counter() - t0) * 1000.0
            last_exc = exc
            if attempt == 0:
                continue
        except Exception as exc:
            last_latency_ms = (time.perf_counter() - t0) * 1000.0
            _log_feed_failure(exc)
            _record_shoonya_telemetry(False, last_latency_ms)
            return None

    if last_exc is not None:
        _log_feed_failure(last_exc)
    _record_shoonya_telemetry(False, last_latency_ms)
    return None


def fetch_feed_snapshot_sync(
    symbols: List[str],
    timeout: Optional[float] = None,
    *,
    allow_simulated: bool = False,
) -> Optional[Dict[str, Any]]:
    """Synchronous version for threads and synchronous endpoints."""
    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).rstrip("/")
    if not url or not symbols:
        return None

    to = timeout or FEED_TIMEOUT_SEC
    token = os.environ.get("FEED_INTERNAL_TOKEN", FEED_INTERNAL_TOKEN)
    headers = {"X-Internal-Token": token} if token else {}

    t0 = time.perf_counter()
    try:
        client = _get_sync_client(to)
        resp = client.get(
            f"{url}/snapshot",
            params={"symbols": ",".join(symbols)},
            headers=headers,
            timeout=to,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return _process_snapshot_response(resp, latency_ms, allow_simulated)
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        _log_feed_failure(exc)
        _record_shoonya_telemetry(False, latency_ms)
        return None


def convert_feed_bars_to_df(bars: List[Dict[str, Any]]) -> Optional[pd.DataFrame]:
    """
    Convert Box C bar dictionaries into standard OHLCV DataFrame
    compatible with Box A's feature engineering and signal engines.
    """
    if not bars:
        return None

    try:
        df = pd.DataFrame(bars)
        if "t" in df.columns:
            df["timestamp"] = pd.to_datetime(df["t"])
            df.set_index("timestamp", inplace=True)
            df.drop(columns=["t"], inplace=True, errors="ignore")

        # Standardize column naming
        rename_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
        df.rename(columns=rename_map, inplace=True)

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df.sort_index(inplace=True)
        return df
    except Exception as exc:
        logger.debug("[FeedClient] Error converting bars to DataFrame: %s", exc)
        return None
