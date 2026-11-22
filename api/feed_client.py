"""
TradeAI Pro — Client for Dedicated Market Data Feed Service (Box C).
Fetches real-time LTP snapshots and 5-minute rolling bars from Box C,
allowing Box A to completely bypass heavy Yahoo Finance network scraping.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

FEED_SERVICE_URL = os.environ.get("FEED_SERVICE_URL", "").rstrip("/")
FEED_INTERNAL_TOKEN = os.environ.get("FEED_INTERNAL_TOKEN", "")
FEED_TIMEOUT_SEC = float(os.environ.get("FEED_TIMEOUT_SEC", "3.0"))

# Persistent HTTP client with connection pooling
_LIMITS = httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=30.0)
_async_client: Optional[httpx.AsyncClient] = None
_sync_client: Optional[httpx.Client] = None
_last_warn_time: float = 0.0


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


def is_feed_configured() -> bool:
    """Check if FEED_SERVICE_URL is set in environment."""
    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).strip()
    return bool(url)


async def fetch_feed_snapshot(
    symbols: List[str],
    timeout: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Fetch live quote snapshots and 5-minute bars from Box C.
    Returns None on network failure, timeout, or missing configuration.
    """
    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).rstrip("/")
    if not url or not symbols:
        return None

    to = timeout or FEED_TIMEOUT_SEC
    token = os.environ.get("FEED_INTERNAL_TOKEN", FEED_INTERNAL_TOKEN)
    headers = {"X-Internal-Token": token} if token else {}

    try:
        client = _get_async_client(to)
        resp = await client.get(
            f"{url}/snapshot",
            params={"symbols": ",".join(symbols)},
            headers=headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
        logger.debug("[FeedClient] Feed returned status %s: %s", resp.status_code, resp.text)
        return None
    except Exception as exc:
        _log_feed_failure(exc)
        return None


def fetch_feed_snapshot_sync(
    symbols: List[str],
    timeout: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Synchronous version for threads and synchronous endpoints."""
    url = os.environ.get("FEED_SERVICE_URL", FEED_SERVICE_URL).rstrip("/")
    if not url or not symbols:
        return None

    to = timeout or FEED_TIMEOUT_SEC
    token = os.environ.get("FEED_INTERNAL_TOKEN", FEED_INTERNAL_TOKEN)
    headers = {"X-Internal-Token": token} if token else {}

    try:
        client = _get_sync_client(to)
        resp = client.get(
            f"{url}/snapshot",
            params={"symbols": ",".join(symbols)},
            headers=headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
        return None
    except Exception as exc:
        _log_feed_failure(exc)
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
