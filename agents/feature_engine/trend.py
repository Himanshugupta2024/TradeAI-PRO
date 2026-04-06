"""
Higher Timeframe (HTF) Trend Classifier.

Determines market trend direction using EMA configuration
on the higher timeframe (default: 1H) and propagates it
down to lower timeframe feature rows.
"""

import pandas as pd

from config import settings
from db.models import TrendDirection
from agents.feature_engine.indicators import _ema


def classify_trend(htf_df: pd.DataFrame) -> pd.DataFrame:
    """
    Classify trend direction based on EMA alignment on the higher timeframe.

    Logic:
        - BULLISH:  EMA_20 > EMA_50 > EMA_200  (strong uptrend)
        - BEARISH:  EMA_20 < EMA_50 < EMA_200  (strong downtrend)
        - NEUTRAL:  Everything else (choppy / ranging)

    Args:
        htf_df: Higher timeframe OHLCV DataFrame (must include 'close')

    Returns:
        DataFrame with added column: htf_trend (TrendDirection enum)
    """
    if htf_df.empty:
        htf_df = htf_df.copy()
        htf_df["htf_trend"] = TrendDirection.NEUTRAL.value
        return htf_df

    result = htf_df.copy()

    # Calculate EMAs if not already present
    for period in settings.EMA_PERIODS:
        col = f"EMA_{period}"
        if col not in result.columns:
            result[col] = _ema(result["close"], period)

    # Classify trend row by row — store .value strings to avoid enum repr issues
    # FIX: Added minimum EMA spread requirement (0.5%) to prevent tiny EMA
    # differences from forcing BULLISH/BEARISH. When EMAs are close together,
    # the market is ranging/choppy, not trending.
    MIN_EMA_SPREAD_PCT = 0.005  # 0.5% minimum spread between EMA20 and EMA50

    def _classify(row):
        ema20  = row.get("EMA_20")
        ema50  = row.get("EMA_50")
        ema200 = row.get("EMA_200")
        close  = row.get("close")

        if pd.isna(ema20) or pd.isna(ema50):
            return TrendDirection.NEUTRAL.value

        # Calculate EMA spread as percentage
        ema_mid = (ema20 + ema50) / 2.0
        if ema_mid > 0:
            ema_spread_pct = abs(ema20 - ema50) / ema_mid
        else:
            ema_spread_pct = 0.0

        # If EMAs are too close together, market is ranging — not trending
        if ema_spread_pct < MIN_EMA_SPREAD_PCT:
            return TrendDirection.NEUTRAL.value

        # Strong trend: perfect 3-EMA alignment with sufficient spread
        if not pd.isna(ema200):
            if ema20 > ema50 > ema200:
                return TrendDirection.BULLISH.value
            if ema20 < ema50 < ema200:
                return TrendDirection.BEARISH.value

        # Moderate trend: 2-EMA alignment with price confirmation
        if ema20 > ema50:
            if close is not None and not pd.isna(close) and close > ema20:
                return TrendDirection.BULLISH.value   # Price above rising EMAs = bullish
            # EMA20 > EMA50 but price below EMA20 = weakening, need wider spread
            if ema_spread_pct >= MIN_EMA_SPREAD_PCT * 2:
                return TrendDirection.BULLISH.value   # Strong enough spread = still bullish
            return TrendDirection.NEUTRAL.value       # Weak spread + price below = neutral
        if ema20 < ema50:
            if close is not None and not pd.isna(close) and close < ema20:
                return TrendDirection.BEARISH.value   # Price below falling EMAs = bearish
            if ema_spread_pct >= MIN_EMA_SPREAD_PCT * 2:
                return TrendDirection.BEARISH.value   # Strong enough spread = still bearish
            return TrendDirection.NEUTRAL.value       # Weak spread + price above = neutral

        return TrendDirection.NEUTRAL.value

    result["htf_trend"] = result.apply(_classify, axis=1)

    return result


def propagate_trend_to_ltf(
    ltf_df: pd.DataFrame,
    htf_trend_series: pd.DataFrame,
) -> pd.DataFrame:
    """
    Map higher timeframe trend values down to lower timeframe rows.

    Uses merge_asof to align HTF trend to LTF timestamps (forward-fill).

    Args:
        ltf_df: Lower timeframe DataFrame with 'timestamp' column
        htf_trend_series: HTF DataFrame with 'timestamp' and 'htf_trend' columns

    Returns:
        LTF DataFrame with 'htf_trend' column added.
    """
    if ltf_df.empty or htf_trend_series.empty:
        ltf_df = ltf_df.copy()
        ltf_df["htf_trend"] = TrendDirection.NEUTRAL.value
        return ltf_df

    ltf = ltf_df.copy().sort_values("timestamp")
    htf = htf_trend_series[["timestamp", "htf_trend"]].copy().sort_values("timestamp")

    # ── Normalize timestamps to timezone-naive UTC ────────────────────────
    # yfinance returns tz-aware (UTC) for intraday and tz-naive for daily.
    # pd.merge_asof requires identical dtypes on the merge key.
    def _to_naive_utc(ts_series: pd.Series) -> pd.Series:
        """Strip timezone info, converting to UTC first if tz-aware."""
        if hasattr(ts_series.dtype, "tz") and ts_series.dtype.tz is not None:
            return ts_series.dt.tz_convert("UTC").dt.tz_localize(None)
        return ts_series

    ltf["timestamp"] = _to_naive_utc(ltf["timestamp"])
    htf["timestamp"] = _to_naive_utc(htf["timestamp"])

    # Ensure both are the same datetime64 resolution
    ltf["timestamp"] = pd.to_datetime(ltf["timestamp"])
    htf["timestamp"] = pd.to_datetime(htf["timestamp"])

    merged = pd.merge_asof(
        ltf,
        htf,
        on="timestamp",
        direction="backward",
    )

    # Fill any leading NaN with NEUTRAL string value
    merged["htf_trend"] = merged["htf_trend"].fillna(TrendDirection.NEUTRAL.value)

    return merged
