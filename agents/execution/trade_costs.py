"""
Round-trip transaction-cost estimate for a closed paper trade.

Reuses the same statutory profiles as the backtester so paper results and
backtests agree: delivery ~29.3 bps round trip, intraday ~8.25 bps, before
slippage. Paper fills already assume no slippage, so only statutory charges
are deducted here.

Crypto pairs are outside the NSE cost model and return a zero-cost profile
rather than a made-up number.
"""

from __future__ import annotations

from agents.backtester.costs import DELIVERY, INTRADAY

ZERO_COST = {
    "profile": "NONE",
    "buy_cost": 0.0,
    "sell_cost": 0.0,
    "total_cost": 0.0,
    "roundtrip_bps": 0.0,
    "effective_bps": 0.0,
}


def cost_profile_for(symbol: str, signal_type: str):
    """Delivery for swing equity, intraday otherwise; None for crypto."""
    upper = str(symbol or "").upper()
    if "/" in upper or upper.endswith("USDT"):
        return None
    if "SWING" in str(signal_type or "").upper():
        return DELIVERY
    return INTRADAY


def estimate_trade_costs(
    *,
    entry_price: float,
    exit_price: float,
    quantity: float,
    symbol: str,
    signal_type: str = "",
) -> dict:
    profile = cost_profile_for(symbol, signal_type)
    if profile is None:
        return dict(ZERO_COST)

    entry = float(entry_price or 0.0)
    exit_ = float(exit_price or 0.0)
    qty = float(quantity or 0.0)
    if entry <= 0 or exit_ <= 0 or qty <= 0:
        out = dict(ZERO_COST)
        out["profile"] = profile.name
        return out

    buy_notional = entry * qty
    sell_notional = exit_ * qty
    buy_cost = buy_notional * profile.buy_bps() / 10_000.0
    sell_cost = sell_notional * profile.sell_bps() / 10_000.0
    total = buy_cost + sell_cost
    average_notional = (buy_notional + sell_notional) / 2.0

    return {
        "profile": profile.name,
        "buy_cost": round(buy_cost, 2),
        "sell_cost": round(sell_cost, 2),
        "total_cost": round(total, 2),
        "roundtrip_bps": round(profile.roundtrip_bps(), 2),
        "effective_bps": round(total / average_notional * 10_000.0, 2) if average_notional > 0 else 0.0,
    }
