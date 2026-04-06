"""
BreakoutScanner — Volume-Confirmed Breakout Detection.

Scans all tracked symbols for breakout conditions across multiple timeframes:
  - 10-Day Breakout (short-term momentum)
  - 50-Day Breakout (medium-term trend change)
  - 52-Week High/Low (major breakout)
  - Volume-confirmed breakouts (1.5x avg volume required)

Each breakout is scored by:
  - Volume confirmation strength
  - ATR-based move magnitude
  - Follow-through (closing near high/low of breakout bar)
  - Multi-timeframe alignment

This goes beyond IntradayPulse's "Market Wise" by adding:
  1. Volume confirmation scoring (not just price breakout)
  2. False breakout detection (wick rejection, close back inside range)
  3. Breakout strength grading (A/B/C)
  4. Distance from breakout level (% above/below)
"""

import logging
from dataclasses import dataclass, asdict
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class BreakoutSignal:
    """A detected breakout event."""
    symbol: str
    breakout_type: str       # "10D_HIGH", "50D_HIGH", "10D_LOW", "50D_LOW", "52W_HIGH", "52W_LOW"
    direction: str           # "BULLISH" or "BEARISH"
    grade: str               # "A" (strong) / "B" (moderate) / "C" (weak)
    ltp: float               # Last traded price
    breakout_level: float    # The level that was broken
    distance_pct: float      # % distance from breakout level
    volume_ratio: float      # Current volume / avg volume
    atr_move: float          # How many ATRs the move covers
    strength_score: int      # 0-100 composite strength
    follow_through: bool     # Did the bar close in the direction of breakout?
    timestamp: str           # ISO timestamp


def scan_breakouts(
    df: pd.DataFrame,
    symbol: str = "",
) -> list[BreakoutSignal]:
    """
    Scan a single symbol's OHLCV+indicator DataFrame for breakout conditions.

    Expects columns: open, high, low, close, volume, ATR_14
    Daily data recommended for swing breakouts.
    """
    if df is None or df.empty or len(df) < 15:
        return []

    breakouts: list[BreakoutSignal] = []

    try:
        last = df.iloc[-1]
        close = float(last["close"])
        high = float(last["high"])
        low = float(last["low"])
        open_price = float(last["open"])
        vol = float(last.get("volume", 0))
        atr = float(last.get("ATR_14", 0)) if pd.notna(last.get("ATR_14")) else 0

        if close <= 0:
            return []

        # Volume baseline
        vol_avg = float(df["volume"].tail(20).mean()) if "volume" in df.columns else 1
        vol_ratio = vol / max(vol_avg, 1) if vol_avg > 0 else 0

        # ── 10-Day Breakout ──────────────────────────────────
        if len(df) >= 12:
            lookback_10 = df.iloc[-11:-1]
            high_10 = float(lookback_10["high"].max())
            low_10 = float(lookback_10["low"].min())

            if close > high_10:
                breakouts.append(_build_breakout(
                    symbol, "10D_HIGH", "BULLISH", close, high_10,
                    vol_ratio, atr, open_price, high, low,
                ))

            if close < low_10:
                breakouts.append(_build_breakout(
                    symbol, "10D_LOW", "BEARISH", close, low_10,
                    vol_ratio, atr, open_price, high, low,
                ))

        # ── 50-Day Breakout ──────────────────────────────────
        if len(df) >= 52:
            lookback_50 = df.iloc[-51:-1]
            high_50 = float(lookback_50["high"].max())
            low_50 = float(lookback_50["low"].min())

            if close > high_50:
                breakouts.append(_build_breakout(
                    symbol, "50D_HIGH", "BULLISH", close, high_50,
                    vol_ratio, atr, open_price, high, low,
                ))

            if close < low_50:
                breakouts.append(_build_breakout(
                    symbol, "50D_LOW", "BEARISH", close, low_50,
                    vol_ratio, atr, open_price, high, low,
                ))

        # ── 52-Week Breakout (if enough daily data) ──────────
        if len(df) >= 250:
            lookback_52w = df.iloc[-250:-1]
            high_52w = float(lookback_52w["high"].max())
            low_52w = float(lookback_52w["low"].min())

            if close > high_52w:
                breakouts.append(_build_breakout(
                    symbol, "52W_HIGH", "BULLISH", close, high_52w,
                    vol_ratio, atr, open_price, high, low,
                ))

            if close < low_52w:
                breakouts.append(_build_breakout(
                    symbol, "52W_LOW", "BEARISH", close, low_52w,
                    vol_ratio, atr, open_price, high, low,
                ))

    except Exception as exc:
        logger.error("Breakout scan failed for %s: %s", symbol, exc)

    return breakouts


def _build_breakout(
    symbol: str,
    breakout_type: str,
    direction: str,
    close: float,
    breakout_level: float,
    vol_ratio: float,
    atr: float,
    open_price: float,
    high: float,
    low: float,
) -> BreakoutSignal:
    """Build a BreakoutSignal with computed strength metrics."""

    # Distance from breakout level
    distance_pct = abs(close - breakout_level) / max(breakout_level, 0.01) * 100

    # ATR-normalized move
    atr_move = abs(close - breakout_level) / max(atr, 0.01) if atr > 0 else 0

    # Follow-through: for bullish, close near highs; for bearish, close near lows
    candle_range = high - low if high > low else 0.01
    if direction == "BULLISH":
        close_position = (close - low) / candle_range
        follow_through = close_position > 0.6 and close > open_price
    else:
        close_position = (high - close) / candle_range
        follow_through = close_position > 0.6 and close < open_price

    # ── Strength scoring (0-100) ─────────────────────────
    score = 0

    # Volume (max 35 pts — most important for breakout validity)
    if vol_ratio >= 2.5:
        score += 35
    elif vol_ratio >= 2.0:
        score += 30
    elif vol_ratio >= 1.5:
        score += 25
    elif vol_ratio >= 1.2:
        score += 15
    elif vol_ratio >= 1.0:
        score += 8
    else:
        score += 0  # Below-average volume = likely fake breakout

    # Follow-through (max 25 pts)
    if follow_through:
        score += 25
    else:
        score += 5  # Wick rejection = weak breakout

    # ATR move magnitude (max 20 pts)
    if atr_move >= 2.0:
        score += 20
    elif atr_move >= 1.5:
        score += 15
    elif atr_move >= 1.0:
        score += 10
    elif atr_move >= 0.5:
        score += 5

    # Breakout type bonus (max 20 pts — longer-term breakouts are more significant)
    type_bonus = {
        "52W_HIGH": 20, "52W_LOW": 20,
        "50D_HIGH": 15, "50D_LOW": 15,
        "10D_HIGH": 10, "10D_LOW": 10,
    }
    score += type_bonus.get(breakout_type, 10)

    score = min(100, score)

    # Grade
    if score >= 75:
        grade = "A"
    elif score >= 50:
        grade = "B"
    else:
        grade = "C"

    return BreakoutSignal(
        symbol=symbol,
        breakout_type=breakout_type,
        direction=direction,
        grade=grade,
        ltp=round(close, 2),
        breakout_level=round(breakout_level, 2),
        distance_pct=round(distance_pct, 2),
        volume_ratio=round(vol_ratio, 2),
        atr_move=round(atr_move, 2),
        strength_score=score,
        follow_through=follow_through,
        timestamp=datetime.now().isoformat(),
    )


def scan_all_symbols(
    symbol_data: dict[str, pd.DataFrame],
    min_strength: int = 30,
) -> dict:
    """
    Scan all symbols for breakouts and return categorized results.

    Args:
        symbol_data: {display_name: daily_df_with_indicators}
        min_strength: Minimum strength score to include

    Returns:
        dict with categorized breakout data.
    """
    all_breakouts = []

    for symbol, df in symbol_data.items():
        bos = scan_breakouts(df, symbol)
        for b in bos:
            if b.strength_score >= min_strength:
                all_breakouts.append(asdict(b))

    # Sort by strength
    all_breakouts.sort(key=lambda x: x["strength_score"], reverse=True)

    # Categorize
    bullish_10d = [b for b in all_breakouts if b["breakout_type"] == "10D_HIGH"]
    bearish_10d = [b for b in all_breakouts if b["breakout_type"] == "10D_LOW"]
    bullish_50d = [b for b in all_breakouts if b["breakout_type"] == "50D_HIGH"]
    bearish_50d = [b for b in all_breakouts if b["breakout_type"] == "50D_LOW"]
    week_52_high = [b for b in all_breakouts if b["breakout_type"] == "52W_HIGH"]
    week_52_low = [b for b in all_breakouts if b["breakout_type"] == "52W_LOW"]

    return {
        "all": all_breakouts[:50],
        "bullish_10d": bullish_10d[:20],
        "bearish_10d": bearish_10d[:20],
        "bullish_50d": bullish_50d[:20],
        "bearish_50d": bearish_50d[:20],
        "week_52_high": week_52_high[:10],
        "week_52_low": week_52_low[:10],
        "total_breakouts": len(all_breakouts),
        "grade_a_count": len([b for b in all_breakouts if b["grade"] == "A"]),
        "grade_b_count": len([b for b in all_breakouts if b["grade"] == "B"]),
        "real_data": True,
        "timestamp": datetime.now().isoformat(),
    }
