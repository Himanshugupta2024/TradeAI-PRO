from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class IntradaySignal:
    symbol: str
    strategy: str  # ORB | VWAP | GAP_GO | MOMENTUM
    direction: str  # LONG | SHORT | SKIP
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
    grade: str = "C"  # Confluence grade: A = 3+ strats agree, B = 2, C = 1
    confluence_count: int = 1  # Number of strategies agreeing on direction
    rvol: float | None = None
    vwap: float | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["score"] = round(float(d["score"]), 2)
        if d.get("entry") is not None:
            d["entry"] = round(float(d["entry"]), 4)
        if d.get("stop_loss") is not None:
            d["stop_loss"] = round(float(d["stop_loss"]), 4)
        if d.get("target") is not None:
            d["target"] = round(float(d["target"]), 4)
        if d.get("atr") is not None:
            d["atr"] = round(float(d["atr"]), 4)
        if d.get("risk_reward") is not None:
            d["risk_reward"] = round(float(d["risk_reward"]), 2)
        if d.get("rvol") is not None:
            d["rvol"] = round(float(d["rvol"]), 2)
        if d.get("vwap") is not None:
            d["vwap"] = round(float(d["vwap"]), 2)
        if d.get("qty") is not None:
            d["qty"] = int(d["qty"])
        return d
