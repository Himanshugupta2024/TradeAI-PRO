from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings

_IST = ZoneInfo("Asia/Kolkata")


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def calculate_position_size(
    capital: float,
    entry: float,
    atr: float,
    risk_pct: float = 0.01,
    sl_atr_multiplier: float | None = None,
    max_single_trade_pct: float | None = None,
) -> int:
    """Risk a fixed capital fraction per trade, scaled by ATR stop distance.

    Args:
        capital: Available capital.
        entry: Planned entry price.
        atr: Current ATR value.
        risk_pct: Fraction of capital to risk, e.g. 0.01 = 1%.
        sl_atr_multiplier: ATR multiple used for stop distance.
        max_single_trade_pct: Max notional per trade as fraction of capital.

    Returns:
        Integer quantity. Returns 0 when constraints cannot be satisfied.
    """
    cap = _to_float(capital)
    px = _to_float(entry)
    atr_v = _to_float(atr)
    risk_f = _to_float(risk_pct, 0.01)

    sl_mult = _to_float(
        sl_atr_multiplier,
        _to_float(getattr(settings, "INTRADAY_ATR_SL_MULTIPLIER", 1.5), 1.5),
    )
    max_trade_f = _to_float(
        max_single_trade_pct,
        _to_float(getattr(settings, "INTRADAY_MAX_SINGLE_TRADE_PCT", 0.20), 0.20),
    )

    if cap <= 0 or px <= 0 or atr_v <= 0 or risk_f <= 0 or sl_mult <= 0 or max_trade_f <= 0:
        return 0

    risk_amount = cap * risk_f
    stop_distance = max(sl_mult * atr_v, px * 0.001)
    if stop_distance <= 0:
        return 0

    raw_qty = risk_amount / stop_distance
    max_qty_by_notional = (cap * max_trade_f) / px

    if max_qty_by_notional < 1:
        return 0

    qty = int(min(raw_qty, max_qty_by_notional))
    return max(qty, 1)


def calculate_sl_tp(
    entry: float,
    atr: float,
    direction: str = "LONG",
    sl_atr_multiplier: float | None = None,
    target_atr_multiplier: float | None = None,
) -> tuple[float, float]:
    """Compute ATR-based stop-loss and target for LONG/SHORT setups."""
    px = _to_float(entry)
    atr_v = _to_float(atr)
    if px <= 0 or atr_v <= 0:
        return (0.0, 0.0)

    sl_mult = _to_float(
        sl_atr_multiplier,
        _to_float(getattr(settings, "INTRADAY_ATR_SL_MULTIPLIER", 1.5), 1.5),
    )
    tp_mult = _to_float(
        target_atr_multiplier,
        _to_float(getattr(settings, "INTRADAY_TARGET_ATR_MULTIPLIER", 3.0), 3.0),
    )

    sl_dist = max(sl_mult * atr_v, px * 0.001)
    tp_dist = max(tp_mult * atr_v, sl_dist)

    d = str(direction or "LONG").upper()
    if d == "SHORT":
        stop = px + sl_dist
        target = px - tp_dist
    else:
        stop = px - sl_dist
        target = px + tp_dist

    return round(stop, 4), round(target, 4)


def calculate_risk_reward(entry: float, stop_loss: float, target: float) -> float:
    """Return absolute reward/risk ratio."""
    e = _to_float(entry)
    s = _to_float(stop_loss)
    t = _to_float(target)
    risk = abs(e - s)
    reward = abs(t - e)
    if risk <= 0:
        return 0.0
    return float(round(reward / risk, 4))


def can_take_trade(
    portfolio_state: dict,
    symbol: str | None = None,
    sector: str | None = None,
) -> tuple[bool, str]:
    """Portfolio-level guardrails for intraday execution."""
    state = portfolio_state or {}
    open_trades = int(state.get("open_trades", 0) or 0)
    daily_pnl_pct = _to_float(state.get("daily_pnl_pct", 0.0), 0.0)

    from agents.execution.runtime_controls import effective_float, effective_int

    max_open = effective_int(
        "INTRADAY_MAX_OPEN_TRADES", int(getattr(settings, "INTRADAY_MAX_OPEN_TRADES", 4) or 4)
    )
    max_loss = effective_float(
        "INTRADAY_MAX_DAILY_LOSS_PCT",
        _to_float(getattr(settings, "INTRADAY_MAX_DAILY_LOSS_PCT", 0.02), 0.02),
    )

    if open_trades >= max_open:
        return False, "Max open trades reached"
    if daily_pnl_pct <= -max_loss:
        return False, "Daily loss limit hit - trading paused"

    # Sector concentration limit (max 2 positions per sector)
    if sector and state.get("sector_positions"):
        sec_positions = int(state["sector_positions"].get(sector.upper(), 0) or 0)
        if sec_positions >= 2:
            return False, f"Sector concentration limit reached for {sector} (max 2 positions)"

    return True, "OK"


def should_square_off_now(now: datetime | None = None) -> bool:
    """Return True when IST time is beyond mandatory square-off cut-off."""
    dt = now
    if dt is None:
        dt = datetime.now(_IST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=_IST)
    else:
        dt = dt.astimezone(_IST)

    sq_h = int(getattr(settings, "INTRADAY_SQUARE_OFF_HOUR_IST", 15) or 15)
    sq_m = int(getattr(settings, "INTRADAY_SQUARE_OFF_MINUTE_IST", 15) or 15)
    return (dt.hour, dt.minute) >= (sq_h, sq_m)


def is_entry_cutoff_active(now: datetime | None = None) -> bool:
    """Return True when IST time is beyond intraday new trade entry cut-off (default 14:30 IST)."""
    dt = now
    if dt is None:
        dt = datetime.now(_IST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=_IST)
    else:
        dt = dt.astimezone(_IST)

    cut_h = int(getattr(settings, "INTRADAY_ENTRY_CUTOFF_HOUR_IST", 14) or 14)
    cut_m = int(getattr(settings, "INTRADAY_ENTRY_CUTOFF_MINUTE_IST", 30) or 30)
    return (dt.hour, dt.minute) >= (cut_h, cut_m)
