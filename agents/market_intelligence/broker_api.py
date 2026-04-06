"""
Broker API — Real NSE Option Chain Data.

NSE's public website uses Akamai bot protection that blocks Python requests.
Real option chain OI data requires a broker API.

Supported brokers (configure ONE via .env):
  1. Zerodha Kite Connect  — ZERODHA_API_KEY + ZERODHA_ACCESS_TOKEN
  2. Angel Broking SmartAPI — ANGEL_API_KEY + ANGEL_CLIENT_ID + ANGEL_PASSWORD + ANGEL_TOTP_SECRET

Install the library for your broker:
  pip install kiteconnect          # Zerodha
  pip install smartapi-python pyotp  # Angel Broking

The functions here return data in the same NSE-compatible format so
market_data.py parse logic works unchanged.
"""

import os
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Instrument cache (NSE/NFO instruments list is ~5 MB, refresh once per day) ──
_instruments_cache: dict = {}           # exchange → {"data": [...], "loaded_at": float}
_INSTRUMENTS_TTL = 3600 * 8            # 8 hours


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def fetch_option_chain_from_broker(symbol: str) -> dict | None:
    """
    Try each configured broker and return an NSE-compatible option chain dict,
    or None if no broker is configured or all fail.
    """
    result = _try_zerodha(symbol)
    if result:
        return result

    result = _try_angel(symbol)
    if result:
        return result

    return None


def get_broker_status() -> dict:
    """Return which broker credentials are configured (for the dashboard status API)."""
    zerodha_ok = bool(os.getenv("ZERODHA_API_KEY") and os.getenv("ZERODHA_ACCESS_TOKEN"))
    angel_ok   = bool(
        os.getenv("ANGEL_API_KEY") and os.getenv("ANGEL_CLIENT_ID")
        and os.getenv("ANGEL_PASSWORD") and os.getenv("ANGEL_TOTP_SECRET")
    )
    return {
        "zerodha": zerodha_ok,
        "angel":   angel_ok,
        "any":     zerodha_ok or angel_ok,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Zerodha Kite Connect
# ─────────────────────────────────────────────────────────────────────────────

_NSE_SYMBOL_TO_ZERODHA = {
    "NIFTY 50":  "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
}


def _try_zerodha(symbol: str) -> dict | None:
    api_key      = os.getenv("ZERODHA_API_KEY", "").strip()
    access_token = os.getenv("ZERODHA_ACCESS_TOKEN", "").strip()
    if not api_key or not access_token:
        return None

    try:
        from kiteconnect import KiteConnect  # type: ignore
    except ImportError:
        logger.debug("kiteconnect not installed — run: pip install kiteconnect")
        return None

    kite_sym = _NSE_SYMBOL_TO_ZERODHA.get(symbol)
    if not kite_sym:
        return None

    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        # Load NFO instruments (heavy file — cached)
        instruments = _load_zerodha_instruments(kite)
        if not instruments:
            return None

        # Select nearest expiry for this underlying
        now = datetime.now()
        expiry_dates = sorted(set(
            i["expiry"] for i in instruments
            if i.get("name") == kite_sym and i.get("instrument_type") in ("CE", "PE")
            and i["expiry"] >= now.date()
        ))
        if not expiry_dates:
            logger.warning("No upcoming expiry found for %s in Zerodha instruments", kite_sym)
            return None
        nearest_expiry = expiry_dates[0]

        # Get all strikes for nearest expiry ± 8 around current price
        all_options = [
            i for i in instruments
            if i.get("name") == kite_sym
            and i.get("instrument_type") in ("CE", "PE")
            and i["expiry"] == nearest_expiry
        ]
        if not all_options:
            return None

        strikes = sorted(set(i["strike"] for i in all_options))

        # Get approximate ATM from last traded price of NIFTY futures
        try:
            fut = next((i for i in instruments
                        if i.get("name") == kite_sym
                        and i.get("instrument_type") == "FUT"
                        and i.get("expiry") == nearest_expiry), None)
            if fut:
                qt = kite.quote([f"NFO:{fut['tradingsymbol']}"])
                spot = qt[f"NFO:{fut['tradingsymbol']}"]["last_price"]
            else:
                qt = kite.quote([f"NSE:{kite_sym} 50" if kite_sym == "NIFTY" else f"NSE:{kite_sym}"])
                spot = list(qt.values())[0]["last_price"]
        except Exception:
            # Fallback: mid of available strikes
            spot = strikes[len(strikes) // 2]

        step = 50 if kite_sym == "NIFTY" else 100
        atm  = int(round(spot / step) * step)

        # Select ATM ± 4 strikes
        atm_idx       = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm))
        sel_strikes   = strikes[max(0, atm_idx - 4): atm_idx + 5]

        # Build trading symbol list for batch quote
        trading_syms  = [i["tradingsymbol"] for i in all_options
                         if i["strike"] in sel_strikes]
        if not trading_syms:
            return None

        # Batch quote (Zerodha allows up to 500 instruments per call)
        batch = {f"NFO:{ts}": ts for ts in trading_syms}
        quotes = kite.quote(list(batch.keys()))

        # Build NSE-compatible chain
        chain_map: dict[int, dict] = {}
        for nfo_key, ts in batch.items():
            row  = quotes.get(nfo_key, {})
            inst = next((i for i in all_options if i["tradingsymbol"] == ts), {})
            strike  = int(inst.get("strike", 0))
            opt_type = inst.get("instrument_type", "CE")
            oi       = int(row.get("oi", 0) or 0)
            oi_day   = int(row.get("oi_day_change", 0) or 0)
            ltp      = float(row.get("last_price", 0) or 0)

            if strike not in chain_map:
                chain_map[strike] = {
                    "strike": strike, "atm": (strike == atm),
                    "call_oi": 0, "call_chg": 0, "call_ltp": 0.0,
                    "put_oi":  0, "put_chg":  0, "put_ltp":  0.0,
                }
            if opt_type == "CE":
                chain_map[strike].update({"call_oi": oi, "call_chg": oi_day, "call_ltp": round(ltp, 1)})
            else:
                chain_map[strike].update({"put_oi":  oi, "put_chg":  oi_day, "put_ltp":  round(ltp, 1)})

        chain = [chain_map[s] for s in sorted(chain_map)]

        total_call_oi = sum(r["call_oi"] for r in chain)
        total_put_oi  = sum(r["put_oi"]  for r in chain)
        pcr = round(total_put_oi / max(total_call_oi, 1), 2)

        logger.info("Zerodha option chain fetched for %s — %d strikes, PCR=%.2f", symbol, len(chain), pcr)

        return {
            "records": {
                "underlyingValue": round(float(spot), 2),
                "expiryDates": [nearest_expiry.strftime("%d-%b-%Y")],
                "data": [
                    {
                        "strikePrice":  r["strike"],
                        "expiryDate":   nearest_expiry.strftime("%d-%b-%Y"),
                        "CE": {
                            "openInterest": r["call_oi"],
                            "changeinOpenInterest": r["call_chg"],
                            "lastPrice": r["call_ltp"],
                        },
                        "PE": {
                            "openInterest": r["put_oi"],
                            "changeinOpenInterest": r["put_chg"],
                            "lastPrice": r["put_ltp"],
                        },
                    }
                    for r in chain
                ],
            },
            "_broker": "zerodha",
        }

    except Exception as exc:
        logger.warning("Zerodha option chain failed for %s: %s", symbol, exc)
        return None


def _load_zerodha_instruments(kite) -> list:
    """Load (and cache) the full NFO instruments list from Zerodha."""
    cached = _instruments_cache.get("NFO", {})
    if cached and (time.time() - cached.get("loaded_at", 0)) < _INSTRUMENTS_TTL:
        return cached["data"]

    try:
        instruments = kite.instruments("NFO")
        _instruments_cache["NFO"] = {"data": instruments, "loaded_at": time.time()}
        logger.info("Zerodha NFO instruments loaded: %d instruments", len(instruments))
        return instruments
    except Exception as exc:
        logger.warning("Zerodha instruments fetch failed: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Angel Broking SmartAPI
# ─────────────────────────────────────────────────────────────────────────────

_angel_session: dict = {}   # {"obj": SmartConnect, "created_at": float}
_ANGEL_SESSION_TTL = 3600   # Re-login every hour

_ANGEL_SYMBOL_MAP = {
    "NIFTY 50":  "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
}

# Angel Broking exchange segment for NSE F&O
_ANGEL_NFO_SEGMENT = "NFO"


def _get_angel_session():
    """Return a valid Angel Broking SmartConnect session (auto-login with TOTP)."""
    cached = _angel_session.get("obj")
    if cached and (time.time() - _angel_session.get("created_at", 0)) < _ANGEL_SESSION_TTL:
        return cached

    try:
        from SmartApi import SmartConnect  # type: ignore
        import pyotp                       # type: ignore
    except ImportError:
        logger.debug("smartapi-python/pyotp not installed — run: pip install smartapi-python pyotp")
        return None

    api_key     = os.getenv("ANGEL_API_KEY", "").strip()
    client_id   = os.getenv("ANGEL_CLIENT_ID", "").strip()
    password    = os.getenv("ANGEL_PASSWORD", "").strip()
    totp_secret = os.getenv("ANGEL_TOTP_SECRET", "").strip()

    if not all([api_key, client_id, password, totp_secret]):
        return None

    try:
        obj  = SmartConnect(api_key=api_key)
        totp = pyotp.TOTP(totp_secret).now()
        resp = obj.generateSession(client_id, password, totp)

        if not resp or not resp.get("status"):
            logger.warning("Angel Broking login failed: %s", resp)
            return None

        _angel_session["obj"] = obj
        _angel_session["created_at"] = time.time()
        logger.info("Angel Broking session established for %s", client_id)
        return obj

    except Exception as exc:
        logger.warning("Angel Broking login error: %s", exc)
        return None


def _try_angel(symbol: str) -> dict | None:
    if not (os.getenv("ANGEL_API_KEY") and os.getenv("ANGEL_CLIENT_ID")):
        return None

    angel_sym = _ANGEL_SYMBOL_MAP.get(symbol)
    if not angel_sym:
        return None

    obj = _get_angel_session()
    if not obj:
        return None

    try:
        # Angel Broking option chain via getOptionChain()
        # Requires: name (NIFTY/BANKNIFTY), expirydate (DD-Mon-YYYY format), strikeprice, optiontype
        # First get ATM price
        ltp_resp = obj.ltpData("NSE", angel_sym, "99926000" if angel_sym == "NIFTY" else "99920000")
        if not ltp_resp or not ltp_resp.get("status"):
            logger.warning("Angel LTP fetch failed for %s", angel_sym)
            return None

        spot  = float(ltp_resp["data"]["ltp"])
        step  = 50 if angel_sym == "NIFTY" else 100
        atm   = int(round(spot / step) * step)

        # Get available expiry dates from master contract
        expiry_resp = obj.getExpiryDates(angel_sym, "OPTIDX")
        if not expiry_resp or not expiry_resp.get("data"):
            return None
        expiry_dates = expiry_resp["data"]
        nearest_expiry = expiry_dates[0] if expiry_dates else None
        if not nearest_expiry:
            return None

        # Fetch option chain for ATM ± 4 strikes
        chain = []
        total_call_oi = 0
        total_put_oi  = 0

        for offset in range(-4, 5):
            strike = atm + offset * step
            try:
                ce_resp = obj.optionGreek(angel_sym, nearest_expiry, str(strike), "CE", "OPTIDX")
                pe_resp = obj.optionGreek(angel_sym, nearest_expiry, str(strike), "PE", "OPTIDX")

                call_oi  = int(ce_resp.get("data", {}).get("openInterest", 0) or 0)
                put_oi   = int(pe_resp.get("data", {}).get("openInterest", 0) or 0)
                call_chg = int(ce_resp.get("data", {}).get("changeinOpenInterest", 0) or 0)
                put_chg  = int(pe_resp.get("data", {}).get("changeinOpenInterest", 0) or 0)
                call_ltp = float(ce_resp.get("data", {}).get("ltp", 0) or 0)
                put_ltp  = float(pe_resp.get("data", {}).get("ltp", 0) or 0)

                total_call_oi += call_oi
                total_put_oi  += put_oi

                chain.append({
                    "strike":   strike,
                    "call_oi":  call_oi, "call_chg": call_chg, "call_ltp": round(call_ltp, 1),
                    "put_oi":   put_oi,  "put_chg":  put_chg,  "put_ltp":  round(put_ltp, 1),
                    "atm":      (strike == atm),
                })
            except Exception as row_exc:
                logger.debug("Angel option row error strike=%d: %s", strike, row_exc)
                continue

        if not chain:
            return None

        pcr = round(total_put_oi / max(total_call_oi, 1), 2)
        logger.info("Angel option chain for %s — %d strikes, PCR=%.2f", symbol, len(chain), pcr)

        return {
            "records": {
                "underlyingValue": round(spot, 2),
                "expiryDates": expiry_dates,
                "data": [
                    {
                        "strikePrice": r["strike"],
                        "expiryDate":  nearest_expiry,
                        "CE": {
                            "openInterest": r["call_oi"],
                            "changeinOpenInterest": r["call_chg"],
                            "lastPrice": r["call_ltp"],
                        },
                        "PE": {
                            "openInterest": r["put_oi"],
                            "changeinOpenInterest": r["put_chg"],
                            "lastPrice": r["put_ltp"],
                        },
                    }
                    for r in chain
                ],
            },
            "_broker": "angel",
        }

    except Exception as exc:
        logger.warning("Angel option chain failed for %s: %s", symbol, exc)
        return None
