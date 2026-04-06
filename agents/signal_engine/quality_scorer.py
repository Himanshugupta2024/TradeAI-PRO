"""
Signal Quality Scorer — 9-component composite score 0-100.

Component weights:
  Trend Alignment      20 pts  — HTF trend matches signal direction
  Volume Confirmation  15 pts  — TOD-normalised volume vs average
  Regime Match         15 pts  — regime suits the signal type
  S/R Proximity        15 pts  — price near support (LONG) / resistance (SHORT)
  Momentum             15 pts  — RSI + MACD + ROC alignment (strict directional)
  Pattern Confirmation 10 pts  — candlestick pattern score
  Price Action Context 10 pts  — breakout/exhaustion/candle structure
  Session Quality      10 pts  — trading session liquidity score
  (Total capped at 100)

Grade mapping:
  A  80+  → emit LONG / SHORT
  B  60+  → emit LONG / SHORT
  C  40+  → emit SKIP  (setup visible but not clean enough)
  D  <40  → emit SKIP / HOLD

Hard gates (any one failing → SKIP regardless of score):
  - Confidence < 55%          → SKIP (system is not sure enough)
  - SHORT when price > VWAP+2% → SKIP (price trending bullish, bad short entry)
  - LONG when price < VWAP-2%  → SKIP (price trending bearish, bad long entry)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from agents.feature_engine.regime_detector import MarketRegime
from agents.feature_engine.session_features import get_session_quality

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── Grade thresholds (restored to strict values) ────────────────────────────────────────
GRADE_A = 80  # Restored — only premium, high-conviction setups
GRADE_B = 60  # Restored — good setups with solid confirmation
GRADE_C = 40  # Restored — below this → SKIP, too risky

# ── Hard gate: minimum AI confidence to emit LONG/SHORT (not just quality score) ─────
# Below this threshold, signal is forced to SKIP even if quality score is high.
# 47% confidence (like the SOL/USDT SHORT) should NEVER emit a live signal.
MIN_CONFIDENCE_FOR_SIGNAL = 55  # percent


# ─────────────────────────────────────────────────────────────────────────────

def score_signal(
    df: pd.DataFrame,
    idx: int,
    direction: str,          # "LONG" or "SHORT"
    regime: MarketRegime | None,
    symbol: str = "",
    confidence: float = 100.0,  # AI confidence (0-100) — used as hard gate
) -> dict:
    """
    Score a single candidate signal bar at position `idx`.

    Parameters
    ----------
    df : pd.DataFrame
        Full OHLCV + indicator DataFrame (post calculate_indicators + classify_trend).
    idx : int
        Integer position (iloc index) of the signal bar to score.
    direction : str
        "LONG" or "SHORT".
    regime : MarketRegime | None
    symbol : str
        Used for session quality scoring.
    confidence : float
        AI confidence score 0-100. Hard gate: if < MIN_CONFIDENCE_FOR_SIGNAL,
        should_trade is forced to False regardless of quality score.

    Returns
    -------
    dict with keys:
      total          : int   (0-100)
      grade          : str   ("A" / "B" / "C" / "D")
      breakdown      : dict  {component: score}
      should_trade   : bool  (True if grade A or B AND confidence >= MIN_CONFIDENCE_FOR_SIGNAL)
      hard_gate_fail : str   (reason for hard gate failure, or "" if passed)
    """
    if idx < 0 or idx >= len(df):
        return _empty_score()

    row = df.iloc[idx]
    is_long = direction.upper() == "LONG"

    breakdown: dict[str, int] = {}

    # 1. Trend Alignment (20 pts)
    breakdown["trend_alignment"] = _score_trend_alignment(row, is_long)

    # 2. Volume Confirmation (15 pts) — TOD-normalised
    breakdown["volume"] = _score_volume(df, idx)

    # 3. Regime Match (15 pts)
    breakdown["regime_match"] = _score_regime_match(is_long, regime)

    # 4. S/R Proximity (15 pts)
    breakdown["sr_proximity"] = _score_sr_proximity(row, is_long)

    # 5. Momentum (15 pts)
    breakdown["momentum"] = _score_momentum(df, idx, is_long)

    # 6. Pattern Confirmation (10 pts)
    breakdown["pattern"] = _score_pattern(df, idx, is_long)

    # 7. Price Action Context (10 pts)
    breakdown["price_action"] = _score_price_action(df, idx, is_long)

    # 8. Session Quality (10 pts)
    breakdown["session"] = _score_session(row, symbol)

    total = int(min(100, sum(breakdown.values())))
    grade = _grade(total)

    # ── Hard Gate Checks ─────────────────────────────────────────────────────
    # These override the quality score — a signal can score 95 but still be
    # blocked if basic sanity conditions fail.
    hard_gate_fail = ""

    # Gate 1: AI confidence too low — system itself is uncertain
    if confidence < MIN_CONFIDENCE_FOR_SIGNAL:
        hard_gate_fail = f"low_confidence({confidence:.0f}%<{MIN_CONFIDENCE_FOR_SIGNAL}%)"

    # Gate 2: VWAP guard — SHORT when price is >2% above VWAP is a high-risk short
    # (price has strong bullish momentum relative to fair value)
    if not hard_gate_fail and not is_long:
        close = float(row.get("close", 0) or 0)
        vwap  = float(row.get("VWAP", 0) or 0)
        if vwap > 0 and close > 0:
            vwap_pct_above = (close - vwap) / vwap * 100
            if vwap_pct_above > 2.0:  # Price >2% above VWAP = bullish momentum
                hard_gate_fail = f"short_above_vwap(+{vwap_pct_above:.1f}%)"
                breakdown["vwap_gate"] = -10  # Penalise score too

    # Gate 3: LONG when price is >2% below VWAP = bearish momentum — bad long entry
    if not hard_gate_fail and is_long:
        close = float(row.get("close", 0) or 0)
        vwap  = float(row.get("VWAP", 0) or 0)
        if vwap > 0 and close > 0:
            vwap_pct_below = (vwap - close) / vwap * 100
            if vwap_pct_below > 2.0:  # Price >2% below VWAP = bearish momentum
                hard_gate_fail = f"long_below_vwap(-{vwap_pct_below:.1f}%)"
                breakdown["vwap_gate"] = -10

    # Recalculate total after any penalty additions
    total = int(min(100, max(0, sum(breakdown.values()))))
    grade = _grade(total)
    should_trade = (grade in ("A", "B")) and (not hard_gate_fail)

    return {
        "total":          total,
        "grade":          grade,
        "breakdown":      breakdown,
        "should_trade":   should_trade,
        "hard_gate_fail": hard_gate_fail,
    }


def score_signals_vectorised(
    df: pd.DataFrame,
    directions: pd.Series,       # "LONG" / "SHORT" / other per row
    regime: MarketRegime | None,
    symbol: str = "",
    confidences: pd.Series = None,  # AI confidence per row (optional)
) -> pd.DataFrame:
    """
    Compute quality scores for every row in df that has a direction.
    Rows with direction not in ("LONG", "SHORT") get score=0, grade="D".

    Now uses per-row AI confidence as a hard gate via score_signal().
    Rows with confidence < MIN_CONFIDENCE_FOR_SIGNAL are blocked even if
    quality score is high.

    Returns a DataFrame with columns: quality_score, quality_grade,
    quality_breakdown, quality_should_trade, quality_hard_gate_fail.
    """
    scores       = pd.Series(0,     index=df.index, dtype=int)
    grades       = pd.Series("D",   index=df.index, dtype=object)
    breakdowns   = pd.Series("{}",  index=df.index, dtype=object)
    should_trade = pd.Series(False, index=df.index)
    gate_fails   = pd.Series("",    index=df.index, dtype=object)

    signal_mask = directions.isin(["LONG", "SHORT"])
    indices = df.index[signal_mask]

    for label_idx in indices:
        iloc_idx  = df.index.get_loc(label_idx)
        direction = directions.loc[label_idx]
        # Pull per-row confidence if provided, otherwise use 100 (no gate)
        conf = float(confidences.loc[label_idx]) if confidences is not None else 100.0
        result = score_signal(df, iloc_idx, direction, regime, symbol, confidence=conf)
        scores.loc[label_idx]        = result["total"]
        grades.loc[label_idx]        = result["grade"]
        breakdowns.loc[label_idx]    = json.dumps(result["breakdown"])
        should_trade.loc[label_idx]  = result["should_trade"]
        gate_fails.loc[label_idx]    = result.get("hard_gate_fail", "")

    out = pd.DataFrame({
        "quality_score":        scores,
        "quality_grade":        grades,
        "quality_breakdown":    breakdowns,
        "quality_should_trade": should_trade,
        "quality_hard_gate":    gate_fails,
    }, index=df.index)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Component scorers
# ─────────────────────────────────────────────────────────────────────────────

def _score_trend_alignment(row: pd.Series, is_long: bool) -> int:
    """20 pts — HTF trend matches signal direction.

    STRICT RULES (restored):
    - Trend-following signal (LONG in BULL, SHORT in BEAR):  20 pts
    - Neutral HTF trend:                                      10 pts
    - Counter-trend with extreme RSI (mean reversion):         6 pts
      (requires RSI < 28 for LONG-in-BEAR or RSI > 72 for SHORT-in-BULL)
    - Counter-trend with normal RSI:                           0 pts
      (RSI 64 SHORT in a BEARISH trend = 0 pts, not 3)

    Key fix: RSI in 30-70 range = NO counter-trend credit.
    Example: SOL RSI=64.1, SHORT in BEARISH → 0 pts (was giving 3 pts).
    """
    htf = str(row.get("htf_trend", "")).upper()
    rsi = row.get("RSI_14")
    rsi_val = float(rsi) if rsi is not None and not pd.isna(rsi) else 50.0

    if is_long:
        if "BULL" in htf:
            return 20   # Perfect alignment
        if "NEUTRAL" in htf:
            return 10   # Neutral — no penalty but no bonus
        # Counter-trend LONG in BEARISH market
        if rsi_val < 28:    # Must be deeply oversold for mean reversion credit
            return 6
        return 0            # RSI 28-100 in bearish = no counter-trend credit
    else:
        if "BEAR" in htf:
            return 20   # Perfect alignment
        if "NEUTRAL" in htf:
            return 10   # Neutral
        # Counter-trend SHORT in BULLISH market
        if rsi_val > 72:    # Must be deeply overbought for mean reversion credit
            return 6
        return 0            # RSI 0-72 in bullish = no counter-trend credit (RSI 64 = 0!)


def _score_volume(df: pd.DataFrame, idx: int) -> int:
    """15 pts — TOD-normalised volume (same hour avg) or flat 20-bar fallback."""
    if "volume" not in df.columns:
        return 8   # Neutral when data missing

    vol = df["volume"].iloc[idx]
    if pd.isna(vol) or vol <= 0:
        return 0

    # Prefer TOD ratio (same hour-of-day baseline) — far more meaningful
    if "volume_ratio_tod" in df.columns:
        ratio = df["volume_ratio_tod"].iloc[idx]
        if pd.notna(ratio) and ratio > 0:
            if ratio >= 2.0:
                return 15   # 2x same-hour average = strong unusual volume
            if ratio >= 1.3:
                return 12
            if ratio >= 0.8:
                return 8    # Normal for this time of day
            if ratio >= 0.5:
                return 4
            return 0

    # Fallback: flat 20-bar rolling average
    lookback = min(20, idx)
    if lookback < 3:
        return 7
    avg_vol = df["volume"].iloc[max(0, idx - lookback): idx].mean()
    if avg_vol <= 0:
        return 7
    ratio = vol / avg_vol
    if ratio >= 1.5:
        return 15
    if ratio >= 1.0:
        return 10
    if ratio >= 0.5:
        return 5
    return 0


def _score_regime_match(is_long: bool, regime: MarketRegime | None) -> int:
    """15 pts — Signal type suits the current market regime."""
    if regime is None:
        return 8   # Neutral

    if regime == MarketRegime.TRENDING:
        # Both LONG and SHORT are valid in trending markets
        return 15
    if regime == MarketRegime.HIGH_VOLATILITY:
        # Mean-reversion trades work but with higher risk
        return 10
    if regime == MarketRegime.RANGING:
        # LONG near support, SHORT near resistance = valid
        # But trend-following signals in ranging market = risky
        return 8   # No directional penalty — S/R check handles that


def _score_sr_proximity(row: pd.Series, is_long: bool) -> int:
    """15 pts — Price near support (LONG) or resistance (SHORT)."""
    close = float(row.get("close", 0) or 0)
    if close <= 0:
        return 7

    if is_long:
        support = row.get("support")
        if pd.isna(support) or support is None or float(support) <= 0:
            return 7   # No S/R data → neutral
        support = float(support)
        pct_above = (close - support) / support * 100
        if pct_above <= 0.5:
            return 15   # Very close to support
        if pct_above <= 1.5:
            return 10
        if pct_above <= 3.0:
            return 5
        return 2    # Far from support
    else:
        resistance = row.get("resistance")
        if pd.isna(resistance) or resistance is None or float(resistance) <= 0:
            return 7
        resistance = float(resistance)
        pct_below = (resistance - close) / resistance * 100
        if pct_below <= 0.5:
            return 15
        if pct_below <= 1.5:
            return 10
        if pct_below <= 3.0:
            return 5
        return 2


def _score_momentum(df: pd.DataFrame, idx: int, is_long: bool) -> int:
    """15 pts — RSI + MACD + ROC momentum assessment.
    
    FIX: For mean-reversion signals (LONG when RSI < 30, SHORT when RSI > 70),
    RSI being in the opposite zone is actually CORRECT (that's the setup).
    Added MACD turning direction as an additional factor.
    """
    row = df.iloc[idx]
    signals_agree = 0
    total_factors = 0

    rsi = row.get("RSI_14")
    rsi_val = float(rsi) if rsi is not None and not pd.isna(rsi) else 50.0

    # Detect if this is a mean-reversion setup
    is_mean_reversion = (is_long and rsi_val < 30) or (not is_long and rsi_val > 70)

    # RSI — directional zone OR mean-reversion zone
    if rsi is not None and not pd.isna(rsi):
        total_factors += 1
        if is_mean_reversion:
            signals_agree += 1   # RSI in extreme zone = mean reversion confirmation
        elif is_long and rsi_val < 55:
            signals_agree += 1   # Below 55 = room to rise (relaxed from 50)
        elif not is_long and rsi_val > 45:
            signals_agree += 1   # Above 45 = room to fall (relaxed from 50)

    # MACD histogram — must match direction
    macd_hist = row.get("MACD_hist")
    if macd_hist is not None and not pd.isna(macd_hist):
        total_factors += 1
        if is_long and float(macd_hist) > 0:
            signals_agree += 1
        elif not is_long and float(macd_hist) < 0:
            signals_agree += 1

    # MACD turning direction (momentum shift) — more useful for mean reversion
    prev_macd = None
    if idx > 0:
        prev_macd = df.iloc[idx - 1].get("MACD_hist")
    if macd_hist is not None and prev_macd is not None and not pd.isna(macd_hist) and not pd.isna(prev_macd):
        total_factors += 1
        if is_long and float(macd_hist) > float(prev_macd):
            signals_agree += 1   # Histogram improving = bullish momentum shift
        elif not is_long and float(macd_hist) < float(prev_macd):
            signals_agree += 1   # Histogram deteriorating = bearish momentum shift

    # ROC (rate of change) — must match direction
    roc = row.get("ROC_5")
    if roc is not None and not pd.isna(roc):
        total_factors += 1
        if is_long and float(roc) > 0:
            signals_agree += 1
        elif not is_long and float(roc) < 0:
            signals_agree += 1

    if total_factors == 0:
        return 7   # Neutral when data missing

    ratio = signals_agree / total_factors
    if ratio >= 1.0:
        return 15   # All agree — full score
    if ratio >= 0.67:
        return 10   # Most agree — good
    if ratio >= 0.34:
        return 6    # Some agree — partial credit
    return 2        # Minimal agreement (was 0, now gives small credit)


def _score_price_action(df: pd.DataFrame, idx: int, is_long: bool) -> int:
    """
    10 pts — Price action context: breakout, candle structure, exhaustion.

    Rewards:
      Breakout from 20-bar consolidation in the signal direction  (+6)
      Strong body-to-range ratio (>= 0.6, solid conviction candle) (+2)
      Close in upper (LONG) or lower (SHORT) 25% of candle range  (+2)

    Penalises:
      Exhaustion: 5+ consecutive candles in the signal direction  (-5)
      Inside bar (body < 20% of range, low conviction)            (-2)
    """
    if idx < 1 or idx >= len(df):
        return 5   # Neutral

    row = df.iloc[idx]
    score = 0

    # 1. Breakout from consolidation range
    breakout = int(row.get("breakout_from_range", 0) or 0)
    if is_long and breakout == 1:
        score += 6   # Bullish breakout
    elif not is_long and breakout == -1:
        score += 6   # Bearish breakdown
    elif breakout != 0:
        score -= 3   # Breakout against signal direction

    # 2. Candle body strength (conviction)
    body_ratio = float(row.get("candle_body_ratio") or 0)
    if body_ratio >= 0.6:
        score += 2   # Strong decisive candle
    elif body_ratio < 0.2:
        score -= 2   # Inside bar / spinning top = indecision

    # 3. Close position in range (where did price close in the candle?)
    close_pos = float(row.get("close_position_in_range") or 0.5)
    if is_long and close_pos >= 0.75:
        score += 2   # Closed in top quarter = bull strength
    elif not is_long and close_pos <= 0.25:
        score += 2   # Closed in bottom quarter = bear strength

    # 4. Exhaustion: 5+ consecutive candles same direction = likely reversal point
    consecutive = int(row.get("candle_consecutive") or 0)
    if is_long and consecutive >= 5:
        score -= 5   # Too many up candles = exhaustion risk
    elif not is_long and consecutive <= -5:
        score -= 5   # Too many down candles = bounce risk

    return max(0, min(10, score + 5))  # Bias to neutral (5), clamp 0-10


def _score_pattern(df: pd.DataFrame, idx: int, is_long: bool) -> int:
    """10 pts — Candlestick pattern confirmation via fallback_predictor."""
    try:
        from agents.ai_engine.fallback_predictor import _detect_candlestick_patterns

        lookback = min(5, idx)
        window = df.iloc[max(0, idx - lookback): idx + 1]
        if len(window) < 2:
            return 5

        result = _detect_candlestick_patterns(window)
        # result is a dict with "patterns" list and "score" float (-1.0 to +1.0)
        pattern_score = float(result.get("score", 0))

        if is_long:
            # Positive score = bullish patterns
            if pattern_score >= 0.5:
                return 10
            if pattern_score >= 0.2:
                return 6
            if pattern_score >= 0:
                return 4
            return 1   # Bearish pattern = conflict
        else:
            # Negative score = bearish patterns
            if pattern_score <= -0.5:
                return 10
            if pattern_score <= -0.2:
                return 6
            if pattern_score <= 0:
                return 4
            return 1
    except Exception:
        return 5   # Neutral on error


def _score_session(row: pd.Series, symbol: str) -> int:
    """10 pts — Trading session quality."""
    ts = row.get("timestamp")
    if ts is None or pd.isna(ts):
        # Try the index
        return 7   # Neutral when timestamp unavailable

    quality = get_session_quality(pd.Timestamp(ts), symbol)
    return int(round(quality * 10))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _grade(total: int) -> str:
    if total >= GRADE_A:
        return "A"
    if total >= GRADE_B:
        return "B"
    if total >= GRADE_C:
        return "C"
    return "D"


def _empty_score() -> dict:
    return {
        "total": 0, "grade": "D",
        "breakdown": {}, "should_trade": False,
    }
