"""
Support & Resistance Zone Detector.

Two methods combined for stronger levels:
  1. Pivot-based S/R (swing highs/lows with reduced window for faster detection)
  2. Volume Profile S/R (POC, Value Area High/Low — where most volume traded)

The final support/resistance picks the level closest to price from either method.
"""

import pandas as pd
import numpy as np


def _find_pivot_highs(df: pd.DataFrame, window: int = 10) -> pd.Series:
    """Find local high pivot points (swing highs)."""
    highs = df["high"]
    pivot_highs = pd.Series(np.nan, index=df.index)

    for i in range(window, len(df) - window):
        if highs.iloc[i] == highs.iloc[i - window : i + window + 1].max():
            pivot_highs.iloc[i] = highs.iloc[i]

    return pivot_highs


def _find_pivot_lows(df: pd.DataFrame, window: int = 10) -> pd.Series:
    """Find local low pivot points (swing lows)."""
    lows = df["low"]
    pivot_lows = pd.Series(np.nan, index=df.index)

    for i in range(window, len(df) - window):
        if lows.iloc[i] == lows.iloc[i - window : i + window + 1].min():
            pivot_lows.iloc[i] = lows.iloc[i]

    return pivot_lows


def _compute_volume_profile(
    df: pd.DataFrame,
    start: int,
    end: int,
    num_bins: int = 30,
) -> dict:
    """
    Compute volume profile for a slice of the DataFrame.

    Returns dict with:
      poc   : Point of Control (price level with highest volume)
      vah   : Value Area High  (upper bound of 70% volume zone)
      val   : Value Area Low   (lower bound of 70% volume zone)
    """
    slc = df.iloc[start:end]
    if slc.empty or "volume" not in slc.columns:
        return {"poc": np.nan, "vah": np.nan, "val": np.nan}

    prices = slc["close"]
    volumes = slc["volume"].fillna(0)

    if volumes.sum() <= 0:
        return {"poc": np.nan, "vah": np.nan, "val": np.nan}

    price_min = prices.min()
    price_max = prices.max()
    if price_max == price_min:
        return {"poc": float(price_min), "vah": float(price_max), "val": float(price_min)}

    # Create price bins and accumulate volume in each bin
    bin_edges = np.linspace(price_min, price_max, num_bins + 1)
    bin_volumes = np.zeros(num_bins)

    for price, vol in zip(prices, volumes):
        bin_idx = int(np.clip(
            np.searchsorted(bin_edges, price, side="right") - 1,
            0, num_bins - 1,
        ))
        bin_volumes[bin_idx] += vol

    # POC = midpoint of the bin with highest volume
    poc_bin = int(np.argmax(bin_volumes))
    poc = float((bin_edges[poc_bin] + bin_edges[poc_bin + 1]) / 2)

    # Value Area: expand from POC until 70% of total volume is covered
    total_vol = bin_volumes.sum()
    target_vol = total_vol * 0.70
    va_low_idx = poc_bin
    va_high_idx = poc_bin
    accumulated = bin_volumes[poc_bin]

    while accumulated < target_vol and (va_low_idx > 0 or va_high_idx < num_bins - 1):
        # Expand toward the side with more volume
        vol_below = bin_volumes[va_low_idx - 1] if va_low_idx > 0 else 0
        vol_above = bin_volumes[va_high_idx + 1] if va_high_idx < num_bins - 1 else 0

        if vol_below >= vol_above and va_low_idx > 0:
            va_low_idx -= 1
            accumulated += bin_volumes[va_low_idx]
        elif va_high_idx < num_bins - 1:
            va_high_idx += 1
            accumulated += bin_volumes[va_high_idx]
        else:
            va_low_idx -= 1
            accumulated += bin_volumes[va_low_idx]

    val = float(bin_edges[va_low_idx])
    vah = float(bin_edges[va_high_idx + 1])

    return {"poc": poc, "vah": vah, "val": val}


def calculate_support_resistance(
    df: pd.DataFrame,
    pivot_window: int = 10,
    lookback: int = 100,
) -> pd.DataFrame:
    """
    Calculate nearest support and resistance levels for each candle.

    Combines:
      - Pivot-based S/R (swing highs/lows)
      - Volume Profile S/R (POC, VAH, VAL)

    The final level picks whichever method gives the level CLOSEST to
    (but still below/above) the current price.

    Args:
        df: OHLCV DataFrame (must have open/high/low/close/volume columns)
        pivot_window: Window size for pivot detection (reduced to 10 for speed)
        lookback: How many past candles to search

    Returns:
        DataFrame with added columns: support, resistance, poc, vah, val
    """
    if df.empty or len(df) < pivot_window * 2 + 1:
        df = df.copy()
        for col in ("support", "resistance", "poc", "vah", "val"):
            df[col] = np.nan
        return df

    result = df.copy()

    pivot_highs = _find_pivot_highs(result, window=pivot_window)
    pivot_lows = _find_pivot_lows(result, window=pivot_window)

    supports = []
    resistances = []
    pocs = []
    vahs = []
    vals = []

    for i in range(len(result)):
        current_close = result["close"].iloc[i]
        start_idx = max(0, i - lookback)

        # ── Pivot-based S/R ──
        recent_lows = pivot_lows.iloc[start_idx : i + 1].dropna()
        below = recent_lows[recent_lows < current_close]
        pivot_support = float(below.iloc[-1]) if not below.empty else np.nan

        recent_highs = pivot_highs.iloc[start_idx : i + 1].dropna()
        above = recent_highs[recent_highs > current_close]
        pivot_resist = float(above.iloc[-1]) if not above.empty else np.nan

        # ── Volume Profile S/R ──
        vp_support = np.nan
        vp_resist = np.nan
        poc_val = np.nan

        if i >= 30:   # Need min 30 bars for meaningful profile
            vp = _compute_volume_profile(result, start_idx, i + 1)
            poc_val = vp["poc"]
            # VAL acts as support, VAH acts as resistance
            if vp["val"] < current_close:
                vp_support = vp["val"]
            if vp["vah"] > current_close:
                vp_resist = vp["vah"]
            # POC itself acts as S or R depending on price position
            if not np.isnan(poc_val):
                if poc_val < current_close:
                    vp_support = max(vp_support, poc_val) if not np.isnan(vp_support) else poc_val
                elif poc_val > current_close:
                    vp_resist = min(vp_resist, poc_val) if not np.isnan(vp_resist) else poc_val

        # ── Combine: pick the level CLOSEST to price ──
        support_candidates = [s for s in [pivot_support, vp_support] if not np.isnan(s)]
        resist_candidates = [r for r in [pivot_resist, vp_resist] if not np.isnan(r)]

        final_support = max(support_candidates) if support_candidates else np.nan
        final_resist = min(resist_candidates) if resist_candidates else np.nan

        supports.append(final_support)
        resistances.append(final_resist)
        pocs.append(poc_val)
        vahs.append(vp["vah"] if i >= 30 else np.nan)
        vals.append(vp["val"] if i >= 30 else np.nan)

    result["support"] = supports
    result["resistance"] = resistances
    result["poc"] = pocs
    result["vah"] = vahs
    result["val"] = vals

    return result

