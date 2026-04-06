"""
SwingSpectrum — Multi-Day Swing Trading Pattern Scanner.

Scans all tracked symbols for high-probability swing trade setups:
  1. Pullback-to-EMA in uptrend (buy the dip)
  2. Pullback-to-EMA in downtrend (sell the rally)
  3. Range breakout (price breaks 20-day consolidation)
  4. Bullish/Bearish divergence (RSI vs price)
  5. Golden/Death cross (EMA 50 x 200)
  6. Inside bar breakout (3+ inside bars followed by expansion)

Each pattern gets a strength score (0-100) and risk/reward estimate.

Data source: yFinance daily candles (60d history).
"""

import logging
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SwingSetup:
    """A detected swing trading setup."""
    symbol: str
    pattern: str           # Pattern name
    direction: str         # "LONG" or "SHORT"
    strength: int          # 0-100 signal strength
    entry_zone: float      # Suggested entry price
    stop_loss: float       # Recommended SL
    target_1: float        # Target 1 (1:2 RR)
    target_2: float        # Target 2 (1:3 RR)
    risk_reward: float     # Best R:R ratio
    timeframe: str         # "2-5 days", "5-10 days", etc.
    description: str       # Human-readable explanation
    indicators: dict       # Supporting indicator values


def scan_swing_setups(
    df: pd.DataFrame,
    symbol: str = "",
) -> list[SwingSetup]:
    """
    Scan a daily OHLCV+indicators DataFrame for swing trading setups.

    Expects columns: open, high, low, close, volume, RSI_14, EMA_20, EMA_50,
    EMA_200, MACD_hist, ATR_14, BB_upper, BB_lower, ADX_14

    Returns list of SwingSetup objects, sorted by strength (strongest first).
    """
    if df is None or df.empty or len(df) < 30:
        return []

    setups: list[SwingSetup] = []

    try:
        # Ensure we have required columns
        required = ["close", "high", "low", "RSI_14", "EMA_20", "EMA_50", "ATR_14"]
        if not all(c in df.columns for c in required):
            logger.debug("Missing required columns for swing scan on %s", symbol)
            return []

        last = df.iloc[-1]
        prev = df.iloc[-2]
        atr = float(last["ATR_14"]) if pd.notna(last.get("ATR_14")) else 0
        close = float(last["close"])

        if atr <= 0 or close <= 0:
            return []

        # 1. Pullback to EMA20 in uptrend
        setup = _check_pullback_to_ema(df, symbol, atr)
        if setup:
            setups.append(setup)

        # 2. Range breakout (20-day high/low)
        setup = _check_range_breakout(df, symbol, atr)
        if setup:
            setups.append(setup)

        # 3. RSI divergence
        setup = _check_rsi_divergence(df, symbol, atr)
        if setup:
            setups.append(setup)

        # 4. Golden / Death cross
        setup = _check_ema_cross(df, symbol, atr)
        if setup:
            setups.append(setup)

        # 5. Inside bar breakout
        setup = _check_inside_bar(df, symbol, atr)
        if setup:
            setups.append(setup)

        # 6. Bollinger Band squeeze breakout
        setup = _check_bb_squeeze(df, symbol, atr)
        if setup:
            setups.append(setup)

    except Exception as exc:
        logger.error("Swing scan failed for %s: %s", symbol, exc)

    # Sort by strength descending
    setups.sort(key=lambda s: s.strength, reverse=True)
    return setups


def _check_pullback_to_ema(df: pd.DataFrame, symbol: str, atr: float) -> Optional[SwingSetup]:
    """
    Pullback-to-EMA in a trending market.
    LONG: Uptrend (EMA20 > EMA50) + price pulled back to touch EMA20 + bouncing
    SHORT: Downtrend (EMA20 < EMA50) + price rallied to EMA20 + rejecting
    """
    last = df.iloc[-1]
    close = float(last["close"])
    ema20 = float(last["EMA_20"])
    ema50 = float(last["EMA_50"])
    rsi = float(last["RSI_14"]) if pd.notna(last.get("RSI_14")) else 50

    # Check if EMA200 exists
    ema200 = float(last.get("EMA_200", ema50)) if pd.notna(last.get("EMA_200")) else ema50

    # LONG: Uptrend pullback
    if ema20 > ema50:
        # Price within 1.5% of EMA20 (touching the moving average)
        proximity = abs(close - ema20) / ema20 * 100
        if proximity < 1.5 and close > ema50:
            # Check bounce: last bar closed green or above previous low
            bouncing = close > float(df.iloc[-2]["close"]) or float(last["low"]) > float(df.iloc[-2]["low"])
            if bouncing:
                sl = close - atr * 2
                tp1 = close + atr * 4
                tp2 = close + atr * 6

                strength = 60
                if ema50 > ema200:  # Super uptrend
                    strength += 15
                if rsi > 40 and rsi < 60:  # Not overbought
                    strength += 10
                if proximity < 0.5:  # Very close to EMA
                    strength += 10
                strength = min(95, strength)

                return SwingSetup(
                    symbol=symbol,
                    pattern="Pullback to EMA20 (Uptrend)",
                    direction="LONG",
                    strength=strength,
                    entry_zone=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(tp1, 2),
                    target_2=round(tp2, 2),
                    risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
                    timeframe="3-7 days",
                    description=f"Price pulled back to EMA20 (₹{ema20:.0f}) in uptrend. "
                                f"EMA20 > EMA50 confirms trend. RSI at {rsi:.0f} shows room to run.",
                    indicators={"rsi": round(rsi, 1), "ema20": round(ema20, 2),
                                "ema50": round(ema50, 2), "proximity_pct": round(proximity, 2)},
                )

    # SHORT: Downtrend rally
    if ema20 < ema50:
        proximity = abs(close - ema20) / ema20 * 100
        if proximity < 1.5 and close < ema50:
            rejecting = close < float(df.iloc[-2]["close"]) or float(last["high"]) < float(df.iloc[-2]["high"])
            if rejecting:
                sl = close + atr * 2
                tp1 = close - atr * 4
                tp2 = close - atr * 6

                strength = 60
                if ema50 < ema200:
                    strength += 15
                if rsi > 40 and rsi < 65:
                    strength += 10
                if proximity < 0.5:
                    strength += 10
                strength = min(95, strength)

                return SwingSetup(
                    symbol=symbol,
                    pattern="Rally to EMA20 (Downtrend)",
                    direction="SHORT",
                    strength=strength,
                    entry_zone=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(tp1, 2),
                    target_2=round(tp2, 2),
                    risk_reward=round((close - tp1) / max(sl - close, 0.01), 1),
                    timeframe="3-7 days",
                    description=f"Price rallied to EMA20 (₹{ema20:.0f}) in downtrend. "
                                f"EMA20 < EMA50 confirms bearish trend. RSI at {rsi:.0f}.",
                    indicators={"rsi": round(rsi, 1), "ema20": round(ema20, 2),
                                "ema50": round(ema50, 2), "proximity_pct": round(proximity, 2)},
                )
    return None


def _check_range_breakout(df: pd.DataFrame, symbol: str, atr: float) -> Optional[SwingSetup]:
    """
    Breakout from 20-day consolidation range with volume confirmation.
    Must close above/below the range with above-average volume.
    """
    close = float(df.iloc[-1]["close"])
    vol = float(df.iloc[-1].get("volume", 0))
    vol_avg = float(df["volume"].tail(20).mean()) if "volume" in df.columns else 0

    # 20-day high/low (excluding current bar)
    lookback = df.iloc[-21:-1]
    if len(lookback) < 15:
        return None

    range_high = float(lookback["high"].max())
    range_low = float(lookback["low"].min())
    range_width = range_high - range_low

    # Range must be meaningful (at least 2% of price — not too tight)
    if range_width / close < 0.02:
        return None

    # Volume confirmation: today's volume >= 1.3x average
    vol_confirmed = vol_avg > 0 and vol >= vol_avg * 1.3

    # BULLISH breakout
    if close > range_high:
        sl = range_high - atr * 0.5  # SL just below breakout level
        tp1 = close + range_width      # Measured move = range width
        tp2 = close + range_width * 1.5

        strength = 55
        if vol_confirmed:
            strength += 20
        if float(df.iloc[-1].get("ADX_14", 0) or 0) > 25:
            strength += 10
        rsi = float(df.iloc[-1].get("RSI_14", 50) or 50)
        if rsi < 75:
            strength += 5
        strength = min(95, strength)

        return SwingSetup(
            symbol=symbol,
            pattern="20-Day Range Breakout (Bullish)",
            direction="LONG",
            strength=strength,
            entry_zone=round(close, 2),
            stop_loss=round(sl, 2),
            target_1=round(tp1, 2),
            target_2=round(tp2, 2),
            risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
            timeframe="5-10 days",
            description=f"Broke above 20-day range high ₹{range_high:.0f}. "
                        f"{'Volume confirmed ✓' if vol_confirmed else 'Low volume ⚠️'}. "
                        f"Measured move target: ₹{tp1:.0f}.",
            indicators={"range_high": round(range_high, 2), "range_low": round(range_low, 2),
                        "vol_ratio": round(vol / max(vol_avg, 1), 1), "range_width_pct": round(range_width / close * 100, 1)},
        )

    # BEARISH breakdown
    if close < range_low:
        sl = range_low + atr * 0.5
        tp1 = close - range_width
        tp2 = close - range_width * 1.5

        strength = 55
        if vol_confirmed:
            strength += 20
        if float(df.iloc[-1].get("ADX_14", 0) or 0) > 25:
            strength += 10
        strength = min(95, strength)

        return SwingSetup(
            symbol=symbol,
            pattern="20-Day Range Breakdown (Bearish)",
            direction="SHORT",
            strength=strength,
            entry_zone=round(close, 2),
            stop_loss=round(sl, 2),
            target_1=round(tp1, 2),
            target_2=round(tp2, 2),
            risk_reward=round((close - tp1) / max(sl - close, 0.01), 1),
            timeframe="5-10 days",
            description=f"Broke below 20-day range low ₹{range_low:.0f}. "
                        f"{'Volume confirmed ✓' if vol_confirmed else 'Low volume ⚠️'}.",
            indicators={"range_high": round(range_high, 2), "range_low": round(range_low, 2),
                        "vol_ratio": round(vol / max(vol_avg, 1), 1)},
        )
    return None


def _check_rsi_divergence(df: pd.DataFrame, symbol: str, atr: float) -> Optional[SwingSetup]:
    """
    Detect RSI divergence over 10-20 bars.
    Bullish divergence: price lower low, RSI higher low → reversal UP
    Bearish divergence: price higher high, RSI lower high → reversal DOWN
    """
    if len(df) < 20:
        return None

    close = df["close"].values
    rsi = df["RSI_14"].values if "RSI_14" in df.columns else None
    if rsi is None:
        return None

    last_close = float(close[-1])

    # Compare last 5 bars vs 10-15 bars ago
    recent_window = slice(-5, None)
    earlier_window = slice(-15, -8)

    recent_low = float(np.nanmin(close[recent_window]))
    earlier_low = float(np.nanmin(close[earlier_window]))
    recent_rsi_low = float(np.nanmin(rsi[recent_window]))
    earlier_rsi_low = float(np.nanmin(rsi[earlier_window]))

    recent_high = float(np.nanmax(close[recent_window]))
    earlier_high = float(np.nanmax(close[earlier_window]))
    recent_rsi_high = float(np.nanmax(rsi[recent_window]))
    earlier_rsi_high = float(np.nanmax(rsi[earlier_window]))

    # Bullish divergence: price makes lower low, RSI makes higher low
    if recent_low < earlier_low and recent_rsi_low > earlier_rsi_low:
        if recent_rsi_low < 40:  # RSI in oversold territory
            sl = recent_low - atr * 1.5
            tp1 = last_close + atr * 3
            tp2 = last_close + atr * 5

            divergence_strength = int((recent_rsi_low - earlier_rsi_low) * 2)
            strength = min(85, 55 + divergence_strength + (5 if recent_rsi_low < 30 else 0))

            return SwingSetup(
                symbol=symbol,
                pattern="Bullish RSI Divergence",
                direction="LONG",
                strength=strength,
                entry_zone=round(last_close, 2),
                stop_loss=round(sl, 2),
                target_1=round(tp1, 2),
                target_2=round(tp2, 2),
                risk_reward=round((tp1 - last_close) / max(last_close - sl, 0.01), 1),
                timeframe="3-7 days",
                description=f"Price made lower low but RSI made higher low (bullish divergence). "
                            f"RSI at {recent_rsi_low:.0f} showing momentum shift.",
                indicators={"rsi_low": round(recent_rsi_low, 1), "prev_rsi_low": round(earlier_rsi_low, 1),
                            "price_low": round(recent_low, 2), "prev_price_low": round(earlier_low, 2)},
            )

    # Bearish divergence: price makes higher high, RSI makes lower high
    if recent_high > earlier_high and recent_rsi_high < earlier_rsi_high:
        if recent_rsi_high > 60:
            sl = recent_high + atr * 1.5
            tp1 = last_close - atr * 3
            tp2 = last_close - atr * 5

            divergence_strength = int((earlier_rsi_high - recent_rsi_high) * 2)
            strength = min(85, 55 + divergence_strength + (5 if recent_rsi_high > 70 else 0))

            return SwingSetup(
                symbol=symbol,
                pattern="Bearish RSI Divergence",
                direction="SHORT",
                strength=strength,
                entry_zone=round(last_close, 2),
                stop_loss=round(sl, 2),
                target_1=round(tp1, 2),
                target_2=round(tp2, 2),
                risk_reward=round((last_close - tp1) / max(sl - last_close, 0.01), 1),
                timeframe="3-7 days",
                description=f"Price made higher high but RSI made lower high (bearish divergence). "
                            f"RSI at {recent_rsi_high:.0f} showing weakening momentum.",
                indicators={"rsi_high": round(recent_rsi_high, 1), "prev_rsi_high": round(earlier_rsi_high, 1)},
            )
    return None


def _check_ema_cross(df: pd.DataFrame, symbol: str, atr: float) -> Optional[SwingSetup]:
    """
    Golden Cross (EMA50 crosses above EMA200) → LONG
    Death Cross (EMA50 crosses below EMA200) → SHORT
    Require the cross to have happened within the last 3 bars.
    """
    if "EMA_50" not in df.columns or "EMA_200" not in df.columns:
        return None

    for i in range(-3, 0):
        try:
            curr_50 = float(df["EMA_50"].iloc[i])
            curr_200 = float(df["EMA_200"].iloc[i])
            prev_50 = float(df["EMA_50"].iloc[i - 1])
            prev_200 = float(df["EMA_200"].iloc[i - 1])
        except (IndexError, ValueError):
            continue

        if pd.isna(curr_50) or pd.isna(curr_200) or pd.isna(prev_50) or pd.isna(prev_200):
            continue

        close = float(df.iloc[-1]["close"])

        # Golden cross
        if prev_50 <= prev_200 and curr_50 > curr_200:
            sl = close - atr * 3
            tp1 = close + atr * 6
            tp2 = close + atr * 9
            return SwingSetup(
                symbol=symbol,
                pattern="Golden Cross (EMA50 × EMA200)",
                direction="LONG",
                strength=80,
                entry_zone=round(close, 2),
                stop_loss=round(sl, 2),
                target_1=round(tp1, 2),
                target_2=round(tp2, 2),
                risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
                timeframe="10-20 days",
                description="EMA50 crossed above EMA200 — classic long-term bullish signal. "
                            "Best for positional/swing entries.",
                indicators={"ema50": round(curr_50, 2), "ema200": round(curr_200, 2)},
            )

        # Death cross
        if prev_50 >= prev_200 and curr_50 < curr_200:
            sl = close + atr * 3
            tp1 = close - atr * 6
            tp2 = close - atr * 9
            return SwingSetup(
                symbol=symbol,
                pattern="Death Cross (EMA50 × EMA200)",
                direction="SHORT",
                strength=75,
                entry_zone=round(close, 2),
                stop_loss=round(sl, 2),
                target_1=round(tp1, 2),
                target_2=round(tp2, 2),
                risk_reward=round((close - tp1) / max(sl - close, 0.01), 1),
                timeframe="10-20 days",
                description="EMA50 crossed below EMA200 — classic long-term bearish signal.",
                indicators={"ema50": round(curr_50, 2), "ema200": round(curr_200, 2)},
            )
    return None


def _check_inside_bar(df: pd.DataFrame, symbol: str, atr: float) -> Optional[SwingSetup]:
    """
    Inside bar pattern: 2+ consecutive bars where H/L stays within the mother bar.
    Breakout from inside bars = explosive move. Common swing setup.
    """
    if len(df) < 5:
        return None

    # Find the "mother bar" (bar with the widest range followed by smaller bars)
    for mother_idx in range(-5, -2):
        try:
            mother_high = float(df.iloc[mother_idx]["high"])
            mother_low = float(df.iloc[mother_idx]["low"])
        except (IndexError, ValueError):
            continue

        inside_count = 0
        for j in range(mother_idx + 1, 0):
            bar_high = float(df.iloc[j]["high"])
            bar_low = float(df.iloc[j]["low"])
            if bar_high <= mother_high and bar_low >= mother_low:
                inside_count += 1
            else:
                break

        if inside_count >= 2:
            close = float(df.iloc[-1]["close"])
            # Breakout direction
            if close > mother_high:
                sl = mother_low
                tp1 = close + (mother_high - mother_low)
                tp2 = close + (mother_high - mother_low) * 1.5
                return SwingSetup(
                    symbol=symbol,
                    pattern=f"Inside Bar Breakout ({inside_count} bars)",
                    direction="LONG",
                    strength=min(90, 60 + inside_count * 10),
                    entry_zone=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(tp1, 2),
                    target_2=round(tp2, 2),
                    risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
                    timeframe="2-5 days",
                    description=f"{inside_count} inside bars compressed within mother bar. "
                                f"Bullish breakout above ₹{mother_high:.0f}.",
                    indicators={"mother_high": round(mother_high, 2), "mother_low": round(mother_low, 2),
                                "inside_bars": inside_count},
                )
            elif close < mother_low:
                sl = mother_high
                tp1 = close - (mother_high - mother_low)
                tp2 = close - (mother_high - mother_low) * 1.5
                return SwingSetup(
                    symbol=symbol,
                    pattern=f"Inside Bar Breakdown ({inside_count} bars)",
                    direction="SHORT",
                    strength=min(90, 60 + inside_count * 10),
                    entry_zone=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(tp1, 2),
                    target_2=round(tp2, 2),
                    risk_reward=round((close - tp1) / max(sl - close, 0.01), 1),
                    timeframe="2-5 days",
                    description=f"{inside_count} inside bars. Bearish breakdown below ₹{mother_low:.0f}.",
                    indicators={"mother_high": round(mother_high, 2), "mother_low": round(mother_low, 2),
                                "inside_bars": inside_count},
                )
    return None


def _check_bb_squeeze(df: pd.DataFrame, symbol: str, atr: float) -> Optional[SwingSetup]:
    """
    Bollinger Band squeeze: BB width narrows to multi-week low, then expands.
    Indicates a big move is imminent. Direction is determined by breakout side.
    """
    if "BB_width" not in df.columns or len(df) < 25:
        return None

    bb_width = df["BB_width"].dropna()
    if len(bb_width) < 20:
        return None

    current_width = float(bb_width.iloc[-1])
    avg_width = float(bb_width.tail(20).mean())
    min_width = float(bb_width.tail(20).min())

    # Squeeze: current width was at or near the 20-day minimum
    # Expansion: width is now increasing from that squeeze
    prev_width = float(bb_width.iloc[-2]) if len(bb_width) >= 2 else current_width

    is_expanding = current_width > prev_width
    was_squeezed = min_width < avg_width * 0.6  # Width was 60% below average

    if was_squeezed and is_expanding:
        close = float(df.iloc[-1]["close"])
        bb_upper = float(df.iloc[-1].get("BB_upper", close + atr))
        bb_lower = float(df.iloc[-1].get("BB_lower", close - atr))

        # Breakout direction
        if close > bb_upper * 0.998:  # Near or above upper band
            sl = close - atr * 2
            tp1 = close + atr * 4
            tp2 = close + atr * 6
            return SwingSetup(
                symbol=symbol,
                pattern="BB Squeeze Breakout (Bullish)",
                direction="LONG",
                strength=70,
                entry_zone=round(close, 2),
                stop_loss=round(sl, 2),
                target_1=round(tp1, 2),
                target_2=round(tp2, 2),
                risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
                timeframe="3-7 days",
                description="Bollinger Bands squeezed to 20-day low and now expanding upward. "
                            "Expect a strong directional move.",
                indicators={"bb_width": round(current_width, 4), "avg_width": round(avg_width, 4),
                            "squeeze_ratio": round(min_width / max(avg_width, 0.001), 2)},
            )
        elif close < bb_lower * 1.002:
            sl = close + atr * 2
            tp1 = close - atr * 4
            tp2 = close - atr * 6
            return SwingSetup(
                symbol=symbol,
                pattern="BB Squeeze Breakout (Bearish)",
                direction="SHORT",
                strength=70,
                entry_zone=round(close, 2),
                stop_loss=round(sl, 2),
                target_1=round(tp1, 2),
                target_2=round(tp2, 2),
                risk_reward=round((close - tp1) / max(sl - close, 0.01), 1),
                timeframe="3-7 days",
                description="BB Squeeze + bearish expansion. Expect continuation lower.",
                indicators={"bb_width": round(current_width, 4), "avg_width": round(avg_width, 4)},
            )
    return None


def scan_all_symbols(
    symbol_data: dict[str, pd.DataFrame],
) -> list[dict]:
    """
    Scan all symbols for swing setups.

    Args:
        symbol_data: {display_name: daily_df_with_indicators}

    Returns:
        List of dicts (JSON-serializable swing setups), sorted by strength.
    """
    all_setups = []
    for symbol, df in symbol_data.items():
        setups = scan_swing_setups(df, symbol)
        for s in setups:
            all_setups.append(asdict(s))

    all_setups.sort(key=lambda x: x["strength"], reverse=True)
    return all_setups
