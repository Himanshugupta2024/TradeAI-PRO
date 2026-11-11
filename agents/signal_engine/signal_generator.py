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
  - Edge Engine macro intelligence layer (optional confidence modifier)

Calculates ATR-based Stop Loss and Targets with minimum 1:2 Risk:Reward.
"""

import os
import json
import pandas as pd
import numpy as np

from config import settings
from config.logger import get_logger
from db.models import TrendDirection
from agents.feature_engine.regime_detector import MarketRegime
from agents.data_ingestion.context_fetcher import build_context_fetcher
from agents.macro.edge_score_engine import EdgeScoreEngine, EdgeScoreBreakdown

log = get_logger(__name__)

# Initialize EdgeScoreEngine at module level (singleton pattern)
_edge_engine = None
_tradeflow_signal_cache: dict = {"fetched_at": 0.0, "ctx": None}
_TRADEFLOW_SIGNAL_CACHE_TTL = 300

def _get_edge_engine() -> EdgeScoreEngine:
    """Lazy-load EdgeScoreEngine singleton"""
    global _edge_engine
    if _edge_engine is None:
        _edge_engine = EdgeScoreEngine()
    return _edge_engine


def _get_tradeflow_signal_context() -> dict:
    default_ctx = {
        "sentiment": "UNAVAILABLE",
        "buy_multiplier": 1.0,
        "sell_multiplier": 1.0,
        "applied": False,
        "true_intent": "UNAVAILABLE",
        "true_intent_score": 0.0,
        "dii_context": "UNAVAILABLE",
        "source_stale": True,
    }

    if not getattr(settings, "TRADEFLOW_SIGNAL_INFLUENCE_ENABLED", True):
        return default_ctx

    now_ts = float(pd.Timestamp.now("UTC").timestamp())
    cached = _tradeflow_signal_cache.get("ctx")
    cached_at = float(_tradeflow_signal_cache.get("fetched_at", 0.0) or 0.0)
    if cached and (now_ts - cached_at) < _TRADEFLOW_SIGNAL_CACHE_TTL:
        return cached

    try:
        from agents.market_intelligence.market_data import fetch_fii_dii_flow

        tf = fetch_fii_dii_flow()
    except Exception as exc:
        log.debug("Tradeflow scoring context unavailable: %s", exc)
        _tradeflow_signal_cache["ctx"] = default_ctx
        _tradeflow_signal_cache["fetched_at"] = now_ts
        return default_ctx

    sentiment = str(tf.get("sentiment", "UNAVAILABLE") or "UNAVAILABLE").upper()
    stale = bool(tf.get("stale", True))
    stale_hours = float(tf.get("age_hours", 0.0) or 0.0)
    disable_hours = float(getattr(settings, "TRADEFLOW_SIGNAL_STALE_DISABLE_HOURS", 24))
    ignore_stale = bool(getattr(settings, "TRADEFLOW_SIGNAL_IGNORE_STALE", True))

    if stale and ignore_stale and stale_hours >= disable_hours:
        ctx = {
            **default_ctx,
            "sentiment": sentiment,
            "true_intent": str(tf.get("true_intent", "UNAVAILABLE") or "UNAVAILABLE"),
            "true_intent_score": float(tf.get("true_intent_score", 0.0) or 0.0),
            "dii_context": str(tf.get("dii_context", "UNAVAILABLE") or "UNAVAILABLE"),
            "source_stale": True,
        }
        _tradeflow_signal_cache["ctx"] = ctx
        _tradeflow_signal_cache["fetched_at"] = now_ts
        return ctx

    bullish = {
        "BOTH_BUYING",
        "FII_STRONG_BUY",
        "FII_MODERATE_BUY",
        "FII_WEAK_BUY",
        "FII_DOMINANT_BUY",
    }
    bearish = {
        "BOTH_SELLING",
        "FII_STRONG_SELL",
        "FII_MODERATE_SELL",
        "FII_WEAK_SELL",
        "FII_DOMINANT_SELL",
    }

    bias_mult = float(getattr(settings, "TRADEFLOW_SIGNAL_BIAS_MULTIPLIER", 1.15))
    counter_mult = float(getattr(settings, "TRADEFLOW_SIGNAL_COUNTER_MULTIPLIER", 0.85))

    buy_multiplier = 1.0
    sell_multiplier = 1.0
    applied = False

    if sentiment in bullish:
        buy_multiplier = bias_mult
        sell_multiplier = counter_mult
        applied = True
    elif sentiment in bearish:
        buy_multiplier = counter_mult
        sell_multiplier = bias_mult
        applied = True

    ctx = {
        "sentiment": sentiment,
        "buy_multiplier": float(buy_multiplier),
        "sell_multiplier": float(sell_multiplier),
        "applied": applied,
        "true_intent": str(tf.get("true_intent", "UNAVAILABLE") or "UNAVAILABLE"),
        "true_intent_score": float(tf.get("true_intent_score", 0.0) or 0.0),
        "dii_context": str(tf.get("dii_context", "UNAVAILABLE") or "UNAVAILABLE"),
        "source_stale": stale,
    }

    _tradeflow_signal_cache["ctx"] = ctx
    _tradeflow_signal_cache["fetched_at"] = now_ts
    return ctx


def _compute_feature_drift(
    df: pd.DataFrame,
    lookback_bars: int = 120,
    recent_bars: int = 20,
) -> tuple[float, bool, str]:
    """Compute simple distribution-shift score using recent-vs-baseline z-delta."""
    if df is None or df.empty or len(df) < max(lookback_bars, recent_bars * 2):
        return 0.0, False, ""

    candidate_cols = ["RSI_14", "MACD_hist", "ATR_14", "volume", "close"]
    scores: dict[str, float] = {}

    for col in candidate_cols:
        if col not in df.columns:
            continue
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(series) < recent_bars * 2:
            continue

        recent = series.tail(recent_bars)
        baseline_window = series.iloc[-(lookback_bars + recent_bars):-recent_bars]
        if len(baseline_window) < recent_bars:
            baseline_window = series.iloc[:-recent_bars]
        if len(baseline_window) < recent_bars:
            continue

        base_mean = float(baseline_window.mean())
        base_std = float(baseline_window.std(ddof=0))
        if base_std <= 1e-9:
            continue

        z_delta = abs(float(recent.mean()) - base_mean) / base_std
        scores[col] = float(z_delta)

    if not scores:
        return 0.0, False, ""

    top_feature, top_score = max(scores.items(), key=lambda kv: kv[1])
    drift_detected = top_score >= float(settings.DRIFT_Z_THRESHOLD)
    return round(float(top_score), 4), drift_detected, top_feature


def _is_altcoin_symbol(symbol: str) -> bool:
    """Return True when symbol is a non-BTC USDT pair."""
    if not symbol:
        return False
    normalized = symbol.replace("/", "").upper()
    return normalized.endswith("USDT") and normalized != "BTCUSDT"


def _apply_context_rules(
    direction: str,
    confidence: float,
    context_score: float,
    event_risk_active: bool,
    news_volume_spike: bool,
    btc_dominance_7d_trend: str,
    symbol: str,
) -> tuple[float, bool, str | None, list[str]]:
    """Apply ordered context confidence adjustments for one signal direction."""
    if direction in ("SKIP", "HOLD"):
        return confidence, False, None, []

    original = float(confidence)
    updated = float(confidence)
    reasons: list[str] = []
    warning: str | None = None

    # 1) Event-risk dampener.
    if event_risk_active:
        updated *= 0.75
        reasons.append("event_risk_active")

    # 2) Context opposing direction dampener.
    if direction == "LONG" and context_score < settings.CONTEXT_STRONG_NEGATIVE:
        updated *= 0.80
        reasons.append("negative_context_vs_long")
    elif direction == "SHORT" and context_score > 0.30:
        updated *= 0.80
        reasons.append("positive_context_vs_short")

    # 3) Strong positive context boost for LONG.
    if direction == "LONG" and context_score > settings.CONTEXT_STRONG_POSITIVE:
        updated = min(updated * 1.10, 95.0)
        reasons.append("strong_positive_context_long")

    # 4) News spike warning for directional signals.
    if news_volume_spike:
        warning = "NEWS_SPIKE - verify before trusting signal"

    # 5) Altcoin LONG penalty when BTC dominance is rising.
    if direction == "LONG" and btc_dominance_7d_trend == "RISING" and _is_altcoin_symbol(symbol):
        updated *= 0.85
        reasons.append("btc_dominance_rising_altcoin_long")

    adjusted = len(reasons) > 0
    if adjusted:
        log.info(
            "[CONTEXT] %s confidence adjusted from %.2f to %.2f - reason: %s",
            symbol,
            original,
            updated,
            ",".join(reasons),
        )

    return round(max(0.0, min(updated, 100.0)), 1), adjusted, warning, reasons


def _candle_quality_confidence_penalty(result: pd.DataFrame) -> pd.Series:
    """Negative confidence points for indecision candles, tiny range vs ATR, color fighting signal."""
    z = pd.Series(0.0, index=result.index)
    if not getattr(settings, "CANDLE_QUALITY_CONF_ADJUST_ENABLED", True):
        return z
    if "_raw_direction" not in result.columns or "close" not in result.columns:
        return z

    if "candle_body_ratio" in result.columns:
        br = pd.to_numeric(result["candle_body_ratio"], errors="coerce").fillna(0.5).clip(0, 1)
    else:
        rng = (result["high"] - result["low"]).replace(0, np.nan)
        body = (result["close"] - result["open"]).abs()
        br = (body / rng).clip(0, 1).fillna(0.5)

    min_br = float(getattr(settings, "CANDLE_QUALITY_MIN_BODY_RATIO", 0.35))
    low_body = br < min_br
    atr_col = f"ATR_{settings.ATR_PERIOD}"
    if atr_col in result.columns:
        atr = pd.to_numeric(result[atr_col], errors="coerce").replace(0, np.nan)
        bar_rng = (result["high"] - result["low"]).abs()
        rel = bar_rng / atr
        tiny_range = rel < float(getattr(settings, "CANDLE_QUALITY_MIN_RANGE_ATR_RATIO", 0.25))
    else:
        tiny_range = pd.Series(False, index=result.index)

    pb = float(getattr(settings, "CANDLE_QUALITY_BODY_PENALTY", 8.0))
    pr = float(getattr(settings, "CANDLE_QUALITY_RANGE_PENALTY", 5.0))
    pf = float(getattr(settings, "CANDLE_QUALITY_DIRECTION_FIGHT_PENALTY", 6.0))

    z = z - low_body.astype(float) * pb
    z = z - tiny_range.astype(float) * pr

    rd = result["_raw_direction"].astype(str).str.upper()
    red = result["close"] < result["open"]
    green = result["close"] > result["open"]
    z = z - ((rd == "LONG") & red).astype(float) * pf
    z = z - ((rd == "SHORT") & green).astype(float) * pf

    mask = rd.isin(["LONG", "SHORT"])
    return z.where(mask, 0.0)


def _mtf_agreement_votes_and_penalty(
    result: pd.DataFrame,
    htf_bull: pd.Series,
    htf_bear: pd.Series,
    ema_align_buy: pd.Series,
    ema_align_sell: pd.Series,
    macd_bull: pd.Series,
    macd_bear: pd.Series,
    vwap_bull: pd.Series,
    vwap_bear: pd.Series,
    mtf_score: float | None,
) -> tuple[pd.Series, pd.Series]:
    """
    Returns (mtf_agreement_votes per row, confidence_delta negative/positive).
    When mtf_score is provided, votes are mapped from |score|; weak scores get a small penalty.
    """
    votes_out = pd.Series(0, index=result.index, dtype=int)
    delta = pd.Series(0.0, index=result.index)
    if not getattr(settings, "MTF_VOTE_CONF_ADJUST_ENABLED", True):
        return votes_out, delta
    if "_raw_direction" not in result.columns:
        return votes_out, delta

    rd = result["_raw_direction"].astype(str).str.upper()
    has_vwap = "VWAP" in result.columns and result["VWAP"].notna().any()

    vl = htf_bull.astype(int) + ema_align_buy.astype(int) + macd_bull.astype(int)
    vs = htf_bear.astype(int) + ema_align_sell.astype(int) + macd_bear.astype(int)
    if has_vwap:
        vl = vl + vwap_bull.astype(int)
        vs = vs + vwap_bear.astype(int)
    max_v = 4 if has_vwap else 3

    if mtf_score is not None:
        mag = abs(float(mtf_score))
        mapped = int(round(mag / 100.0 * max_v))
        directional = rd.isin(["LONG", "SHORT"])
        votes_out = pd.Series(
            np.where(directional, mapped, 0),
            index=result.index,
            dtype=int,
        )
        weak_thr = float(getattr(settings, "MTF_SCORE_WEAK_THRESHOLD", 35))
        weak_pen = float(getattr(settings, "MTF_SCORE_WEAK_PENALTY", 6.0))
        if mag < weak_thr:
            dmag = -((weak_thr - mag) / weak_thr * weak_pen)
            delta = pd.Series(dmag, index=result.index) * directional.astype(float)
        return votes_out, delta

    min_v = max(1, int(getattr(settings, "MTF_AGREEMENT_MIN_VOTES", 2)))
    pen_unit = float(getattr(settings, "MTF_AGREEMENT_LOW_VOTE_PENALTY", 7.0)) / float(min_v)

    shortfall_l = (min_v - vl.astype(float)).clip(lower=0)
    shortfall_s = (min_v - vs.astype(float)).clip(lower=0)

    delta = delta - (rd == "LONG").astype(float) * shortfall_l * pen_unit
    delta = delta - (rd == "SHORT").astype(float) * shortfall_s * pen_unit

    votes_out = np.where(rd == "LONG", vl, np.where(rd == "SHORT", vs, 0))
    votes_out = pd.Series(votes_out, index=result.index, dtype=int)

    return votes_out, delta


def generate_signals(
    df: pd.DataFrame,
    ai_confidence: pd.Series = None,
    regime: MarketRegime = None,
    mtf_score: float = None,
    symbol: str = "",
    market_trend: str = None,  # "BULLISH" | "BEARISH" | None (from Nifty/BankNifty)
    edge_score: EdgeScoreBreakdown = None,  # Optional EdgeScore from macro intelligence layer
    asset_class: str = None,  # "forex" | "crypto" | "nse" | "index" (for EdgeScore routing)
    offline: bool = False,
    news_sentiment: dict | None = None,
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
        - edge_score: EdgeScoreBreakdown from macro intelligence layer
        - asset_class: Asset classification for EdgeScore routing

    Added columns:
        - signal:            "LONG" | "SHORT" | "SKIP" | "HOLD"
        - stop_loss:         ATR-based SL price
        - target:            ATR-based target price
        - confidence:        composite confidence score (0-100)
        - candle_quality_penalty: confidence points deducted for weak candle structure
        - mtf_agreement_votes: 0–4 alignment stack (HTF/EMA/MACD[/VWAP]) or mapped from mtf_score
        - mtf_vote_confidence_delta: net confidence change from MTF vote / weak-score logic
        - quality_score:     Signal quality score (0-100)
        - quality_grade:     "A" | "B" | "C" | "D"
        - quality_breakdown: JSON string with per-component scores
        - edge_score:        EdgeScore from macro intelligence layer (-100 to +100)
        - edge_macro:        Macro component score
        - edge_cot:          COT component score
        - edge_sentiment:    Sentiment component score
        - edge_risk:         Risk component score
        - edge_seasonal:     Seasonal bonus
        - edge_carry:        Carry bonus

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
    if "htf_trend" not in result.columns:
        result["htf_trend"] = TrendDirection.NEUTRAL.value

    # ── Edge Engine Integration ──────────────────────────────────────────────
    # Fetch EdgeScore from macro intelligence layer if not provided
    edge_breakdown = edge_score
    if edge_breakdown is None and symbol and asset_class:
        try:
            edge_engine = _get_edge_engine()
            edge_breakdown = edge_engine.get_edge_score(symbol, asset_class)
            log.info(
                f"EdgeScore for {symbol}: {edge_breakdown.edge_score:.1f} "
                f"(macro: {edge_breakdown.macro_score:.1f}, cot: {edge_breakdown.cot_score:.1f}, "
                f"sentiment: {edge_breakdown.sentiment_score:.1f}, risk: {edge_breakdown.risk_score:.1f})"
            )
        except Exception as e:
            log.warning(f"Failed to fetch EdgeScore for {symbol}: {e}")
            edge_breakdown = None

    # Store EdgeScore components in result DataFrame (will be added to all rows later)
    _edge_score_value = edge_breakdown.edge_score if edge_breakdown else 0.0
    _edge_macro = edge_breakdown.macro_score if edge_breakdown else 0.0
    _edge_cot = edge_breakdown.cot_score if edge_breakdown else 0.0
    _edge_sentiment = edge_breakdown.sentiment_score if edge_breakdown else 0.0
    _edge_risk = edge_breakdown.risk_score if edge_breakdown else 0.0
    _edge_seasonal = edge_breakdown.seasonal_bonus if edge_breakdown else 0.0
    _edge_carry = edge_breakdown.carry_bonus if edge_breakdown else 0.0

    # ── Self-Learning Weights ────────────────────────
    try:
        from agents.analytics.feedback_loop import WeightAdapter
        _w = WeightAdapter()._load()
    except Exception:
        _w = {}
    
    w_rsi  = _w.get("RSI_BOUNCE", 1.0)
    w_macd = _w.get("MACD_CROSS", 1.0)
    w_ema  = _w.get("EMA_CROSS", 1.0)
    w_vol  = _w.get("VOLUME_SPIKE", 1.0)
    w_div  = _w.get("DIVERGENCE", 1.0)

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
        is_lightweight_mode = os.getenv("LIGHTWEIGHT_API_MODE", "false").lower() in ("1", "true", "yes")
        if not is_lightweight_mode:
            try:
                from agents.ai_engine.ensemble import predict_batch_ensemble
                ensemble_conf = predict_batch_ensemble(result, symbol=symbol)
                result["ai_confidence"] = ensemble_conf.values
                ensemble_ok = True
                log.debug("ai_confidence sourced from advanced ensemble (XGBoost + Statistical + Sentiment)")
            except Exception as _ens_err:
                log.debug("Ensemble confidence unavailable (%s) — using composite fallback", _ens_err)

        # ── Fallback 4: directional composite (RSI + MACD + EMA + HTF) ───
        if not ensemble_ok:
            rsi_col   = result["RSI_14"].fillna(50)
            macd_col  = result["MACD_hist"].fillna(0)
            close_col = result["close"]
            ema20_col = result["EMA_20"].fillna(close_col)
            ema50_col = result["EMA_50"].fillna(close_col)

            # Directional alignment: centered at 50 (>50 bull, <50 bear)
            ema_align_bull = (close_col > ema20_col) & (ema20_col > ema50_col)
            ema_align_bear = (close_col < ema20_col) & (ema20_col < ema50_col)

            bull_pts = (
                ((rsi_col - 50).clip(lower=0) / 50.0 * 18.0)
                + ((macd_col > 0).astype(float) * 12.0)
                + (ema_align_bull.astype(float) * 10.0)
            )
            bear_pts = (
                ((50 - rsi_col).clip(lower=0) / 50.0 * 18.0)
                + ((macd_col < 0).astype(float) * 12.0)
                + (ema_align_bear.astype(float) * 10.0)
            )

            if "htf_trend" in result.columns:
                htf_bull_pts = (result["htf_trend"] == TrendDirection.BULLISH.value).astype(float) * 10.0
                htf_bear_pts = (result["htf_trend"] == TrendDirection.BEARISH.value).astype(float) * 10.0
                bull_pts += htf_bull_pts
                bear_pts += htf_bear_pts

            composite = (50.0 + (bull_pts - bear_pts)).clip(10.0, 90.0)
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
    # The regime table stays authoritative where defined; the operator-tunable
    # AI_CONFIDENCE_THRESHOLD is the live fallback for unlisted regimes.
    from agents.execution.runtime_controls import effective_int

    conf_threshold = settings.REGIME_CONFIDENCE_THRESHOLDS.get(
        regime_key,
        effective_int("AI_CONFIDENCE_THRESHOLD", int(settings.AI_CONFIDENCE_THRESHOLD)),
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
    # Direction-aware AI confidence scaling: >50=bullish (+0 to +8 pts), <50=bearish (-0 to -8 pts)
    ai_directional_delta = ((result["ai_confidence"] - 50.0).clip(-45.0, 45.0) / 45.0 * 8.0)

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

    # ── Volume filter: 1.5x spike bonus; 1.2x basic entry filter ─────────────
    # Real breakouts have above-average volume. Low volume = fake signal.
    # volume_ok  (1.2x): basic gate — filters dead-volume noise
    # volume_spike (1.5x): strong confirmation — adds score + confidence bonus
    volume_ok    = pd.Series(True,  index=result.index)
    volume_spike = pd.Series(False, index=result.index)
    if "volume" in result.columns and result["volume"].notna().any():
        vol_avg = result["volume"].rolling(20, min_periods=5).mean()
        volume_ok    = (result["volume"] > vol_avg * 1.2).fillna(True)
        volume_spike = (result["volume"] > vol_avg * settings.VOLUME_SPIKE_MULTIPLIER).fillna(False)


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
        + (rsi_buy.astype(float) * 15 * w_rsi)
        + (deep_oversold.astype(float) * 8 * w_rsi)
        # EMA crossover (max 12) — ACTIVE trigger (price crossing EMA)
        + ((ema_buy | ema20_buy).astype(float) * 12 * w_ema)
        # MACD momentum shift (max 8) — ACTIVE trigger (histogram improving)
        + (macd_turning_bull.astype(float) * 8 * w_macd)
        # Volume spike confirmation (max 5 base + 15 spike bonus) — ACTIVE (above-avg volume)
        + (volume_ok.astype(float) * 5 * w_vol)
        + (volume_spike.astype(float) * settings.VOLUME_SPIKE_SCORE_BONUS * w_vol)
        # StochRSI crossover (max 5) — ACTIVE trigger
        + stoch_buy.astype(float) * 5
        # Hidden bullish divergence (max 8) — ACTIVE (trend continuation)
        + (hidden_bullish.astype(float) * 8 * w_div)
        # AI confidence — direction-aware soft calibration (+8 for bull, -8 for bear)
        + ai_directional_delta
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
        + (rsi_sell.astype(float) * 15 * w_rsi)
        + (deep_overbought.astype(float) * 8 * w_rsi)
        # EMA crossover (max 12) — ACTIVE trigger
        + ((ema_sell | ema20_sell).astype(float) * 12 * w_ema)
        # MACD momentum shift (max 8) — ACTIVE trigger (histogram deteriorating)
        + (macd_turning_bear.astype(float) * 8 * w_macd)
        # Volume spike confirmation (max 5 base + 15 spike bonus) — ACTIVE
        + (volume_ok.astype(float) * 5 * w_vol)
        + (volume_spike.astype(float) * settings.VOLUME_SPIKE_SCORE_BONUS * w_vol)
        # StochRSI crossover (max 5) — ACTIVE trigger
        + stoch_sell.astype(float) * 5
        # Hidden bearish divergence (max 8) — ACTIVE (trend continuation)
        + (hidden_bearish.astype(float) * 8 * w_div)
        # AI confidence — direction-aware soft calibration (+8 for bear, -8 for bull)
        - ai_directional_delta
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

    # ── Tradeflow sentiment multiplier (optional, A/B-testable) ────────────
    tradeflow_ctx = {} if (offline or not symbol) else _get_tradeflow_signal_context()
    buy_mult = float(tradeflow_ctx.get("buy_multiplier", 1.0) or 1.0)
    sell_mult = float(tradeflow_ctx.get("sell_multiplier", 1.0) or 1.0)
    buy_score = buy_score * buy_mult
    sell_score = sell_score * sell_mult

    result["tradeflow_sentiment"] = tradeflow_ctx.get("sentiment", "UNAVAILABLE")
    result["tradeflow_applied"] = bool(tradeflow_ctx.get("applied", False))
    result["tradeflow_multiplier_buy"] = round(buy_mult, 4)
    result["tradeflow_multiplier_sell"] = round(sell_mult, 4)
    result["tradeflow_true_intent"] = tradeflow_ctx.get("true_intent", "UNAVAILABLE")
    result["tradeflow_true_intent_score"] = float(tradeflow_ctx.get("true_intent_score", 0.0) or 0.0)
    result["tradeflow_dii_context"] = tradeflow_ctx.get("dii_context", "UNAVAILABLE")
    result["tradeflow_source_stale"] = bool(tradeflow_ctx.get("source_stale", True))

    # ── Regime-adaptive scoring threshold ────────────────────────────────────
    signal_threshold = settings.SIGNAL_SCORE_THRESHOLDS.get(
        regime_key, settings.SIGNAL_SCORE_DEFAULT
    )
    # AI confidence is now used as a SOFT BONUS in the score, not a hard gate.
    # The old hard gate (buy_score >= threshold) & ai_pass was double-gatekeeping
    # and blocking most signals in ranging markets with naturally low confidence.
    buy_condition = buy_score >= signal_threshold
    sell_condition = sell_score >= signal_threshold

    # ── Accuracy gates: confluence + trend + volume + MTF alignment ─────────
    div_buy = hidden_bullish | bullish_divergence
    div_sell = hidden_bearish | bearish_divergence

    confluence_buy_votes = (
        rsi_buy.astype(int)
        + ema_align_buy.astype(int)
        + macd_turning_bull.astype(int)
        + volume_ok.astype(int)
        + div_buy.astype(int)
    )
    confluence_sell_votes = (
        rsi_sell.astype(int)
        + ema_align_sell.astype(int)
        + macd_turning_bear.astype(int)
        + volume_ok.astype(int)
        + div_sell.astype(int)
    )

    confluence_min_votes = max(1, int(getattr(settings, "SIGNAL_CONFLUENCE_MIN_VOTES", 3)))
    confluence_buy_ok = confluence_buy_votes >= confluence_min_votes
    confluence_sell_ok = confluence_sell_votes >= confluence_min_votes

    ema200_long_ok = pd.Series(True, index=result.index)
    ema200_short_ok = pd.Series(True, index=result.index)
    if (
        getattr(settings, "SIGNAL_EMA200_HARD_FILTER_ENABLED", True)
        and "EMA_200" in result.columns
        and result["EMA_200"].notna().any()
    ):
        ema200_long_ok = result["close"] >= result["EMA_200"]
        ema200_short_ok = result["close"] <= result["EMA_200"]

    volume_gate_buy_ok = volume_ok.copy()
    volume_gate_sell_ok = volume_ok.copy()
    if not getattr(settings, "SIGNAL_REQUIRE_VOLUME_GATE", True):
        volume_gate_buy_ok = pd.Series(True, index=result.index)
        volume_gate_sell_ok = pd.Series(True, index=result.index)

    mtf_buy_votes = htf_bull.astype(int) + ema_align_buy.astype(int) + macd_bull.astype(int)
    mtf_sell_votes = htf_bear.astype(int) + ema_align_sell.astype(int) + macd_bear.astype(int)
    if "VWAP" in result.columns and result["VWAP"].notna().any():
        mtf_buy_votes = mtf_buy_votes + vwap_bull.astype(int)
        mtf_sell_votes = mtf_sell_votes + vwap_bear.astype(int)
    mtf_signal_min_votes = max(1, int(getattr(settings, "MTF_SIGNAL_MIN_VOTES", 2)))
    mtf_signal_gate_buy_ok = mtf_buy_votes >= mtf_signal_min_votes
    mtf_signal_gate_sell_ok = mtf_sell_votes >= mtf_signal_min_votes

    if getattr(settings, "SIGNAL_CONFLUENCE_GATE_ENABLED", True):
        buy_condition = buy_condition & confluence_buy_ok
        sell_condition = sell_condition & confluence_sell_ok

    buy_condition = buy_condition & ema200_long_ok & volume_gate_buy_ok
    sell_condition = sell_condition & ema200_short_ok & volume_gate_sell_ok

    if getattr(settings, "MTF_SIGNAL_GATE_ENABLED", True):
        buy_condition = buy_condition & mtf_signal_gate_buy_ok
        sell_condition = sell_condition & mtf_signal_gate_sell_ok

    # ── News Sentiment Advisory Gate ──────────────────────────────────────────
    sentiment_tone = 0.0
    sentiment_label = "NEUTRAL"
    sentiment_buy_ok = True
    sentiment_sell_ok = True

    if news_sentiment and getattr(settings, "SENTIMENT_GATE_ENABLED", True):
        try:
            sentiment_tone = float(news_sentiment.get("tone_score", 0.0) or 0.0)
            sentiment_label = str(news_sentiment.get("sentiment", "NEUTRAL") or "NEUTRAL").upper()
            if sentiment_label == "BEARISH" and sentiment_tone <= -4.0:
                sentiment_buy_ok = False
                log.info("[SentimentGate] Blocked LONG for %s: tone=%.1f (%s)", symbol, sentiment_tone, sentiment_label)
            elif sentiment_label == "BULLISH" and sentiment_tone >= 4.0:
                sentiment_sell_ok = False
                log.info("[SentimentGate] Blocked SHORT for %s: tone=%.1f (%s)", symbol, sentiment_tone, sentiment_label)
        except Exception as sent_err:
            log.debug("[SentimentGate] Parse error for %s: %s", symbol, sent_err)

    if getattr(settings, "SENTIMENT_GATE_ENABLED", True):
        buy_condition = buy_condition & sentiment_buy_ok
        sell_condition = sell_condition & sentiment_sell_ok

    # Store scores for debugging and dashboard display
    result["signal_score_buy"] = buy_score.round(1)
    result["signal_score_sell"] = sell_score.round(1)
    result["confluence_votes_buy"] = confluence_buy_votes
    result["confluence_votes_sell"] = confluence_sell_votes
    result["confluence_gate_ok"] = (confluence_buy_ok | confluence_sell_ok)
    result["sentiment_tone"] = sentiment_tone
    result["sentiment_label"] = sentiment_label
    result["sentiment_gate_ok"] = np.where(buy_condition, sentiment_buy_ok, np.where(sell_condition, sentiment_sell_ok, True))
    result["ema200_gate_ok"] = np.where(
        buy_condition,
        ema200_long_ok,
        np.where(sell_condition, ema200_short_ok, ema200_long_ok | ema200_short_ok),
    )
    result["volume_gate_ok"] = np.where(
        buy_condition,
        volume_gate_buy_ok,
        np.where(sell_condition, volume_gate_sell_ok, volume_gate_buy_ok | volume_gate_sell_ok),
    )

    timeframe_alignment_score = np.where(
        buy_condition,
        mtf_buy_votes,
        np.where(sell_condition, mtf_sell_votes, np.maximum(mtf_buy_votes, mtf_sell_votes)),
    )
    result["timeframe_alignment_score"] = pd.Series(timeframe_alignment_score, index=result.index).clip(0, 3).astype(int)
    result["auto_exec_eligible_mtf"] = result["timeframe_alignment_score"] >= mtf_signal_min_votes
    high_conv_votes = max(mtf_signal_min_votes, int(getattr(settings, "MTF_HIGH_CONVICTION_VOTES", 3)))
    result["high_conviction_mtf"] = result["timeframe_alignment_score"] >= high_conv_votes

    # ── Assign raw directional signals (pre-quality-filter) ──────────────────
    result["_raw_signal"] = "HOLD"
    result.loc[buy_condition,  "_raw_signal"] = "LONG"
    result.loc[sell_condition, "_raw_signal"] = "SHORT"

    # ── Quality scoring for every LONG/SHORT candidate ────────────────────────
    try:
        from agents.signal_engine.quality_scorer import score_signals_vectorised, MIN_CONFIDENCE_FOR_SIGNAL
        # Directional AI confidence: >50 is bullish, <50 is bearish
        # For SHORT candidates, convert to directional strength (100 - conf)
        directional_conf_series = result["ai_confidence"].copy()
        raw_short_mask = result["_raw_signal"] == "SHORT"
        directional_conf_series[raw_short_mask] = (100.0 - directional_conf_series[raw_short_mask]).clip(0, 100)

        quality_df = score_signals_vectorised(
            result, result["_raw_signal"], regime, symbol,
            confidences=directional_conf_series,
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

    # ── Conflict Guardrails (post-score, pre-final-signal) ───────────────────
    from agents.signal_engine.quality_scorer import detect_signal_conflicts

    # Store directional confidence (0-100) aligned with signal direction
    directional_conf_series = result["ai_confidence"].copy()
    raw_short_mask = result["_raw_signal"] == "SHORT"
    directional_conf_series[raw_short_mask] = (100.0 - directional_conf_series[raw_short_mask]).clip(0, 100)
    result["confidence"] = directional_conf_series.clip(0, 100).round(1)

    conflict_payloads = []
    conflict_severity = []
    conflict_joined_reasons = []
    conflict_penalty = []

    for _, row in result.iterrows():
        direction = str(row.get("_raw_signal", "HOLD") or "HOLD").upper()
        if direction not in ("LONG", "SHORT"):
            conflict = {
                "has_conflict": False,
                "severity": "none",
                "reasons": [],
                "confidence_penalty": 0,
            }
        else:
            trend_val = str(row.get("htf_trend", "") or "")
            if "BULL" in trend_val.upper():
                trend_val = "BULLISH"
            elif "BEAR" in trend_val.upper():
                trend_val = "BEARISH"
            else:
                trend_val = "NEUTRAL"
            conflict = detect_signal_conflicts(
                direction=direction,
                rsi=row.get("RSI_14"),
                trend=trend_val,
                macd=row.get("MACD_hist"),
            )

        reasons = conflict.get("reasons", [])
        joined = " | ".join(reasons) if reasons else ""

        conflict_payloads.append(conflict)
        conflict_severity.append(conflict.get("severity", "none"))
        conflict_joined_reasons.append(joined)
        conflict_penalty.append(int(conflict.get("confidence_penalty", 0) or 0))

    result["conflict"] = conflict_payloads
    result["conflict_severity"] = conflict_severity
    result["signal_conflict"] = conflict_joined_reasons
    result["_conflict_penalty"] = pd.Series(conflict_penalty, index=result.index).astype(float)

    candidate_mask = result["_raw_signal"].isin(["LONG", "SHORT"])
    result.loc[candidate_mask, "confidence"] = (
        result.loc[candidate_mask, "confidence"] - result.loc[candidate_mask, "_conflict_penalty"]
    ).clip(lower=10)

    conflict_block_mask = candidate_mask & (result["conflict_severity"] == "block")
    if conflict_block_mask.any():
        block_reason = result.loc[conflict_block_mask, "signal_conflict"].fillna("")
        existing_gate = result.loc[conflict_block_mask, "quality_hard_gate"].fillna("")
        result.loc[conflict_block_mask, "_raw_signal"] = "HOLD"
        result.loc[conflict_block_mask, "quality_hard_gate"] = np.where(
            existing_gate == "",
            block_reason,
            existing_gate,
        )
        result.loc[conflict_block_mask, "skip_reason"] = block_reason

    if "skip_reason" not in result.columns:
        result["skip_reason"] = ""
    result["skip_reason"] = result["skip_reason"].fillna("")

    # ── Apply quality gate: LONG/SHORT or SKIP ────────────────────────────────
    # LONG/SHORT → quality grade A or B (score >= quality_threshold) AND hard gates passed
    # SKIP       → conditions met but grade C/D, OR hard gate failed (low confidence, VWAP)
    # HOLD       → no signal conditions met at all
    quality_ok = result["quality_score"] >= quality_threshold
    gate_passed = result["quality_hard_gate"].fillna("") == ""

    # High-QS SKIP tightening: strong setups should still show unless conflict is blocked.
    high_qs_force = (
        ((result["quality_score"] >= 85) & (result["conflict_severity"] != "block"))
        | (
            (result["quality_score"] >= 75)
            & (result["confidence"] >= 80)
            & (result["conflict_severity"] != "block")
        )
    )
    result["force_show"] = high_qs_force

    result["signal"] = "HOLD"
    # Signals that passed quality AND hard gates → LONG/SHORT
    result.loc[
        (result["_raw_signal"] == "LONG")  & ((quality_ok & gate_passed) | high_qs_force), "signal"
    ] = "LONG"
    result.loc[
        (result["_raw_signal"] == "SHORT") & ((quality_ok & gate_passed) | high_qs_force), "signal"
    ] = "SHORT"
    # Signals that failed either quality score OR hard gate → SKIP
    result.loc[
        (result["_raw_signal"] == "LONG")  & (~((quality_ok & gate_passed) | high_qs_force)), "signal"
    ] = "SKIP"
    result.loc[
        (result["_raw_signal"] == "SHORT") & (~((quality_ok & gate_passed) | high_qs_force)), "signal"
    ] = "SKIP"

    # Preserve underlying direction for SKIP rows (needed for correct SL/Target)
    result["_raw_direction"] = result["_raw_signal"]
    result = result.drop(columns=["_raw_signal"])

    # ── Stop Loss & Target (ATR-based) ────────────────────────────────────────
    atr = result[f"ATR_{settings.ATR_PERIOD}"]
    sl_distance = atr * atr_sl_mult
    tp_distance = sl_distance * settings.MIN_RISK_REWARD  # Enforce min 1:2 RR

    result["stop_loss"] = np.nan
    result["target"]    = np.nan

    # Keep confidence semantics direct: higher base confidence means stronger setup.
    # Confidence already includes conflict penalty above.
    result["confidence"] = result["confidence"].clip(0, 100).round(1)

    # ── Confidence Boosters ─────────────────────────────────────────────────────
    # These post-score confidence adjustments reward high-conviction setups
    # that the base composite underweights. Applied after initial confidence
    # so they correctly push borderline signals over the 40% SKIP threshold.

    # (A) Volume Spike: strong trend + 1.5x volume = +20 confidence pts
    #     Rationale: real institutional moves ALWAYS come with volume
    conf_boost = pd.Series(0.0, index=result.index)
    conf_boost += volume_spike.astype(float) * settings.VOLUME_SPIKE_CONF_BONUS

    # (B) RSI Divergence: bullish divergence on LONG bars, bearish on SHORT bars
    #     +15 pts — confirms the signal type and adds predictive edge
    #     (Divergence already blocks *opposing* signals; here we reward *aligned* ones)
    if "_raw_direction" in result.columns:
        div_aligned_long  = bullish_divergence & (result["_raw_direction"] == "LONG")
        div_aligned_short = bearish_divergence & (result["_raw_direction"] == "SHORT")
        conf_boost += (div_aligned_long | div_aligned_short).astype(float) * settings.RSI_DIVERGENCE_CONF_BONUS
    else:
        # Before _raw_direction is available, apply divergence boost to candidate buy/sell bars
        div_aligned_long  = bullish_divergence & buy_condition
        div_aligned_short = bearish_divergence & sell_condition
        conf_boost += (div_aligned_long | div_aligned_short).astype(float) * settings.RSI_DIVERGENCE_CONF_BONUS

    # (C) S/R Level Test: price within 0.5% of key support (LONG) or resistance (SHORT)
    #     +10 pts — confirms the price is at a meaningful structural level
    if "support" in result.columns and "resistance" in result.columns:
        close_px = result["close"]
        sup = result["support"].fillna(0)
        res = result["resistance"].fillna(float("inf"))
        near_support    = (sup > 0) & ((close_px - sup) / sup.replace(0, np.nan) * 100 <= 0.5)
        near_resistance = (res > 0) & (res < float("inf")) & ((res - close_px) / res.replace(0, np.nan) * 100 <= 0.5)
        sr_test_buy  = near_support  & buy_condition
        sr_test_sell = near_resistance & sell_condition
        conf_boost += (sr_test_buy | sr_test_sell).astype(float) * settings.SR_TEST_CONF_BONUS

    # (D) MTF Alignment: 15m + 1h + 1d all agree on direction → +15 pts
    #     If mtf_score is provided (full MTF pipeline), convert it to a bonus;
    #     if not, check if the HTF trend strongly agrees (htf_bull/bear is a proxy).
    if mtf_score is not None:
        # mtf_score is -100 to +100; map extremes to full bonus
        mtf_alignment_bonus = (abs(mtf_score) / 100.0 * settings.MTF_ALIGNMENT_CONF_BONUS)
        result["confidence"] = (result["confidence"] + mtf_alignment_bonus).clip(0, 100)
    else:
        # HTF strongly aligned + EMA alignment + MACD direction = proxy for MTF agreement
        strong_mtf_bull = htf_bull & ema_align_buy  & macd_bull
        strong_mtf_bear = htf_bear & ema_align_sell & macd_bear
        mtf_proxy_bonus = ((strong_mtf_bull & buy_condition) | (strong_mtf_bear & sell_condition))
        conf_boost += mtf_proxy_bonus.astype(float) * settings.MTF_ALIGNMENT_CONF_BONUS

    # Apply all boosts (cap at 100)
    result["confidence"] = (result["confidence"] + conf_boost).clip(0, 100).round(1)

    # ── Edge Engine Confidence Modifier ───────────────────────────────────────
    # Apply EdgeScore-based confidence adjustment AFTER all other boosts
    # Strong bullish edge (+70) on LONG: +15% confidence
    # Strong bearish edge (-70) on SHORT: +15% confidence
    # Conflicting edge: -20% confidence
    edge_conf_modifier = pd.Series(1.0, index=result.index)  # Default: no change (1.0x)
    edge_adjusted = pd.Series(False, index=result.index)

    if edge_breakdown and "_raw_direction" in result.columns:
        edge_val = _edge_score_value
        
        # Strong bullish edge (≥70) on LONG signals
        strong_bullish_edge = (edge_val >= 70) & (result["_raw_direction"] == "LONG")
        edge_conf_modifier = edge_conf_modifier.where(~strong_bullish_edge, 1.15)
        edge_adjusted = edge_adjusted | strong_bullish_edge
        
        # Strong bearish edge (≤-70) on SHORT signals
        strong_bearish_edge = (edge_val <= -70) & (result["_raw_direction"] == "SHORT")
        edge_conf_modifier = edge_conf_modifier.where(~strong_bearish_edge, 1.15)
        edge_adjusted = edge_adjusted | strong_bearish_edge
        
        # Conflicting edge: bullish edge on SHORT or bearish edge on LONG
        conflicting_long = (edge_val <= -30) & (result["_raw_direction"] == "LONG")
        conflicting_short = (edge_val >= 30) & (result["_raw_direction"] == "SHORT")
        conflicting_edge = conflicting_long | conflicting_short
        edge_conf_modifier = edge_conf_modifier.where(~conflicting_edge, 0.80)
        edge_adjusted = edge_adjusted | conflicting_edge
        
        # Apply modifier
        result["confidence"] = (result["confidence"] * edge_conf_modifier).clip(0, 100).round(1)
        
        # Log EdgeScore adjustments
        if edge_adjusted.any():
            log.info(
                f"EdgeScore confidence adjustments for {symbol}: "
                f"{strong_bullish_edge.sum()} bullish boosts, "
                f"{strong_bearish_edge.sum()} bearish boosts, "
                f"{conflicting_edge.sum()} conflict penalties"
            )

    result["edge_adjusted"] = edge_adjusted

    # (E) Candle quality + MTF alignment vote-stack — penalize indecision / weak stack
    candle_pen = _candle_quality_confidence_penalty(result)
    result["candle_quality_penalty"] = candle_pen.round(1)
    vote_series, mtf_adj = _mtf_agreement_votes_and_penalty(
        result,
        htf_bull,
        htf_bear,
        ema_align_buy,
        ema_align_sell,
        macd_bull,
        macd_bear,
        vwap_bull,
        vwap_bear,
        mtf_score,
    )
    result["mtf_agreement_votes"] = vote_series
    result["mtf_vote_confidence_delta"] = mtf_adj.round(1)
    result["confidence"] = (result["confidence"] + candle_pen + mtf_adj).clip(0, 100).round(1)

    # Floor: LONG/SHORT signals always show at least 25% on dashboard
    long_or_short = result["signal"].isin(["LONG", "SHORT"])
    result.loc[long_or_short & (result["confidence"] < 25), "confidence"] = 25.0
    result["tradeflow_multiplier"] = np.where(
        result["signal"] == "LONG",
        round(buy_mult, 4),
        np.where(result["signal"] == "SHORT", round(sell_mult, 4), 1.0),
    )

    # ── Context Layer modifier (Step 7 in lifecycle) ───────────────────────
    # Applied after AI confidence and quality gates, before returning signals.
    try:
        if offline or not symbol:
            context_snapshot = None
        else:
            context_fetcher = build_context_fetcher()
            context_snapshot = context_fetcher.get_snapshot_sync(symbol)
    except Exception as ctx_exc:
        log.warning("Context fetch failed for %s: %s", symbol, ctx_exc)
        context_snapshot = None

    context_score = float(getattr(context_snapshot, "context_score", 0.0) or 0.0)
    event_risk_active = bool(getattr(context_snapshot, "event_risk_active", False))
    days_to_next_event = int(getattr(context_snapshot, "days_to_next_event", 99) or 99)
    news_volume_spike = bool(getattr(context_snapshot, "news_volume_spike", False))
    news_sentiment_24hr = float(getattr(context_snapshot, "news_sentiment_24hr", 0.0) or 0.0)
    news_sentiment_7d = float(getattr(context_snapshot, "news_sentiment_7d", 0.0) or 0.0)
    btc_dominance_7d_trend = str(getattr(context_snapshot, "btc_dominance_7d_trend", "FLAT") or "FLAT")

    result["context_score"] = context_score
    result["event_risk_active"] = event_risk_active
    result["days_to_next_event"] = days_to_next_event
    result["news_sentiment_24hr"] = news_sentiment_24hr
    result["news_sentiment_7d"] = news_sentiment_7d
    result["context_adjusted"] = False
    result["context_warning"] = None

    for idx, row in result.iterrows():
        direction = str(row.get("signal", "HOLD") or "HOLD").upper()
        new_conf, adjusted, warning, _ = _apply_context_rules(
            direction=direction,
            confidence=float(row.get("confidence", 0.0) or 0.0),
            context_score=context_score,
            event_risk_active=event_risk_active,
            news_volume_spike=news_volume_spike,
            btc_dominance_7d_trend=btc_dominance_7d_trend,
            symbol=symbol,
        )
        result.at[idx, "confidence"] = new_conf
        result.at[idx, "context_adjusted"] = adjusted
        if warning and direction not in ("SKIP", "HOLD"):
            result.at[idx, "context_warning"] = warning

    # ── Feature Drift Guardrail ───────────────────────────────────────────
    drift_z, drift_detected, drift_feature = _compute_feature_drift(result)
    result["drift_z_score"] = drift_z
    result["drift_detected"] = drift_detected
    result["drift_feature"] = drift_feature
    result["drift_adjusted"] = False

    if drift_detected:
        penalty_factor = max(0.0, 1.0 - float(settings.DRIFT_CONFIDENCE_PENALTY))
        directional_mask = result["signal"].isin(["LONG", "SHORT"])
        result.loc[directional_mask, "confidence"] = (
            result.loc[directional_mask, "confidence"] * penalty_factor
        ).clip(0, 100).round(1)
        result.loc[directional_mask, "drift_adjusted"] = True

        warning_mask = directional_mask & result["context_warning"].isna()
        result.loc[warning_mask, "context_warning"] = f"FEATURE_DRIFT({drift_feature})"

    # Cleanup helper column used only during conflict penalty application.
    if "_conflict_penalty" in result.columns:
        result = result.drop(columns=["_conflict_penalty"])

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
            "Latest: %s (conf=%.1f%%, QS=%d/%s, RSI=%.1f, Regime=%s, Drift=%.2f/%s%s)",
            long_mask.sum(), short_mask.sum(), skip_count, hold_count,
            ai_rejected, gate_blocked, conf_threshold, quality_threshold,
            last.get("signal", "HOLD"),
            last.get("ai_confidence", 0),
            last.get("quality_score", 0),
            last.get("quality_grade", "?"),
            last.get("RSI_14", 0) if pd.notna(last.get("RSI_14")) else 0,
            regime.value if regime else "N/A",
            drift_z,
            drift_feature or "none",
            f", Gate={gate_reason}" if gate_reason else "",
        )

    # ── Add EdgeScore columns to result DataFrame ─────────────────────────────
    result["edge_score"] = _edge_score_value
    result["edge_macro"] = _edge_macro
    result["edge_cot"] = _edge_cot
    result["edge_sentiment"] = _edge_sentiment
    result["edge_risk"] = _edge_risk
    result["edge_seasonal"] = _edge_seasonal
    result["edge_carry"] = _edge_carry

    return result


def signal_generator_for_backtest(train_df: pd.DataFrame, test_df: pd.DataFrame) -> pd.DataFrame:
    """
    Walk-forward compatible signal generator.

    Uses the training data to establish the strategy parameters (currently fixed),
    then applies signals to the test set.

    This is the callback passed to `run_walk_forward()`.
    """
    return generate_signals(test_df)
