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
