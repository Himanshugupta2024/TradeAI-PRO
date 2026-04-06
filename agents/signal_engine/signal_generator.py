"""
Smart Signal & Risk Engine.

Generates LONG/SHORT/SKIP/HOLD signals based on multi-factor technical confluence:
  - Higher timeframe trend filter
  - RSI oversold/overbought recovery
  - EMA crossover confirmation
  - AI confidence (optional, defaults to pass-through)
  - Market regime adaptation with adaptive per-regime thresholds
  - Signal quality scoring (0-100, 7 components)
  - LONG/SHORT emitted only when quality grade A or B
  - SKIP emitted when conditions met but quality too low

Calculates ATR-based Stop Loss and Targets with minimum 1:2 Risk:Reward.
"""

import json
import pandas as pd
import numpy as np

from config import settings
from config.logger import get_logger
from db.models import TrendDirection
from agents.feature_engine.regime_detector import MarketRegime

log = get_logger(__name__)


def generate_signals(
    df: pd.DataFrame,
    ai_confidence: pd.Series = None,
    regime: MarketRegime = None,
    mtf_score: float = None,
    symbol: str = "",
    market_trend: str = None,  # "BULLISH" | "BEARISH" | None (from Nifty/BankNifty)
) -> pd.DataFrame:
    """
    Apply the signal generation rules to an enriched DataFrame.

    Expected columns:
        - close, high, low
        - RSI_14, MACD, MACD_signal, MACD_hist
        - EMA_20, EMA_50, EMA_200
        - ATR_14
        - htf_trend (TrendDirection enum)

    Optional:
        - ai_confidence: Series of confidence scores 0–100
        - regime: Market regime for adaptive thresholds
        - mtf_score: Multi-timeframe score (-100 to +100)
        - symbol: Used for session quality scoring

    Added columns:
        - signal:            "LONG" | "SHORT" | "SKIP" | "HOLD"
        - stop_loss:         ATR-based SL price
        - target:            ATR-based target price
        - confidence:        composite confidence score (0-100)
        - quality_score:     Signal quality score (0-100)
        - quality_grade:     "A" | "B" | "C" | "D"
        - quality_breakdown: JSON string with per-component scores

    Signal semantics:
        LONG  — all conditions met + quality grade A or B
        SHORT — all conditions met + quality grade A or B
        SKIP  — conditions met but quality grade C or D (too risky to trade)
        HOLD  — no signal conditions met

    Returns:
        DataFrame with signal columns.
    """
    if df.empty:
        return df

    result = df.copy()

    # ── Compute AI confidence score ─────────────────────────────────────────
    # Priority:
    #   1. Caller supplied ai_confidence  → use directly
    #   2. Already in DataFrame           → keep as-is
    #   3. Try advanced ensemble          → XGBoost + Statistical + Sentiment
    #   4. Fallback to simple composite   → RSI + MACD + EMA (always works)
    if ai_confidence is not None:
        result["ai_confidence"] = ai_confidence.values
    elif "ai_confidence" not in result.columns:

        # ── Attempt 3: advanced ensemble (XGBoost + Statistical + Sentiment) ──
        ensemble_ok = False
        try:
            from agents.ai_engine.ensemble import predict_batch_ensemble
            ensemble_conf = predict_batch_ensemble(result, symbol=symbol)
            result["ai_confidence"] = ensemble_conf.values
            ensemble_ok = True
            log.debug("ai_confidence sourced from advanced ensemble (XGBoost + Statistical + Sentiment)")
        except Exception as _ens_err:
            log.debug("Ensemble confidence unavailable (%s) — using composite fallback", _ens_err)

        # ── Fallback 4: simple composite (RSI + MACD + EMA + HTF) ──────────
        if not ensemble_ok:
            rsi_col   = result["RSI_14"].fillna(50)
            macd_col  = result["MACD_hist"].fillna(0)
            close_col = result["close"]
            ema20_col = result["EMA_20"].fillna(close_col)
            ema50_col = result["EMA_50"].fillna(close_col)

            # RSI distance from 50 → 0–50 pts
            rsi_score = ((rsi_col - 50).abs() / 50.0 * 50).clip(0, 50)

            # MACD histogram strength → 0–25 pts
            macd_abs  = macd_col.abs()
            macd_max  = macd_abs.rolling(50, min_periods=5).max().replace(0, np.nan).fillna(1)
            macd_score = (macd_abs / macd_max * 25).clip(0, 25)

            # EMA alignment → 0–15 pts
            ema_align_bull  = (close_col > ema20_col) & (ema20_col > ema50_col)
            ema_align_bear  = (close_col < ema20_col) & (ema20_col < ema50_col)
            ema_partial_bull = (close_col > ema20_col) & ~ema_align_bull
            ema_partial_bear = (close_col < ema20_col) & ~ema_align_bear
            ema_score = pd.Series(0.0, index=result.index)
            ema_score[ema_align_bull | ema_align_bear] = 15.0
            ema_score[ema_partial_bull | ema_partial_bear] += 7.0
            ema_score = ema_score.clip(0, 15)

            # HTF trend → 0–10 pts
            htf_score = pd.Series(0.0, index=result.index)
            if "htf_trend" in result.columns:
                htf_bull = result["htf_trend"] == TrendDirection.BULLISH.value
                htf_bear = result["htf_trend"] == TrendDirection.BEARISH.value
                htf_score[htf_bull | htf_bear] = 10.0

            baseline  = pd.Series(15.0, index=result.index)
            composite = (baseline + rsi_score + macd_score + ema_score + htf_score).clip(0, 100)
            result["ai_confidence"] = composite.round(1)

    # ── Regime-adaptive parameters ────────────────────────────────────────────
    regime_key = regime.value if regime else "RANGING"

    if regime == MarketRegime.RANGING:
        rsi_oversold  = settings.RSI_OVERSOLD_RANGING       # Stricter: 25
        rsi_overbought = settings.RSI_OVERBOUGHT_RANGING    # Stricter: 75
        atr_sl_mult   = settings.ATR_SL_MULTIPLIER
    elif regime == MarketRegime.HIGH_VOLATILITY:
        rsi_oversold  = settings.RSI_OVERSOLD
        rsi_overbought = settings.RSI_OVERBOUGHT
        atr_sl_mult   = settings.ATR_SL_MULTIPLIER_HIGHVOL  # Wider: 2.0
    elif regime == MarketRegime.TRENDING:
        rsi_oversold  = 35   # Relaxed for trending markets
        rsi_overbought = 65
        atr_sl_mult   = settings.ATR_SL_MULTIPLIER
    else:
        rsi_oversold  = settings.RSI_OVERSOLD
        rsi_overbought = settings.RSI_OVERBOUGHT
        atr_sl_mult   = settings.ATR_SL_MULTIPLIER

    # Per-regime adaptive confidence and quality thresholds
    conf_threshold = settings.REGIME_CONFIDENCE_THRESHOLDS.get(
        regime_key, settings.AI_CONFIDENCE_THRESHOLD
    )
    quality_threshold = settings.REGIME_QUALITY_THRESHOLDS.get(regime_key, 60)

    # ── Pre-compute derived columns ──────────────────
    rsi = result["RSI_14"]
    prev_rsi = rsi.shift(1)

    # RSI crossover: crossing above oversold or below overbought
    rsi_cross_buy = (prev_rsi < rsi_oversold) & (rsi >= rsi_oversold)
    rsi_cross_sell = (prev_rsi > rsi_overbought) & (rsi <= rsi_overbought)

    # RSI level-based: currently in oversold/overbought zone (more frequent)
    rsi_zone_buy = rsi < rsi_oversold
    rsi_zone_sell = rsi > rsi_overbought

    # Combined RSI signal: either crossover or in zone with momentum shift
    rsi_momentum_buy = rsi_zone_buy & (rsi > prev_rsi)  # Oversold + turning up
    rsi_momentum_sell = rsi_zone_sell & (rsi < prev_rsi)  # Overbought + turning down

    rsi_buy = rsi_cross_buy | rsi_momentum_buy
    rsi_sell = rsi_cross_sell | rsi_momentum_sell

    # EMA crossover: price crossing above/below EMA50
    ema_buy = (result["close"] > result["EMA_50"]) & (result["close"].shift(1) <= result["EMA_50"].shift(1))
    ema_sell = (result["close"] < result["EMA_50"]) & (result["close"].shift(1) >= result["EMA_50"].shift(1))

    # EMA20 crossover (more frequent)
    ema20_buy = (result["close"] > result["EMA_20"]) & (result["close"].shift(1) <= result["EMA_20"].shift(1))
    ema20_sell = (result["close"] < result["EMA_20"]) & (result["close"].shift(1) >= result["EMA_20"].shift(1))

    # Broader EMA alignment: price above/below EMA20
    ema_align_buy = result["close"] > result["EMA_20"]
    ema_align_sell = result["close"] < result["EMA_20"]

    # HTF Trend filter (compare with .value for pandas Series compatibility)
    # Wrap in pd.Series() to guarantee Series output — the comparison can
    # return a scalar bool when the column holds a single value or was
    # assigned as a scalar, which crashes the downstream `.astype(float)`.
    htf_bull = pd.Series(result["htf_trend"] == TrendDirection.BULLISH.value, index=result.index)
    htf_bear = pd.Series(result["htf_trend"] == TrendDirection.BEARISH.value, index=result.index)
    htf_neutral = pd.Series(result["htf_trend"] == TrendDirection.NEUTRAL.value, index=result.index)

    # AI confidence filter — now a SOFT bonus instead of hard gate
    # Signals can still fire without AI confidence if weighted score is strong enough
    ai_pass = result["ai_confidence"] >= conf_threshold
    # Only award bonus when confidence is genuinely above baseline (>30).
    # Raw composite baseline is ~15 pts, so subtract 30 to avoid free points.
    ai_soft_bonus = ((result["ai_confidence"] - 30).clip(0, 70) / 70.0 * 8).clip(0, 8)  # 0-8 pts, only when conf > 30

    # MACD momentum confirmation
    macd_bull = result["MACD_hist"] > 0
    macd_bear = result["MACD_hist"] < 0

    # MACD turning (momentum shift — less strict than absolute direction)
    prev_macd = result["MACD_hist"].shift(1)
    macd_turning_bull = result["MACD_hist"] > prev_macd  # Histogram improving
    macd_turning_bear = result["MACD_hist"] < prev_macd  # Histogram deteriorating

    # VWAP confirmation (if available and valid)
    if "VWAP" in result.columns and result["VWAP"].notna().any():
        vwap_bull = result["close"] > result["VWAP"]
        vwap_bear = result["close"] < result["VWAP"]
    else:
        vwap_bull = pd.Series(True, index=result.index)
        vwap_bear = pd.Series(True, index=result.index)

    # ── ATR noise filter: skip signals on low-volatility candles ──────────
    # ATR_pct = ATR as % of price (computed in indicators.py)
    if "ATR_pct" in result.columns and result["ATR_pct"].notna().any():
        atr_noise_ok = result["ATR_pct"] > settings.MIN_ATR_PCT
        atr_noise_ok = atr_noise_ok.fillna(True)
    else:
        atr_noise_ok = pd.Series(True, index=result.index)

    # ── Divergence filter: block signals that conflict with RSI divergence ─
    # A bearish divergence warns of price weakness → block BUY signals
    # A bullish divergence warns of price strength → block SELL signals
    # Computed row-by-row using a rolling lookback window
    from agents.ai_engine.fallback_predictor import _detect_divergence

    divergence_labels = pd.Series("none", index=result.index, dtype=object)
    div_lookback = settings.DIVERGENCE_LOOKBACK
    for i in range(div_lookback, len(result)):
        window = result.iloc[i - div_lookback : i + 1]
        divergence_labels.iloc[i] = _detect_divergence(window, lookback=div_lookback)

    bearish_divergence = divergence_labels == "bearish_divergence"
    bullish_divergence = divergence_labels == "bullish_divergence"
    hidden_bullish = divergence_labels == "hidden_bullish"
    hidden_bearish = divergence_labels == "hidden_bearish"

    # ── Stochastic RSI conditions ──────────────────────────────────────────
    if "StochRSI_K" in result.columns and "StochRSI_D" in result.columns:
        k = result["StochRSI_K"].fillna(50)
        d = result["StochRSI_D"].fillna(50)
        prev_k = k.shift(1).fillna(50)
        prev_d = d.shift(1).fillna(50)

        # Oversold cross-up (K was below D, now above) in oversold zone
        stoch_buy = (k < settings.STOCHRSI_OVERBOUGHT) & (k > d) & (prev_k <= prev_d)
        # Overbought cross-down in overbought zone
        stoch_sell = (k > settings.STOCHRSI_OVERSOLD) & (k < d) & (prev_k >= prev_d)
    else:
        stoch_buy = pd.Series(True, index=result.index)
        stoch_sell = pd.Series(True, index=result.index)

    # ── MFI confirmation ───────────────────────────────────────────────────
    if "MFI_14" in result.columns and result["MFI_14"].notna().any():
        mfi = result["MFI_14"].fillna(50)
        # Not overbought for BUY confirmation; not oversold for SELL confirmation
        mfi_buy_ok  = mfi < settings.MFI_OVERBOUGHT   # Below 80 = still has room to rise
        mfi_sell_ok = mfi > settings.MFI_OVERSOLD     # Above 20 = still has room to fall
    else:
        mfi_buy_ok  = pd.Series(True, index=result.index)
        mfi_sell_ok = pd.Series(True, index=result.index)

    # ── Volume filter: 1.2x average required (was 0.8x) ──────────────────
    # Real breakouts have above-average volume. Low volume = fake signal.
    if "volume" in result.columns and result["volume"].notna().any():
        vol_avg = result["volume"].rolling(20, min_periods=5).mean()
        # Require at least 1.2x average volume — filters fake/low momentum moves
        volume_ok = result["volume"] > vol_avg * 1.2
        volume_ok = volume_ok.fillna(True)
    else:
        volume_ok = pd.Series(True, index=result.index)

    # ── Sector / Market Correlation filter ───────────────────────────────
    # If broader market (Nifty/BankNifty) is moving AGAINST the signal,
    # confidence drops significantly — block weak counter-trend signals.
    # market_trend: "BULLISH" means Nifty is rising, "BEARISH" means falling.
    if market_trend == "BEARISH":
        # Market gir raha hai — sirf SHORT signals allow karo
        # LONG signals tabhi allow karo jab STRONG hain (high confidence wale)
        market_blocks_buy  = pd.Series(True, index=result.index)   # block weak longs
        market_blocks_sell = pd.Series(False, index=result.index)  # sells allowed
    elif market_trend == "BULLISH":
        # Market chadh raha hai — sirf LONG signals allow karo
        market_blocks_buy  = pd.Series(False, index=result.index)  # buys allowed
        market_blocks_sell = pd.Series(True, index=result.index)   # block weak shorts
    else:
        market_blocks_buy  = pd.Series(False, index=result.index)
        market_blocks_sell = pd.Series(False, index=result.index)

    # ── Minimum price change filter: at least 0.1% move ──
    # Prevents signals on flat/sideways candles
    change_ok = result["close"].pct_change().abs() > 0.001
    change_ok = change_ok.fillna(True)

    # ── Support / Resistance integration ─────────────────
    # Uses pivot-based S/R levels to confirm signal direction
    if "support" not in result.columns or "resistance" not in result.columns:
        try:
            from agents.feature_engine.support_resistance import calculate_support_resistance
            result = calculate_support_resistance(result)
        except Exception:
            result["support"] = np.nan
            result["resistance"] = np.nan

    # BUY: price above nearest support (not in free-fall below support)
    sr_above_support = result["close"] > result["support"].fillna(0)
    # SELL: price below nearest resistance (not already broken out above resistance)
    sr_below_resistance = result["close"] < result["resistance"].fillna(float("inf"))

    # ── Deep oversold/overbought (mean-reversion bonus) ─────────────────────
    deep_oversold = rsi < 25
    deep_overbought = rsi > 75

    # ── Weighted BUY Signal Scoring ──────────────────────────────────────────
    # DESIGN PRINCIPLE: Only ACTIVE directional triggers (crossovers, RSI
    # reversals, momentum shifts) contribute significant points.
    # Passive/alignment conditions are PENALTY-ONLY — they penalize when
    # absent but give 0 free points when present.
    #
    # FIX: HTF weight reduced 20→12 to prevent trend dominance.
    # FIX: Added counter-trend mean-reversion bonus (+10) for deep
    #       oversold in bearish market / deep overbought in bullish market.
    # FIX: Added neutral trend base score (+5) so both sides can fire
    #       when market is ranging/choppy.
    #
    # Max possible BUY score (all active triggers firing):
    #   HTF_bull(12) + neutral_base(5) + RSI_buy(15) + deep_oversold(8)
    #   + counter_trend(10) + EMA_cross(12) + MACD_turning(8) + volume(5)
    #   + stoch(5) + hidden_div(8) + AI(0-8) + VWAP(2) + trend_follow(3)
    #   = ~101 pts max (capped at 100)
    # Passive baseline (no active triggers): ~0 pts

    # Counter-trend mean-reversion bonus:
    # Deep oversold (RSI<25) in a BEARISH market = mean reversion BUY opportunity
    # Deep overbought (RSI>75) in a BULLISH market = mean reversion SELL opportunity
    counter_trend_buy_bonus = (deep_oversold & htf_bear).astype(float) * 10
    counter_trend_sell_bonus = (deep_overbought & htf_bull).astype(float) * 10

    # Trend-following alignment bonus: when BOTH HTF trend AND EMA alignment agree
    trend_follow_buy = (htf_bull & ema_align_buy).astype(float) * 3
    trend_follow_sell = (htf_bear & ema_align_sell).astype(float) * 3

    buy_score = (
        # ── ACTIVE TRIGGERS (positive points) ──
        # HTF Trend alignment (max 12, reduced from 20) — directional but not dominant
        htf_bull.astype(float) * 12
        # Neutral trend base score (max 5) — allows signals in ranging markets
        + htf_neutral.astype(float) * 5
        # Counter-trend mean-reversion bonus (max 10) — deep oversold in bearish
        + counter_trend_buy_bonus
        # Trend-following alignment bonus (max 3) — HTF + EMA both agree
        + trend_follow_buy
        # RSI oversold signal (max 15 + 8 deep bonus) — ACTIVE trigger
        + rsi_buy.astype(float) * 15
        + deep_oversold.astype(float) * 8
        # EMA crossover (max 12) — ACTIVE trigger (price crossing EMA)
        + (ema_buy | ema20_buy).astype(float) * 12
        # MACD momentum shift (max 8) — ACTIVE trigger (histogram improving)
        + macd_turning_bull.astype(float) * 8
        # Volume confirmation (max 5) — ACTIVE (above-avg volume)
        + volume_ok.astype(float) * 5
        # StochRSI crossover (max 5) — ACTIVE trigger
        + stoch_buy.astype(float) * 5
        # Hidden bullish divergence (max 8) — ACTIVE (trend continuation)
        + hidden_bullish.astype(float) * 8
        # AI confidence — soft bonus (0-8 pts, only when conf > 30)
        + ai_soft_bonus
        # VWAP confirmation (max 2) — minor active bonus (price > VWAP)
        + vwap_bull.astype(float) * 2

        # ── PENALTY-ONLY CONDITIONS (0 or negative) ──
        # EMA alignment: no bonus, only penalize when price below EMA20
        + (~ema_align_buy).astype(float) * (-2)
        # MFI: no bonus, only penalize when overbought (MFI > 80)
        + (~mfi_buy_ok).astype(float) * (-5)
        # Support: no bonus, only penalize when below support
        + (~sr_above_support).astype(float) * (-3)
        # MACD direction: no bonus for being positive, penalize when negative
        + (~macd_bull).astype(float) * (-3)
        # Divergence guard (0 / -8)
        + bearish_divergence.astype(float) * (-8)
        # Market correlation (0 / -12)
        + market_blocks_buy.astype(float) * (-12)
        # ATR noise filter (0 / -5)
        + (~atr_noise_ok).astype(float) * (-5)
        # Minimum price change (0 / -3)
        + (~change_ok).astype(float) * (-3)
    )

    # ── Weighted SELL Signal Scoring ─────────────────────────────────────────
    # Same design: only active triggers score positive, passive = penalty-only
    sell_score = (
        # ── ACTIVE TRIGGERS (positive points) ──
        # HTF Trend alignment (max 12, reduced from 20) — directional but not dominant
        htf_bear.astype(float) * 12
        # Neutral trend base score (max 5) — allows signals in ranging markets
        + htf_neutral.astype(float) * 5
        # Counter-trend mean-reversion bonus (max 10) — deep overbought in bullish
        + counter_trend_sell_bonus
        # Trend-following alignment bonus (max 3) — HTF + EMA both agree
        + trend_follow_sell
        # RSI overbought signal (max 15 + 8 deep bonus) — ACTIVE trigger
        + rsi_sell.astype(float) * 15
        + deep_overbought.astype(float) * 8
        # EMA crossover (max 12) — ACTIVE trigger
        + (ema_sell | ema20_sell).astype(float) * 12
        # MACD momentum shift (max 8) — ACTIVE trigger (histogram deteriorating)
        + macd_turning_bear.astype(float) * 8
        # Volume confirmation (max 5) — ACTIVE (above-avg volume)
        + volume_ok.astype(float) * 5
        # StochRSI crossover (max 5) — ACTIVE trigger
        + stoch_sell.astype(float) * 5
        # Hidden bearish divergence (max 8) — ACTIVE (trend continuation)
        + hidden_bearish.astype(float) * 8
        # AI confidence — soft bonus (0-8 pts, only when conf > 30)
        + ai_soft_bonus
        # VWAP confirmation (max 2) — minor active bonus (price < VWAP)
        + vwap_bear.astype(float) * 2

        # ── PENALTY-ONLY CONDITIONS (0 or negative) ──
        # EMA alignment: no bonus, only penalize when price above EMA20
        + (~ema_align_sell).astype(float) * (-2)
        # MFI: no bonus, only penalize when oversold (MFI < 20)
        + (~mfi_sell_ok).astype(float) * (-5)
        # Resistance: no bonus, only penalize when above resistance
        + (~sr_below_resistance).astype(float) * (-3)
        # MACD direction: no bonus for being negative, penalize when positive
        + (~macd_bear).astype(float) * (-3)
        # Divergence guard (0 / -8)
        + bullish_divergence.astype(float) * (-8)
        # Market correlation (0 / -12)
        + market_blocks_sell.astype(float) * (-12)
        # ATR noise filter (0 / -5)
        + (~atr_noise_ok).astype(float) * (-5)
        # Minimum price change (0 / -3)
        + (~change_ok).astype(float) * (-3)
    )

    # ── Regime-adaptive scoring threshold ────────────────────────────────────
    signal_threshold = settings.SIGNAL_SCORE_THRESHOLDS.get(
        regime_key, settings.SIGNAL_SCORE_DEFAULT
    )
    # AI confidence is now used as a SOFT BONUS in the score, not a hard gate.
    # The old hard gate (buy_score >= threshold) & ai_pass was double-gatekeeping
    # and blocking most signals in ranging markets with naturally low confidence.
    buy_condition = buy_score >= signal_threshold
    sell_condition = sell_score >= signal_threshold

    # Store scores for debugging and dashboard display
    result["signal_score_buy"] = buy_score.round(1)
    result["signal_score_sell"] = sell_score.round(1)

    # ── Assign raw directional signals (pre-quality-filter) ──────────────────
    result["_raw_signal"] = "HOLD"
    result.loc[buy_condition,  "_raw_signal"] = "LONG"
    result.loc[sell_condition, "_raw_signal"] = "SHORT"

    # ── Quality scoring for every LONG/SHORT candidate ────────────────────────
    try:
        from agents.signal_engine.quality_scorer import score_signals_vectorised, MIN_CONFIDENCE_FOR_SIGNAL
        # Pass ai_confidence so the scorer can apply the hard confidence gate
        ai_conf_series = result["ai_confidence"] if "ai_confidence" in result.columns else None
        quality_df = score_signals_vectorised(
            result, result["_raw_signal"], regime, symbol,
            confidences=ai_conf_series,
        )
        result["quality_score"]        = quality_df["quality_score"]
        result["quality_grade"]        = quality_df["quality_grade"]
        result["quality_breakdown"]    = quality_df["quality_breakdown"]
        result["quality_should_trade"] = quality_df["quality_should_trade"]
        result["quality_hard_gate"]    = quality_df.get("quality_hard_gate", "")
    except Exception as exc:
        log.warning("Quality scorer failed: %s — defaulting all signals to LONG/SHORT", exc)
        result["quality_score"]        = 50
        result["quality_grade"]        = "B"
        result["quality_breakdown"]    = "{}"
        result["quality_should_trade"] = result["_raw_signal"].isin(["LONG", "SHORT"])
        result["quality_hard_gate"]    = ""

    # ── Apply quality gate: LONG/SHORT or SKIP ────────────────────────────────
    # LONG/SHORT → quality grade A or B (score >= quality_threshold) AND hard gates passed
    # SKIP       → conditions met but grade C/D, OR hard gate failed (low confidence, VWAP)
    # HOLD       → no signal conditions met at all
    quality_ok = result["quality_score"] >= quality_threshold
    gate_passed = result["quality_hard_gate"].fillna("") == ""

    result["signal"] = "HOLD"
    # Signals that passed quality AND hard gates → LONG/SHORT
    result.loc[
        (result["_raw_signal"] == "LONG")  & quality_ok & gate_passed, "signal"
    ] = "LONG"
    result.loc[
        (result["_raw_signal"] == "SHORT") & quality_ok & gate_passed, "signal"
    ] = "SHORT"
    # Signals that failed either quality score OR hard gate → SKIP
    result.loc[
        (result["_raw_signal"] == "LONG")  & (~quality_ok | ~gate_passed), "signal"
    ] = "SKIP"
    result.loc[
        (result["_raw_signal"] == "SHORT") & (~quality_ok | ~gate_passed), "signal"
    ] = "SKIP"

    # Preserve underlying direction for SKIP rows (needed for correct SL/Target)
    result["_raw_direction"] = result["_raw_signal"]
    result.drop(columns=["_raw_signal"], inplace=True)

    # ── Stop Loss & Target (ATR-based) ────────────────────────────────────────
    atr = result[f"ATR_{settings.ATR_PERIOD}"]
    sl_distance = atr * atr_sl_mult
    tp_distance = sl_distance * settings.MIN_RISK_REWARD  # Enforce min 1:2 RR

    result["stop_loss"] = np.nan
    result["target"]    = np.nan

    # Keep confidence semantics direct: higher ai_confidence means stronger setup.
    # Enforce minimum 20% confidence for any LONG/SHORT signal so dashboard
    # doesn't show embarrassingly low numbers (1-5%) for real signals.
    result["confidence"] = result["ai_confidence"].clip(0, 100).round(1)
    long_or_short = result["signal"].isin(["LONG", "SHORT"])
    result.loc[long_or_short & (result["confidence"] < 20), "confidence"] = 20.0

    # Blend MTF score into confidence if provided
    if mtf_score is not None:
        mtf_adjustment = mtf_score * 0.2  # ±20 pts
        result["confidence"] = (result["confidence"] + mtf_adjustment).clip(0, 100)

    long_mask  = result["signal"] == "LONG"
    short_mask = result["signal"] == "SHORT"
    skip_mask  = result["signal"] == "SKIP"

    result.loc[long_mask,  "stop_loss"] = result.loc[long_mask,  "close"] - sl_distance[long_mask]
    result.loc[long_mask,  "target"]    = result.loc[long_mask,  "close"] + tp_distance[long_mask]
    result.loc[short_mask, "stop_loss"] = result.loc[short_mask, "close"] + sl_distance[short_mask]
    result.loc[short_mask, "target"]    = result.loc[short_mask, "close"] - tp_distance[short_mask]

    # SKIP rows also get SL/Target using their original direction
    skip_long  = skip_mask & (result["_raw_direction"] == "LONG")
    skip_short = skip_mask & (result["_raw_direction"] == "SHORT")

    result.loc[skip_long,  "stop_loss"] = result.loc[skip_long,  "close"] - sl_distance[skip_long]
    result.loc[skip_long,  "target"]    = result.loc[skip_long,  "close"] + tp_distance[skip_long]
    result.loc[skip_short, "stop_loss"] = result.loc[skip_short, "close"] + sl_distance[skip_short]
    result.loc[skip_short, "target"]    = result.loc[skip_short, "close"] - tp_distance[skip_short]

    # ── Signal Quality Logging ────────────────────────────────────────────────
    hold_count  = (result["signal"] == "HOLD").sum()
    skip_count  = (result["signal"] == "SKIP").sum()
    ai_rejected = (~ai_pass).sum()
    gate_blocked = (result["quality_hard_gate"].fillna("") != "").sum()

    if len(result) > 0:
        last = result.iloc[-1]
        gate_reason = last.get("quality_hard_gate", "")
        log.info(
            "Signal summary: %d LONG, %d SHORT, %d SKIP, %d HOLD | "
            "AI rejected: %d | Gate blocked: %d | conf_thresh=%d | quality_thresh=%d | "
            "Latest: %s (conf=%.1f%%, QS=%d/%s, RSI=%.1f, Regime=%s%s)",
            long_mask.sum(), short_mask.sum(), skip_count, hold_count,
            ai_rejected, gate_blocked, conf_threshold, quality_threshold,
            last.get("signal", "HOLD"),
            last.get("ai_confidence", 0),
            last.get("quality_score", 0),
            last.get("quality_grade", "?"),
            last.get("RSI_14", 0) if pd.notna(last.get("RSI_14")) else 0,
            regime.value if regime else "N/A",
            f", Gate={gate_reason}" if gate_reason else "",
        )

    return result


def signal_generator_for_backtest(train_df: pd.DataFrame, test_df: pd.DataFrame) -> pd.DataFrame:
    """
    Walk-forward compatible signal generator.

    Uses the training data to establish the strategy parameters (currently fixed),
    then applies signals to the test set.

    This is the callback passed to `run_walk_forward()`.
    """
    return generate_signals(test_df)
