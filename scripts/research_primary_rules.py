"""
Primary-rule research harness.

Before tuning a meta-model, check whether the primary signal has any gross
edge at all. This script applies a rule to a daily panel, labels every signal
with the triple-barrier method, and reports train/test expectancy after costs.

    python scripts/research_primary_rules.py --panel panel_real.csv --out results.csv

Rules:
    ema_trend        close > EMA50, EMA20 > EMA50, 5-bar momentum > 0 (LONG), mirrored SHORT
    mean_reversion   RSI14 < 30 above EMA50 (LONG), RSI14 > 70 below EMA50 (SHORT)
    breakout         close above prior 20-day high (LONG), below prior 20-day low (SHORT)

The output is a table, not a verdict engine. Read it with the breakeven win
rate column: for RR=2 you need >33.3% wins before costs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.ai_engine.meta_labeler import (  # noqa: E402
    build_meta_dataset,
    compute_features,
    time_split,
)

DEFAULT_GRID = [(1.0, 2.0, 10), (1.0, 1.5, 10), (1.5, 2.0, 10), (2.0, 3.0, 10)]


def apply_rule(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = compute_features(frame)
    out["signal"] = "HOLD"

    if rule == "ema_trend":
        out.loc[
            (out["close"] > out["ema50"]) & (out["ema20"] > out["ema50"]) & (out["ret_5"] > 0),
            "signal",
        ] = "LONG"
        out.loc[
            (out["close"] < out["ema50"]) & (out["ema20"] < out["ema50"]) & (out["ret_5"] < 0),
            "signal",
        ] = "SHORT"

    elif rule == "mean_reversion":
        out.loc[(out["rsi_14"] < 30) & (out["close"] > out["ema50"]), "signal"] = "LONG"
        out.loc[(out["rsi_14"] > 70) & (out["close"] < out["ema50"]), "signal"] = "SHORT"

    elif rule == "breakout":
        out["hh20"] = out["high"].rolling(20).max().shift(1)
        out["ll20"] = out["low"].rolling(20).min().shift(1)
        out.loc[out["close"] > out["hh20"], "signal"] = "LONG"
        out.loc[out["close"] < out["ll20"], "signal"] = "SHORT"

    else:
        raise ValueError(f"unknown rule '{rule}'")

    return out


def build_signal_panel(panel: pd.DataFrame, rule: str, min_gap: int = 0) -> pd.DataFrame:
    """Apply the rule per symbol; optionally enforce a gap between signals.

    Triple-barrier labels overlap when a signal fires every bar, so the
    effective sample is much smaller than ``len(data)``. A gap of 10 bars
    gives roughly independent observations.
    """
    frames = []
    for _, group in panel.groupby("symbol"):
        if len(group) < 60:
            continue
        frame = apply_rule(group.copy().sort_values("timestamp"), rule)

        if min_gap > 0:
            actionable = frame["signal"].isin(["LONG", "SHORT"]).to_numpy()
            keep = []
            last = -10 ** 9
            for index, flag in enumerate(actionable):
                if flag and index - last >= min_gap:
                    keep.append(True)
                    last = index
                else:
                    keep.append(False)
            frame.loc[~pd.Series(keep, index=frame.index), "signal"] = "HOLD"

        frames.append(frame)
    if not frames:
        raise ValueError("no symbol had enough history")
    return pd.concat(frames, ignore_index=True)


def evaluate(
    panel: pd.DataFrame,
    rule: str,
    sl_atr_mult: float,
    rr_ratio: float,
    max_bars: int,
    cost_pct: float,
    min_gap: int = 0,
) -> dict:
    signal_panel = build_signal_panel(panel, rule, min_gap=min_gap)
    data = build_meta_dataset(
        signal_panel,
        max_bars=max_bars,
        sl_atr_mult=sl_atr_mult,
        rr_ratio=rr_ratio,
        cost_pct=cost_pct,
    )
    split = time_split(data)
    return {
        "rule": rule,
        "sl_atr": sl_atr_mult,
        "rr": rr_ratio,
        "max_bars": max_bars,
        "min_gap": min_gap,
        "signals": len(data),
        "breakeven_wr": round(100.0 / (1.0 + rr_ratio), 1),
        "train_wr": round(float(split.train["meta_label"].mean()) * 100.0, 1),
        "train_exp_pct": round(float(split.train["net_return_pct"].mean()), 3),
        "test_wr": round(float(split.test["meta_label"].mean()) * 100.0, 1),
        "test_exp_pct": round(float(split.test["net_return_pct"].mean()), 3),
        "test_rows": len(split.test),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Primary-rule edge sweep")
    parser.add_argument("--panel", required=True, help="CSV with timestamp,symbol,OHLCV")
    parser.add_argument("--rules", default="ema_trend,mean_reversion,breakout")
    parser.add_argument("--cost-pct", type=float, default=0.39)
    parser.add_argument("--min-gap", type=int, default=0, help="minimum bars between signals (10 = ~independent)")
    parser.add_argument("--out", default="primary_rule_results.csv")
    args = parser.parse_args(argv)

    panel = pd.read_csv(args.panel, parse_dates=["timestamp"])
    if "date" in panel.columns and "timestamp" not in panel.columns:
        panel = panel.rename(columns={"date": "timestamp"})

    rules = [r.strip() for r in args.rules.split(",") if r.strip()]
    rows = []
    for rule in rules:
        for sl, rr, bars in DEFAULT_GRID:
            try:
                rows.append(evaluate(panel, rule, sl, rr, bars, args.cost_pct, min_gap=args.min_gap))
            except Exception as exc:
                print(f"skip {rule} sl={sl} rr={rr} bars={bars}: {exc}")

    table = pd.DataFrame(rows).sort_values(["rule", "train_exp_pct"], ascending=[True, False])
    print(table.to_string(index=False))
    table.to_csv(args.out, index=False)
    print(f"\nwrote {len(table)} rows to {args.out}")

    positive = table[table["train_exp_pct"] > 0]
    if positive.empty:
        print("No configuration had positive train expectancy — fix the primary rule before modelling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
