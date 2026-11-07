"""
Public track-record builder.

Turns the paper-trading history into a single, honest summary: every closed
trade (winners and losers), per-year and per-strategy breakdowns, drawdown and
sample-size qualification.

No filtering, no cherry-picking, no "best trades only". If the record is bad,
the page should say so.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # NaN guard


def trade_pnl(trade: dict) -> float:
    """Rupee P&L for one closed trade, net of estimated costs when available."""
    if trade.get("cost_profile") and trade.get("cost_profile") != "UNAVAILABLE":
        net = _as_float(trade.get("net_pnl"))
        if abs(net) > 1e-9:
            return net

    for key in ("total_pnl", "pnl_inr", "realized_pnl"):
        val = trade.get(key)
        if val is not None and abs(_as_float(val)) > 1e-9:
            return _as_float(val)

    if trade.get("net_pnl") is not None and abs(_as_float(trade.get("net_pnl"))) > 1e-9:
        return _as_float(trade.get("net_pnl"))

    if trade.get("pnl") is not None:
        pnl_calc = _as_float(trade.get("pnl")) * _as_float(trade.get("quantity"), 1.0)
        if abs(pnl_calc) > 1e-9:
            return pnl_calc

    for key in ("total_pnl", "net_pnl", "pnl_inr", "realized_pnl"):
        if trade.get(key) is not None:
            return _as_float(trade.get(key))

    return _as_float(trade.get("pnl")) * _as_float(trade.get("quantity"), 1.0)


def _trade_return_pct(trade: dict) -> Optional[float]:
    pnl = trade_pnl(trade)
    entry = _as_float(trade.get("entry_price"))
    qty = _as_float(trade.get("quantity"), 1.0)
    notional = entry * qty
    if notional <= 0:
        return None
    return round(pnl / notional * 100.0, 2)


def _normalise_trade(trade: dict) -> dict:
    qc = trade.get("quote_currency") or ("USDT" if "/" in str(trade.get("symbol", "")) else "INR")
    fx_status = trade.get("fx_status") or "ACTIVE"
    gross = trade.get("total_pnl")
    gross_val = _as_float(gross) if gross is not None else trade_pnl(trade)
    return {
        "id": trade.get("id"),
        "symbol": trade.get("symbol"),
        "direction": str(trade.get("direction") or trade.get("side") or "").upper(),
        "strategy": trade.get("signal_type") or trade.get("strategy") or "UNKNOWN",
        "entry_price": _as_float(trade.get("entry_price")),
        "exit_price": _as_float(trade.get("exit_price")),
        "quantity": _as_float(trade.get("quantity"), 1.0),
        "quote_currency": qc,
        "fx_status": fx_status,
        "pnl": round(trade_pnl(trade), 2),
        "gross_pnl": round(gross_val, 2),
        "estimated_costs": round(_as_float(trade.get("estimated_costs")), 2),
        "net_pnl": round(trade_pnl(trade), 2),
        "return_pct": _trade_return_pct(trade),
        "exit_reason": trade.get("exit_reason") or trade.get("status") or "",
        "entry_time": trade.get("entry_time"),
        "exit_time": trade.get("exit_time") or trade.get("timestamp"),
    }


def _summarise(trades: list[dict]) -> dict:
    pnls = [trade_pnl(t) for t in trades]
    wins = [p for p in pnls if p > 1e-6]
    losses = [p for p in pnls if p < -1e-6]
    breakevens = [p for p in pnls if abs(p) <= 1e-6]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total = sum(pnls)

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": len(breakevens),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 1) if trades else 0.0,
        "pnl": round(total, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
    }


def build_track_record(
    trades: Iterable[dict],
    initial_capital: Optional[float] = None,
    min_sample: int = 100,
) -> dict:
    """Build the full public record from closed paper trades."""
    closed: list[dict] = []
    for trade in trades or []:
        if not isinstance(trade, dict):
            continue
        status = str(trade.get("status") or "").upper()
        if status in ("OPEN", "PENDING"):
            continue
        if not trade.get("exit_time") and not trade.get("exit_price"):
            continue
        closed.append(trade)

    closed.sort(key=lambda t: str(t.get("exit_time") or t.get("timestamp") or ""))

    # Quarantine unconverted non-INR legacy trades from INR totals (F05)
    inr_closed: list[dict] = []
    unconverted_trades: list[dict] = []
    for t in closed:
        if t.get("fx_status") == "LEGACY_UNCONVERTED" and (t.get("quote_currency") or "INR") != "INR":
            unconverted_trades.append(t)
        else:
            inr_closed.append(t)

    overall = _summarise(inr_closed)
    normalised = [_normalise_trade(t) for t in closed]
    inr_normalised = [_normalise_trade(t) for t in inr_closed]

    # Equity curve and drawdown based on INR trades
    start_equity = _as_float(initial_capital) if initial_capital is not None else 0.0
    equity = start_equity
    peak = start_equity
    max_dd = 0.0
    max_dd_pct = 0.0
    curve = []
    for trade in inr_normalised:
        equity += trade["pnl"]
        peak = max(peak, equity)
        if peak > 0:
            drawdown = peak - equity
            if drawdown > max_dd:
                max_dd = drawdown
                max_dd_pct = drawdown / peak * 100.0
        curve.append({"t": trade["exit_time"], "equity": round(equity, 2)})

    # Per-year
    by_year: dict[str, list[dict]] = defaultdict(list)
    for trade in inr_closed:
        stamp = str(trade.get("exit_time") or trade.get("timestamp") or "")
        by_year[stamp[:4] if len(stamp) >= 4 else "unknown"].append(trade)
    per_year = [
        {"year": year, **_summarise(items)}
        for year, items in sorted(by_year.items())
    ]

    # Per-strategy
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for trade in inr_closed:
        key = str(trade.get("signal_type") or trade.get("strategy") or "UNKNOWN").upper()
        by_strategy[key].append(trade)
    per_strategy = [
        {"strategy": key, **_summarise(items)}
        for key, items in sorted(by_strategy.items(), key=lambda kv: -len(kv[1]))
    ]

    # Per-symbol (top 15 by trade count, based on inr_closed)
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for trade in inr_closed:
        by_symbol[str(trade.get("symbol") or "?")].append(trade)
    per_symbol = [
        {"symbol": key, **_summarise(items)}
        for key, items in sorted(by_symbol.items(), key=lambda kv: -len(kv[1]))[:15]
    ]

    best = max((t["pnl"] for t in inr_normalised), default=0.0)
    worst = min((t["pnl"] for t in inr_normalised), default=0.0)

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "paper_only": True,
        "initial_capital": round(start_equity, 2) if initial_capital is not None else None,
        "sample_status": "VALID" if overall["total_trades"] >= min_sample else "INSUFFICIENT_SAMPLE",
        "min_sample": min_sample,
        **overall,
        "best_trade": round(best, 2),
        "worst_trade": round(worst, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "equity_curve": curve,
        "per_year": per_year,
        "per_strategy": per_strategy,
        "per_symbol": per_symbol,
        "trades": inr_normalised,
        "unconverted_legacy_trades": [_normalise_trade(t) for t in unconverted_trades],
        "disclaimer": (
            "Paper trading only. No broker connection, no real orders. "
            "Not investment advice. Not a SEBI-registered research analyst or investment adviser. "
            "Every closed trade is shown, including losses; results are not audited."
        ),
    }
