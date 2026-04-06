"""
Market Regime Detector.

Classifies the current market environment into one of:
  - TRENDING:       Strong directional move (ADX > 25, tight BB)
  - RANGING:        Sideways / choppy (ADX < 20, moderate BB width)
  - HIGH_VOLATILITY: Wide swings without clear direction (high BB width + ATR spike)

Used by the signal generator to auto-adjust entry conditions
and risk parameters for each regime.
"""

import enum
import pandas as pd
import numpy as np

from config import settings
from config.logger import get_logger

log = get_logger(__name__)


class MarketRegime(str, enum.Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"


def detect_regime(
    df: pd.DataFrame,
    adx_trending_threshold: float = 25.0,
    adx_ranging_threshold: float = 20.0,
    bb_width_percentile_high: float = 80.0,
    atr_percentile_high: float = 80.0,
    lookback: int = 50,
) -> MarketRegime:
    """
    Classify the current market regime using ADX, BB width, and ATR.

    Decision Tree:
        1. If ATR is in top 20th percentile AND BB width is wide → HIGH_VOLATILITY
        2. If ADX > 25 → TRENDING
        3. If ADX < 20 → RANGING
        4. Otherwise → RANGING (default conservative)

    Args:
        df: DataFrame with ADX_14, BB_width, ATR_14 columns
        adx_trending_threshold: ADX above this = trending
        adx_ranging_threshold: ADX below this = ranging
        bb_width_percentile_high: BB width percentile for high vol detection
        atr_percentile_high: ATR percentile for high vol detection
        lookback: Number of candles to analyze for percentile calculations

    Returns:
        MarketRegime enum value
    """
    if df.empty or len(df) < lookback:
        return MarketRegime.RANGING

    recent = df.tail(lookback)
    last = df.iloc[-1]

    # Get ADX value
    adx_col = f"ADX_{settings.ADX_PERIOD}"
    adx = last.get(adx_col, 0)
    if pd.isna(adx):
        adx = 0

    # Get BB width
    bb_width = last.get("BB_width", 0)
    if pd.isna(bb_width):
        bb_width = 0

    # Get ATR and compute percentile
    atr_col = f"ATR_{settings.ATR_PERIOD}"
    atr = last.get(atr_col, 0)
    if pd.isna(atr):
        atr = 0

    atr_series = recent[atr_col].dropna()
    bb_series = recent["BB_width"].dropna() if "BB_width" in recent.columns else pd.Series([0])

    # Percentile checks — use the correct parameter for each series
    atr_pct = np.percentile(atr_series, atr_percentile_high) if len(atr_series) > 5 else atr
    bb_pct = np.percentile(bb_series, bb_width_percentile_high) if len(bb_series) > 5 else bb_width

    # Decision tree
    is_high_atr = atr >= atr_pct
    is_wide_bb = bb_width >= bb_pct

    if is_high_atr and is_wide_bb:
        regime = MarketRegime.HIGH_VOLATILITY
    elif adx >= adx_trending_threshold:
        regime = MarketRegime.TRENDING
    elif adx <= adx_ranging_threshold:
        regime = MarketRegime.RANGING
    else:
        regime = MarketRegime.RANGING

    log.debug(
        "Regime: %s (ADX=%.1f, BB_width=%.4f, ATR=%.2f, ATR_pct=%.2f)",
        regime.value, adx, bb_width, atr, atr_pct,
    )
    return regime


def detect_regime_series(df: pd.DataFrame, window: int = 50) -> pd.Series:
    """
    Compute regime for every row in the DataFrame using a rolling window.

    Returns a Series of MarketRegime values.
    """
    regimes = []
    for i in range(len(df)):
        if i < window:
            regimes.append(MarketRegime.RANGING)
            continue
        window_df = df.iloc[max(0, i - window):i + 1]
        regimes.append(detect_regime(window_df, lookback=window))

    return pd.Series(regimes, index=df.index, name="regime")
