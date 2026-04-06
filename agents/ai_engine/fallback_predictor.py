"""
Fallback Statistical Predictor.

When PyTorch is not available (Python 3.14), this module provides
direction prediction using a momentum-based statistical approach.
Uses RSI, MACD, EMA alignment, recent price momentum, volume trend,
ADX strength, divergence detection, candlestick patterns, Stochastic RSI,
MFI, OBV trend, and volume-price relationship analysis.

Scoring is calibrated so that the confidence distribution meaningfully
filters signals: weak setups score <40%, moderate 40-65%, strong >65%.
"""

import numpy as np
import pandas as pd
from config import settings
from config.logger import get_logger

log = get_logger(__name__)


# ── Helper: RSI Divergence Detection ──────────────────────────────────────

def _detect_divergence(df: pd.DataFrame, lookback: int = 14) -> str:
    """
    Detect regular and hidden RSI divergence.

    Regular:
      Bearish: price higher high, RSI lower high → weakness
      Bullish: price lower low,  RSI higher low → strength

    Hidden (trend continuation):
      Hidden bullish: price higher low + RSI lower low → uptrend continues
      Hidden bearish: price lower high + RSI higher high → downtrend continues

    Returns: "bullish_divergence" | "bearish_divergence" |
             "hidden_bullish" | "hidden_bearish" | "none"
    """
    if len(df) < lookback or "RSI_14" not in df.columns:
        return "none"

    recent = df.tail(lookback)
    price_first = recent["close"].iloc[0]
    price_last  = recent["close"].iloc[-1]
    rsi_first   = recent["RSI_14"].iloc[0]
    rsi_last    = recent["RSI_14"].iloc[-1]

    if not (pd.notna(price_first) and pd.notna(price_last)
            and pd.notna(rsi_first) and pd.notna(rsi_last)):
        return "none"

    price_up = price_last > price_first
    rsi_up   = rsi_last   > rsi_first

    # Regular divergence (reversal signals)
    if price_up and not rsi_up:
        return "bearish_divergence"   # Momentum weakening despite price rise
    if not price_up and rsi_up:
        return "bullish_divergence"   # Momentum strengthening despite price fall

    # Hidden divergence (continuation signals) — check swing lows/highs
    price_low_first = recent["low"].iloc[:lookback // 2].min()
    price_low_last  = recent["low"].iloc[lookback // 2:].min()
    price_hi_first  = recent["high"].iloc[:lookback // 2].max()
    price_hi_last   = recent["high"].iloc[lookback // 2:].max()
    rsi_low_first   = recent["RSI_14"].iloc[:lookback // 2].min()
    rsi_low_last    = recent["RSI_14"].iloc[lookback // 2:].min()
    rsi_hi_first    = recent["RSI_14"].iloc[:lookback // 2].max()
    rsi_hi_last     = recent["RSI_14"].iloc[lookback // 2:].max()

    # Hidden bullish: higher low in price + lower low in RSI
    if price_low_last > price_low_first and rsi_low_last < rsi_low_first:
        return "hidden_bullish"

    # Hidden bearish: lower high in price + higher high in RSI
    if price_hi_last < price_hi_first and rsi_hi_last > rsi_hi_first:
        return "hidden_bearish"

    return "none"


# ── Helper: Candlestick Pattern Recognition ────────────────────────────────

def _detect_candlestick_patterns(df: pd.DataFrame) -> dict:
    """
    Detect single and two-bar candlestick patterns.

    Returns dict with detected patterns and a composite score:
    Positive = bullish bias, Negative = bearish bias.
    """
    if len(df) < 2:
        return {"patterns": [], "score": 0.0}

    last = df.iloc[-1]
    prev = df.iloc[-2]
    patterns = []
    score = 0.0

    # Basic measures
    body        = abs(last["close"] - last["open"])
    full_range  = last["high"] - last["low"]
    if full_range == 0:
        return {"patterns": ["doji"], "score": 0.0}

    body_ratio   = body / full_range
    upper_wick   = last["high"] - max(last["open"], last["close"])
    lower_wick   = min(last["open"], last["close"]) - last["low"]
    is_bullish   = last["close"] > last["open"]
    is_bearish   = last["close"] < last["open"]

    # ── Doji (indecision / potential reversal)
    if body_ratio < 0.1:
        patterns.append("doji")
        # Score depends on context — no directional score on its own

    # ── Hammer (bullish reversal — long lower wick, small body at top)
    if lower_wick > body * 2.0 and upper_wick < body * 0.5 and body_ratio < 0.4:
        patterns.append("hammer")
        score += 0.8

    # ── Shooting Star (bearish — long upper wick, small body at bottom)
    if upper_wick > body * 2.0 and lower_wick < body * 0.5 and body_ratio < 0.4:
        patterns.append("shooting_star")
        score -= 0.8

    # ── Engulfing patterns (two-bar reversal)
    prev_body = abs(prev["close"] - prev["open"])
    if prev_body > 0 and body > prev_body * 1.3:
        if is_bullish and prev["close"] < prev["open"]:    # Bullish engulfing
            patterns.append("bullish_engulfing")
            score += 1.0
        elif is_bearish and prev["close"] > prev["open"]:  # Bearish engulfing
            patterns.append("bearish_engulfing")
            score -= 1.0

    # ── Strong bullish/bearish candle (momentum)
    if body_ratio > 0.7 and is_bullish:
        patterns.append("strong_bull_candle")
        score += 0.5
    elif body_ratio > 0.7 and is_bearish:
        patterns.append("strong_bear_candle")
        score -= 0.5

    # ── Inside bar (consolidation / potential breakout setup)
    if last["high"] < prev["high"] and last["low"] > prev["low"]:
        patterns.append("inside_bar")
        # Neutral by itself — no score

    return {"patterns": patterns, "score": round(score, 2)}


# ── Helper: Volume-Price Relationship ─────────────────────────────────────

def _volume_price_analysis(df: pd.DataFrame) -> float:
    """
    Measure alignment between volume direction and price direction.

    Bullish: price up on above-avg volume → strong buying
    Bearish: price down on above-avg volume → strong selling

    Returns float in [-1.0, +1.0]: positive = bullish, negative = bearish.
    """
    if len(df) < 10 or "volume" not in df.columns:
        return 0.0

    recent = df.tail(10).copy()
    avg_vol = recent["volume"].rolling(5, min_periods=3).mean()
    price_up  = recent["close"].diff() > 0
    vol_above  = recent["volume"] > avg_vol

    bullish_confirm = int((price_up & vol_above).sum())
    bearish_confirm = int((~price_up & vol_above).sum())

    denom = bullish_confirm + bearish_confirm
    if denom == 0:
        return 0.0
    return round((bullish_confirm - bearish_confirm) / denom, 2)


# ── Main Predictor ─────────────────────────────────────────────────────────

def predict_direction(df: pd.DataFrame, lookback: int = 20) -> dict:
    """
    Statistical direction prediction based on technical indicator confluence.

    Scoring system (max_score = 9.5):
        ±1.0  RSI oversold/overbought
        ±0.5  RSI mild zone
        ±1.5  MACD histogram direction + acceleration
        ±1.5  EMA alignment (full/partial)
        ±1.0  Price momentum (lookback period)
        ±0.5  Volume confirmation (proportional weight)
        ±1.0  ADX trend strength (directional boost)
        ±1.0  Candlestick patterns
        ±0.5  RSI divergence (−0.5 for bearish, +0.5 for bullish)
        ±0.5  Stochastic RSI oversold/overbought
        ±0.5  MFI oversold/overbought
        ±0.5  OBV trend alignment

    Returns:
        dict with direction, confidence, prob_up, prob_down, method,
        score_breakdown, detected_patterns, divergence
    """
    if df.empty or len(df) < lookback + 1:
        return {
            "direction": "NEUTRAL",
            "confidence": 0,
            "prob_up": 50,
            "prob_down": 50,
            "method": "statistical_fallback",
            "score_breakdown": {},
            "detected_patterns": [],
            "divergence": "none",
        }

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last

    score = 0.0
    max_score = 10.0  # Updated: RSI now ±1.5 (was ±1.0) + rest unchanged
    breakdown = {}

    # 1. RSI Signal (±1.5) — wider range for clearer directional bias
    # Key fix: RSI 60-65 zone was getting only -0.5 (mild) but is actually
    # a clear BULLISH momentum zone — shorts should be heavily penalised.
    rsi = last.get("RSI_14", 50)
    if pd.notna(rsi):
        if rsi < 30:
            score += 1.5;  breakdown["rsi"] = "+1.5 (deeply oversold — strong buy signal)"
        elif rsi < 40:
            score += 1.0;  breakdown["rsi"] = "+1.0 (oversold)"
        elif rsi < 48:
            score += 0.5;  breakdown["rsi"] = "+0.5 (mild oversold)"
        elif rsi > 70:
            score -= 1.5;  breakdown["rsi"] = "-1.5 (deeply overbought — strong sell signal)"
        elif rsi > 60:
            score -= 1.0;  breakdown["rsi"] = "-1.0 (overbought zone — SHORT unfavoured)"
        elif rsi > 52:
            score -= 0.5;  breakdown["rsi"] = "-0.5 (mild overbought)"
        else:
            breakdown["rsi"] = "0 (neutral 48-52)"
    else:
        breakdown["rsi"] = "0 (N/A)"

    # 2. MACD Histogram (±1.5)
    macd_hist = last.get("MACD_hist", 0)
    prev_hist = prev.get("MACD_hist", 0)
    if pd.notna(macd_hist) and pd.notna(prev_hist):
        if macd_hist > 0 and macd_hist > prev_hist:
            score += 1.5;  breakdown["macd"] = "+1.5 (bullish + accelerating)"
        elif macd_hist > 0:
            score += 0.5;  breakdown["macd"] = "+0.5 (bullish decelerating)"
        elif macd_hist < 0 and macd_hist < prev_hist:
            score -= 1.5;  breakdown["macd"] = "-1.5 (bearish + accelerating)"
        elif macd_hist < 0:
            score -= 0.5;  breakdown["macd"] = "-0.5 (bearish decelerating)"
        else:
            breakdown["macd"] = "0 (flat)"
    else:
        breakdown["macd"] = "0 (N/A)"

    # 3. EMA Alignment (±1.5)
    ema20  = last.get("EMA_20")
    ema50  = last.get("EMA_50")
    ema200 = last.get("EMA_200")
    close  = last.get("close")
    if all(pd.notna(v) for v in [ema20, ema50, close]):
        if pd.notna(ema200) and close > ema20 > ema50 > ema200:
            score += 1.5;  breakdown["ema"] = "+1.5 (full bullish stack)"
        elif close > ema20 > ema50:
            score += 0.5;  breakdown["ema"] = "+0.5 (partial bullish)"
        elif pd.notna(ema200) and close < ema20 < ema50 < ema200:
            score -= 1.5;  breakdown["ema"] = "-1.5 (full bearish stack)"
        elif close < ema20 < ema50:
            score -= 0.5;  breakdown["ema"] = "-0.5 (partial bearish)"
        else:
            breakdown["ema"] = "0 (mixed)"
    else:
        breakdown["ema"] = "0 (N/A)"

    # 4. Price Momentum (±1.0)
    if len(df) > lookback:
        past_close = df.iloc[-lookback]["close"]
        if pd.notna(past_close) and pd.notna(close) and past_close > 0:
            pct_move = (close - past_close) / past_close * 100
            if pct_move > 2.0:
                score += 1.0;  breakdown["momentum"] = f"+1.0 ({pct_move:+.1f}%)"
            elif pct_move > 0.5:
                score += 0.5;  breakdown["momentum"] = f"+0.5 ({pct_move:+.1f}%)"
            elif pct_move < -2.0:
                score -= 1.0;  breakdown["momentum"] = f"-1.0 ({pct_move:+.1f}%)"
            elif pct_move < -0.5:
                score -= 0.5;  breakdown["momentum"] = f"-0.5 ({pct_move:+.1f}%)"
            else:
                breakdown["momentum"] = f"0 ({pct_move:+.1f}% flat)"
        else:
            breakdown["momentum"] = "0 (N/A)"
    else:
        breakdown["momentum"] = "0 (insufficient data)"

    # 5. Volume (±0.5 proportional, not binary)
    vol = last.get("volume_ratio_20") if "volume_ratio_20" in df.columns else None
    if vol is None:
        vol_raw = last.get("volume")
        if pd.notna(vol_raw) and len(df) > 20:
            avg_vol = df["volume"].iloc[-20:].mean()
            vol = (vol_raw / avg_vol) if avg_vol > 0 else None
    if vol is not None and pd.notna(vol):
        # Proportional: 0.5x→0pts, 1.0x→0pts, 1.5x→0.25pts, 3x→0.5pts (capped)
        vol_boost = min(0.5, max(0, (vol - 1.0) / 4.0))
        directional_vol = vol_boost if score > 0 else -vol_boost if score < 0 else 0
        score += directional_vol
        breakdown["volume"] = f"{directional_vol:+.2f} (ratio={vol:.2f}x)"
    else:
        breakdown["volume"] = "0 (N/A)"

    # 6. ADX trend strength (±1.0 directional boost)
    adx_val = last.get(f"ADX_{settings.ADX_PERIOD}")
    if pd.notna(adx_val) and adx_val > 25:
        adx_boost = 1.0 if score > 0 else -1.0 if score < 0 else 0
        score += adx_boost
        breakdown["adx"] = f"{adx_boost:+.1f} (ADX={adx_val:.0f})"
    else:
        adx_display = f"{adx_val:.0f}" if pd.notna(adx_val) else "N/A"
        breakdown["adx"] = f"0 (ADX={adx_display}, weak)"

    # 7. Candlestick patterns (±1.0)
    candle_result = _detect_candlestick_patterns(df)
    candle_score = candle_result["score"]
    if abs(candle_score) > 0:
        # Cap contribution at ±1.0
        candle_contrib = max(-1.0, min(1.0, candle_score))
        score += candle_contrib
        breakdown["candles"] = f"{candle_contrib:+.1f} {candle_result['patterns']}"
    else:
        breakdown["candles"] = f"0 {candle_result['patterns']}"

    # 8. RSI divergence (±0.5)
    divergence = _detect_divergence(df)
    if divergence == "bullish_divergence":
        score += 0.5
        breakdown["divergence"] = "+0.5 (bullish divergence)"
    elif divergence == "bearish_divergence":
        score -= 0.5
        breakdown["divergence"] = "-0.5 (bearish divergence)"
    else:
        breakdown["divergence"] = "0 (none)"

    # 9. Stochastic RSI (±0.5)
    stoch_k = last.get("StochRSI_K")
    stoch_d = last.get("StochRSI_D")
    if pd.notna(stoch_k) and pd.notna(stoch_d):
        if stoch_k < 20 and stoch_k > stoch_d:        # Oversold + K crossing up
            score += 0.5;  breakdown["stoch_rsi"] = "+0.5 (oversold crossup)"
        elif stoch_k > 80 and stoch_k < stoch_d:      # Overbought + K crossing down
            score -= 0.5;  breakdown["stoch_rsi"] = "-0.5 (overbought crossdown)"
        else:
            breakdown["stoch_rsi"] = "0 (neutral)"
    else:
        breakdown["stoch_rsi"] = "0 (N/A)"

    # 10. MFI (±0.5)
    mfi = last.get("MFI_14")
    if pd.notna(mfi):
        if mfi < 20:
            score += 0.5;  breakdown["mfi"] = "+0.5 (MFI oversold)"
        elif mfi > 80:
            score -= 0.5;  breakdown["mfi"] = "-0.5 (MFI overbought)"
        else:
            breakdown["mfi"] = f"0 (MFI={mfi:.0f})"
    else:
        breakdown["mfi"] = "0 (N/A)"

    # 11. OBV trend (±0.5 — uses OBV_ROC5 if available)
    obv_roc = last.get("OBV_ROC5")
    if pd.notna(obv_roc):
        if obv_roc > 2 and score > 0:
            score += 0.5;  breakdown["obv"] = f"+0.5 (OBV rising {obv_roc:.1f}%)"
        elif obv_roc < -2 and score < 0:
            score -= 0.5;  breakdown["obv"] = f"-0.5 (OBV falling {obv_roc:.1f}%)"
        else:
            breakdown["obv"] = f"0 (OBV_ROC={obv_roc:.1f}%)"
    else:
        breakdown["obv"] = "0 (N/A)"

    # ── Convert score → direction + confidence ─────────
    abs_score = abs(score)
    raw_confidence = (abs_score / max_score) * 100

    if abs_score < 0.5:
        direction = "NEUTRAL"
    elif score > 0:
        direction = "UP"
    else:
        direction = "DOWN"

    confidence = min(round(raw_confidence, 1), 95)

    if direction == "UP":
        prob_up, prob_down = 50 + confidence / 2, 50 - confidence / 2
    elif direction == "DOWN":
        prob_down, prob_up = 50 + confidence / 2, 50 - confidence / 2
    else:
        prob_up = prob_down = 50.0

    log.debug(
        "Prediction: %s (conf=%.1f%%) | Score: %.2f/%.1f | Divergence: %s | Patterns: %s",
        direction, confidence, score, max_score,
        divergence, candle_result["patterns"],
    )

    return {
        "direction":        direction,
        "confidence":       confidence,
        "prob_up":          round(prob_up, 1),
        "prob_down":        round(prob_down, 1),
        "method":           "statistical_fallback_v2",
        "score_breakdown":  breakdown,
        "detected_patterns": candle_result["patterns"],
        "divergence":       divergence,
    }


def predict_ensemble(df: pd.DataFrame) -> dict:
    """
    Ensemble predictor: combines three lookback windows with recency weighting.

    Three "sub-models":
        Short  (lookback=10, weight=0.50) — captures recent momentum shifts
        Medium (lookback=20, weight=0.30) — standard swing-trade horizon
        Long   (lookback=40, weight=0.20) — broader trend context

    Each sub-model runs predict_direction() independently.  The signed
    confidence scores are blended; agreement between time-frames boosts the
    final confidence while disagreement dampens it (noise reduction).

    Returns the same dict structure as predict_direction() with an additional
    key ``ensemble_agreement`` (0–3 sub-models agreeing on direction).
    """
    if len(df) < 42:
        return {
            "direction":          "NEUTRAL",
            "confidence":         0,
            "prob_up":            50,
            "prob_down":          50,
            "method":             "ensemble_fallback",
            "score_breakdown":    {},
            "detected_patterns":  [],
            "divergence":         "none",
            "ensemble_agreement": 0,
        }

    windows = [
        (10, 0.50, "short"),
        (20, 0.30, "medium"),
        (40, 0.20, "long"),
    ]

    signed_scores = []   # +confidence for UP, -confidence for DOWN, 0 for NEUTRAL
    results = []

    for lookback, weight, label in windows:
        window = df.tail(lookback + 20)   # Extra rows so indicators are warm
        pred = predict_direction(window, lookback=min(lookback, len(window) - 1))
        results.append((pred, weight, label))

        if pred["direction"] == "UP":
            signed_scores.append(pred["confidence"] * weight)
        elif pred["direction"] == "DOWN":
            signed_scores.append(-pred["confidence"] * weight)
        else:
            signed_scores.append(0.0)

    # Weighted blended score (already weighted by sub-model weights summing to 1.0)
    blended = sum(signed_scores)

    # Agreement count: how many sub-models point same direction as blended
    if blended > 0:
        final_dir = "UP"
        agreement = sum(1 for s in signed_scores if s > 0)
    elif blended < 0:
        final_dir = "DOWN"
        agreement = sum(1 for s in signed_scores if s < 0)
    else:
        final_dir = "NEUTRAL"
        agreement = 0

    # Confidence = |blended score|, boosted when all 3 agree
    raw_conf = abs(blended)
    if agreement == 3:
        raw_conf = min(raw_conf * 1.15, 95)  # 15% boost for full consensus
    elif agreement == 1:
        raw_conf = raw_conf * 0.85            # 15% penalty for poor consensus

    confidence = round(min(raw_conf, 95), 1)

    if final_dir == "UP":
        prob_up, prob_down = 50 + confidence / 2, 50 - confidence / 2
    elif final_dir == "DOWN":
        prob_down, prob_up = 50 + confidence / 2, 50 - confidence / 2
    else:
        prob_up = prob_down = 50.0

    # Use short-window result for pattern/divergence details (most current)
    short_pred = results[0][0]

    log.debug(
        "Ensemble: %s (conf=%.1f%%, agreement=%d/3) | short=%.1f medium=%.1f long=%.1f",
        final_dir, confidence, agreement,
        signed_scores[0], signed_scores[1], signed_scores[2],
    )

    return {
        "direction":          final_dir,
        "confidence":         confidence,
        "prob_up":            round(prob_up, 1),
        "prob_down":          round(prob_down, 1),
        "method":             "ensemble_fallback",
        "score_breakdown": {
            f"[{label}]_score": f"{sc:+.1f} ({pred['direction']})"
            for (pred, weight, label), sc in zip(results, signed_scores)
        },
        "detected_patterns":  short_pred.get("detected_patterns", []),
        "divergence":         short_pred.get("divergence", "none"),
        "ensemble_agreement": agreement,
    }


def predict_batch(df: pd.DataFrame, seq_length: int = 60) -> pd.Series:
    """
    Generate predictions for each row in a DataFrame using ensemble voting.

    Returns a Series of confidence scores:
      - Values > 50 = bullish bias  (higher = more bullish)
      - Values < 50 = bearish bias  (lower = more bearish)
      - Values near 50 = neutral / no conviction

    This feeds into the signal generator as ai_confidence.
    Uses ensemble (3-window) prediction for each row — falls back to single
    predict_direction() during the warmup period.
    """
    confidences = []
    # Ensemble needs ~60 rows (40 lookback + indicator warmup)
    ensemble_min = seq_length

    for i in range(len(df)):
        if i < ensemble_min:
            confidences.append(50.0)  # Neutral during warmup
            continue

        window = df.iloc[max(0, i - seq_length) : i + 1]
        pred = predict_ensemble(window)

        if pred["direction"] == "UP":
            confidences.append(50.0 + pred["confidence"] / 2)
        elif pred["direction"] == "DOWN":
            confidences.append(50.0 - pred["confidence"] / 2)
        else:
            confidences.append(50.0)  # NEUTRAL

    return pd.Series(confidences, index=df.index, name="ai_confidence")
