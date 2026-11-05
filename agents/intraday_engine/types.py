from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


def get_instrument_tick_size(symbol: str) -> float:
    """Return exchange-standard minimum tick size for instrument."""
    sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "")

    # Forex
    if "/" in sym or sym.endswith("=X"):
        if "JPY" in sym:
            return 0.001
        if "INR" in sym:
            return 0.0025  # NSE Currency Derivatives standard tick
        return 0.0001

    # Crypto
    if any(c in sym for c in ("USDT", "BUSD", "USDC")) or "/" in symbol:
        if "BTC" in sym:
            return 0.10
        if any(m in sym for m in ("ETH", "SOL", "BNB")):
            return 0.01
        if any(m in sym for m in ("DOGE", "SHIB", "PEPE")):
            return 0.00001
        return 0.0001

    # Indian Equities (NSE cash market standard tick size is ₹0.05)
    return 0.05


def normalize_price_to_tick(
    symbol: str, raw_price: float | None, direction: str = "ROUND"
) -> float | None:
    """
    Normalize price strictly to exchange/instrument tick boundaries.
    direction:
      - 'ROUND': round to nearest valid tick
      - 'FLOOR': round down to nearest valid tick (e.g. SL for LONG, TP for SHORT)
      - 'CEIL':  round up to nearest valid tick (e.g. TP for LONG, SL for SHORT)
    """
    if raw_price is None:
        return None
    try:
        price = float(raw_price)
    except (ValueError, TypeError):
        return None

    if price <= 0:
        return 0.0

    tick = get_instrument_tick_size(symbol)
    if tick <= 0:
        return price

    steps = price / tick

    if direction == "FLOOR":
        norm = math.floor(steps + 1e-9) * tick
    elif direction == "CEIL":
        norm = math.ceil(steps - 1e-9) * tick
    else:
        norm = round(steps) * tick

    if tick >= 1.0:
        return round(norm, 1)
    elif tick >= 0.01:
        return round(norm, 2)
    elif tick >= 0.001:
        return round(norm, 3)
    elif tick >= 0.0001:
        return round(norm, 4)
    elif tick >= 0.00001:
        return round(norm, 5)
    else:
        return round(norm, 8)


def format_price_by_magnitude(val: float | None, symbol: str = "") -> float | None:
    """Format price for display based on instrument tick size and price magnitude."""
    return normalize_price_to_tick(symbol, val, "ROUND")


@dataclass
class IntradaySignal:
    symbol: str
    strategy: str  # ORB | VWAP | GAP_GO | MOMENTUM | CPR | SUPERTREND | EMA | RSI
    direction: str  # LONG | SHORT | HOLD | SKIP
    score: float
    entry: float | None
    stop_loss: float | None
    target: float | None
    qty: int | None = None
    risk_reward: float | None = None
    atr: float | None = None
    risk_ok: bool = True
    risk_reason: str = "OK"
    rationale: str = ""
    interval: str = "5m"
    grade: str = "C"  # A = 3+ strats, B = 2, C = 1
    confluence_count: int = 1
    rvol: float | None = None
    vwap: float | None = None
    # Enhanced Institutional Fields
    target_1: float | None = None
    target_2: float | None = None
    support: float | None = None
    resistance: float | None = None
    confluence_score: float | None = None
    classification: str = "NEUTRAL"  # STRONG LONG | LONG | WATCH LONG | NEUTRAL | WATCH SHORT | SHORT | STRONG SHORT
    factors: List[Dict[str, Any]] | None = None
    reasons: List[str] | None = None
    risks: List[str] | None = None
    why_ranked: str = ""
    technical_snapshot: Dict[str, Any] | None = None
    data_status: str = "DELAYED"  # LIVE | DELAYED | EOD | STALE | UNAVAILABLE
    session_state: str = "NORMAL_SESSION"
    signal_session_date: str | None = None
    generated_at: str | None = None
    source_candle_time: str | None = None
    valid_until: str | None = None
    is_expired: bool = False
    rank: int = 1

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["score"] = round(float(d["score"]), 2)
        if d.get("confluence_score") is not None:
            d["confluence_score"] = round(float(d["confluence_score"]), 1)
        else:
            d["confluence_score"] = round(float(d["score"]), 1)

        for num_field in ("entry", "stop_loss", "target", "target_1", "target_2", "support", "resistance", "atr"):
            if d.get(num_field) is not None:
                d[num_field] = format_price_by_magnitude(d[num_field], self.symbol)

        if d.get("risk_reward") is not None:
            d["risk_reward"] = round(float(d["risk_reward"]), 2)
        if d.get("rvol") is not None:
            d["rvol"] = round(float(d["rvol"]), 2)
        if d.get("vwap") is not None:
            d["vwap"] = format_price_by_magnitude(d["vwap"], self.symbol)
        if d.get("qty") is not None:
            d["qty"] = int(d["qty"])
        return d
