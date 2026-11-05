from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


def format_price_by_magnitude(val: float | None, symbol: str = "") -> float | None:
    """Format price dynamically based on asset class and price magnitude to prevent rounding collapse."""
    if val is None:
        return None
    try:
        f = float(val)
    except (ValueError, TypeError):
        return None

    abs_f = abs(f)
    if abs_f == 0.0:
        return 0.0

    # Forex pairs (e.g. EUR/USD, GBP/USD, USD/INR)
    if "/" in symbol:
        if "JPY" in symbol:
            return round(f, 3)
        return round(f, 5)

    # Sub-rupee or micro-price crypto / penny tokens
    if abs_f < 0.001:
        return round(f, 8)
    elif abs_f < 0.1:
        return round(f, 6)
    elif abs_f < 1.0:
        return round(f, 4)
    elif abs_f < 50.0:
        return round(f, 3)
    else:
        return round(f, 2)


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
