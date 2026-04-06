"""
Multi-Timeframe (MTF) Scorer.

Generates a weighted confluence score from all tracked timeframes
for a symbol. Each timeframe contributes:
  - Trend alignment (EMA stack direction)
  - RSI position (oversold/overbought/neutral)
  - MACD momentum (histogram direction)

Weight distribution (configurable):
  1D: 40%  →  1H: 30%  →  15m: 20%  →  5m: 10%

Score ranges from -100 (strong sell) to +100 (strong buy).
"""

import pandas as pd
import numpy as np

from config import settings
from config.logger import get_logger

log = get_logger(__name__)


def _score_timeframe(df: pd.DataFrame) -> float:
    """
    Score a single timeframe's latest candle from -100 to +100.

    Scoring breakdown (each factor: -33 to +33):
        1. EMA Alignment: close > EMA_20 > EMA_50 = +33, reverse = -33
        2. RSI Position: < 30 = +33 (oversold bounce), > 70 = -33
        3. MACD Momentum: histogram > 0 and rising = +33, reverse = -33

    Returns:
        Score from -100 to +100
    """
    if df.empty or len(df) < 2:
        return 0.0

    last = df.iloc[-1]
    prev = df.iloc[-2]
    score = 0.0

    # 1. EMA Alignment (±33)
    ema20 = last.get("EMA_20")
    ema50 = last.get("EMA_50")
    close = last.get("close")

    if all(pd.notna(v) for v in [ema20, ema50, close]):
        if close > ema20 > ema50:
            score += 33.0
        elif close < ema20 < ema50:
            score -= 33.0
        elif close > ema20:
            score += 15.0
        elif close < ema20:
            score -= 15.0

    # 2. RSI Position (±33)
    rsi = last.get(f"RSI_{settings.RSI_PERIOD}")
    if pd.notna(rsi):
        if rsi < 30:
            score += 33.0  # Oversold → bullish potential
        elif rsi < 40:
            score += 15.0
        elif rsi > 70:
            score -= 33.0  # Overbought → bearish potential
        elif rsi > 60:
            score -= 15.0

    # 3. MACD Momentum (±34)
    macd_hist = last.get("MACD_hist")
    prev_hist = prev.get("MACD_hist")

    if pd.notna(macd_hist) and pd.notna(prev_hist):
        if macd_hist > 0 and macd_hist > prev_hist:
            score += 34.0  # Bullish and accelerating
        elif macd_hist > 0:
            score += 15.0  # Bullish but decelerating
        elif macd_hist < 0 and macd_hist < prev_hist:
            score -= 34.0  # Bearish and accelerating
        elif macd_hist < 0:
            score -= 15.0  # Bearish but decelerating

    return round(max(-100, min(100, score)), 1)


def calculate_mtf_score(
    data_store: dict[str, pd.DataFrame],
    weights: dict[str, float] = None,
) -> float:
    """
    Calculate weighted multi-timeframe score for a symbol.

    Args:
        data_store: {timeframe: enriched_df} — e.g. {"1d": df, "1h": df, "15m": df}
        weights: {timeframe: weight} — must sum to 1.0

    Returns:
        Score from -100 to +100 (weighted average of all TF scores)
    """
    weights = weights or settings.MTF_WEIGHTS

    total_score = 0.0
    total_weight = 0.0

    for tf, weight in weights.items():
        if tf in data_store and not data_store[tf].empty:
            tf_score = _score_timeframe(data_store[tf])
            total_score += tf_score * weight
            total_weight += weight
            log.debug("MTF %s: score=%.1f × weight=%.2f", tf, tf_score, weight)

    if total_weight > 0:
        final = total_score / total_weight
    else:
        final = 0.0

    return round(max(-100, min(100, final)), 1)


def mtf_to_confidence(mtf_score: float) -> float:
    """
    Convert MTF score (-100 to +100) to confidence (0–100).

    Maps: -100 → 0, 0 → 50, +100 → 100
    """
    return round((mtf_score + 100) / 2, 1)
