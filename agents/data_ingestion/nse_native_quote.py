"""Optional same-day NSE quotes via nsepython (equity + select indices).

No API key. Used as the first hop in context_fetcher; falls back to HTTP
wrappers if nsepython is missing or NSE calls fail.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Internal display symbol / common aliases → NSE index name for nse_get_index_quote
_INDEX_QUOTE_NAMES: dict[str, str] = {
    "NIFTY 50": "NIFTY 50",
    "NIFTY50": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "NIFTY IT": "NIFTY IT",
    "NIFTYIT": "NIFTY IT",
}


def _safe_float(val: Any) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    s = re.sub(r",", "", str(val).strip())
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_equity_quote(payload: dict[str, Any]) -> dict[str, float] | None:
    price_info = payload.get("priceInfo")
    if not isinstance(price_info, dict):
        return None
    pct = _safe_float(price_info.get("pChange"))
    vol = 0.0
    pom = payload.get("preOpenMarket")
    if isinstance(pom, dict) and pom.get("totalTradedVolume") is not None:
        vol = _safe_float(pom.get("totalTradedVolume"))
    return {"price_change_pct": pct, "volume": vol}


def _parse_index_quote(payload: dict[str, Any]) -> dict[str, float] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("percChange") is None and payload.get("last") is None:
        return None
    pct = _safe_float(payload.get("percChange"))
    return {"price_change_pct": pct, "volume": 0.0}


def try_nsepython_stock_snapshot(display_symbol: str) -> dict[str, float] | None:
    """
    Return {price_change_pct, volume} from NSE India public JSON, or None to fall back.

    Supports NIFTY 50, BANKNIFTY (→ NIFTY BANK), NIFTY IT, and equity symbols (e.g. RELIANCE).
    SENSEX / BSE-only symbols return None (use fallback).
    """
    from config import settings

    if not getattr(settings, "NSEPYTHON_QUOTES_ENABLED", True):
        return None

    try:
        from nsepython import nse_eq, nse_get_index_quote
    except ImportError:
        logger.debug("nsepython not installed; skipping native NSE quote")
        return None

    key = display_symbol.replace(".NS", "").replace("^", "").strip().upper()
    key_compact = key.replace(" ", "")

    index_name = _INDEX_QUOTE_NAMES.get(key) or _INDEX_QUOTE_NAMES.get(key_compact)
    if index_name:
        try:
            raw = nse_get_index_quote(index_name)
        except Exception as exc:
            logger.debug("nse_get_index_quote failed for %s: %s", index_name, exc)
            return None
        if not raw:
            return None
        parsed = _parse_index_quote(raw if isinstance(raw, dict) else {})
        return parsed

    if not key_compact or key_compact == "SENSEX" or key.startswith("SENSEX"):
        return None

    try:
        raw = nse_eq(key_compact)
    except Exception as exc:
        logger.debug("nse_eq failed for %s: %s", key_compact, exc)
        return None
    if not isinstance(raw, dict):
        return None
    return _parse_equity_quote(raw)
