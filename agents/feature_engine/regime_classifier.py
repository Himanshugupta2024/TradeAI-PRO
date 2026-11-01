"""
Decoupled Two-Axis Market Regime Engine.

Separates directional trend from volatility state into two orthogonal axes:
  1. Direction Axis:
     - TrendRegime: STRONG_BULL, BULL, RANGING, BEAR, STRONG_BEAR
     - trend_score: continuous float in [-1.0, +1.0]
  2. Volatility Axis:
     - VolatilityRegime: LOW, NORMAL, HIGH, EXTREME
     - volatility_score: continuous float in [0.0, 1.0]

Preserves 100% backward compatibility with legacy MarketRegime enum.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from config import settings
from agents.feature_engine.regime_detector import MarketRegime

log = logging.getLogger(__name__)


class TrendRegime(str, enum.Enum):
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    RANGING = "RANGING"
    BEAR = "BEAR"
    STRONG_BEAR = "STRONG_BEAR"


class VolatilityRegime(str, enum.Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


@dataclass
class TwoAxisRegimeResult:
    trend_regime: TrendRegime
    trend_score: float  # -1.0 to +1.0
    volatility_regime: VolatilityRegime
    volatility_score: float  # 0.0 to 1.0
    legacy_regime: MarketRegime

    def to_dict(self) -> dict[str, Any]:
        return {
            "trend_regime": self.trend_regime.value,
            "trend_score": round(self.trend_score, 3),
            "volatility_regime": self.volatility_regime.value,
            "volatility_score": round(self.volatility_score, 3),
            "legacy_regime": self.legacy_regime.value,
        }


def detect_two_axis_regime(
    df: pd.DataFrame,
    lookback: int = 50,
) -> TwoAxisRegimeResult:
    """
    Classify the current market regime across both Directional and Volatility axes.

    Args:
        df: DataFrame with OHLCV and technical indicators (ADX, ATR, EMA, BB, MACD).
        lookback: Rolling lookback window for percentile calculations.

    Returns:
        TwoAxisRegimeResult instance.
    """
    if df.empty or len(df) < 5:
        return TwoAxisRegimeResult(
            trend_regime=TrendRegime.RANGING,
            trend_score=0.0,
            volatility_regime=VolatilityRegime.NORMAL,
            volatility_score=0.5,
            legacy_regime=MarketRegime.RANGING,
        )

    recent = df.tail(lookback)
    last = df.iloc[-1]

    close = float(last.get("close", 0.0))
    atr_col = f"ATR_{settings.ATR_PERIOD}"
    adx_col = f"ADX_{settings.ADX_PERIOD}"

    # ── 1. Volatility Axis Computation ───────────────────────────────────────
    atr = float(last.get(atr_col, 0.0)) if atr_col in df.columns else 0.0
    bb_width = float(last.get("BB_width", 0.0)) if "BB_width" in df.columns else 0.0

    # ATR percentile against recent lookback
    atr_series = recent[atr_col].dropna() if atr_col in recent.columns else pd.Series([1.0])
    bb_series = recent["BB_width"].dropna() if "BB_width" in recent.columns else pd.Series([1.0])

    if len(atr_series) > 10 and atr_series.max() > atr_series.min():
        atr_pct = float(np.mean(atr_series <= atr))
    else:
        atr_pct = 0.50

    if len(bb_series) > 10 and bb_series.max() > bb_series.min():
        bb_pct = float(np.mean(bb_series <= bb_width))
    else:
        bb_pct = 0.50

    # Composite volatility score: 0.0 (calm) -> 1.0 (extreme)
    volatility_score = float(np.clip(0.60 * atr_pct + 0.40 * bb_pct, 0.0, 1.0))

    if volatility_score >= 0.88:
        vol_regime = VolatilityRegime.EXTREME
    elif volatility_score >= 0.70:
        vol_regime = VolatilityRegime.HIGH
    elif volatility_score <= 0.30:
        vol_regime = VolatilityRegime.LOW
    else:
        vol_regime = VolatilityRegime.NORMAL

    # ── 2. Directional Trend Axis Computation ────────────────────────────────
    adx = float(last.get(adx_col, 0.0)) if adx_col in df.columns else 0.0
    ema20 = float(last.get("EMA_20", close)) if "EMA_20" in df.columns else close
    ema50 = float(last.get("EMA_50", close)) if "EMA_50" in df.columns else close
    ema200 = float(last.get("EMA_200", close)) if "EMA_200" in df.columns else close
    macd_hist = float(last.get("MACD_hist", 0.0)) if "MACD_hist" in df.columns else 0.0
    rsi = float(last.get("RSI_14", 50.0)) if "RSI_14" in df.columns else 50.0

    # Direction score components:
    # A) EMA stack: close > ema20 > ema50 > ema200
    bull_ema = 0.0
    bear_ema = 0.0
    if close > ema20 > ema50:
        bull_ema += 0.4
    elif close < ema20 < ema50:
        bear_ema += 0.4

    if close > ema200:
        bull_ema += 0.2
    elif close < ema200:
        bear_ema += 0.2

    # B) MACD & RSI directional bias
    bull_mom = (0.2 if macd_hist > 0 else 0.0) + (0.2 if rsi > 55 else 0.0)
    bear_mom = (0.2 if macd_hist < 0 else 0.0) + (0.2 if rsi < 45 else 0.0)

    raw_dir = (bull_ema + bull_mom) - (bear_ema + bear_mom)  # Range -1.0 to +1.0

    # Scale directional conviction by ADX trend strength
    # ADX: 0 to 20 -> weak conviction (scale down to 0), 25 to 50+ -> full conviction
    adx_scale = float(np.clip((adx - 15.0) / 20.0, 0.15, 1.0))
    trend_score = float(np.clip(raw_dir * adx_scale, -1.0, 1.0))

    # Categorize Direction Regime
    if adx >= 25.0 and trend_score >= 0.50:
        trend_regime = TrendRegime.STRONG_BULL
    elif trend_score >= 0.20:
        trend_regime = TrendRegime.BULL
    elif adx >= 25.0 and trend_score <= -0.50:
        trend_regime = TrendRegime.STRONG_BEAR
    elif trend_score <= -0.20:
        trend_regime = TrendRegime.BEAR
    else:
        trend_regime = TrendRegime.RANGING

    # ── 3. Legacy MarketRegime Mapping ───────────────────────────────────────
    # Backward compatible with callers expecting TRENDING / RANGING / HIGH_VOLATILITY
    if vol_regime in (VolatilityRegime.HIGH, VolatilityRegime.EXTREME) and trend_regime == TrendRegime.RANGING:
        legacy = MarketRegime.HIGH_VOLATILITY
    elif trend_regime in (TrendRegime.STRONG_BULL, TrendRegime.STRONG_BEAR, TrendRegime.BULL, TrendRegime.BEAR) and adx >= 22.0:
        legacy = MarketRegime.TRENDING
    else:
        legacy = MarketRegime.RANGING

    return TwoAxisRegimeResult(
        trend_regime=trend_regime,
        trend_score=trend_score,
        volatility_regime=vol_regime,
        volatility_score=volatility_score,
        legacy_regime=legacy,
    )


def detect_two_axis_regime_series(df: pd.DataFrame, lookback: int = 50) -> pd.DataFrame:
    """
    Compute two-axis regime for all rows in a DataFrame.

    Returns a DataFrame with columns:
      ['trend_regime', 'trend_score', 'volatility_regime', 'volatility_score', 'legacy_regime']
    """
    results = []
    n = len(df)
    for i in range(n):
        if i < 5:
            results.append({
                "trend_regime": TrendRegime.RANGING.value,
                "trend_score": 0.0,
                "volatility_regime": VolatilityRegime.NORMAL.value,
                "volatility_score": 0.5,
                "legacy_regime": MarketRegime.RANGING.value,
            })
            continue

        window_df = df.iloc[max(0, i - lookback): i + 1]
        res = detect_two_axis_regime(window_df, lookback=lookback)
        results.append(res.to_dict())

    return pd.DataFrame(results, index=df.index)

