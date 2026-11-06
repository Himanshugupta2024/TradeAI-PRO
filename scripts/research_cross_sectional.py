"""
Cross-sectional momentum research.

Per-symbol signals ask "will this chart go up?". Cross-sectional momentum asks
a different question: "which names are strongest relative to their peers right
now?" — and holds a basket, not a single chart.

This script ranks the universe by trailing return, goes long the top quantile
and (optionally) short the bottom, rebalances every N bars, and reports the
portfolio after costs against an equal-weight benchmark.

    python scripts/research_cross_sectional.py --panel panel_real.csv --out xs.csv

No look-ahead: the ranking at date t uses returns up to t-1, and the position
earns returns from t onward.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def load_prices(panel: pd.DataFrame) -> pd.DataFrame:
    if "date" in panel.columns and "timestamp" not in panel.columns:
        panel = panel.rename(columns={"date": "timestamp"})
    panel = panel.copy()
    panel["timestamp"] = pd.to_datetime(panel["timestamp"])
    prices = panel.pivot(index="timestamp", columns="symbol", values="close").sort_index()
    prices = prices.dropna(how="all")
    return prices.astype(float)


def _metrics(returns: pd.Series, label: str) -> dict:
    returns = returns.dropna()
    if returns.empty:
        return {"label": label}
    equity = (1.0 + returns).cumprod()
    total = float(equity.iloc[-1] - 1.0)
    days = max((returns.index[-1] - returns.index[0]).days, 1)
    years = days / 365.25
    cagr = float((1.0 + total) ** (1.0 / years) - 1.0) if years > 0 and total > -1 else -1.0
    vol = float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(returns) > 1 else 0.0
    sharpe = float(returns.mean() / returns.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(returns) > 1 and returns.std(ddof=1) > 0 else 0.0
    peak = equity.cummax()
    drawdown = (equity / peak - 1.0).min()
    return {
        "label": label,
        "total_return_pct": round(total * 100.0, 2),
        "cagr_pct": round(cagr * 100.0, 2),
        "vol_pct": round(vol * 100.0, 2),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(float(drawdown) * 100.0, 2),
        "days": int(len(returns)),
    }


def cross_sectional_backtest(
    prices: pd.DataFrame,
    lookback: int = 20,
    hold: int = 10,
    quantile: float = 0.2,
    cost_pct: float = 0.39,
    long_short: bool = True,
    min_names: int = 10,
    reversal: bool = False,
) -> tuple[dict, pd.Series, pd.Series]:
    """Return (metrics, net daily returns, benchmark daily returns)."""
    returns = prices.pct_change().fillna(0.0)
    momentum = prices.pct_change(lookback).shift(1)

    dates = prices.index
    weights = pd.DataFrame(0.0, index=dates, columns=prices.columns)
    turnover_events = []

    previous = pd.Series(0.0, index=prices.columns)
    next_rebalance = lookback + 1

    for i, day in enumerate(dates):
        if i < next_rebalance or i + 1 >= len(dates):
            continue

        scores = momentum.loc[day].dropna()
        if len(scores) < min_names:
            continue

        n = max(1, int(len(scores) * quantile))
        ranked = scores.sort_values(ascending=reversal)
        longs = list(ranked.index[:n])
        shorts = list(ranked.index[-n:]) if long_short else []

        target = pd.Series(0.0, index=prices.columns)
        if long_short:
            target[longs] = 0.5 / len(longs)
            target[shorts] = -0.5 / len(shorts)
        else:
            target[longs] = 1.0 / len(longs)

        # Hold for `hold` bars: weight applies to returns from the next day.
        end = min(i + 1 + hold, len(dates))
        weights.iloc[i + 1:end] = target.values

        turnover = float((target - previous).abs().sum())
        turnover_events.append(turnover)
        previous = target
        next_rebalance = i + hold

    gross = (weights * returns).sum(axis=1)

    # Costs are charged on the rebalance day: turnover is one-way units, the
    # configured cost is a round trip, so each unit costs cost_pct / 2.
    costs = pd.Series(0.0, index=dates)
    rebalance_days = [dates[i] for i in range(len(dates)) if weights.iloc[i].abs().sum() > 0
                      and (i == 0 or not weights.iloc[i].equals(weights.iloc[i - 1]))]
    # Reconstruct turnover per rebalance from the weights diffs
    diff = weights.diff().abs().sum(axis=1)
    costs = diff * (cost_pct / 100.0 / 2.0)

    net = gross - costs
    benchmark = returns.mean(axis=1)

    metrics = _metrics(net, f"xs_mom_lb{lookback}_hold{hold}_{'ls' if long_short else 'lo'}")
    metrics.update(
        {
            "lookback": lookback,
            "hold": hold,
            "long_short": long_short,
            "avg_turnover": round(float(np.mean(turnover_events)) if turnover_events else 0.0, 3),
            "rebalances": len(turnover_events),
            "gross_total_pct": round(float(((1 + gross).cumprod().iloc[-1] - 1) * 100.0), 2),
            "net_total_pct": metrics.get("total_return_pct", 0.0),
        }
    )
    metrics["benchmark"] = _metrics(benchmark, "equal_weight")
    return metrics, net, benchmark


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-sectional momentum sweep")
    parser.add_argument("--panel", required=True)
    parser.add_argument("--lookbacks", default="20,60,120")
    parser.add_argument("--holds", default="5,10,21")
    parser.add_argument("--quantile", type=float, default=0.2)
    parser.add_argument("--cost-pct", type=float, default=0.39)
    parser.add_argument("--long-only", action="store_true")
    parser.add_argument("--reversal", action="store_true", help="long losers instead of winners")
    parser.add_argument("--out", default="xs_results.csv")
    args = parser.parse_args(argv)

    prices = load_prices(pd.read_csv(args.panel, parse_dates=["timestamp"]))
    lookbacks = [int(x) for x in args.lookbacks.split(",")]
    holds = [int(x) for x in args.holds.split(",")]

    rows = []
    for lookback in lookbacks:
        for hold in holds:
            metrics, _, _ = cross_sectional_backtest(
                prices,
                lookback=lookback,
                hold=hold,
                quantile=args.quantile,
                cost_pct=args.cost_pct,
                long_short=not args.long_only,
                reversal=args.reversal,
            )
            bench = metrics.pop("benchmark")
            metrics["bench_cagr_pct"] = bench["cagr_pct"]
            metrics["bench_sharpe"] = bench["sharpe"]
            rows.append(metrics)

    table = pd.DataFrame(rows).sort_values("cagr_pct", ascending=False)
    print(table.to_string(index=False))
    table.to_csv(args.out, index=False)
    print(f"\nwrote {len(table)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
