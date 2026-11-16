"""
TradeAI Pro — Client for Dedicated ML Inference Sidecar (Box B).
Serializes trailing OHLCV bars using pandas JSON split format to preserve exact
types and naive-IST datetime semantics without network data fetching on Box B.
"""
from __future__ import annotations

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

# Persistent HTTP clients with connection pooling to eliminate TCP/TLS handshake latency
_LIMITS = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=30.0)
_sync_client: Optional[httpx.Client] = None
_async_client: Optional[httpx.AsyncClient] = None
_last_warn_time: float = 0.0


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


def infer_remote_sync(
    symbol: str,
    df: pd.DataFrame,
    timeout: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Synchronous HTTP call to the ML Sidecar."""
    url = os.environ.get("ML_SIDECAR_URL", ML_SIDECAR_URL).rstrip("/")
    if not url:
        return None

    to = timeout or ML_SIDECAR_TIMEOUT_SEC
    token = os.environ.get("ML_INTERNAL_TOKEN", ML_INTERNAL_TOKEN)
    headers = {"X-Internal-Token": token} if token else {}

    try:
        df_json = serialize_df(df)
        payload = {
            "symbol": symbol,
            "df_json": df_json,
            "offline": True,
            "force_refresh": False,
        }
        client = _get_sync_client(to)
        resp = client.post(f"{url}/infer", json=payload, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                data["source"] = "ML_SIDECAR"
                return data
        logger.debug("ML sidecar responded %s: %s", resp.status_code, resp.text)
        return None
    except Exception as exc:
        _log_sidecar_failure(symbol, exc)
        return None


async def infer_remote_async(
    symbol: str,
    df: pd.DataFrame,
    timeout: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Asynchronous HTTP call to the ML Sidecar."""
    url = os.environ.get("ML_SIDECAR_URL", ML_SIDECAR_URL).rstrip("/")
    if not url:
        return None

    to = timeout or ML_SIDECAR_TIMEOUT_SEC
    token = os.environ.get("ML_INTERNAL_TOKEN", ML_INTERNAL_TOKEN)
    headers = {"X-Internal-Token": token} if token else {}

    try:
        df_json = serialize_df(df)
        payload = {
            "symbol": symbol,
            "df_json": df_json,
            "offline": True,
            "force_refresh": False,
        }
        client = _get_async_client(to)
        resp = await client.post(f"{url}/infer", json=payload, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                data["source"] = "ML_SIDECAR"
                return data
        logger.debug("ML sidecar async responded %s: %s", resp.status_code, resp.text)
        return None
    except Exception as exc:
        _log_sidecar_failure(symbol, exc)
        return None
