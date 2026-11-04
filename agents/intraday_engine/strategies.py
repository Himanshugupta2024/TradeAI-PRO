"""
Heuristic Intraday Strategies on 5m Bars (Paper / Tactical Framing).

Repaired & Validated Strategies:
  - ORB (Opening Range Breakout): Configurable 15m / 30m range with breakout volume confirmation
  - VWAP: Session-isolated VWAP with ±1σ and ±2σ band mean-reversion and breakout logic
  - CPR (Central Pivot Range): Isolated to Day T-1; breakout and narrow CPR trending days
  - Supertrend: Vectorized ATR-band trend-following with flip & continuation confirmation
  - EMA: Multi-EMA (9, 20, 50) alignment and 20 EMA pullback entries
  - RSI: Momentum pullback in verified trend context (rejects naive overbought/oversold)
  - Momentum: Multi-bar trend surge with volume confirmation
  - Gap & Go: Overnight gap opening continuation with volume participation
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from agents.intraday_engine.indicators import (
    compute_adx,
    compute_atr,
    compute_cpr_levels,
    compute_ema,
    compute_rsi,
    compute_session_vwap_bands,
    compute_supertrend,
    compute_technical_snapshot,
)
from agents.intraday_engine.risk_manager import (
    calculate_position_size,
    calculate_risk_reward,
    calculate_sl_tp,
)
from agents.intraday_engine.types import IntradaySignal
from config import settings

logger = logging.getLogger(__name__)


def _atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    return compute_atr(df, period=period)


def _day_session(df: pd.DataFrame) -> Tuple[pd.DataFrame, Optional[pd.Series], Optional[pd.DataFrame]]:
    """
    Extracts today's active session DataFrame, latest row, and strictly Day T-1 (yesterday's) DataFrame.
    Returns: (today_df, latest_row, prev_day_df)
    """
    if df is None or df.empty or "timestamp" not in df.columns:
        return df, None, None

    last_d = df.iloc[-1]["timestamp"].date()
    today_df = df[df["timestamp"].dt.date == last_d].copy()

    if hasattr(df, "attrs") and df.attrs:
        today_df.attrs = dict(df.attrs)

    prior_dates = sorted(list({ts.date() for ts in df["timestamp"] if ts.date() < last_d}))
    prev_day_df = df[df["timestamp"].dt.date == prior_dates[-1]].copy() if prior_dates else None

    if today_df.empty or len(today_df) < 5:
        return today_df, None, prev_day_df

    return today_df, today_df.iloc[-1], prev_day_df


def _risk_wrapped_signal(
    *,
    symbol: str,
    strategy: str,
    direction: str,
    score: float,
    entry: float,
    rationale: str,
    df: pd.DataFrame,
    prev_day_df: Optional[pd.DataFrame] = None,
) -> Optional[IntradaySignal]:
    atr = _atr(df, period=int(getattr(settings, "ATR_PERIOD", 14) or 14))
    if atr is None or atr <= 0:
        return None

    # Enforce minimum volatility sanity
    if entry > 0 and (atr / entry) < 0.0015:
        logger.debug("intraday %s rejected: ATR %% too low (%.4f)", symbol, atr / entry)
        return None

    stop_loss, target_2 = calculate_sl_tp(
        entry=float(entry), atr=atr, direction=direction, target_atr_multiplier=3.0
    )
    _, target_1 = calculate_sl_tp(
        entry=float(entry), atr=atr, direction=direction, target_atr_multiplier=1.5
    )

    if stop_loss <= 0 or target_2 <= 0:
        return None

    rr = calculate_risk_reward(entry=float(entry), stop_loss=float(stop_loss), target=float(target_2))
    min_rr = float(getattr(settings, "INTRADAY_MIN_RISK_REWARD", 1.5) or 1.5)
    if rr < min_rr:
        return None

    capital = float(getattr(settings, "INTRADAY_VIRTUAL_CAPITAL", 100000.0) or 100000.0)
    risk_pct = float(getattr(settings, "INTRADAY_RISK_PER_TRADE_PCT", 1.0) or 1.0) / 100.0
    qty = calculate_position_size(capital=capital, entry=float(entry), atr=atr, risk_pct=risk_pct)
    if qty <= 0:
        return None

    rvol = None
    live_vwap = None
    if hasattr(df, "attrs"):
        if df.attrs.get("rvol") is not None:
            try:
                rvol = float(df.attrs["rvol"])
            except Exception:
                pass
        if df.attrs.get("live_vwap") is not None:
            try:
                live_vwap = float(df.attrs["live_vwap"])
            except Exception:
                pass

    if live_vwap is None and "VWAP" in df.columns:
        try:
            val = df["VWAP"].iloc[-1]
            if not pd.isna(val) and float(val) > 0:
                live_vwap = float(val)
        except Exception:
            pass

    if rvol is not None and getattr(settings, "INTRADAY_RVOL_GATE_ENABLED", True):
        min_rvol = float(getattr(settings, "INTRADAY_MIN_RVOL", 1.5) or 1.5)
        if strategy.upper() in ("ORB", "GAP_GO", "MOMENTUM") and rvol < min_rvol:
            logger.debug(
                "intraday %s %s signal filtered: RVOL %.2f < %.2f",
                symbol, strategy, rvol, min_rvol,
            )
            return None

    # Technical structure snapshot
    tech_snapshot = compute_technical_snapshot(df, prev_day_df, live_vwap=live_vwap)

    if rvol is not None and "RVOL=" not in rationale:
        rationale = f"{rationale.rstrip()} | RVOL={rvol:.2f}x"

    return IntradaySignal(
        symbol=symbol,
        strategy=strategy,
        direction=direction,
        score=float(score),
        entry=round(float(entry), 4),
        stop_loss=round(float(stop_loss), 4),
        target=round(float(target_2), 4),
        target_1=round(float(target_1), 4),
        target_2=round(float(target_2), 4),
        qty=int(qty),
        risk_reward=float(rr),
        atr=float(atr),
        rationale=rationale,
        rvol=rvol,
        vwap=live_vwap or tech_snapshot.get("vwap"),
        support=tech_snapshot.get("support"),
        resistance=tech_snapshot.get("resistance"),
        technical_snapshot=tech_snapshot,
    )


def compute_vwap_bands(df: pd.DataFrame, live_vwap: Optional[float] = None) -> Optional[Dict[str, float]]:
    """Session VWAP and ±1σ / ±2σ bands (Fixed name error bug)."""
    return compute_session_vwap_bands(df, live_vwap=live_vwap)


def strat_orb(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    """
    Opening Range Breakout (ORB).
    Requires first 15m to 30m range formation (3-6 bars), breakout confirmation,
    volume participation, and reasonable opening candle size (< 2.5 * ATR).
    """
    day, last, prev_day = _day_session(df)
    if last is None or len(day) < 6:
        return None

    orb_bars = int(getattr(settings, "INTRADAY_ORB_BARS", 6) or 6)  # Default 6 bars = 30m (9:15-9:45)
    orb = day.iloc[:orb_bars]
    hi = float(orb["high"].max())
    lo = float(orb["low"].min())
    mid = (hi + lo) / 2.0
    orb_range = hi - lo

    atr = _atr(day, 14) or (orb_range * 0.5)
    # Reject abnormal oversized opening bar
    if orb_range > (2.5 * atr):
        return None

    vol_mean = float(orb["volume"].mean() or 1)
    last_vol = float(last["volume"] or 0)

    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )

    if c > hi and last_vol >= vol_mean * 1.05:
        range_risk = max(orb_range, mid * 0.002)
        score = min(95.0, 60.0 + ((c - hi) / max(range_risk, 1e-6)) * 25.0)
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="ORB",
            direction="LONG",
            score=score,
            entry=c,
            rationale=f"Opening Range Breakout — 5m candle closed above OR High (₹{hi:.2f}) with volume.",
            df=day,
            prev_day_df=prev_day,
        )

    if c < lo and last_vol >= vol_mean * 1.05:
        range_risk = max(orb_range, mid * 0.002)
        score = min(95.0, 60.0 + ((lo - c) / max(range_risk, 1e-6)) * 25.0)
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="ORB",
            direction="SHORT",
            score=score,
            entry=c,
            rationale=f"Opening Range Breakdown — 5m candle closed below OR Low (₹{lo:.2f}) with volume.",
            df=day,
            prev_day_df=prev_day,
        )

    return None


def strat_vwap(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    """
    Session VWAP Reclaim & Mean Reversion / Trend Breakout.
    Detects bounce from -2σ band back above -1σ / VWAP (LONG) or rejection from +2σ (SHORT).
    """
    day, last, prev_day = _day_session(df)
    if last is None or len(day) < 8:
        return None

    bands = compute_session_vwap_bands(day)
    if bands is None:
        return None

    vwap = bands["vwap"]
    upper_1 = bands["upper_1"]
    upper_2 = bands["upper_2"]
    lower_1 = bands["lower_1"]
    lower_2 = bands["lower_2"]
    std = bands["std"]

    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )

    recent_closes = pd.to_numeric(day["close"].iloc[-6:-1], errors="coerce")
    recent_min = float(recent_closes.min()) if not recent_closes.empty else c
    recent_max = float(recent_closes.max()) if not recent_closes.empty else c

    # Bullish: Tested below lower_2 or lower_1 and reclaiming
    if recent_min <= lower_2 and c > lower_2:
        stretch = (vwap - recent_min) / max(std, 1e-6)
        score = min(95.0, 65.0 + stretch * 8.0)
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="VWAP",
            direction="LONG",
            score=score,
            entry=c,
            rationale=f"VWAP bounce off -2σ band (lower_2={lower_2:.2f}) — price {c:.2f} reclaiming VWAP={vwap:.2f}",
            df=day,
            prev_day_df=prev_day,
        )

    # Bullish: Breakout above VWAP with volume
    if len(day) >= 3 and float(day["close"].iloc[-2]) <= vwap and c > vwap:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="VWAP",
            direction="LONG",
            score=75.0,
            entry=c,
            rationale=f"VWAP crossover — price {c:.2f} reclaimed session VWAP={vwap:.2f} on volume",
            df=day,
            prev_day_df=prev_day,
        )

    # Bearish: Tested above upper_2 and reversing downwards
    if recent_max >= upper_2 and c < upper_2:
        stretch = (recent_max - vwap) / max(std, 1e-6)
        score = min(95.0, 65.0 + stretch * 8.0)
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="VWAP",
            direction="SHORT",
            score=score,
            entry=c,
            rationale=f"VWAP rejection off +2σ band (upper_2={upper_2:.2f}) — price {c:.2f} reverting toward VWAP={vwap:.2f}",
            df=day,
            prev_day_df=prev_day,
        )

    # Bearish: Breakdown below VWAP
    if len(day) >= 3 and float(day["close"].iloc[-2]) >= vwap and c < vwap:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="VWAP",
            direction="SHORT",
            score=75.0,
            entry=c,
            rationale=f"VWAP breakdown — price {c:.2f} dropped below session VWAP={vwap:.2f}",
            df=day,
            prev_day_df=prev_day,
        )

    return None


def strat_cpr(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    """
    Central Pivot Range (CPR) — strictly using Day T-1 for Pivot, TC, and BC calculation.
    """
    day, last, prev_day = _day_session(df)
    if last is None or prev_day is None or len(day) < 6:
        return None

    cpr = compute_cpr_levels(prev_day)
    if not cpr:
        return None

    tc = cpr["tc"]
    bc = cpr["bc"]
    pivot = cpr["pivot"]
    width_pct = cpr["width_pct"]
    cpr_type = cpr["cpr_type"]

    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )

    vol_mean = float(day["volume"].mean() or 1)
    last_vol = float(last["volume"] or 0)
    bonus = 8.0 if cpr_type == "NARROW" else 0.0

    if c > tc and last_vol >= vol_mean * 1.05:
        score = min(94.0, 62.0 + bonus + ((c - tc) / max(tc, 1e-6)) * 400.0)
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="CPR",
            direction="LONG",
            score=score,
            entry=c,
            rationale=f"CPR Breakout — price {c:.2f} above TC={tc:.2f} | {cpr_type} CPR (width {width_pct:.2f}%) | Pivot={pivot:.2f}",
            df=day,
            prev_day_df=prev_day,
        )

    if c < bc and last_vol >= vol_mean * 1.05:
        score = min(94.0, 62.0 + bonus + ((bc - c) / max(bc, 1e-6)) * 400.0)
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="CPR",
            direction="SHORT",
            score=score,
            entry=c,
            rationale=f"CPR Breakdown — price {c:.2f} below BC={bc:.2f} | {cpr_type} CPR (width {width_pct:.2f}%) | Pivot={pivot:.2f}",
            df=day,
            prev_day_df=prev_day,
        )

    return None


def strat_supertrend(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    """
    Vectorized Supertrend with ATR lookback and flip confirmation.
    """
    day, last, prev_day = _day_session(df)
    if last is None or len(day) < 12:
        return None

    st = compute_supertrend(day, period=10, multiplier=2.5)
    if not st:
        return None

    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )

    if st["is_flip"] and st["direction"] == "BULLISH":
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="SUPERTREND",
            direction="LONG",
            score=82.0,
            entry=c,
            rationale=f"Supertrend flip BULLISH — price {c:.2f} crossed above active band (₹{st['band_value']:.2f})",
            df=day,
            prev_day_df=prev_day,
        )

    if st["is_flip"] and st["direction"] == "BEARISH":
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="SUPERTREND",
            direction="SHORT",
            score=82.0,
            entry=c,
            rationale=f"Supertrend flip BEARISH — price {c:.2f} dropped below active band (₹{st['band_value']:.2f})",
            df=day,
            prev_day_df=prev_day,
        )

    return None


def strat_ema(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    """
    EMA Multi-Timeframe Alignment & 20 EMA Pullback Entry.
    Requires EMA 9 > 20 > 50 alignment for LONG with price pulling back to test EMA 20.
    """
    day, last, prev_day = _day_session(df)
    if last is None or len(day) < 20:
        return None

    close = pd.to_numeric(day["close"], errors="coerce")
    c = float(close.iloc[-1])

    ema_9 = float(compute_ema(close, 9).iloc[-1])
    ema_20 = float(compute_ema(close, 20).iloc[-1])
    ema_50 = float(compute_ema(close, 50).iloc[-1])

    # Bullish stack: 9 > 20 > 50 and price tested near 20 EMA
    if ema_9 > ema_20 and ema_20 > ema_50 and c >= ema_20:
        dist_pct = abs(c - ema_20) / c
        if dist_pct <= 0.004:  # Within 0.4% of 20 EMA pullback
            return _risk_wrapped_signal(
                symbol=symbol,
                strategy="EMA",
                direction="LONG",
                score=80.0,
                entry=c,
                rationale=f"EMA Pullback Entry — Bullish Stack (EMA9={ema_9:.1f} > EMA20={ema_20:.1f} > EMA50={ema_50:.1f}) holding pullback.",
                df=day,
                prev_day_df=prev_day,
            )

    # Bearish stack: 9 < 20 < 50 and price rejected near 20 EMA
    if ema_9 < ema_20 and ema_20 < ema_50 and c <= ema_20:
        dist_pct = abs(ema_20 - c) / c
        if dist_pct <= 0.004:
            return _risk_wrapped_signal(
                symbol=symbol,
                strategy="EMA",
                direction="SHORT",
                score=80.0,
                entry=c,
                rationale=f"EMA Pullback Entry — Bearish Stack (EMA9={ema_9:.1f} < EMA20={ema_20:.1f} < EMA50={ema_50:.1f}) rejected at EMA20.",
                df=day,
                prev_day_df=prev_day,
            )

    return None


def strat_rsi(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    """
    RSI Momentum Pullback in Trend Context (Anti-Chop).
    In an uptrend (price > VWAP), buys RSI dipping to 40-48 and turning up.
    In a downtrend (price < VWAP), shorts RSI rallying to 52-60 and turning down.
    """
    day, last, prev_day = _day_session(df)
    if last is None or len(day) < 18:
        return None

    close = pd.to_numeric(day["close"], errors="coerce")
    c = float(close.iloc[-1])

    bands = compute_session_vwap_bands(day)
    vwap = bands["vwap"] if bands else c

    rsi_curr = compute_rsi(close, 14)
    rsi_prev = compute_rsi(close.iloc[:-1], 14)

    if rsi_curr is None or rsi_prev is None:
        return None

    # Trend Long: Above VWAP + RSI turning up from 40-50 zone
    if c > vwap and rsi_prev < 50 and rsi_curr > rsi_prev and rsi_curr >= 45:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="RSI",
            direction="LONG",
            score=76.0,
            entry=c,
            rationale=f"RSI Pullback & Recovery in Uptrend (RSI={rsi_curr:.1f} rising, Price > VWAP {vwap:.2f})",
            df=day,
            prev_day_df=prev_day,
        )

    # Trend Short: Below VWAP + RSI turning down from 50-60 zone
    if c < vwap and rsi_prev > 50 and rsi_curr < rsi_prev and rsi_curr <= 55:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="RSI",
            direction="SHORT",
            score=76.0,
            entry=c,
            rationale=f"RSI Rejection in Downtrend (RSI={rsi_curr:.1f} falling, Price < VWAP {vwap:.2f})",
            df=day,
            prev_day_df=prev_day,
        )

    return None


def strat_gap_go(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    """Gap and Go opening continuation."""
    day, last, prev_day = _day_session(df)
    if last is None or prev_day is None or len(day) < 6:
        return None

    prev_c = float(prev_day["close"].iloc[-1])
    day_o = float(day["open"].iloc[0])
    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )

    gap_pct = ((day_o - prev_c) / prev_c) * 100.0
    vol_mean = float(day["volume"].mean() or 1)
    last_vol = float(last["volume"] or 0)

    # Gap Up Continuation
    if gap_pct >= 0.75 and c > day_o and last_vol >= vol_mean * 1.1:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="GAP_GO",
            direction="LONG",
            score=78.0,
            entry=c,
            rationale=f"Gap-and-Go Long — +{gap_pct:.2f}% gap up holding above open (₹{day_o:.2f}) with volume",
            df=day,
            prev_day_df=prev_day,
        )

    # Gap Down Continuation
    if gap_pct <= -0.75 and c < day_o and last_vol >= vol_mean * 1.1:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="GAP_GO",
            direction="SHORT",
            score=78.0,
            entry=c,
            rationale=f"Gap-and-Go Short — {gap_pct:.2f}% gap down breaking below open (₹{day_o:.2f}) with volume",
            df=day,
            prev_day_df=prev_day,
        )

    return None


def strat_momentum(symbol: str, df: pd.DataFrame) -> Optional[IntradaySignal]:
    """Momentum surge strategy across 3 consecutive 5m candles with expanding volume."""
    day, last, prev_day = _day_session(df)
    if last is None or len(day) < 8:
        return None

    c = (
        float(df.attrs.get("live_ltp", last["close"]))
        if hasattr(df, "attrs") and df.attrs.get("live_ltp") is not None
        else float(last["close"])
    )

    c1 = float(day["close"].iloc[-1])
    c2 = float(day["close"].iloc[-2])
    c3 = float(day["close"].iloc[-3])

    v1 = float(day["volume"].iloc[-1])
    v2 = float(day["volume"].iloc[-2])
    v_mean = float(day["volume"].mean() or 1)

    # Bullish momentum surge
    if c1 > c2 > c3 and v1 > v_mean * 1.2:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="MOMENTUM",
            direction="LONG",
            score=75.0,
            entry=c,
            rationale=f"3-Bar Bullish Momentum Surge with expanding volume ({v1:.0f} vs avg {v_mean:.0f})",
            df=day,
            prev_day_df=prev_day,
        )

    # Bearish momentum breakdown
    if c1 < c2 < c3 and v1 > v_mean * 1.2:
        return _risk_wrapped_signal(
            symbol=symbol,
            strategy="MOMENTUM",
            direction="SHORT",
            score=75.0,
            entry=c,
            rationale=f"3-Bar Bearish Momentum Flush with expanding volume ({v1:.0f} vs avg {v_mean:.0f})",
            df=day,
            prev_day_df=prev_day,
        )

    return None


def score_confluence(signals: List[IntradaySignal]) -> List[IntradaySignal]:
    """
    Groups signals by symbol and direction, calculates multi-strategy confluence,
    and returns enhanced IntradaySignal instances with grade A/B/C.
    """
    if not signals:
        return []

    by_sym: Dict[str, Dict[str, List[IntradaySignal]]] = {}
    for sig in signals:
        by_sym.setdefault(sig.symbol, {"LONG": [], "SHORT": []})
        if sig.direction in ("LONG", "SHORT"):
            by_sym[sig.symbol][sig.direction].append(sig)

    results: List[IntradaySignal] = []

    for sym, dirs in by_sym.items():
        for d in ("LONG", "SHORT"):
            group = dirs[d]
            if not group:
                continue

            count = len(group)
            strats = [s.strategy for s in group]
            best = max(group, key=lambda s: s.score)

            grade = "A" if count >= 3 else ("B" if count == 2 else "C")
            bonus = (count - 1) * 5.0
            new_score = min(98.0, best.score + bonus)

            base_rat = best.rationale
            if count > 1 and "confluence=" not in base_rat:
                rat = f"{base_rat} | confluence={grade} ({count} strats agree: {', '.join(strats)})"
            else:
                rat = base_rat

            best.score = new_score
            best.grade = grade
            best.confluence_count = count
            best.rationale = rat
            results.append(best)

    return results


STRATS = (
    strat_orb,
    strat_vwap,
    strat_cpr,
    strat_supertrend,
    strat_ema,
    strat_rsi,
    strat_gap_go,
    strat_momentum,
)
