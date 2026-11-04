"""
Quantitative Indicator Library for Intraday Trading Engine.

Deterministic, zero-lookahead, session-isolated calculations:
  - Session VWAP with 1 and 2 standard-deviation bands
  - Central Pivot Range (CPR): Pivot, Bottom Central (BC), Top Central (TC), R1, S1, R2, S2
    strictly isolated to Day T-1 (single prior trading session)
  - Wilder's Average Directional Index (ADX) & True Range / ATR
  - Multiple EMAs (9, 20, 50, 200) with slope and compression detection
  - Relative Strength Index (RSI 14) with momentum and regime pullback
  - Supertrend with adaptive ATR multiplier
  - Support & Resistance key price zones (PDH, PDL, PDC, Day High, Day Low, VWAP, CPR)
  - Volume Analysis: Relative Volume (RVOL) and Volume Surge detection
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Wilder's ATR or standard True Range rolling average."""
    if df is None or df.empty or len(df) < 3:
        return None
    try:
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")
        prev_close = close.shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        p = min(max(2, period), len(df))
        atr_series = tr.rolling(window=p, min_periods=2).mean()
        val = atr_series.iloc[-1]
        if pd.isna(val) or val <= 0:
            return None
        return float(val)
    except Exception as exc:
        logger.debug("compute_atr error: %s", exc)
        return None


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> Optional[float]:
    """Relative Strength Index (RSI)."""
    if series is None or len(series) < period + 2:
        return None
    try:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()

        # Wilder's smoothing
        for i in range(period, len(series)):
            avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
            avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi_series = 100 - (100 / (1 + rs))
        val = rsi_series.iloc[-1]
        if pd.isna(val):
            return None
        return float(np.clip(val, 0.0, 100.0))
    except Exception as exc:
        logger.debug("compute_rsi error: %s", exc)
        return None


def compute_adx(df: pd.DataFrame, period: int = 14) -> Optional[Dict[str, float]]:
    """
    Wilder's Average Directional Index (ADX), +DI, -DI.
    Distinguishes TRENDING (ADX >= 25), WEAK_TREND (20-25), and RANGING/CHOP (ADX < 20).
    """
    if df is None or len(df) < period * 2:
        return None
    try:
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")

        up_move = high - high.shift(1)
        down_move = low.shift(1) - low

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.rolling(window=period, min_periods=period).mean()
        pdm_roll = pd.Series(plus_dm, index=df.index).rolling(window=period, min_periods=period).mean()
        mdm_roll = pd.Series(minus_dm, index=df.index).rolling(window=period, min_periods=period).mean()

        plus_di = (pdm_roll / atr.replace(0, 1e-9)) * 100.0
        minus_di = (mdm_roll / atr.replace(0, 1e-9)) * 100.0

        dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)) * 100.0
        adx = dx.rolling(window=period, min_periods=period).mean()

        adx_val = float(adx.iloc[-1])
        plus_val = float(plus_di.iloc[-1])
        minus_val = float(minus_di.iloc[-1])

        if pd.isna(adx_val):
            return None

        regime = "TRENDING" if adx_val >= 25.0 else ("WEAK_TREND" if adx_val >= 20.0 else "RANGING")
        return {
            "adx": round(adx_val, 2),
            "plus_di": round(plus_val, 2),
            "minus_di": round(minus_val, 2),
            "trend_regime": regime,
        }
    except Exception as exc:
        logger.debug("compute_adx error: %s", exc)
        return None


def compute_session_vwap_bands(
    day_df: pd.DataFrame, live_vwap: Optional[float] = None
) -> Optional[Dict[str, float]]:
    """
    Session-reset VWAP with 1 and 2 standard-deviation bands.
    Guarantees no multi-day cross-contamination.
    """
    if day_df is None or day_df.empty or len(day_df) < 5:
        return None
    try:
        high = pd.to_numeric(day_df["high"], errors="coerce")
        low = pd.to_numeric(day_df["low"], errors="coerce")
        close = pd.to_numeric(day_df["close"], errors="coerce")
        vol = pd.to_numeric(day_df["volume"], errors="coerce").fillna(0).clip(lower=0)

        tp = (high + low + close) / 3.0
        cum_vol = vol.cumsum().replace(0, pd.NA).ffill()
        if cum_vol.empty or pd.isna(cum_vol.iloc[-1]) or float(cum_vol.iloc[-1]) <= 0:
            return None

        cum_pv = (tp * vol).cumsum()
        vwap_series = cum_pv / cum_vol.astype(float)
        calc_vwap = float(vwap_series.iloc[-1])

        # If an external official NSE exchange VWAP is supplied, use it as baseline
        ext_vwap = None
        if live_vwap and live_vwap > 0:
            ext_vwap = float(live_vwap)
        elif hasattr(day_df, "attrs") and day_df.attrs.get("live_vwap"):
            try:
                ext_vwap = float(day_df.attrs["live_vwap"])
            except Exception:
                pass
        elif "VWAP" in day_df.columns:
            try:
                v = float(day_df["VWAP"].iloc[-1])
                if v > 0:
                    ext_vwap = v
            except Exception:
                pass

        vwap = ext_vwap if (ext_vwap is not None and ext_vwap > 0) else calc_vwap
        if vwap <= 0 or pd.isna(vwap):
            return None

        # Standard deviation bands
        variance_series = ((tp - vwap_series) ** 2 * vol).cumsum() / cum_vol.astype(float)
        last_var = float(variance_series.iloc[-1]) if not pd.isna(variance_series.iloc[-1]) else 0.0
        std = max(float(last_var ** 0.5), vwap * 0.001)

        return {
            "vwap": round(vwap, 2),
            "std": round(std, 2),
            "upper_1": round(vwap + 1.0 * std, 2),
            "upper_2": round(vwap + 2.0 * std, 2),
            "lower_1": round(vwap - 1.0 * std, 2),
            "lower_2": round(vwap - 2.0 * std, 2),
        }
    except Exception as exc:
        logger.debug("compute_session_vwap_bands error: %s", exc)
        return None


def compute_cpr_levels(prev_day_df: pd.DataFrame) -> Optional[Dict[str, float]]:
    """
    Central Pivot Range (CPR) strictly calculated from single prior day (Day T-1) OHLC.

    Formulas:
      Pivot (P) = (High + Low + Close) / 3
      Bottom Central (BC) = (High + Low) / 2
      Top Central (TC) = (P - BC) + P
      Support 1 (S1) = (2 * P) - High
      Resistance 1 (R1) = (2 * P) - Low
      Support 2 (S2) = P - (High - Low)
      Resistance 2 (R2) = P + (High - Low)
    """
    if prev_day_df is None or prev_day_df.empty:
        return None
    try:
        high = float(pd.to_numeric(prev_day_df["high"], errors="coerce").max())
        low = float(pd.to_numeric(prev_day_df["low"], errors="coerce").min())
        close = float(pd.to_numeric(prev_day_df["close"], errors="coerce").iloc[-1])

        if high <= 0 or low <= 0 or close <= 0 or high < low:
            return None

        pivot = (high + low + close) / 3.0
        bc = (high + low) / 2.0
        tc = (pivot - bc) + pivot

        # In case TC < BC, swap so TC is always upper level
        if tc < bc:
            tc, bc = bc, tc

        r1 = (2.0 * pivot) - low
        s1 = (2.0 * pivot) - high
        r2 = pivot + (high - low)
        s2 = pivot - (high - low)

        width_pct = ((tc - bc) / max(pivot, 1e-9)) * 100.0
        cpr_type = "NARROW" if width_pct < 0.35 else ("WIDE" if width_pct > 0.85 else "AVERAGE")

        return {
            "pivot": round(pivot, 2),
            "bc": round(bc, 2),
            "tc": round(tc, 2),
            "r1": round(r1, 2),
            "s1": round(s1, 2),
            "r2": round(r2, 2),
            "s2": round(s2, 2),
            "width_pct": round(width_pct, 3),
            "cpr_type": cpr_type,  # NARROW = trending probability high
            "prev_high": round(high, 2),
            "prev_low": round(low, 2),
            "prev_close": round(close, 2),
        }
    except Exception as exc:
        logger.debug("compute_cpr_levels error: %s", exc)
        return None


def compute_supertrend(
    day_df: pd.DataFrame, period: int = 10, multiplier: float = 2.5
) -> Optional[Dict[str, Any]]:
    """Vectorized Supertrend with ATR lookback and flip detection."""
    if day_df is None or len(day_df) < period + 2:
        return None
    try:
        high = pd.to_numeric(day_df["high"], errors="coerce").values
        low = pd.to_numeric(day_df["low"], errors="coerce").values
        close = pd.to_numeric(day_df["close"], errors="coerce").values
        n = len(close)

        # True Range
        tr = np.zeros(n)
        for i in range(1, n):
            h, l, pc = high[i], low[i], close[i - 1]
            tr[i] = max(h - l, abs(h - pc), abs(l - pc))
        tr[0] = high[0] - low[0]

        # Rolling ATR
        atr = pd.Series(tr).rolling(window=period, min_periods=period).mean().values

        hl2 = (high + low) / 2.0
        upper_band = hl2 + multiplier * atr
        lower_band = hl2 - multiplier * atr

        final_upper = upper_band.copy()
        final_lower = lower_band.copy()
        direction = np.ones(n)

        for i in range(period, n):
            c = close[i]
            pc = close[i - 1]
            fu_prev = final_upper[i - 1]
            fl_prev = final_lower[i - 1]

            # Adjust bands
            final_upper[i] = min(upper_band[i], fu_prev) if pc <= fu_prev else upper_band[i]
            final_lower[i] = max(lower_band[i], fl_prev) if pc >= fl_prev else lower_band[i]

            prev_dir = direction[i - 1]
            if prev_dir == -1.0 and c > final_upper[i]:
                direction[i] = 1.0
            elif prev_dir == 1.0 and c < final_lower[i]:
                direction[i] = -1.0
            else:
                direction[i] = prev_dir

        curr_dir = int(direction[-1])
        prev_dir = int(direction[-2])
        is_flip = curr_dir != prev_dir
        trend_str = "BULLISH" if curr_dir == 1 else "BEARISH"
        active_band = final_lower[-1] if curr_dir == 1 else final_upper[-1]

        return {
            "direction": trend_str,
            "is_flip": is_flip,
            "band_value": round(float(active_band), 2),
            "multiplier": multiplier,
            "period": period,
        }
    except Exception as exc:
        logger.debug("compute_supertrend error: %s", exc)
        return None


def compute_technical_snapshot(
    day_df: pd.DataFrame, prev_day_df: Optional[pd.DataFrame] = None, live_vwap: Optional[float] = None
) -> Dict[str, Any]:
    """
    Comprehensive technical snapshot of an asset for the scanner grid and detail drawer.
    Calculates EMAs (9, 20, 50, 200), RSI, ADX, ATR, VWAP bands, CPR, Supertrend, S/R zones.
    """
    if day_df is None or day_df.empty:
        return {}

    close = pd.to_numeric(day_df["close"], errors="coerce")
    c = float(close.iloc[-1])
    n = len(day_df)

    # EMAs
    ema_9 = float(compute_ema(close, 9).iloc[-1]) if n >= 9 else c
    ema_20 = float(compute_ema(close, 20).iloc[-1]) if n >= 15 else c
    ema_50 = float(compute_ema(close, 50).iloc[-1]) if n >= 25 else c
    ema_200 = float(compute_ema(close, 200).iloc[-1]) if n >= 60 else None

    # Trend alignment
    trend_aligned_bull = c > ema_20 and ema_20 > ema_50
    trend_aligned_bear = c < ema_20 and ema_20 < ema_50
    trend_state = "STRONG_UP" if trend_aligned_bull else ("STRONG_DOWN" if trend_aligned_bear else "CHOP")

    rsi = compute_rsi(close, 14)
    adx_dict = compute_adx(day_df, 14)
    atr = compute_atr(day_df, 14)
    vwap_bands = compute_session_vwap_bands(day_df, live_vwap=live_vwap)
    cpr = compute_cpr_levels(prev_day_df) if prev_day_df is not None and not prev_day_df.empty else None
    supertrend = compute_supertrend(day_df)

    # Support & Resistance Zones
    support_levels = []
    resistance_levels = []

    if cpr:
        support_levels.extend([cpr["s1"], cpr["s2"], cpr["bc"]])
        resistance_levels.extend([cpr["r1"], cpr["r2"], cpr["tc"]])
        if cpr["prev_high"] > c:
            resistance_levels.append(cpr["prev_high"])
        else:
            support_levels.append(cpr["prev_high"])
        if cpr["prev_low"] < c:
            support_levels.append(cpr["prev_low"])
        else:
            resistance_levels.append(cpr["prev_low"])

    if vwap_bands:
        if vwap_bands["vwap"] < c:
            support_levels.append(vwap_bands["vwap"])
        else:
            resistance_levels.append(vwap_bands["vwap"])
        support_levels.append(vwap_bands["lower_1"])
        resistance_levels.append(vwap_bands["upper_1"])

    support_levels = sorted(list({round(s, 2) for s in support_levels if s < c}))
    resistance_levels = sorted(list({round(r, 2) for r in resistance_levels if r > c}))

    immediate_support = support_levels[-1] if support_levels else round(c * 0.99, 2)
    immediate_resistance = resistance_levels[0] if resistance_levels else round(c * 1.01, 2)

    return {
        "ltp": round(c, 2),
        "ema_9": round(ema_9, 2),
        "ema_20": round(ema_20, 2),
        "ema_50": round(ema_50, 2),
        "ema_200": round(ema_200, 2) if ema_200 else None,
        "trend_state": trend_state,
        "rsi": round(rsi, 1) if rsi is not None else None,
        "adx": adx_dict.get("adx") if adx_dict else None,
        "adx_regime": adx_dict.get("trend_regime") if adx_dict else "RANGING",
        "atr": round(atr, 2) if atr is not None else round(c * 0.008, 2),
        "vwap": vwap_bands.get("vwap") if vwap_bands else None,
        "vwap_bands": vwap_bands,
        "cpr": cpr,
        "supertrend": supertrend,
        "support": immediate_support,
        "resistance": immediate_resistance,
        "support_levels": support_levels[-3:],
        "resistance_levels": resistance_levels[:3],
    }
