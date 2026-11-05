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
from datetime import datetime, timezone
from typing import Any

from config import settings
from utils.atomic_io import atomic_write_json

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional dependency runtime path
    yf = None
logger = logging.getLogger(__name__)

# ── FII/DII History persistence ─────────────────────────────────────────────
_TRADEFLOW_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "tradeflow_history.json")
_tradeflow_cache: dict = {}
_tradeflow_fetched_at: float = 0.0
_TRADEFLOW_CACHE_TTL = 300  # 5 minutes — NSE updates data once per day
_usdinr_cache: dict[str, Any] = {"fetched_at": 0.0, "change_pct": None}
_USDINR_CACHE_TTL = 900


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        return float(value)
    except Exception:
        return None


def _parse_trade_date(date_str: str) -> datetime:
    try:
        return datetime.strptime(date_str, "%d-%b-%Y")
    except Exception:
        return datetime.min


def _history_entry(
    date: str,
    fii_net: float,
    dii_net: float,
    sentiment: str,
    fii_magnitude: str,
    dii_context: str,
    true_intent: str,
    true_intent_score: float,
    fo_fii_net: float | None,
) -> dict:
    return {
        "date": date,
        "fii_net": round(fii_net, 2),
        "dii_net": round(dii_net, 2),
        "sentiment": sentiment,
        "fii_magnitude": fii_magnitude,
        "dii_context": dii_context,
        "true_intent": true_intent,
        "true_intent_score": round(true_intent_score, 1),
        "fo_fii_net": round(fo_fii_net, 2) if fo_fii_net is not None else None,
    }


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


def _upsert_tf_history(history: list, entry: dict) -> list:
    date = str(entry.get("date", ""))
    for existing in history:
        if existing.get("date") == date:
            existing.update(entry)
            return history
    history.append(entry)
    history.sort(key=lambda x: _parse_trade_date(str(x.get("date", ""))))
    return history[-90:]  # keep last 90 trading days


def _classify_fii_magnitude(fii_net: float) -> str:
    strong = float(getattr(settings, "TRADEFLOW_FII_STRONG_THRESHOLD_CR", 3000.0))
    noise = float(getattr(settings, "TRADEFLOW_FII_NOISE_THRESHOLD_CR", 500.0))

    if fii_net >= strong:
        return "FII_STRONG_BUY"
    if fii_net >= noise:
        return "FII_MODERATE_BUY"
    if fii_net > 0:
        return "FII_WEAK_BUY"

    if fii_net <= -strong:
        return "FII_STRONG_SELL"
    if fii_net <= -noise:
        return "FII_MODERATE_SELL"
    if fii_net < 0:
        return "FII_WEAK_SELL"

    return "FII_FLAT"


def _build_fii_5d_trend(history: list[dict]) -> dict:
    lookback = max(2, int(getattr(settings, "TRADEFLOW_FIVE_DAY_LOOKBACK", 5)))
    ordered = sorted(history, key=lambda x: _parse_trade_date(str(x.get("date", ""))))
    recent = ordered[-lookback:]
    fii_vals = [float(x.get("fii_net", 0.0) or 0.0) for x in recent]
    positive_days = sum(1 for x in fii_vals if x > 0)
    negative_days = sum(1 for x in fii_vals if x < 0)
    avg = sum(fii_vals) / len(fii_vals) if fii_vals else 0.0

    if positive_days >= 3 and avg > 0:
        trend = "CONSISTENT_BUYING"
    elif negative_days >= 3 and avg < 0:
        trend = "CONSISTENT_SELLING"
    else:
        trend = "MIXED"

    return {
        "lookback_days": lookback,
        "positive_days": positive_days,
        "negative_days": negative_days,
        "avg_net_cr": round(avg, 2),
        "trend": trend,
        "consistent": trend in ("CONSISTENT_BUYING", "CONSISTENT_SELLING"),
    }


def _build_dii_context(fii_net: float, dii_net: float) -> str:
    if dii_net > 0 and fii_net < 0:
        return "DII_ABSORBING_FII_OUTFLOW"
    if dii_net > 0 and fii_net > 0:
        return "DUAL_INSTITUTIONAL_ACCUMULATION"
    if dii_net < 0 and fii_net > 0:
        return "DII_PROFIT_BOOKING_WHILE_FII_BUYS"
    if dii_net < 0 and fii_net < 0:
        return "DUAL_DISTRIBUTION"
    return "MIXED_INSTITUTIONAL_FLOW"


def _fetch_usdinr_change_pct() -> float | None:
    now_ts = time.time()
    if (now_ts - float(_usdinr_cache.get("fetched_at", 0.0) or 0.0)) < _USDINR_CACHE_TTL:
        return _safe_float(_usdinr_cache.get("change_pct"))

    if yf is None:
        return None

    try:
        df = yf.download("INR=X", period="5d", interval="1d", progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        close = df["Close"].dropna()
        if len(close) < 2:
            return None
        prev_close = float(close.iloc[-2])
        last_close = float(close.iloc[-1])
        if prev_close <= 0:
            return None
        change_pct = ((last_close - prev_close) / prev_close) * 100.0
        _usdinr_cache["change_pct"] = float(change_pct)
        _usdinr_cache["fetched_at"] = now_ts
        return float(change_pct)
    except Exception:
        return None


def _build_fii_exit_context(fii_net: float, usdinr_change_pct: float | None) -> str:
    if fii_net >= 0:
        return "FII_NOT_EXITING"
    if usdinr_change_pct is None:
        return "FII_EXIT_UNCLASSIFIED"

    threshold = float(getattr(settings, "TRADEFLOW_USDINR_EXIT_THRESHOLD_PCT", 0.5))
    if usdinr_change_pct >= threshold:
        return "FII_CURRENCY_EXIT"
    return "FII_EQUITY_EXIT"


def _extract_fii_like_net(payload: Any) -> float | None:
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            cat = str(item.get("category", "") or "").upper()
            if "FII" in cat or "FPI" in cat:
                for key in ("netValue", "net", "netvalue", "net_value", "fii_net", "fpi_net"):
                    if key in item:
                        net_val = _safe_float(item.get(key))
                        if net_val is not None:
                            return net_val

    if isinstance(payload, dict):
        for key in ("fii_net", "fpi_net", "net_fii", "net_fpi"):
            if key in payload:
                net_val = _safe_float(payload.get(key))
                if net_val is not None:
                    return net_val

        for nested_key in ("data", "rows", "records", "result"):
            if nested_key in payload:
                nested_net = _extract_fii_like_net(payload.get(nested_key))
                if nested_net is not None:
                    return nested_net

        for key, value in payload.items():
            if isinstance(value, (dict, list)) and ("fii" in str(key).lower() or "fpi" in str(key).lower()):
                nested_net = _extract_fii_like_net(value)
                if nested_net is not None:
                    return nested_net

    return None


def _fetch_fo_flow(ctx: ssl.SSLContext) -> dict:
    endpoint = str(getattr(settings, "TRADEFLOW_FO_ENDPOINT", "") or "").strip()
    if not endpoint:
        return {
            "available": False,
            "source": "UNAVAILABLE",
            "reason": "TRADEFLOW_FO_ENDPOINT not configured",
            "fii_net_cr": None,
        }

    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        url = endpoint
    else:
        normalized = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = f"https://www.nseindia.com{normalized}"

    req = urllib.request.Request(
        url,
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

    try:
        with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:
        return {
            "available": False,
            "source": "UNAVAILABLE",
            "reason": f"F&O fetch failed: {exc}",
            "fii_net_cr": None,
        }

    fo_fii_net = _extract_fii_like_net(payload)
    if fo_fii_net is None:
        return {
            "available": False,
            "source": "UNAVAILABLE",
            "reason": "Could not parse FII F&O net from payload",
            "fii_net_cr": None,
        }

    return {
        "available": True,
        "source": "NSE",
        "reason": "ok",
        "fii_net_cr": round(float(fo_fii_net), 2),
    }


def _build_true_intent(fii_cash_net: float, fii_fo_net: float | None) -> tuple[str, float]:
    if fii_fo_net is None:
        return "UNAVAILABLE", 0.0

    if fii_cash_net >= 0 and fii_fo_net >= 0:
        return "GENUINE_ACCUMULATION", 85.0
    if fii_cash_net < 0 and fii_fo_net >= 0:
        return "HEDGED_HOLD", 25.0
    if fii_cash_net >= 0 and fii_fo_net < 0:
        return "DISTRIBUTION_DISGUISED_AS_BUYING", -45.0
    return "RISK_OFF_UNWIND", -85.0


def _resolve_sentiment(fii_net: float, dii_net: float, fii_magnitude: str) -> str:
    if fii_net > 0 and dii_net > 0:
        return "BOTH_BUYING"
    if fii_net < 0 and dii_net < 0:
        return "BOTH_SELLING"
    if abs(fii_net) >= abs(dii_net):
        if fii_magnitude != "FII_FLAT":
            return fii_magnitude
        return "FII_DOMINANT_BUY" if fii_net >= 0 else "FII_DOMINANT_SELL"
    if dii_net > 0:
        return "DII_DOMINANT_BUY"
    return "DII_DOMINANT_SELL"


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
        fii_magnitude = _classify_fii_magnitude(fii_net)

        # Optional F&O intent layer (endpoint is configurable; gracefully unavailable by default)
        fo_flow = _fetch_fo_flow(ctx)
        fo_fii_net = _safe_float(fo_flow.get("fii_net_cr"))
        true_intent, true_intent_score = _build_true_intent(fii_net, fo_fii_net)

        dii_context = _build_dii_context(fii_net, dii_net)
        usdinr_change_pct = _fetch_usdinr_change_pct()
        fii_exit_context = _build_fii_exit_context(fii_net, usdinr_change_pct)

        sentiment = _resolve_sentiment(fii_net, dii_net, fii_magnitude)

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
                "category": "Estimated Other / Residual",
                "label": "Estimated Other / Residual",
                "icon": "⚖️",
                "buy_cr": None,
                "sell_cr": None,
                "net_cr": retail_net,
                "direction": "BUY" if retail_net >= 0 else "SELL",
                "note": "Mathematical balancing residual [-(FII + DII)]; represents retail, HNI, proprietary, and unclassified non-institutional flow.",
            },
        ]

        # Update rolling history
        history = _load_tf_history()
        hist_row = _history_entry(
            date=trade_date,
            fii_net=fii_net,
            dii_net=dii_net,
            sentiment=sentiment,
            fii_magnitude=fii_magnitude,
            dii_context=dii_context,
            true_intent=true_intent,
            true_intent_score=true_intent_score,
            fo_fii_net=fo_fii_net,
        )
        history = _upsert_tf_history(history, hist_row)
        _save_tf_history(history)
        fii_5d_trend = _build_fii_5d_trend(history)

        fetched_iso = datetime.now(timezone.utc).isoformat()
        official_date_str = f"Data Date: {trade_date} EOD Official"

        result = {
            "flows": flows,
            "date": trade_date,
            "display_date": official_date_str,
            "units": "₹ Crores (1 Cr = ₹10,000,000)",
            "residual_explanation": "Estimated Other / Residual is the mathematical balancing residual -(FII Net + DII Net), accounting for retail investors, HNIs, and proprietary desks. It is not a direct exchange-polled client category.",
            "sentiment": sentiment,
            "fii_magnitude": fii_magnitude,
            "fii_5d_trend": fii_5d_trend,
            "dii_context": dii_context,
            "fo_flow": fo_flow,
            "true_intent": true_intent,
            "true_intent_score": round(true_intent_score, 1),
            "fii_exit_context": fii_exit_context,
            "usdinr_change_pct": round(usdinr_change_pct, 3) if usdinr_change_pct is not None else None,
            "combined_net": combined_net,
            "history": history[-30:],
            "source": "NSE",
            "real_data": True,
            "stale": False,
            "age_minutes": 0.0,
            "age_hours": 0.0,
            "fetched_at": fetched_iso,
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
        age_minutes = max(0.0, (time.time() - _tradeflow_fetched_at) / 60.0)
        stale["age_minutes"] = round(age_minutes, 1)
        stale["age_hours"] = round(age_minutes / 60.0, 2)
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
        "age_minutes": None,
        "age_hours": None,
        "fetched_at": None,
    }


def generate_trade_flow() -> list[dict]:
    """
    Deprecated stub — server.py uses fetch_fii_dii_flow() (real NSE data) instead.
    Returns empty list so callers that reference this function don't crash.
    """
    logger.warning("generate_trade_flow() called — use fetch_fii_dii_flow() for real NSE data")
    return []
