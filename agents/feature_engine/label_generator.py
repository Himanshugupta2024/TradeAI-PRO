"""
Outcome-Based Label Generator.

Answers: "Did this trade hit a 1:2 Risk-Reward within N candles?"
— a far more practical training label than "will next candle be green?"

Label values:
   1  (WIN)     — price reached TP (entry ± rr_ratio×ATR) within forward_bars
  -1  (LOSS)    — price hit SL (entry ± 1×ATR) before TP
   0  (TIMEOUT) — neither TP nor SL hit within forward_bars → treat as SKIP

Usage:
  labels = label_1r2r_outcome(df, forward_bars=5, rr_ratio=2.0)
  trainer.py can use this instead of the binary next-candle-direction label.
  trade_journal.py uses this to record actual outcomes for scorecard analysis.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from config import settings

logger = logging.getLogger(__name__)


def label_1r2r_outcome(
    df: pd.DataFrame,
    forward_bars: int = 5,
    rr_ratio: float = 2.0,
    atr_sl_multiplier: float | None = None,
) -> pd.Series:
    """
    Compute 1:RR outcome labels for every bar in df that has a LONG or SHORT
    signal. Bars with HOLD/SKIP get label 0 by default.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns: close, high, low, signal (LONG/SHORT/HOLD/SKIP),
        ATR_14 (or ATR_{ATR_PERIOD}).
    forward_bars : int
        Maximum number of candles to look forward for TP/SL hit.
    rr_ratio : float
        Risk-Reward ratio. TP = entry ± rr_ratio × SL_distance.
    atr_sl_multiplier : float | None
        ATR multiplier for SL distance. Defaults to settings.ATR_SL_MULTIPLIER.

    Returns
    -------
    pd.Series of int8 (1 / -1 / 0), index aligned to df.
    """
    if df.empty:
        return pd.Series(dtype="int8")

    sl_mult = atr_sl_multiplier if atr_sl_multiplier is not None else settings.ATR_SL_MULTIPLIER
    atr_col = f"ATR_{settings.ATR_PERIOD}"

    if atr_col not in df.columns:
        logger.warning("label_1r2r_outcome: %s column missing — returning all zeros", atr_col)
        return pd.Series(0, index=df.index, dtype="int8")

    if "signal" not in df.columns:
        logger.warning("label_1r2r_outcome: 'signal' column missing — returning all zeros")
        return pd.Series(0, index=df.index, dtype="int8")

    labels = pd.Series(0, index=df.index, dtype="int8")
    n = len(df)
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    atrs   = df[atr_col].values
    signals = df["signal"].values

    for i in range(n):
        sig = str(signals[i]).upper()
        if sig not in ("LONG", "BUY"):
            if sig not in ("SHORT", "SELL"):
                continue

        is_long = sig in ("LONG", "BUY")
        entry = closes[i]
        atr   = atrs[i]

        if np.isnan(atr) or atr <= 0:
            continue

        sl_dist = atr * sl_mult
        tp_dist = sl_dist * rr_ratio

        if is_long:
            tp = entry + tp_dist
            sl = entry - sl_dist
        else:
            tp = entry - tp_dist
            sl = entry + sl_dist

        outcome = 0  # default: timeout

        # Scan forward bars for TP or SL hit (whichever comes first)
        end = min(i + forward_bars + 1, n)
        for j in range(i + 1, end):
            if is_long:
                if highs[j] >= tp:
                    outcome = 1    # TP hit
                    break
                if lows[j] <= sl:
                    outcome = -1   # SL hit
                    break
            else:
                if lows[j] <= tp:
                    outcome = 1    # TP hit (short)
                    break
                if highs[j] >= sl:
                    outcome = -1   # SL hit (short)
                    break

        labels.iloc[i] = outcome

    return labels


def compute_label_stats(labels: pd.Series) -> dict:
    """
    Return basic win/loss/timeout statistics for a label Series.
    Useful for validating label distribution before training.
    """
    total = len(labels)
    if total == 0:
        return {"total": 0, "win_rate": 0.0, "loss_rate": 0.0, "timeout_rate": 0.0}

    wins     = int((labels == 1).sum())
    losses   = int((labels == -1).sum())
    timeouts = int((labels == 0).sum())

    signal_bars = wins + losses + timeouts

    return {
        "total_bars":    total,
        "signal_bars":   signal_bars,
        "wins":          wins,
        "losses":        losses,
        "timeouts":      timeouts,
        "win_rate":      round(wins   / max(signal_bars, 1), 4),
        "loss_rate":     round(losses / max(signal_bars, 1), 4),
        "timeout_rate":  round(timeouts / max(signal_bars, 1), 4),
    }


def add_outcome_labels(df: pd.DataFrame, **kwargs) -> pd.DataFrame:
    """
    Convenience wrapper — adds `outcome_label` column to df in-place.
    Pass forward_bars, rr_ratio, atr_sl_multiplier as kwargs if needed.
    """
    result = df.copy()
    result["outcome_label"] = label_1r2r_outcome(result, **kwargs)
    return result


# ── Triple-barrier labelling (López de Prado) ────────────────────────────────
#
# The 1R/2R helper above answers a narrow question and resolves a same-bar
# TP/SL touch in favour of the target. Triple-barrier labelling is stricter:
#
#   • upper barrier  = entry + rr_ratio × sl_distance   (target)
#   • lower barrier  = entry − sl_distance              (stop)
#   • vertical       = max_bars after entry             (time stop)
#   • same-bar touch of both barriers resolves to the STOP (conservative)
#
# Barriers are scaled by ATR at the entry bar, so they adapt to volatility.
# No future data is used to set the barriers.

TRIPLE_BARRIER_COLUMNS = [
    "tb_label",
    "tb_side",
    "tb_barrier",
    "tb_bars_held",
    "tb_exit_price",
    "tb_return_pct",
]


def triple_barrier_labels(
    df: pd.DataFrame,
    side: str = "auto",
    max_bars: int = 10,
    sl_atr_mult: float = 1.0,
    rr_ratio: float = 2.0,
    atr_col: str | None = None,
) -> pd.DataFrame:
    """Label every bar with its first-barrier outcome.

    Parameters
    ----------
    df : DataFrame
        Requires ``close``, ``high``, ``low`` and an ATR column.
        With ``side="auto"`` it also requires a ``signal`` column
        (LONG/BUY/SHORT/SELL); other values are left unlabelled.
    side : "auto" | "LONG" | "SHORT"
        Fixed side, or derive the side from the signal column.
    max_bars : int
        Vertical barrier: max bars to wait before a time exit.
    sl_atr_mult : float
        Stop distance in ATR units.
    rr_ratio : float
        Target distance as a multiple of the stop distance.
    atr_col : str | None
        Defaults to ``ATR_{settings.ATR_PERIOD}``.

    Returns
    -------
    DataFrame indexed like ``df`` with columns:

        tb_label      1 = target, -1 = stop, 0 = vertical, NaN = no side
        tb_side       LONG / SHORT
        tb_barrier    "target" | "stop" | "vertical"
        tb_bars_held  bars until the barrier was touched
        tb_exit_price barrier price (or close at the vertical barrier)
        tb_return_pct signed return from entry to exit, in percent
    """
    atr_name = atr_col or f"ATR_{settings.ATR_PERIOD}"
    empty = pd.DataFrame(index=df.index, columns=TRIPLE_BARRIER_COLUMNS, dtype="object")

    if df.empty or "close" not in df.columns or "high" not in df.columns or "low" not in df.columns:
        logger.warning("triple_barrier_labels: OHLC columns missing — returning empty frame")
        return empty
    if atr_name not in df.columns:
        logger.warning("triple_barrier_labels: %s missing — returning empty frame", atr_name)
        return empty

    n = len(df)
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    atrs = df[atr_name].to_numpy(dtype=float)

    if str(side).upper() == "AUTO":
        if "signal" not in df.columns:
            logger.warning("triple_barrier_labels: side='auto' needs a signal column")
            return empty
        sides = df["signal"].astype(str).str.upper().to_numpy()
    else:
        sides = [str(side).upper()] * n

    max_bars = max(int(max_bars), 1)
    labels: list[float] = []
    side_out: list[str] = []
    barriers: list[str] = []
    bars_held: list[float] = []
    exits: list[float] = []
    returns: list[float] = []

    for i in range(n):
        raw_side = str(sides[i]).upper()
        is_long = raw_side in ("LONG", "BUY")
        is_short = raw_side in ("SHORT", "SELL")
        if not (is_long or is_short):
            labels.append(np.nan); side_out.append(""); barriers.append("")
            bars_held.append(np.nan); exits.append(np.nan); returns.append(np.nan)
            continue

        entry = closes[i]
        atr = atrs[i]
        if not np.isfinite(entry) or entry <= 0 or not np.isfinite(atr) or atr <= 0:
            labels.append(np.nan); side_out.append("LONG" if is_long else "SHORT")
            barriers.append(""); bars_held.append(np.nan); exits.append(np.nan); returns.append(np.nan)
            continue

        sl_dist = atr * float(sl_atr_mult)
        tp_dist = sl_dist * float(rr_ratio)
        if sl_dist <= 0:
            labels.append(np.nan); side_out.append("LONG" if is_long else "SHORT")
            barriers.append(""); bars_held.append(np.nan); exits.append(np.nan); returns.append(np.nan)
            continue

        if is_long:
            target = entry + tp_dist
            stop = entry - sl_dist
        else:
            target = entry - tp_dist
            stop = entry + sl_dist

        label = 0.0
        barrier = "vertical"
        bars = 0
        exit_price = closes[min(i + max_bars, n - 1)]

        for k in range(1, max_bars + 1):
            j = i + k
            if j >= n:
                break
            stop_hit = lows[j] <= stop if is_long else highs[j] >= stop
            target_hit = highs[j] >= target if is_long else lows[j] <= target

            # Conservative same-bar arbitration: stop wins.
            if stop_hit:
                label, barrier, bars, exit_price = -1.0, "stop", k, stop
                break
            if target_hit:
                label, barrier, bars, exit_price = 1.0, "target", k, target
                break

        if barrier == "vertical":
            j = min(i + max_bars, n - 1)
            bars = max(j - i, 0)
            exit_price = closes[j]

        ret_pct = ((exit_price - entry) / entry) * 100.0
        if not is_long:
            ret_pct = -ret_pct

        labels.append(label)
        side_out.append("LONG" if is_long else "SHORT")
        barriers.append(barrier)
        bars_held.append(float(bars))
        exits.append(float(exit_price))
        returns.append(float(ret_pct))

    return pd.DataFrame(
        {
            "tb_label": labels,
            "tb_side": side_out,
            "tb_barrier": barriers,
            "tb_bars_held": bars_held,
            "tb_exit_price": exits,
            "tb_return_pct": returns,
        },
        index=df.index,
    )


def add_triple_barrier_labels(df: pd.DataFrame, **kwargs) -> pd.DataFrame:
    """Return a copy of df with the triple-barrier columns appended."""
    result = df.copy()
    tb = triple_barrier_labels(result, **kwargs)
    for column in TRIPLE_BARRIER_COLUMNS:
        result[column] = tb[column]
    return result


def triple_barrier_stats(tb: pd.DataFrame) -> dict:
    """Distribution of barrier outcomes — sanity-check before training."""
    if tb is None or tb.empty or "tb_label" not in tb.columns:
        return {"labelled": 0, "targets": 0, "stops": 0, "timeouts": 0,
                "target_rate": 0.0, "stop_rate": 0.0, "timeout_rate": 0.0}

    labelled = tb["tb_label"].dropna()
    total = int(len(labelled))
    targets = int((labelled == 1).sum())
    stops = int((labelled == -1).sum())
    timeouts = int((labelled == 0).sum())

    return {
        "labelled": total,
        "targets": targets,
        "stops": stops,
        "timeouts": timeouts,
        "target_rate": round(targets / total, 4) if total else 0.0,
        "stop_rate": round(stops / total, 4) if total else 0.0,
        "timeout_rate": round(timeouts / total, 4) if total else 0.0,
    }


def meta_labels_from_triple_barrier(
    tb: pd.DataFrame,
    primary_signals: pd.Series,
) -> pd.Series:
    """Meta-label a primary signal: 1 if it reached the target, else 0.

    Rows where the primary signal is not LONG/SHORT are NaN. This is the label
    for the second-stage model that decides whether to take (and how to size)
    the primary signal — it does not predict direction, only success.
    """
    if tb is None or tb.empty or "tb_label" not in tb.columns:
        return pd.Series(dtype="float64")

    actionable = primary_signals.astype(str).str.upper().isin(
        ("LONG", "BUY", "SHORT", "SELL")
    )
    labels = pd.Series(np.nan, index=tb.index, dtype="float64")
    labels[actionable & (tb["tb_label"] == 1)] = 1.0
    labels[actionable & (tb["tb_label"] == -1)] = 0.0
    return labels
