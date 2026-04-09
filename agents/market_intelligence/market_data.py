"""
Market Screener API helpers.

Adds to server.py:
  - /api/screener/gainers   — top gainers with X-factor
  - /api/screener/losers    — top losers
  - /api/sectors            — sectoral heatmap data
  - /api/tradeflow          — institutional trade flow (REAL NSE data)

REAL DATA POLICY:
  Never show random/fake data. If real data is unavailable, show zeros with
  real_data=False so the frontend can clearly mark it as "unavailable".
"""

import json
import logging
import os
import ssl
import time
import urllib.request
from datetime import datetime

from utils.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

# ── FII/DII History persistence ─────────────────────────────────────────────
_TRADEFLOW_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "tradeflow_history.json")
_tradeflow_cache: dict = {}
_tradeflow_fetched_at: float = 0.0
_TRADEFLOW_CACHE_TTL = 300  # 5 minutes — NSE updates data once per day


def _load_tf_history() -> list:
    try:
        path = os.path.abspath(_TRADEFLOW_HISTORY_FILE)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_tf_history(history: list) -> None:
    try:
        path = os.path.abspath(_TRADEFLOW_HISTORY_FILE)
        atomic_write_json(path, history, ensure_ascii=False)
    except Exception:
        pass


def _upsert_tf_history(history: list, date: str, fii_net: float, dii_net: float) -> list:
    for entry in history:
        if entry.get("date") == date:
            entry["fii_net"] = round(fii_net, 2)
            entry["dii_net"] = round(dii_net, 2)
            return history
    history.append({"date": date, "fii_net": round(fii_net, 2), "dii_net": round(dii_net, 2)})
    return history[-90:]  # keep last 90 trading days


def fetch_fii_dii_flow() -> dict:
    """
    Fetch real FII/DII institutional flow from NSE public API.
    URL: https://www.nseindia.com/api/fiidiiTradeReact

    Caches result for 5 minutes. Persists rolling history to
    tradeflow_history.json for trend charts.
    Falls back to last cached data (or placeholder) on failure.
    """
    global _tradeflow_cache, _tradeflow_fetched_at

    # Return in-memory cache if fresh enough
    if _tradeflow_cache and (time.time() - _tradeflow_fetched_at) < _TRADEFLOW_CACHE_TTL:
        return _tradeflow_cache

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            "https://www.nseindia.com/api/fiidiiTradeReact",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.nseindia.com/market-data/fii-dii-activity",
                "Origin": "https://www.nseindia.com",
                "Connection": "keep-alive",
            },
        )

        with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
            raw: list = json.loads(resp.read())

        # Parse DII and FII/FPI items
        fii_item = next(
            (x for x in raw if x.get("category") in ("FII/FPI", "FII")), None
        )
        dii_item = next(
            (x for x in raw if x.get("category") == "DII"), None
        )
        if not fii_item or not dii_item:
            raise ValueError(f"Incomplete NSE response — got {[x.get('category') for x in raw]}")

        trade_date = fii_item.get("date", datetime.now().strftime("%d-%b-%Y"))
        fii_buy  = float(fii_item["buyValue"])
        fii_sell = float(fii_item["sellValue"])
        fii_net  = float(fii_item["netValue"])
        dii_buy  = float(dii_item["buyValue"])
        dii_sell = float(dii_item["sellValue"])
        dii_net  = float(dii_item["netValue"])
        retail_net = round(-(fii_net + dii_net), 2)
        combined_net = round(fii_net + dii_net, 2)

        # Determine market sentiment label
        if fii_net > 0 and dii_net > 0:
            sentiment = "BOTH_BUYING"
        elif fii_net < 0 and dii_net < 0:
            sentiment = "BOTH_SELLING"
        elif dii_net > 0 and abs(dii_net) >= abs(fii_net):
            sentiment = "DII_DOMINANT_BUY"
        elif fii_net > 0 and abs(fii_net) > abs(dii_net):
            sentiment = "FII_DOMINANT_BUY"
        elif abs(fii_net) >= abs(dii_net):
            sentiment = "FII_DOMINANT_SELL"
        else:
            sentiment = "DII_DOMINANT_SELL"

        flows = [
            {
                "category": "FII/FPI",
                "label": "Foreign Institutional",
                "icon": "🌐",
                "buy_cr": round(fii_buy, 2),
                "sell_cr": round(fii_sell, 2),
                "net_cr": round(fii_net, 2),
                "direction": "BUY" if fii_net >= 0 else "SELL",
            },
            {
                "category": "DII",
                "label": "Domestic Institutional",
                "icon": "🏦",
                "buy_cr": round(dii_buy, 2),
                "sell_cr": round(dii_sell, 2),
                "net_cr": round(dii_net, 2),
                "direction": "BUY" if dii_net >= 0 else "SELL",
            },
            {
                "category": "Retail",
                "label": "Retail / Others (est.)",
                "icon": "👥",
                "buy_cr": None,
                "sell_cr": None,
                "net_cr": retail_net,
                "direction": "BUY" if retail_net >= 0 else "SELL",
            },
        ]

        # Update rolling history
        history = _load_tf_history()
        history = _upsert_tf_history(history, trade_date, fii_net, dii_net)
        _save_tf_history(history)

        result = {
            "flows": flows,
            "date": trade_date,
            "sentiment": sentiment,
            "combined_net": combined_net,
            "history": history[-30:],
            "source": "NSE",
            "real_data": True,
            "stale": False,
        }

        _tradeflow_cache = result
        _tradeflow_fetched_at = time.time()
        logger.info("FII/DII fetched OK — FII %+.0f | DII %+.0f Cr", fii_net, dii_net)
        return result

    except Exception as exc:
        logger.warning("FII/DII fetch failed: %s — using fallback", exc)

    # Return stale cache if we have one
    if _tradeflow_cache:
        stale = dict(_tradeflow_cache)
        stale["stale"] = True
        return stale

    # Last-resort unavailable payload (no fabricated flows).
    return {
        "flows": [],
        "date": datetime.now().strftime("%d-%b-%Y"),
        "sentiment": "UNAVAILABLE",
        "combined_net": None,
        "history": _load_tf_history()[-30:],
        "source": "OFFLINE",
        "real_data": False,
        "stale": True,
    }


def generate_trade_flow() -> list[dict]:
    """
    Deprecated stub — server.py uses fetch_fii_dii_flow() (real NSE data) instead.
    Returns empty list so callers that reference this function don't crash.
    """
    logger.warning("generate_trade_flow() called — use fetch_fii_dii_flow() for real NSE data")
    return []
