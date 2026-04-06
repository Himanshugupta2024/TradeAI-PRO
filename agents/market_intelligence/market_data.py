"""
Market Screener & Option Chain API Endpoints.

Adds to server.py:
  - /api/screener/gainers   — top gainers with X-factor
  - /api/screener/losers    — top losers
  - /api/sectors            — sectoral heatmap data
  - /api/pcr/{symbol}       — Put-Call Ratio (REAL NSE option chain)
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
import urllib3
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ── FII/DII History persistence ─────────────────────────────────────────────
_TRADEFLOW_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "tradeflow_history.json")
_tradeflow_cache: dict = {}
_tradeflow_fetched_at: float = 0.0
_TRADEFLOW_CACHE_TTL = 300  # 5 minutes — NSE updates data once per day

# ── NSE Option Chain — real data fetcher ─────────────────────────────────────
_NSE_OC_CACHE: dict = {}        # symbol → {"data": ..., "fetched_at": float}
_NSE_OC_TTL   = 180             # 3 minutes cache (NSE updates every ~3 min intraday)
_NSE_SESSION: dict = {}         # {"session": requests.Session, "created_at": float}
_NSE_SESSION_TTL = 240          # 4 minutes — NSE cookies expire quickly

# ── PCR History persistence ───────────────────────────────────────────────────
_PCR_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "pcr_history.json")
_pcr_history_store: dict = {}   # symbol → [{"time": str, "pcr": float}]


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
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
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


# ─────────────────────────────────────────────────────────────────────────────
# NSE Option Chain — Real Data Functions
# ─────────────────────────────────────────────────────────────────────────────

_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
}


def _refresh_nse_session() -> "requests.Session":
    """Establish a fresh NSE requests session — visit homepage to pick up cookies."""
    session = requests.Session()
    session.headers.update(_NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=12, verify=False)
        time.sleep(0.4)
        logger.info("NSE session established")
    except Exception as exc:
        logger.warning("NSE homepage visit failed: %s", exc)
    _NSE_SESSION["session"] = session
    _NSE_SESSION["created_at"] = time.time()
    return session


def _get_nse_session() -> "requests.Session":
    """Return a valid NSE session, refreshing it if the TTL has expired."""
    age = time.time() - _NSE_SESSION.get("created_at", 0)
    if _NSE_SESSION.get("session") and age < _NSE_SESSION_TTL:
        return _NSE_SESSION["session"]
    return _refresh_nse_session()


def _load_pcr_history() -> dict:
    try:
        path = os.path.abspath(_PCR_HISTORY_FILE)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_pcr_history() -> None:
    try:
        path = os.path.abspath(_PCR_HISTORY_FILE)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_pcr_history_store, f, ensure_ascii=False)
    except Exception:
        pass


def fetch_nse_option_chain(symbol: str) -> dict | None:
    """
    Fetch live option chain data.

    Priority:
      1. Broker API (Zerodha Kite / Angel Broking) — if configured in .env
      2. NSE public API (two-step session) — often blocked by Akamai bot protection
      3. Returns None → caller will show OFFLINE state (zero OI, no fake data)
    """
    # ── Priority 1: Broker API ────────────────────────────
    try:
        from agents.market_intelligence.broker_api import fetch_option_chain_from_broker
        broker_data = fetch_option_chain_from_broker(symbol)
        if broker_data and "records" in broker_data:
            _NSE_OC_CACHE[symbol] = {"data": broker_data, "fetched_at": time.time()}
            return broker_data
    except Exception as exc:
        logger.debug("Broker API attempt skipped: %s", exc)

    # ── Priority 2: NSE direct API (Akamai-gated — may return {}) ────────────
    _SYMBOL_MAP = {
        "NIFTY 50":   "NIFTY",
        "BANKNIFTY":  "BANKNIFTY",
        "NIFTY BANK": "BANKNIFTY",
    }
    nse_sym = _SYMBOL_MAP.get(symbol)
    if not nse_sym:
        return None

    # Return cached data if still fresh (avoids repeated Akamai hits)
    cached = _NSE_OC_CACHE.get(symbol, {})
    if cached and (time.time() - cached.get("fetched_at", 0)) < _NSE_OC_TTL:
        logger.debug("Option chain cache hit for %s", symbol)
        return cached["data"]

    url = f"https://www.nseindia.com/api/option-chain-indices?symbol={nse_sym}"

    for attempt in range(2):
        try:
            session = _get_nse_session()
            resp = session.get(url, timeout=15, verify=False)

            if resp.status_code in (401, 403):
                logger.warning("NSE session rejected (attempt %d) — refreshing", attempt + 1)
                _NSE_SESSION.clear()
                continue

            resp.raise_for_status()
            data = resp.json()

            if "records" not in data:
                raise ValueError(f"NSE returned empty response (Akamai bot block): {list(data.keys())}")

            _NSE_OC_CACHE[symbol] = {"data": data, "fetched_at": time.time()}
            logger.info("NSE option chain fetched for %s (%d strikes)", symbol,
                        len(data.get("records", {}).get("data", [])))
            return data

        except Exception as exc:
            logger.warning("NSE option chain attempt %d/%d failed for %s: %s",
                           attempt + 1, 2, symbol, exc)
            _NSE_SESSION.clear()

    return None


def _parse_option_chain(raw: dict, symbol: str) -> dict:
    """
    Parse raw NSE option chain JSON into the PCR data format consumed by the
    frontend. Calculates real PCR, max pain, and updates rolling history.
    """
    records      = raw.get("records", {})
    expiry_dates = records.get("expiryDates", [])
    underlying   = float(records.get("underlyingValue") or 0)

    # Dynamic ATM from live price
    atm  = _get_live_atm(symbol)
    step = {"NIFTY 50": 50, "BANKNIFTY": 100}.get(symbol, 50)

    # Use nearest weekly expiry
    nearest_expiry = expiry_dates[0] if expiry_dates else None
    all_rows = records.get("data", [])
    if nearest_expiry:
        rows = [r for r in all_rows if r.get("expiryDate") == nearest_expiry] or all_rows
    else:
        rows = all_rows

    # Build a strike → row map
    strike_map: dict[int, dict] = {}
    for row in rows:
        sp = row.get("strikePrice")
        if sp is not None:
            strike_map[int(sp)] = row

    # Pick ATM ± 4 strikes
    all_strikes = sorted(strike_map.keys())
    if not all_strikes:
        raise ValueError("No strikes found in NSE option chain data")

    atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - atm))
    selected = all_strikes[max(0, atm_idx - 4): atm_idx + 5]

    chain = []
    total_call_oi = 0
    total_put_oi  = 0

    for strike in selected:
        row  = strike_map.get(strike, {})
        ce   = row.get("CE", {})
        pe   = row.get("PE", {})

        call_oi  = int(ce.get("openInterest", 0) or 0)
        put_oi   = int(pe.get("openInterest", 0) or 0)
        call_chg = int(ce.get("changeinOpenInterest", 0) or 0)
        put_chg  = int(pe.get("changeinOpenInterest", 0) or 0)
        call_ltp = float(ce.get("lastPrice", 0) or 0)
        put_ltp  = float(pe.get("lastPrice", 0) or 0)

        total_call_oi += call_oi
        total_put_oi  += put_oi

        chain.append({
            "strike":   strike,
            "call_oi":  call_oi,
            "call_chg": call_chg,
            "call_ltp": round(call_ltp, 1),
            "put_oi":   put_oi,
            "put_chg":  put_chg,
            "put_ltp":  round(put_ltp, 1),
            "atm":      strike == atm,
        })

    pcr       = round(total_put_oi / max(total_call_oi, 1), 2)
    sentiment = "BEARISH" if pcr > 1.2 else "BULLISH" if pcr < 0.8 else "NEUTRAL"

    # Max pain: strike where total ITM payout to option buyers is minimised
    # Approximation: strike with highest combined OI (magnet strike)
    max_pain_strike = atm
    if chain:
        max_pain_strike = max(chain, key=lambda r: r["call_oi"] + r["put_oi"])["strike"]

    # Update rolling PCR history (persisted to pcr_history.json)
    if not _pcr_history_store:
        _pcr_history_store.update(_load_pcr_history())
    sym_hist = _pcr_history_store.get(symbol, [])
    sym_hist.append({"time": datetime.now().strftime("%H:%M"), "pcr": pcr})
    sym_hist = sym_hist[-40:]
    _pcr_history_store[symbol] = sym_hist
    _save_pcr_history()

    return {
        "symbol":       symbol,
        "atm":          int(atm),
        "underlying":   round(underlying, 2),
        "pcr":          float(pcr),
        "sentiment":    sentiment,
        "total_call_oi": int(total_call_oi),
        "total_put_oi":  int(total_put_oi),
        "max_pain":     int(max_pain_strike),
        "option_chain": chain,
        "pcr_history":  sym_hist,
        "expiry":       nearest_expiry or "N/A",
        "real_data":    True,
        "source":       "NSE",
    }


# Fake generators removed — all data now comes from real APIs in nse_fetcher.py


def _get_live_atm(symbol: str) -> int | None:
    """
    Fetch the current live price for a symbol and round to the nearest strike step.
    Returns None when a live price is unavailable (never fabricates ATM values).
    """
    import yfinance as yf

    _step_map = {"NIFTY 50": 50, "BANKNIFTY": 100, "BTC/USDT": 1000}
    _ticker_map = {"NIFTY 50": "^NSEI", "BANKNIFTY": "^NSEBANK", "BTC/USDT": "BTC-USD"}

    step = _step_map.get(symbol, 100)
    ticker = _ticker_map.get(symbol)
    if ticker:
        try:
            info = yf.Ticker(ticker).fast_info
            price = float(info.get("last_price") or info.get("previousClose") or 0)
            if price > 0:
                return int(round(price / step) * step)
        except Exception as e:
            logger.debug("ATM fetch failed for %s: %s", symbol, e)

    return None


def generate_pcr_data(symbol: str) -> dict:
    """
    PCR and option chain data.

    Priority:
      1. Real NSE option chain API  →  NIFTY 50 / BANKNIFTY
      2. Graceful offline fallback  →  zeros + real ATM, clearly marked real_data=False

    NEVER returns random/fabricated OI numbers.
    """
    # ── Try real NSE option chain ─────────────────────────
    raw = fetch_nse_option_chain(symbol)
    if raw:
        try:
            return _parse_option_chain(raw, symbol)
        except Exception as exc:
            logger.error("Option chain parse error for %s: %s", symbol, exc)

    # ── Offline / unsupported symbol fallback ─────────────
    # Expose unavailability honestly (no synthetic strike ladder).
    atm = _get_live_atm(symbol)

    # Load any persisted PCR history (from previous successful fetches)
    if not _pcr_history_store:
        _pcr_history_store.update(_load_pcr_history())
    sym_hist = _pcr_history_store.get(symbol, [])

    return {
        "symbol":        symbol,
        "atm":           int(atm) if atm is not None else None,
        "underlying":    float(atm) if atm is not None else None,
        "pcr":           0.0,
        "sentiment":     "UNAVAILABLE",
        "total_call_oi": 0,
        "total_put_oi":  0,
        "max_pain":      int(atm) if atm is not None else None,
        "option_chain":  [],
        "pcr_history":   sym_hist,
        "expiry":        "N/A",
        "real_data":     False,
        "source":        "OFFLINE",
    }


def generate_trade_flow() -> list[dict]:
    """
    Deprecated stub — server.py uses fetch_fii_dii_flow() (real NSE data) instead.
    Returns empty list so callers that reference this function don't crash.
    """
    logger.warning("generate_trade_flow() called — use fetch_fii_dii_flow() for real NSE data")
    return []
