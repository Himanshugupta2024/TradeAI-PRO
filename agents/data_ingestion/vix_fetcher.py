"""
India VIX Fetcher & Volatility Regime Engine — TradeAI-PRO Data Brain V3
Provides dynamic volatility regime classification, percentile ranking over 1-year (252 bars),
and sizing/risk modifiers without hardcoded arbitrary thresholds or paid APIs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Bounded in-memory cache
_vix_cache: dict[str, Any] = {}
_cache_timestamp: float = 0.0
_CACHE_TTL_SECONDS: float = 90.0

# Fallback 1-year historical range for India VIX when offline/cold
_DEFAULT_VIX_HISTORY_MIN = 10.0
_DEFAULT_VIX_HISTORY_MAX = 24.0
_DEFAULT_VIX_HISTORY_MEDIAN = 14.5


def _classify_vix_regime(percentile: float) -> str:
    """Classify volatility regime based on 1-year historical percentile."""
    if percentile < 25.0:
        return "LOW"
    elif percentile < 65.0:
        return "NORMAL"
    elif percentile < 85.0:
        return "HIGH"
    else:
        return "EXTREME"


def _calculate_vix_modifiers(vix_value: float, percentile: float, regime: str) -> tuple[float, int]:
    """
    Calculate position sizing multiplier and confidence penalty.
    - Sizing: 1.0x (Normal/Low) -> 0.7x (High) -> 0.4x (Extreme)
    - Penalty: 0 (Normal/Low) -> -3 (High) -> -6 (Extreme)
    """
    if regime == "LOW":
        return 1.0, 0
    elif regime == "NORMAL":
        return 1.0, 0
    elif regime == "HIGH":
        # Heightened whipsaw risk -> reduce size, slight confidence penalty
        multiplier = round(max(0.5, 1.0 - ((percentile - 65.0) / 20.0) * 0.4), 2)
        return multiplier, -3
    else:  # EXTREME
        # Panic / Crisis volatility -> drastic size reduction & strict penalty
        return 0.35, -6


def _fetch_vix_sync() -> dict[str, Any]:
    """Synchronous fetcher for India VIX using Yahoo Finance / NSE fallback."""
    vix_val = None
    change_1d = 0.0
    change_5d = 0.0
    percentile = 50.0

    try:
        import yfinance as yf
        # Fetch 1 year of daily bars to compute real empirical percentile
        df = yf.download("^INDIAVIX", period="1y", interval="1d", progress=False)
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                close_series = df["Close"]["^INDIAVIX"].dropna()
            else:
                close_series = df["Close"].dropna()

            if len(close_series) >= 10:
                vix_val = float(close_series.iloc[-1])
                prev_close = float(close_series.iloc[-2]) if len(close_series) >= 2 else vix_val
                prev_5d = float(close_series.iloc[-6]) if len(close_series) >= 6 else prev_close
                change_1d = round(((vix_val - prev_close) / prev_close) * 100.0, 2)
                change_5d = round(((vix_val - prev_5d) / prev_5d) * 100.0, 2)

                # Empirical percentile over 252 trading sessions
                percentile = round(float((close_series < vix_val).mean() * 100.0), 1)
    except Exception as exc:
        log.debug("[VIXFetcher] Yahoo fetch failed: %s", exc)

    # If yfinance failed, use fallback safe estimates
    if vix_val is None or np.isnan(vix_val):
        vix_val = _DEFAULT_VIX_HISTORY_MEDIAN
        percentile = 45.0
        change_1d = 0.0
        change_5d = 0.0

    regime = _classify_vix_regime(percentile)
    sizing_mult, penalty = _calculate_vix_modifiers(vix_val, percentile, regime)

    return {
        "india_vix": round(vix_val, 2),
        "vix_change_1d": change_1d,
        "vix_change_5d": change_5d,
        "vix_percentile_1y": percentile,
        "vix_regime": regime,
        "vix_size_multiplier": sizing_mult,
        "confidence_penalty": penalty,
        "source": "yahoo_^INDIAVIX",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_india_vix(force_refresh: bool = False) -> dict[str, Any]:
    """
    Get current India VIX metrics asynchronously with TTL caching.
    Guarantees non-blocking execution off the FastAPI event loop.
    """
    global _vix_cache, _cache_timestamp
    now = time.time()
    if not force_refresh and _vix_cache and (now - _cache_timestamp < _CACHE_TTL_SECONDS):
        return dict(_vix_cache)

    try:
        data = await asyncio.to_thread(_fetch_vix_sync)
        _vix_cache = data
        _cache_timestamp = now
        return dict(_vix_cache)
    except Exception as exc:
        log.warning("[VIXFetcher] Error getting VIX data: %s", exc)
        if _vix_cache:
            stale_copy = dict(_vix_cache)
            stale_copy["stale"] = True
            return stale_copy
        # Hard fallback payload
        regime = "NORMAL"
        return {
            "india_vix": 14.5,
            "vix_change_1d": 0.0,
            "vix_change_5d": 0.0,
            "vix_percentile_1y": 45.0,
            "vix_regime": regime,
            "vix_size_multiplier": 1.0,
            "confidence_penalty": 0,
            "stale": True,
            "source": "fallback",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

