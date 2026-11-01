"""Reusable risk helpers for 1-3 day swing setups."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Iterable

from config import settings


def _normalize_direction(direction: str) -> str:
    d = str(direction or "LONG").upper()
    if d in {"BUY", "LONG"}:
        return "LONG"
    if d in {"SELL", "SHORT"}:
        return "SHORT"
    return "LONG"


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _trade_to_mapping(trade: Any) -> dict[str, Any]:
    if isinstance(trade, dict):
        return trade
    if is_dataclass(trade):
        return asdict(trade)
    out: dict[str, Any] = {}
    for key in [
        "entry",
        "entry_price",
        "stop",
        "stop_loss",
        "quantity",
        "qty",
        "risk_amount",
        "sector",
    ]:
        if hasattr(trade, key):
            out[key] = getattr(trade, key)
    return out


def calculate_swing_sl_tp(
    entry: float,
    atr: float,
    direction: str = "LONG",
    sl_multiplier: float | None = None,
    tp_multiplier: float | None = None,
) -> dict[str, float]:
    """Return canonical ATR-based stop/target for swing setups."""
    entry_val = _to_float(entry)
    atr_val = _to_float(atr)
    if entry_val <= 0 or atr_val <= 0:
        return {
            "stop": round(entry_val, 2),
            "target": round(entry_val, 2),
            "sl_distance": 0.0,
            "tp_distance": 0.0,
            "risk_reward": 0.0,
            "atr": atr_val,
        }

    sl_mult = _to_float(sl_multiplier, _to_float(getattr(settings, "SWING_ATR_SL_MULTIPLIER", 2.0), 2.0))
    tp_mult = _to_float(tp_multiplier, _to_float(getattr(settings, "SWING_ATR_TARGET_MULTIPLIER", 3.0), 3.0))
    sl_mult = max(sl_mult, 0.1)
    tp_mult = max(tp_mult, 0.1)

    direction_norm = _normalize_direction(direction)
    sl_distance = atr_val * sl_mult
    tp_distance = atr_val * tp_mult

    if direction_norm == "LONG":
        stop = entry_val - sl_distance
        target = entry_val + tp_distance
    else:
        stop = entry_val + sl_distance
        target = entry_val - tp_distance

    risk_reward = tp_distance / max(sl_distance, 1e-9)
    return {
        "stop": round(stop, 2),
        "target": round(target, 2),
        "sl_distance": round(sl_distance, 4),
        "tp_distance": round(tp_distance, 4),
        "risk_reward": round(risk_reward, 3),
        "atr": round(atr_val, 4),
    }


def calculate_swing_position_size(
    capital: float,
    entry: float,
    stop: float,
    risk_pct: float = 0.015,
) -> dict[str, float | int]:
    """Return quantity and exposure metrics from risk-based sizing."""
    capital_val = max(_to_float(capital), 0.0)
    entry_val = _to_float(entry)
    stop_val = _to_float(stop)
    risk_pct_val = max(_to_float(risk_pct), 0.0)

    if capital_val <= 0 or entry_val <= 0:
        return {
            "qty": 0,
            "risk_amount": 0.0,
            "capital_used": 0.0,
            "risk_pct": 0.0,
            "capital_pct": 0.0,
            "sl_distance": round(abs(entry_val - stop_val), 4),
        }

    risk_per_share = abs(entry_val - stop_val)
    if risk_per_share <= 0:
        return {
            "qty": 0,
            "risk_amount": 0.0,
            "capital_used": 0.0,
            "risk_pct": round(risk_pct_val * 100, 2),
            "capital_pct": 0.0,
            "sl_distance": 0.0,
        }

    risk_amount = capital_val * risk_pct_val
    qty = int(risk_amount // risk_per_share)
    max_affordable_qty = int(capital_val // entry_val)
    qty = max(0, min(qty, max_affordable_qty))

    capital_used = qty * entry_val
    actual_risk = qty * risk_per_share
    return {
        "qty": qty,
        "risk_amount": round(actual_risk, 2),
        "capital_used": round(capital_used, 2),
        "risk_pct": round(risk_pct_val * 100, 2),
        "capital_pct": round((capital_used / capital_val) * 100, 2) if capital_val > 0 else 0.0,
        "sl_distance": round(risk_per_share, 4),
    }


def overnight_risk_check(
    qty: int,
    entry: float,
    capital: float,
    open_trades: Iterable[Any] | None,
    *,
    sector: str | None = None,
    max_capital_per_trade: float | None = None,
    max_open_positions: int | None = None,
    max_sector_concentration: int | None = None,
    max_portfolio_risk_pct: float | None = None,
) -> tuple[bool, str, int]:
    """Apply swing overnight constraints and return (allowed, reason, adjusted_qty)."""
    capital_val = max(_to_float(capital), 0.0)
    entry_val = _to_float(entry)
    adjusted_qty = max(int(qty or 0), 0)
    if capital_val <= 0 or entry_val <= 0 or adjusted_qty <= 0:
        return False, "Invalid trade sizing inputs", 0

    cap_per_trade = _to_float(
        max_capital_per_trade,
        _to_float(getattr(settings, "SWING_MAX_CAPITAL_PER_TRADE", 0.25), 0.25),
    )
    max_positions = int(max_open_positions or getattr(settings, "SWING_MAX_OPEN_POSITIONS", 5) or 5)
    max_sector = int(
        max_sector_concentration
        or getattr(settings, "SWING_MAX_SECTOR_CONCENTRATION", 2)
        or 2
    )
    max_port_risk = _to_float(
        max_portfolio_risk_pct,
        _to_float(getattr(settings, "SWING_MAX_PORTFOLIO_RISK_PCT", 0.06), 0.06),
    )

    open_list = list(open_trades or [])
    reason = "OK"

    max_position_value = max(capital_val * cap_per_trade, 0.0)
    position_value = adjusted_qty * entry_val
    if max_position_value > 0 and position_value > max_position_value:
        adjusted_qty = int(max_position_value // entry_val)
        if adjusted_qty <= 0:
            return False, "Trade exceeds max capital-per-trade", 0
        reason = "Reduced qty to respect max capital-per-trade"

    if len(open_list) >= max_positions:
        return False, f"Max swing positions reached ({max_positions})", 0

    if sector:
        same_sector_count = 0
        for trade in open_list:
            trade_sector = str(_trade_to_mapping(trade).get("sector") or "").strip()
            if trade_sector and trade_sector == sector:
                same_sector_count += 1
        if same_sector_count >= max_sector:
            return False, f"Sector concentration limit reached for {sector}", 0

    total_open_risk = 0.0
    for trade in open_list:
        data = _trade_to_mapping(trade)
        risk_amount = _to_float(data.get("risk_amount"), 0.0)
        if risk_amount <= 0:
            entry_price = _to_float(data.get("entry_price", data.get("entry")), 0.0)
            stop_loss = _to_float(data.get("stop_loss", data.get("stop")), 0.0)
            quantity = _to_float(data.get("quantity", data.get("qty")), 0.0)
            if entry_price > 0 and stop_loss > 0 and quantity > 0:
                risk_amount = abs(entry_price - stop_loss) * quantity
        total_open_risk += max(risk_amount, 0.0)

    if capital_val > 0 and (total_open_risk / capital_val) > max_port_risk:
        return False, "Portfolio heat limit exceeded", 0

    return True, reason, adjusted_qty


def update_trailing_sl(
    entry: float,
    current_price: float,
    atr: float,
    original_sl: float,
    direction: str = "LONG",
) -> dict[str, float | str]:
    """Simple ATR-stage trailing model for swing holds."""
    entry_val = _to_float(entry)
    price_val = _to_float(current_price)
    atr_val = _to_float(atr)
    sl_val = _to_float(original_sl)
    dir_norm = _normalize_direction(direction)

    if entry_val <= 0 or atr_val <= 0:
        return {"new_sl": round(sl_val, 2), "action": "Hold original SL"}

    if dir_norm == "LONG":
        move = price_val - entry_val
        if move >= atr_val * 2:
            new_sl = max(sl_val, entry_val + atr_val * 0.5)
            return {"new_sl": round(new_sl, 2), "action": "Trail to lock gains"}
        if move >= atr_val:
            new_sl = max(sl_val, entry_val)
            return {"new_sl": round(new_sl, 2), "action": "Move SL to breakeven"}
    else:
        move = entry_val - price_val
        if move >= atr_val * 2:
            new_sl = min(sl_val, entry_val - atr_val * 0.5)
            return {"new_sl": round(new_sl, 2), "action": "Trail to lock gains"}
        if move >= atr_val:
            new_sl = min(sl_val, entry_val)
            return {"new_sl": round(new_sl, 2), "action": "Move SL to breakeven"}

    return {"new_sl": round(sl_val, 2), "action": "Hold original SL"}


__all__ = [
    "calculate_swing_sl_tp",
    "calculate_swing_position_size",
    "overnight_risk_check",
    "update_trailing_sl",
]
