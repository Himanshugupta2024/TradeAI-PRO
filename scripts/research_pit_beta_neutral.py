"""
Point-in-time universe + beta-neutral evaluation for the reversal basket.

Two criticisms of the earlier reversal result:

1. **Beta.** The basket had beta ≈ 1, so most of the return was market.
2. **Survivorship.** The universe was today's NIFTY members, not the names that
   existed at the time.

This script addresses both:

* ``--membership`` accepts a point-in-time membership CSV
  (``symbol,start,end``); rows outside a symbol's window never enter the basket.
* ``--hedge beta`` shorts the equal-weight universe with a rolling beta, so the
  reported return is closer to alpha. ``--hedge equal`` shorts 100%.
* ``--exclude`` drops symbols, for concentration stress tests.

    python scripts/research_pit_beta_neutral.py --panel panel_real.csv \
        --membership membership.csv --hedge beta --out pit_beta.csv
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
    return prices.dropna(how="all").astype(float)


def load_membership(path: str | None) -> pd.DataFrame | None:
    if not path:
        return None
    frame = pd.read_csv(path)
    required = {"symbol", "start"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"membership file missing columns: {sorted(missing)}")
    frame["symbol"] = frame["symbol"].astype(str).str.upper().str.strip()
    frame["start"] = pd.to_datetime(frame["start"])
    frame["end"] = pd.to_datetime(frame["end"]) if "end" in frame.columns else pd.NaT
    return frame


def membership_mask(dates: pd.DatetimeIndex, symbols: list[str], membership: pd.DataFrame | None) -> pd.DataFrame:
    """Boolean date × symbol frame: was the symbol in the universe that day?"""
    if membership is None:
        return pd.DataFrame(True, index=dates, columns=symbols)

    mask = pd.DataFrame(False, index=dates, columns=symbols)
    for _, row in membership.iterrows():
        symbol = row["symbol"]
        if symbol not in symbols:
            continue
        active = dates >= row["start"]
        if pd.notna(row["end"]):
            active &= dates <= row["end"]
        mask.loc[active, symbol] = True
    return mask


def _metrics(returns: pd.Series) -> dict:
    returns = returns.dropna()
    if returns.empty:
        return {}
    equity = (1 + returns).cumprod()
    total = float(equity.iloc[-1] - 1)
    years = max((returns.index[-1] - returns.index[0]).days, 1) / 365.25
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1
    vol = float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(returns) > 1 else 0.0
    sharpe = float(returns.mean() / returns.std(ddof=1) * np.sqrt(TRADING_DAYS)) if vol > 0 else 0.0
    drawdown = float((equity / equity.cummax() - 1).min())
    return {
        "total_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "vol_pct": round(vol * 100, 2),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(drawdown * 100, 2),
    }


def run(
    prices: pd.DataFrame,
    lookback: int = 20,
    hold: int = 15,
    quantile: float = 0.2,
    cost_pct: float = 0.39,
    hedge: str = "beta",
    beta_window: int = 60,
    membership: pd.DataFrame | None = None,
) -> dict:
    returns = prices.pct_change().fillna(0.0)
    momentum = prices.pct_change(lookback).shift(1)
    mask = membership_mask(prices.index, list(prices.columns), membership)

    dates = prices.index
    weights = pd.DataFrame(0.0, index=dates, columns=prices.columns)
    hedge_weights = pd.Series(0.0, index=dates)

    next_rebalance = lookback + 1
    long_history = pd.Series(0.0, index=dates)
    bench = returns.mean(axis=1)

    for i, day in enumerate(dates):
        # Track the hypothetical long-only return of the current basket so we can
        # estimate beta at the next rebalance.
        long_history.loc[day] = float((weights.iloc[i] * returns.iloc[i]).sum())

        if i < next_rebalance or i + 1 >= len(dates):
            continue

        eligible = momentum.loc[day][mask.loc[day]].dropna()
        if len(eligible) < 10:
            continue

        ranked = eligible.sort_values(ascending=True)  # reversal: weakest first
        n = max(1, int(len(ranked) * quantile))
        longs = list(ranked.index[:n])

        target = pd.Series(0.0, index=prices.columns)
        target[longs] = 1.0 / len(longs)

        end = min(i + 1 + hold, len(dates))
        weights.iloc[i + 1:end] = target.values

        if hedge == "none":
            beta = 0.0
        elif hedge == "equal":
            beta = 1.0
        else:
            window = long_history.iloc[max(0, i - beta_window):i]
            bench_window = bench.iloc[max(0, i - beta_window):i]
            variance = float(np.var(bench_window))
            beta = float(np.cov(window, bench_window)[0, 1] / variance) if variance > 0 else 1.0
            beta = float(np.clip(beta, 0.0, 1.5))

        hedge_weights.iloc[i + 1:end] = beta
        next_rebalance = i + hold

    gross_long = (weights * returns).sum(axis=1)
    gross = gross_long - hedge_weights * bench

    turnover = (weights.diff().abs().sum(axis=1) + hedge_weights.diff().abs()).fillna(0.0)
    net = gross - turnover * (cost_pct / 100.0 / 2.0)

    beta_total = float(np.cov(net, bench)[0, 1] / np.var(bench)) if np.var(bench) > 0 else 0.0
    alpha_daily = float(net.mean() - beta_total * bench.mean())
    alpha_ann = alpha_daily * TRADING_DAYS

    result = _metrics(net)
    result.update(
        {
            "lookback": lookback,
            "hold": hold,
            "quantile": quantile,
            "hedge": hedge,
            "beta": round(beta_total, 3),
            "alpha_ann_pct": round(alpha_ann * 100, 2),
            "excess_total_pct": round((net - bench).sum() * 100, 2),
            "avg_hedge": round(float(hedge_weights.mean()), 3),
            "turnover_avg": round(float(turnover.mean()), 3),
            "dates": len(net),
            "membership": "yes" if membership is not None else "no",
        }
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PIT universe + beta-neutral reversal")
    parser.add_argument("--panel", required=True)
    parser.add_argument("--membership", help="CSV symbol,start,end")
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--holds", default="10,15,21")
    parser.add_argument("--quantile", type=float, default=0.2)
    parser.add_argument("--cost-pct", type=float, default=0.39)
    parser.add_argument("--beta-window", type=int, default=60)
    parser.add_argument("--exclude", default="", help="comma-separated symbols to drop")
    parser.add_argument("--out", default="pit_beta_results.csv")
    args = parser.parse_args(argv)

    prices = load_prices(pd.read_csv(args.panel, parse_dates=["timestamp"]))
    excludes = [s.strip().upper() for s in args.exclude.split(",") if s.strip()]
    if excludes:
        prices = prices.drop(columns=[s for s in excludes if s in prices.columns])

    membership = load_membership(args.membership)
    if membership is None:
        print("WARNING: no --membership file; survivorship bias is NOT controlled")

    rows = []
    for hold in [int(x) for x in args.holds.split(",")]:
        for hedge in ("none", "equal", "beta"):
            rows.append(
                run(
                    prices,
                    lookback=args.lookback,
                    hold=hold,
                    quantile=args.quantile,
                    cost_pct=args.cost_pct,
                    hedge=hedge,
                    beta_window=args.beta_window,
                    membership=membership,
                )
            )

    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    table.to_csv(args.out, index=False)
    print(f"\nwrote {len(table)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
