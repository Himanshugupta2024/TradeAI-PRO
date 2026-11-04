from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


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
                d[num_field] = round(float(d[num_field]), 2)

        if d.get("risk_reward") is not None:
            d["risk_reward"] = round(float(d["risk_reward"]), 2)
        if d.get("rvol") is not None:
            d["rvol"] = round(float(d["rvol"]), 2)
        if d.get("vwap") is not None:
            d["vwap"] = round(float(d["vwap"]), 2)
        if d.get("qty") is not None:
            d["qty"] = int(d["qty"])
        return d
