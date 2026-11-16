"""
TradeAI Pro — Client for Dedicated ML Inference Sidecar (Box B).
Serializes trailing OHLCV bars using pandas JSON split format to preserve exact
types and naive-IST datetime semantics without network data fetching on Box B.
"""
from __future__ import annotations

import io
import logging
import os
from typing import Any, Dict, Optional

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

ML_SIDECAR_URL = os.environ.get("ML_SIDECAR_URL", "").rstrip("/")
ML_INTERNAL_TOKEN = os.environ.get("ML_INTERNAL_TOKEN", "")
ML_SIDECAR_TIMEOUT_SEC = float(os.environ.get("ML_SIDECAR_TIMEOUT_SEC", "12.0"))


def serialize_df(df: pd.DataFrame, max_rows: int = 60) -> str:
    """Serialize the trailing bars of a DataFrame to JSON using orient='split'."""
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
        with httpx.Client(timeout=to) as client:
            resp = client.post(f"{url}/infer", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    data["source"] = "ML_SIDECAR"
                    return data
            logger.debug("ML sidecar responded %s: %s", resp.status_code, resp.text)
            return None
    except Exception as exc:
        logger.debug("ML sidecar sync call failed for %s: %s", symbol, exc)
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
        async with httpx.AsyncClient(timeout=to) as client:
            resp = await client.post(f"{url}/infer", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    data["source"] = "ML_SIDECAR"
                    return data
            logger.debug("ML sidecar async responded %s: %s", resp.status_code, resp.text)
            return None
    except Exception as exc:
        logger.debug("ML sidecar async call failed for %s: %s", symbol, exc)
        return None
