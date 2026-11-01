"""
Forex Scanner — Multi-timeframe swing setup detector for forex pairs.

Uses 4h candles for pattern detection + daily candles for trend confirmation.
Volume is replaced by ATR momentum (atr_momentum = current_atr / rolling_10_atr_avg).
Setups are cached for FOREX_CACHE_MINUTES to limit expensive API calls.
"""

import logging
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from config import settings
from agents.data_ingestion.forex_fetcher import (
    get_forex_ohlcv,
    get_pip_value,
    is_twelve_data_backed_off,
    is_session_active,
)

logger = logging.getLogger(__name__)

# ── Setup cache ───────────────────────────────────────────────────────────────
_scan_cache: dict = {"setups": [], "fetched_at": 0.0}
_scan_lock = threading.Lock()


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class ForexSetup:
    """A detected forex swing trading setup."""
    symbol: str
    direction: str          # "LONG" or "SHORT"
    pattern: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    sl_pips: float
    rr_ratio: float
    strength_score: int     # 0–100
    session_active: bool
    session_name: str
    daily_trend: str        # "BULLISH" or "BEARISH"
    hold_hours_min: int
    hold_hours_max: int
    timestamp_utc: str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()


def _bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    """Return (upper, middle, lower) Bollinger Bands."""
    middle = series.rolling(period).mean()
    std = series.rolling(period).std()
    return middle + std_dev * std, middle, middle - std_dev * std


def _support_resistance(df: pd.DataFrame, lookback: int = 20) -> tuple[float, float]:
    """Simple swing high/low as resistance/support over a lookback window."""
    window = df.tail(lookback)
    return window["high"].max(), window["low"].min()


# ── Pattern detection ─────────────────────────────────────────────────────────

def _detect_pattern(
    df: pd.DataFrame,
    ema20: pd.Series,
    ema50: pd.Series,
    rsi: pd.Series,
    bb_upper: pd.Series,
    bb_lower: pd.Series,
    atr_momentum: float,
    profile: dict,
) -> tuple[str, str]:
    """
    Detect the strongest applicable pattern.

    Returns:
        (pattern_name, direction) or ("NONE", "NONE")
    """
    close = df["close"]
    last = close.iloc[-1]
    prev = close.iloc[-2] if len(close) > 1 else last

    ema20_last = ema20.iloc[-1]
    ema50_last = ema50.iloc[-1]
    rsi_last = rsi.iloc[-1]

    bb_upper_last = bb_upper.iloc[-1]
    bb_lower_last = bb_lower.iloc[-1]

    allowed = set(profile.get("best_patterns", []))
    momentum_ok = atr_momentum > 1.3

    # EMA20_PULLBACK: price just bounced off EMA20, trending above EMA50
    if "EMA20_PULLBACK" in allowed:
        if (ema20_last > ema50_last and
                abs(last - ema20_last) / last < 0.003 and
                last > ema20_last * 0.998 and
                momentum_ok):
            return "EMA20_PULLBACK", "LONG"
        if (ema20_last < ema50_last and
                abs(last - ema20_last) / last < 0.003 and
                last < ema20_last * 1.002 and
                momentum_ok):
            return "EMA20_PULLBACK", "SHORT"

    # RANGE_BREAKOUT: breakout above/below recent 20-bar high/low with ATR momentum
    if "RANGE_BREAKOUT" in allowed:
        resistance, support = _support_resistance(df.iloc[:-1])  # exclude last bar
        if last > resistance and momentum_ok:
            return "RANGE_BREAKOUT", "LONG"
        if last < support and momentum_ok:
            return "RANGE_BREAKOUT", "SHORT"

    # BB_SQUEEZE: price near lower/upper band with reversal
    if "BB_SQUEEZE" in allowed:
        band_width = (bb_upper_last - bb_lower_last) / (bb_upper_last + 1e-10)
        if band_width < 0.02 and last < bb_lower_last * 1.002 and prev < last:
            return "BB_SQUEEZE", "LONG"
        if band_width < 0.02 and last > bb_upper_last * 0.998 and prev > last:
            return "BB_SQUEEZE", "SHORT"

    # SUPPORT_REVERSAL: price near support with bullish close
    if "SUPPORT_REVERSAL" in allowed:
        _, support = _support_resistance(df, lookback=20)
        if abs(last - support) / support < 0.002 and last > prev and rsi_last < 55:
            return "SUPPORT_REVERSAL", "LONG"
        resistance, _ = _support_resistance(df, lookback=20)
        if abs(last - resistance) / resistance < 0.002 and last < prev and rsi_last > 45:
            return "SUPPORT_REVERSAL", "SHORT"

    # INSIDE_BAR: current bar range inside previous bar, expect breakout
    if "INSIDE_BAR" in allowed:
        if (df["high"].iloc[-1] < df["high"].iloc[-2] and
                df["low"].iloc[-1] > df["low"].iloc[-2] and
                momentum_ok):
            direction = "LONG" if last > ema20_last else "SHORT"
            return "INSIDE_BAR", direction

    # RSI_DIVERGENCE: price lower high but RSI higher high (bullish div)
    if "RSI_DIVERGENCE" in allowed and len(rsi) > 5:
        rsi_rising = rsi.iloc[-1] > rsi.iloc[-3]
        price_falling = last < close.iloc[-3]
        if rsi_rising and price_falling:
            return "RSI_DIVERGENCE", "LONG"
        rsi_falling = rsi.iloc[-1] < rsi.iloc[-3]
        price_rising = last > close.iloc[-3]
        if rsi_falling and price_rising:
            return "RSI_DIVERGENCE", "SHORT"

    return "NONE", "NONE"


def _fallback_trend_pattern(
    ema20_last: float,
    ema50_last: float,
    rsi_last: float,
    daily_trend: str,
) -> tuple[str, str]:
    """
    Fallback setup when strict pattern rules don't trigger.

    This keeps scanner output useful during quiet sessions by emitting
    conservative trend-continuation candidates.
    """
    if daily_trend == "BULLISH" and ema20_last > ema50_last and 42 <= rsi_last <= 68:
        return "TREND_CONTINUATION", "LONG"
    if daily_trend == "BEARISH" and ema20_last < ema50_last and 32 <= rsi_last <= 58:
        return "TREND_CONTINUATION", "SHORT"
    return "NONE", "NONE"


# ── Core scanner ──────────────────────────────────────────────────────────────

def scan_forex_setup(
    symbol: str,
    df_4h: pd.DataFrame,
    df_daily: pd.DataFrame,
) -> Optional[ForexSetup]:
    """
    Analyse a single forex pair and return a ForexSetup if a valid setup is found.

    Returns None if:
    - Session is inactive (for INR pairs)
    - No pattern found
    - SL pips exceed profile max
    - R:R is below profile minimum
    - Daily trend contradicts signal direction
    """
    if df_4h is None or df_4h.empty or len(df_4h) < 30:
        logger.debug("[FOREX] %s: insufficient 4h data (%d bars)", symbol, len(df_4h) if df_4h is not None else 0)
        return None
    if df_daily is None or df_daily.empty or len(df_daily) < 20:
        logger.debug("[FOREX] %s: insufficient daily data (%d bars)", symbol, len(df_daily) if df_daily is not None else 0)
        return None

    # Step a: session check for INR pairs
    session_active, session_name = is_session_active(symbol)
    inr_pairs = {"USD/INR", "EUR/INR", "GBP/INR", "JPY/INR"}
    if symbol in inr_pairs and not session_active:
        logger.debug("[FOREX] %s: session %s inactive — skipping", symbol, session_name)
        return None

    # Step b: load profile
    profile = settings.FOREX_PROFILES.get(symbol, {
        "max_sl_pips": 50,
        "min_rr": 1.5,
        "rsi_buy_zone": [40, 60],
        "best_patterns": ["EMA20_PULLBACK"],
        "hold_hours": [48, 120],
        "session": "LONDON",
    })

    # Step c: compute indicators on 4h data
    close = df_4h["close"]
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, 200) if len(close) >= 200 else _ema(close, len(close))
    rsi = _rsi(close)
    atr = _atr(df_4h)
    bb_upper, bb_mid, bb_lower = _bollinger_bands(close)

    # ATR momentum: current vs rolling 10-bar average
    rolling_atr_avg = atr.rolling(10).mean()
    current_atr = atr.iloc[-1]
    avg_atr = rolling_atr_avg.iloc[-1] if not pd.isna(rolling_atr_avg.iloc[-1]) else current_atr
    atr_momentum = current_atr / avg_atr if avg_atr > 0 else 1.0

    # Step d: detect pattern
    pattern, direction = _detect_pattern(
        df_4h, ema20, ema50, rsi, bb_upper, bb_lower, atr_momentum, profile
    )

    # Step e: daily trend alignment
    daily_close = df_daily["close"]
    daily_ema20 = _ema(daily_close, 20).iloc[-1]
    daily_ema50 = _ema(daily_close, 50).iloc[-1]
    daily_trend = "BULLISH" if daily_ema20 > daily_ema50 else "BEARISH"

    # If no strict pattern, use a conservative fallback trend setup.
    if pattern == "NONE":
        pattern, direction = _fallback_trend_pattern(
            float(ema20.iloc[-1]),
            float(ema50.iloc[-1]),
            float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0,
            daily_trend,
        )
        if pattern == "NONE":
            logger.debug("[FOREX] %s: no pattern detected", symbol)
            return None

    if direction == "LONG" and daily_trend != "BULLISH":
        logger.debug("[FOREX] %s: LONG blocked by bearish daily trend", symbol)
        return None
    if direction == "SHORT" and daily_trend != "BEARISH":
        logger.debug("[FOREX] %s: SHORT blocked by bullish daily trend", symbol)
        return None

    # Step f: calculate trade plan
    entry = float(close.iloc[-1])
    pip = get_pip_value(symbol)

    if direction == "LONG":
        sl = entry - (current_atr * 1.5)
        sl_distance = entry - sl
        rr_target = max(float(profile.get("min_rr", 1.5)), 1.5)
        tp1 = entry + (sl_distance * rr_target)
        tp2 = entry + (sl_distance * (rr_target + 1.0))
    else:
        sl = entry + (current_atr * 1.5)
        sl_distance = sl - entry
        rr_target = max(float(profile.get("min_rr", 1.5)), 1.5)
        tp1 = entry - (sl_distance * rr_target)
        tp2 = entry - (sl_distance * (rr_target + 1.0))

    sl_pips = sl_distance / pip
    max_sl_pips = float(profile.get("max_sl_pips", 50))
    if sl_pips > max_sl_pips:
        # Cap SL width rather than dropping the setup entirely.
        sl_distance = max_sl_pips * pip
        sl_pips = max_sl_pips
        if direction == "LONG":
            sl = entry - sl_distance
            tp1 = entry + (sl_distance * rr_target)
            tp2 = entry + (sl_distance * (rr_target + 1.0))
        else:
            sl = entry + sl_distance
            tp1 = entry - (sl_distance * rr_target)
            tp2 = entry - (sl_distance * (rr_target + 1.0))

    rr = (abs(tp1 - entry) / sl_distance) if sl_distance > 0 else 0.0
    rr_actual = abs(tp1 - entry) / sl_distance if sl_distance > 0 else 0.0
    if rr_actual < profile["min_rr"]:
        logger.debug("[FOREX] %s: R:R %.2f below min %.2f — skipping", symbol, rr_actual, profile["min_rr"])
        return None

    # Step g: strength score
    rsi_last = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0
    rsi_zone = profile.get("rsi_buy_zone", [40, 60])
    ema20_last = float(ema20.iloc[-1])
    ema50_last = float(ema50.iloc[-1])
    ema200_last = float(ema200.iloc[-1])

    strength = 50
    if rsi_zone[0] <= rsi_last <= rsi_zone[1]:
        strength += 15
    if daily_trend == "BULLISH" and direction == "LONG":
        strength += 15
    elif daily_trend == "BEARISH" and direction == "SHORT":
        strength += 15
    primary_session = profile.get("session", "LONDON")
    if session_active and session_name == primary_session:
        strength += 10
    # Full EMA stack
    if direction == "LONG" and ema20_last > ema50_last > ema200_last:
        strength += 10
    elif direction == "SHORT" and ema20_last < ema50_last < ema200_last:
        strength += 10
    if atr_momentum > 1.3:
        strength += 10
    if not session_active:
        strength -= 20
    if direction == "LONG" and rsi_last > 70:
        strength -= 15
    if direction == "SHORT" and rsi_last < 30:
        strength -= 15

    strength = max(0, min(100, strength))

    hold_hours = profile.get("hold_hours", [48, 120])

    return ForexSetup(
        symbol=symbol,
        direction=direction,
        pattern=pattern,
        entry=round(entry, 5),
        sl=round(sl, 5),
        tp1=round(tp1, 5),
        tp2=round(tp2, 5),
        sl_pips=round(sl_pips, 1),
        rr_ratio=round(rr_actual, 2),
        strength_score=strength,
        session_active=session_active,
        session_name=session_name,
        daily_trend=daily_trend,
        hold_hours_min=hold_hours[0],
        hold_hours_max=hold_hours[1],
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )


def scan_all_forex() -> list[ForexSetup]:
    """
    Run the forex scanner across all FOREX_SYMBOLS.

    Fetches 4h and daily OHLCV for each pair, runs scan_forex_setup,
    and returns results sorted by strength_score descending.

    Results are cached for FOREX_CACHE_MINUTES minutes.
    """
    global _scan_cache

    now_ts = time.time()
    cache_age = (now_ts - _scan_cache["fetched_at"]) / 60
    if _scan_cache["setups"] and cache_age < settings.FOREX_CACHE_MINUTES:
        logger.debug("[FOREX] scan_all_forex: cache hit (age=%.1fm)", cache_age)
        return _scan_cache["setups"]

    with _scan_lock:
        now_ts = time.time()
        cache_age = (now_ts - _scan_cache["fetched_at"]) / 60
        if _scan_cache["setups"] and cache_age < settings.FOREX_CACHE_MINUTES:
            return _scan_cache["setups"]

    logger.info("[FOREX] Running full forex scan across %d symbols", len(settings.FOREX_SYMBOLS))
    setups: list[ForexSetup] = []

    # Scan majors first under free-tier minute limits; INR pairs follow.
    ordered_symbols = sorted(settings.FOREX_SYMBOLS, key=lambda s: 1 if s.endswith("/INR") else 0)

    for symbol in ordered_symbols:
        try:
            df_4h = get_forex_ohlcv(symbol, "4h", outputsize=200)
            df_daily = get_forex_ohlcv(symbol, "1day", outputsize=100)
            setup = scan_forex_setup(symbol, df_4h, df_daily)
            if setup is not None:
                setups.append(setup)
                logger.info(
                    "[FOREX] %s %s setup: strength=%d, pattern=%s, R:R=%.2f",
                    symbol, setup.direction, setup.strength_score, setup.pattern, setup.rr_ratio,
                )
        except Exception as e:
            logger.warning("[FOREX] %s scan failed: %s", symbol, e)

    setups.sort(key=lambda s: s.strength_score, reverse=True)
    _scan_cache = {"setups": setups, "fetched_at": now_ts}
    logger.info("[FOREX] Scan complete: %d setups found", len(setups))
    return setups
