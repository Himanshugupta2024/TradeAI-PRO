"""
TradeAI Pro — Client for Dedicated ML Inference Sidecar (Box B).
Serializes trailing OHLCV bars using pandas JSON split format to preserve exact
types and naive-IST datetime semantics without network data fetching on Box B.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

ML_SIDECAR_URL = os.environ.get("ML_SIDECAR_URL", "").rstrip("/")
ML_INTERNAL_TOKEN = os.environ.get("ML_INTERNAL_TOKEN", "")
# Tight timeout to protect caller threads from event loop starvation
ML_SIDECAR_TIMEOUT_SEC = float(os.environ.get("ML_SIDECAR_TIMEOUT_SEC", "6.0"))
ML_SIDECAR_COOLDOWN_SEC = float(os.environ.get("ML_SIDECAR_COOLDOWN_SEC", "60.0"))
ML_SIDECAR_FAILURES_TO_TRIP = int(os.environ.get("ML_SIDECAR_FAILURES_TO_TRIP", "2"))

# Persistent HTTP clients with connection pooling to eliminate TCP/TLS handshake latency
_LIMITS = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=30.0)
_sync_client: Optional[httpx.Client] = None
_async_client: Optional[httpx.AsyncClient] = None
_last_warn_time: float = 0.0
_consecutive_failures: int = 0
_cooldown_until: float = 0.0


def reset_ml_sidecar_state() -> None:
    """Reset in-process ML sidecar circuit breaker and warn throttle state."""
    global _last_warn_time, _consecutive_failures, _cooldown_until
    _last_warn_time = 0.0
    _consecutive_failures = 0
    _cooldown_until = 0.0


def _is_circuit_open() -> bool:
    return time.monotonic() < _cooldown_until


def _record_sidecar_success() -> None:
    global _consecutive_failures, _cooldown_until
    _consecutive_failures = 0
    _cooldown_until = 0.0


def _record_sidecar_failure(symbol: str, exc: Exception) -> None:
    global _consecutive_failures, _cooldown_until
    _consecutive_failures += 1
    threshold = max(1, ML_SIDECAR_FAILURES_TO_TRIP)
    if _consecutive_failures >= threshold:
        _cooldown_until = time.monotonic() + max(1.0, ML_SIDECAR_COOLDOWN_SEC)
    _log_sidecar_failure(symbol, exc)


def _get_sync_client(timeout: float) -> httpx.Client:
    global _sync_client
    if _sync_client is None or _sync_client.is_closed:
        _sync_client = httpx.Client(timeout=timeout, limits=_LIMITS)
    return _sync_client


def _get_async_client(timeout: float) -> httpx.AsyncClient:
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(timeout=timeout, limits=_LIMITS)
    return _async_client


def _log_sidecar_failure(symbol: str, exc: Exception) -> None:
    """Rate-limited warning logging (max once per 60s) so sidecar outages stay visible."""
    global _last_warn_time
    now = time.monotonic()
    if now - _last_warn_time > 60.0:
        logger.warning("[ML-Sidecar] Remote inference failed for %s (throttled 60s): %s", symbol, exc)
        _last_warn_time = now
    else:
        logger.debug("[ML-Sidecar] Remote inference failed for %s: %s", symbol, exc)


def serialize_df(df: pd.DataFrame, max_rows: int = 250) -> str:
    """
    Serialize trailing bars of a DataFrame to JSON using orient='split'.
    Defaults to 250 rows to preserve EMA-200, MACD, and long-window feature validity.
    """
    subset = df.tail(max_rows)
    return subset.to_json(orient="split", date_format="iso")


def deserialize_df(df_json: str) -> pd.DataFrame:
    """Reconstruct DataFrame from split JSON format."""
    return pd.read_json(io.StringIO(df_json), orient="split")


def _build_infer_request(symbol: str, df: pd.DataFrame, timeout: Optional[float]) -> Optional[tuple[str, dict[str, Any], dict[str, str], float]]:
    if os.environ.get("SERVICE_ROLE") == "ml_sidecar":
        return None  # Recursion guard: Box B must never call itself
    url = os.environ.get("ML_SIDECAR_URL", ML_SIDECAR_URL).rstrip("/")
    if not url or _is_circuit_open():
        return None
    to = timeout or ML_SIDECAR_TIMEOUT_SEC
    token = os.environ.get("ML_INTERNAL_TOKEN", ML_INTERNAL_TOKEN)
    headers = {"X-Internal-Token": token} if token else {}
    payload = {
        "symbol": symbol,
        "df_json": serialize_df(df),
        "offline": True,
        "force_refresh": False,
    }
    return f"{url}/infer", payload, headers, to


def _parse_infer_response(symbol: str, resp: httpx.Response) -> tuple[Optional[Dict[str, Any]], bool]:
    """Return (result_or_none, should_retry_transient)."""
    if resp.status_code == 200:
        data = resp.json()
        if isinstance(data, dict):
            data["source"] = "ML_SIDECAR"
            _record_sidecar_success()
            return data, False
    if resp.status_code in (502, 503):
        return None, True
    logger.debug("ML sidecar responded %s for %s: %s", resp.status_code, symbol, resp.text)
    if resp.status_code >= 500:
        _record_sidecar_failure(symbol, RuntimeError(f"HTTP {resp.status_code}"))
    return None, False


def infer_remote_sync(
    symbol: str,
    df: pd.DataFrame,
    timeout: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Synchronous HTTP call to the ML Sidecar with wake retry and cooldown circuit breaker."""
    try:
        req = _build_infer_request(symbol, df, timeout)
    except Exception as exc:
        _record_sidecar_failure(symbol, exc)
        return None
    if req is None:
        return None
    endpoint, payload, headers, to = req

    client = _get_sync_client(to)
    for attempt in range(2):
        try:
            resp = client.post(endpoint, json=payload, headers=headers, timeout=to)
            parsed, should_retry = _parse_infer_response(symbol, resp)
            if parsed is not None or not should_retry:
                return parsed
            if attempt == 0:
                time.sleep(0.4)
                continue
            _record_sidecar_failure(symbol, RuntimeError(f"HTTP {resp.status_code}"))
            return None
        except httpx.ConnectError as exc:
            if attempt == 0:
                time.sleep(0.4)
                continue
            _record_sidecar_failure(symbol, exc)
            return None
        except Exception as exc:
            _record_sidecar_failure(symbol, exc)
            return None
    return None


async def infer_remote_async(
    symbol: str,
    df: pd.DataFrame,
    timeout: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Asynchronous HTTP call to the ML Sidecar with wake retry and cooldown circuit breaker."""
    try:
        req = _build_infer_request(symbol, df, timeout)
    except Exception as exc:
        _record_sidecar_failure(symbol, exc)
        return None
    if req is None:
        return None
    endpoint, payload, headers, to = req

    client = _get_async_client(to)
    for attempt in range(2):
        try:
            resp = await client.post(endpoint, json=payload, headers=headers, timeout=to)
            parsed, should_retry = _parse_infer_response(symbol, resp)
            if parsed is not None or not should_retry:
                return parsed
            if attempt == 0:
                await asyncio.sleep(0.4)
                continue
            _record_sidecar_failure(symbol, RuntimeError(f"HTTP {resp.status_code}"))
            return None
        except httpx.ConnectError as exc:
            if attempt == 0:
                await asyncio.sleep(0.4)
                continue
            _record_sidecar_failure(symbol, exc)
            return None
        except Exception as exc:
            _record_sidecar_failure(symbol, exc)
            return None
    return None
