"""
SwingSpectrum — Multi-Day Swing Trading Pattern Scanner (v2).

Scans all tracked symbols for high-probability swing trade setups:
  1.  Pullback-to-EMA20 in uptrend / downtrend
  2.  20-Day Range Breakout / Breakdown (raised vol: 1.8x)
  3.  Bullish/Bearish RSI Divergence
  4.  Golden / Death Cross (EMA50 × EMA200)
  5.  Inside Bar Breakout (raised vol: 2.0x)
  6.  Bollinger Band Squeeze Breakout (raised vol: 1.5x)
  7.  52-Week High Proximity Breakout [NEW]
  8.  3-Candle Reversal at Support [NEW]
  9.  Post-Result Gap-Up Momentum [NEW]

Improvements over v1:
  - Multi-pattern confluence: same-symbol, same-direction patterns merge with capped strength bonus
  - Tiered price-band strength bonuses (₹50-₹200 = +20, ₹200-₹500 = +15, ₹500-₹1000 = +10)
  - Liquid-ity penalty: avg vol < 50k shares → -20 points
  - SL capped by price band (₹200: 6%, ₹500: 5%, ₹1000: 4%)
  - Minimum R:R gate (1.8x) — setups below are dropped
  - Fibonacci TP/SL alongside ATR-based targets
  - Sector momentum tag (TAILWIND / HEADWIND / NEUTRAL)

Data source: yFinance daily candles (1y history for EMA200 validity).
"""

import logging
from dataclasses import dataclass, asdict, replace
from typing import Optional

import numpy as np
import pandas as pd

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class SwingSetup:
    """A detected swing trading setup."""
    symbol: str
    pattern: str               # Pattern name
    direction: str             # "LONG" or "SHORT"
    strength: int              # 0-100 signal strength
    entry_zone: float          # Suggested entry price
    stop_loss: float           # Recommended SL (ATR or capped)
    target_1: float            # Primary target (ATR-based or Fib 38.2%)
    target_2: float            # Secondary target (ATR-based or Fib 61.8%)
    risk_reward: float         # Best R:R ratio
    timeframe: str             # "2-5 days", "5-10 days", etc.
    description: str           # Human-readable explanation
    indicators: dict           # Supporting indicator values
    # New v2 fields
    fib_tp1: float = 0.0       # Fibonacci 38.2% TP (LONG) / 61.8% (SHORT)
    fib_tp2: float = 0.0       # Fibonacci 61.8% TP (LONG) / 38.2% (SHORT)
    sl_capped: bool = False    # True if SL was wider than price-band cap
    sl_warning: str = ""       # "SL capped — reduce qty" if capped
    sector: str = ""           # Sector bucket from NSE_SECTOR_MAP
    sector_tag: str = ""       # "SECTOR TAILWIND 🟢" / "HEADWIND 🔴" / "NEUTRAL ⚪"
    setup_date: str = ""       # ISO date the setup was detected (for stale badge)
    data_as_of: str = ""       # ISO timestamp of last daily bar used for this scan
    data_stale: bool = False   # True if last bar exceeded SWING_MAX_BAR_AGE_DAYS (still returned if scanned)
    # Multi-pattern confluence (same-symbol merge in dedupe)
    confluence_count: int = 1           # How many patterns agreed (same direction)
    confluence_patterns: str = ""       # Semicolon-separated pattern names when merged


# ── Utility helpers ──────────────────────────────────────────────────────────


def _last_bar_timestamp(df: pd.DataFrame):
    """Return pandas Timestamp for the last row's timestamp column, or NaT."""
    if df is None or df.empty or "timestamp" not in df.columns:
        return pd.NaT
    return pd.to_datetime(df.iloc[-1]["timestamp"], errors="coerce")


def _bar_age_days(last_ts: pd.Timestamp) -> float:
    """Calendar age of last bar in days (naive vs naive, or tz-aware vs same tz)."""
    if last_ts is None or pd.isna(last_ts):
        return float("inf")
    now = pd.Timestamp.now(tz=last_ts.tz) if last_ts.tzinfo else pd.Timestamp.now()
    return max((now - last_ts).total_seconds() / 86400.0, 0.0)


def _data_as_of_iso(last_ts: pd.Timestamp) -> str:
    if last_ts is None or pd.isna(last_ts):
        return ""
    try:
        return last_ts.isoformat()
    except Exception:
        return str(last_ts)

def _price_band_bonus(close: float) -> int:
    """Tiered strength bonus based on price band (retail swing trade sweet spots)."""
    if 50 <= close < 200:
        return 20   # High volatility → big % moves possible
    elif 200 <= close < 500:
        return 15   # Sweet spot for retail swing trades
    elif 500 <= close < 1000:
        return 10   # Good liquidity + manageable lot size
    return 0        # No bonus above ₹1000


def _liquidity_penalty(df: pd.DataFrame) -> int:
    """Deduct points if average daily volume is below 50k shares (illiquid)."""
    if "volume" not in df.columns:
        return 0
    avg_vol = float(df["volume"].tail(20).mean())
    if avg_vol < settings.SWING_MIN_AVG_VOL_SHARES:
        return -settings.SWING_LOW_LIQIDITY_PENALTY
    return 0


def _cap_sl(entry: float, raw_sl: float, direction: str) -> tuple[float, bool, str]:
    """
    Cap stop-loss by price band. Returns (capped_sl, was_capped, warning_msg).
    If ATR-based SL is wider than the cap → tighten the SL and warn to reduce qty.
    """
    caps = settings.SWING_MAX_SL_PCT_BY_PRICE_BAND
    if entry < 200:
        max_pct = caps["under_200"]
    elif entry < 500:
        max_pct = caps["200_to_500"]
    elif entry < 1000:
        max_pct = caps["500_to_1000"]
    else:
        return raw_sl, False, ""  # No cap for stocks > ₹1000

    max_dist = entry * max_pct
    if direction == "LONG":
        actual_dist = entry - raw_sl
        if actual_dist > max_dist:
            return round(entry - max_dist, 2), True, "SL capped — reduce qty to maintain risk"
    else:
        actual_dist = raw_sl - entry
        if actual_dist > max_dist:
            return round(entry + max_dist, 2), True, "SL capped — reduce qty to maintain risk"
    return raw_sl, False, ""


def _fib_levels(df: pd.DataFrame, direction: str, entry: float = 0.0) -> tuple[float, float]:
    """
    Calculate Fibonacci TP levels from the 20-day swing range.
    Only returns values that sit on the correct side of *entry*:
      LONG targets must be > entry; SHORT targets must be < entry.
    Returns (fib_tp1, fib_tp2) or (0.0, 0.0) when nothing valid.
    """
    try:
        lookback = df.iloc[-20:]
        swing_high = float(lookback["high"].max())
        swing_low = float(lookback["low"].min())
        rng = swing_high - swing_low
        if rng <= 0:
            return 0.0, 0.0

        if direction == "LONG":
            tp1 = round(swing_low + rng * 0.618, 2)
            tp2 = round(swing_high, 2)
            if entry > 0:
                if tp1 <= entry:
                    tp1 = 0.0
                if tp2 <= entry:
                    tp2 = 0.0
        else:
            tp1 = round(swing_high - rng * 0.382, 2)
            tp2 = round(swing_high - rng * 0.618, 2)
            if entry > 0:
                if tp1 >= entry:
                    tp1 = 0.0
                if tp2 >= entry:
                    tp2 = 0.0
        return tp1, tp2
    except Exception:
        return 0.0, 0.0


def _meets_min_rr(entry: float, sl: float, tp1: float, direction: str) -> bool:
    """Check if the setup meets the minimum R:R gate (default 1.8)."""
    try:
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        if risk <= 0:
            return False
        return (reward / risk) >= settings.SWING_MIN_RISK_REWARD
    except Exception:
        return True  # Default: pass the gate if numbers are bad


def apply_swing_regime_gate(
    filtered: list[SwingSetup],
    market_context: dict | None,
) -> list[SwingSetup]:
    """Drop LONGs/SHORTs that fight the benchmark trend (used by scan + tests)."""
    if not filtered or not market_context:
        return filtered
    if not getattr(settings, "SWING_REGIME_GATE_ENABLED", True):
        return filtered
    trend = str(market_context.get("index_trend") or "NEUTRAL").upper()
    out = list(filtered)
    if getattr(settings, "SWING_BLOCK_LONG_IN_BEARISH_TREND", True) and trend == "BEARISH":
        out = [s for s in out if s.direction != "LONG"]
    if getattr(settings, "SWING_BLOCK_SHORT_IN_BULLISH_TREND", False) and trend == "BULLISH":
        out = [s for s in out if s.direction != "SHORT"]
    return out


def _pick_confluence_direction_group(
    longs: list[SwingSetup],
    shorts: list[SwingSetup],
) -> list[SwingSetup]:
    """When LONG and SHORT both exist for one symbol, pick one side to merge."""
    if not longs:
        return shorts
    if not shorts:
        return longs
    max_l = max(s.strength for s in longs)
    max_s = max(s.strength for s in shorts)
    if max_l > max_s:
        return longs
    if max_s > max_l:
        return shorts
    if len(longs) != len(shorts):
        return longs if len(longs) > len(shorts) else shorts
    sum_l = sum(s.strength for s in longs)
    sum_s = sum(s.strength for s in shorts)
    return longs if sum_l >= sum_s else shorts


def _merge_confluent_group(group: list[SwingSetup]) -> SwingSetup:
    """Merge same-direction setups: primary = best (strength, R:R), bonus for extra agreeing patterns."""
    group = sorted(group, key=lambda s: (-s.strength, -s.risk_reward))
    primary = group[0]
    n = len(group)
    patterns = [s.pattern for s in group]
    pat_joined = "; ".join(patterns)

    if n == 1:
        ind = dict(primary.indicators) if primary.indicators else {}
        ind.setdefault("confluence_count", 1)
        ind.setdefault("confluence_patterns", primary.pattern)
        return replace(
            primary,
            confluence_count=1,
            confluence_patterns=primary.pattern,
            indicators=ind,
        )

    per = int(getattr(settings, "SWING_CONFLUENCE_BONUS_PER_EXTRA", 5))
    cap = int(getattr(settings, "SWING_CONFLUENCE_MAX_BONUS", 15))
    bonus = min(cap, per * (n - 1))
    new_strength = min(100, primary.strength + bonus)
    label = f"{primary.pattern} (+{n - 1} confluent)"
    ind = dict(primary.indicators) if primary.indicators else {}
    ind["confluence_count"] = n
    ind["confluence_patterns"] = pat_joined
    desc = f"{primary.description} [Confluence ×{n}: {pat_joined}]"
    return replace(
        primary,
        strength=new_strength,
        pattern=label,
        confluence_count=n,
        confluence_patterns=pat_joined,
        description=desc,
        indicators=ind,
    )


def dedupe_swing_setups_one_per_symbol(filtered: list[SwingSetup]) -> list[SwingSetup]:
    """One row per symbol: merge multi-pattern confluence or pick strongest (legacy)."""
    if not filtered:
        return []
    if not getattr(settings, "SWING_DEDUPE_ONE_PER_SYMBOL", True):
        return filtered
    if len(filtered) == 1:
        return [_merge_confluent_group(filtered)]

    if not getattr(settings, "SWING_CONFLUENCE_ENABLED", True):
        return [max(filtered, key=lambda s: (s.strength, s.risk_reward))]

    longs = [s for s in filtered if s.direction == "LONG"]
    shorts = [s for s in filtered if s.direction == "SHORT"]
    chosen = _pick_confluence_direction_group(longs, shorts)
    return [_merge_confluent_group(chosen)]


# ── Main scan entry point ─────────────────────────────────────────────────────

def scan_swing_setups(
    df: pd.DataFrame,
    symbol: str = "",
    sector_momentum: float = 0.0,
    sector: str = "",
    market_context: dict | None = None,
) -> list[SwingSetup]:
    """
    Scan a daily OHLCV+indicators DataFrame for swing trading setups.

    Args:
        df: Daily OHLCV DataFrame with indicators (1y lookback recommended).
        symbol: Display name of the symbol (for logging).
        sector_momentum: Pre-computed 5-day % change avg across same sector symbols.
        sector: Sector bucket string from NSE_SECTOR_MAP.
        market_context: Optional index trend/regime from the API layer for LONG/SHORT gating.

    Returns list of SwingSetup objects, sorted by strength (strongest first).
    Only setups with R:R >= SWING_MIN_RISK_REWARD are included.
    """
    if df is None or df.empty or len(df) < 30:
        return []

    last_ts = _last_bar_timestamp(df)
    data_as_of_str = _data_as_of_iso(last_ts)
    max_age = float(getattr(settings, "SWING_MAX_BAR_AGE_DAYS", 0) or 0)
    if max_age > 0:
        if pd.isna(last_ts):
            logger.debug("Swing skip %s: invalid or missing last-bar timestamp", symbol)
            return []
        age_days = _bar_age_days(last_ts)
        if age_days > max_age:
            logger.debug(
                "Swing skip %s: last bar age %.1f d > max %.1f d",
                symbol,
                age_days,
                max_age,
            )
            return []

    setups: list[SwingSetup] = []

    try:
        required = ["close", "high", "low", "RSI_14", "EMA_20", "EMA_50", "ATR_14"]
        if not all(c in df.columns for c in required):
            logger.debug("Missing required columns for swing scan on %s", symbol)
            return []

        last = df.iloc[-1]
        atr = float(last["ATR_14"]) if pd.notna(last.get("ATR_14")) else 0
        close = float(last["close"])

        if atr <= 0 or close <= 0:
            return []

        # Run all pattern checks
        for checker in [
            _check_pullback_to_ema,
            _check_range_breakout,
            _check_rsi_divergence,
            _check_ema_cross,
            _check_inside_bar,
            _check_bb_squeeze,
            _check_52w_high_breakout,
            _check_support_reversal,
            _check_post_result_gap,
        ]:
            try:
                setup = checker(df, symbol, atr)
                if setup:
                    setups.append(setup)
            except Exception as e:
                logger.debug("%s failed for %s: %s", checker.__name__, symbol, e)

    except Exception as exc:
        logger.error("Swing scan failed for %s: %s", symbol, exc)
        return []

    # ── Fix 1: Tiered price-band bonus + liquidity penalty ────────────────
    price_bonus = _price_band_bonus(close)
    liq_penalty = _liquidity_penalty(df)

    # ── Fix 4: Add Fibonacci levels to every setup ────────────────────────
    # ── Fix 6: Apply SL caps + R:R gate ──────────────────────────────────
    # ── Fix 5: Apply sector momentum tag ─────────────────────────────────
    if sector_momentum > 1.5:
        sector_tag = "SECTOR TAILWIND 🟢"
        sector_str_bonus = 15
    elif sector_momentum < -1.5:
        sector_tag = "SECTOR HEADWIND 🔴"
        sector_str_bonus = -20  # Applied only to LONG setups below
    else:
        sector_tag = "SECTOR NEUTRAL ⚪"
        sector_str_bonus = 0

    import datetime
    today_str = datetime.date.today().isoformat()

    filtered: list[SwingSetup] = []
    for setup in setups:
        # Price-band bonus + liquidity penalty
        setup.strength = max(0, min(100, setup.strength + price_bonus + liq_penalty))

        # Sector momentum
        setup.sector = sector
        setup.sector_tag = sector_tag
        if sector_momentum < -1.5 and setup.direction == "LONG":
            setup.strength = max(0, setup.strength + sector_str_bonus)
        elif sector_momentum > 1.5:
            setup.strength = min(100, setup.strength + sector_str_bonus)

        # SL cap
        capped_sl, was_capped, warn = _cap_sl(setup.entry_zone, setup.stop_loss, setup.direction)
        setup.stop_loss = capped_sl
        setup.sl_capped = was_capped
        setup.sl_warning = warn

        # Fibonacci targets (validated against entry side)
        fib1, fib2 = _fib_levels(df, setup.direction, entry=setup.entry_zone)
        setup.fib_tp1 = fib1
        setup.fib_tp2 = fib2
        if setup.direction == "LONG":
            if fib1 > 0 and fib1 > setup.entry_zone:
                setup.target_1 = min(setup.target_1, fib1)
            if fib2 > 0 and fib2 > setup.entry_zone:
                setup.target_2 = max(setup.target_2, fib2)
        else:
            if fib1 > 0 and fib1 < setup.entry_zone:
                setup.target_1 = max(setup.target_1, fib1)
            if fib2 > 0 and fib2 < setup.entry_zone:
                setup.target_2 = min(setup.target_2, fib2)

        # Final sanity: targets/SL must be on the correct side of entry
        e = setup.entry_zone
        if setup.direction == "LONG":
            if setup.stop_loss >= e:
                setup.stop_loss = round(e - atr * 2, 2)
            if setup.target_1 <= e:
                setup.target_1 = round(e + atr * 4, 2)
            if setup.target_2 <= e:
                setup.target_2 = round(e + atr * 6, 2)
        else:
            if setup.stop_loss <= e:
                setup.stop_loss = round(e + atr * 2, 2)
            if setup.target_1 >= e:
                setup.target_1 = round(e - atr * 4, 2)
            if setup.target_2 >= e:
                setup.target_2 = round(e - atr * 6, 2)

        # Recalculate R:R after all adjustments
        risk = abs(setup.entry_zone - setup.stop_loss)
        if risk > 0:
            setup.risk_reward = round(abs(setup.target_1 - setup.entry_zone) / risk, 1)

        # R:R gate — drop setups below minimum
        if not _meets_min_rr(setup.entry_zone, setup.stop_loss, setup.target_1, setup.direction):
            logger.debug("Dropping %s %s: R:R %.1f < %.1f",
                         symbol, setup.pattern, setup.risk_reward, settings.SWING_MIN_RISK_REWARD)
            continue

        setup.setup_date = today_str
        setup.data_as_of = data_as_of_str
        setup.data_stale = False
        filtered.append(setup)

    filtered = apply_swing_regime_gate(filtered, market_context)
    filtered = dedupe_swing_setups_one_per_symbol(filtered)

    filtered.sort(key=lambda s: s.strength, reverse=True)
    return filtered


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

    # Check if EMA200 exists and is meaningful (needs 200+ bars of data)
    _raw_ema200 = last.get("EMA_200")
    has_ema200 = pd.notna(_raw_ema200) and _raw_ema200 != 0
    ema200 = float(_raw_ema200) if has_ema200 else ema50

    # LONG: Uptrend pullback
    if ema20 > ema50:
        # Price within 1.5% of EMA20 (touching the moving average)
        proximity = abs(close - ema20) / ema20 * 100
        if proximity < 1.5 and close > ema50:
            # Check bounce: last bar closed green or above previous low
            bouncing = close > float(df.iloc[-2]["close"]) or float(last["low"]) > float(df.iloc[-2]["low"])
            # Volume check: must show at least minimum interest (0.8x avg)
            vol = float(last.get("volume", 0))
            vol_avg = float(df["volume"].tail(20).mean()) if "volume" in df.columns else 1
            vol_ok = vol_avg <= 0 or (vol / max(vol_avg, 1)) >= settings.SWING_VOL_EMA_PULLBACK
            if bouncing and vol_ok:
                sl = close - atr * 2
                tp1 = close + atr * 4
                tp2 = close + atr * 6

                strength = 60
                if has_ema200 and ema50 > ema200:  # Super uptrend (only when EMA200 is valid)
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
                                "ema50": round(ema50, 2), "proximity_pct": round(proximity, 2),
                                "vol_ratio": round(vol / max(vol_avg, 1), 2)},
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
                if has_ema200 and ema50 < ema200:  # Super downtrend (only when EMA200 is valid)
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

    # Volume confirmation: raised from 1.3x to 1.8x for mid/small NSE stocks
    vol_confirmed = vol_avg > 0 and vol >= vol_avg * settings.SWING_VOL_RANGE_BREAKOUT

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
    if len(df) < 25:
        return None

    close = df["close"].values
    rsi = df["RSI_14"].values if "RSI_14" in df.columns else None
    if rsi is None:
        return None

    last_close = float(close[-1])

    # Compare last 7 bars vs 12-18 bars ago (wider windows for daily data)
    recent_window = slice(-7, None)
    earlier_window = slice(-18, -10)

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

    # Need at least 200 bars for EMA_200 to be meaningful
    ema200_valid = df["EMA_200"].dropna()
    if len(ema200_valid) < 10:
        return None

    for i in range(-5, 0):  # Look back 5 bars (was 3, too narrow for daily)
        try:
            curr_50 = float(df["EMA_50"].iloc[i])
            curr_200 = float(df["EMA_200"].iloc[i])
            prev_50 = float(df["EMA_50"].iloc[i - 1])
            prev_200 = float(df["EMA_200"].iloc[i - 1])
        except (IndexError, ValueError):
            continue

        if pd.isna(curr_50) or pd.isna(curr_200) or pd.isna(prev_50) or pd.isna(prev_200):
            continue
        if curr_200 == 0 or prev_200 == 0:
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
            vol = float(df.iloc[-1].get("volume", 0))
            vol_avg = float(df["volume"].tail(20).mean()) if "volume" in df.columns else 0
            vol_confirmed = vol_avg <= 0 or vol >= vol_avg * settings.SWING_VOL_INSIDE_BAR
            # Breakout direction
            if close > mother_high:
                sl = mother_low
                tp1 = close + (mother_high - mother_low)
                tp2 = close + (mother_high - mother_low) * 1.5
                strength = min(90, 60 + inside_count * 10)
                if vol_confirmed:
                    strength = min(95, strength + 10)
                return SwingSetup(
                    symbol=symbol,
                    pattern=f"Inside Bar Breakout ({inside_count} bars)",
                    direction="LONG",
                    strength=strength,
                    entry_zone=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(tp1, 2),
                    target_2=round(tp2, 2),
                    risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
                    timeframe="2-5 days",
                    description=f"{inside_count} inside bars. Bullish breakout above ₹{mother_high:.0f}. "
                                f"{'Volume confirmed ✓' if vol_confirmed else 'Volume weak ⚠️'}.",
                    indicators={"mother_high": round(mother_high, 2), "mother_low": round(mother_low, 2),
                                "inside_bars": inside_count, "vol_ratio": round(vol / max(vol_avg, 1), 1)},
                )
            elif close < mother_low:
                sl = mother_high
                tp1 = close - (mother_high - mother_low)
                tp2 = close - (mother_high - mother_low) * 1.5
                strength = min(90, 60 + inside_count * 10)
                if vol_confirmed:
                    strength = min(95, strength + 10)
                return SwingSetup(
                    symbol=symbol,
                    pattern=f"Inside Bar Breakdown ({inside_count} bars)",
                    direction="SHORT",
                    strength=strength,
                    entry_zone=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(tp1, 2),
                    target_2=round(tp2, 2),
                    risk_reward=round((close - tp1) / max(sl - close, 0.01), 1),
                    timeframe="2-5 days",
                    description=f"{inside_count} inside bars. Bearish breakdown below ₹{mother_low:.0f}.",
                    indicators={"mother_high": round(mother_high, 2), "mother_low": round(mother_low, 2),
                                "inside_bars": inside_count, "vol_ratio": round(vol / max(vol_avg, 1), 1)},
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
    was_squeezed = min_width < avg_width * 0.7  # Width was 70% below average (relaxed for real-world conditions)

    if was_squeezed and is_expanding:
        close = float(df.iloc[-1]["close"])
        vol = float(df.iloc[-1].get("volume", 0))
        vol_avg = float(df["volume"].tail(20).mean()) if "volume" in df.columns else 0
        vol_confirmed = vol_avg <= 0 or vol >= vol_avg * settings.SWING_VOL_BB_SQUEEZE
        bb_upper = float(df.iloc[-1].get("BB_upper", close + atr))
        bb_lower = float(df.iloc[-1].get("BB_lower", close - atr))

        # Breakout direction
        if close > bb_upper * 0.998 and vol_confirmed:  # Near or above upper band
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
                description="Bollinger Bands squeezed to 20-day low and now expanding upward (vol ✓). "
                            "Expect a strong directional move.",
                indicators={"bb_width": round(current_width, 4), "avg_width": round(avg_width, 4),
                            "squeeze_ratio": round(min_width / max(avg_width, 0.001), 2),
                            "vol_ratio": round(vol / max(vol_avg, 1), 1)},
            )
        elif close < bb_lower * 1.002 and vol_confirmed:
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
                description="BB Squeeze + bearish expansion (vol ✓). Expect continuation lower.",
                indicators={"bb_width": round(current_width, 4), "avg_width": round(avg_width, 4),
                            "vol_ratio": round(vol / max(vol_avg, 1), 1)},
            )
    return None


# ── Pattern 7: 52-Week High Proximity Breakout ────────────────────────────────

def _check_52w_high_breakout(df: pd.DataFrame, symbol: str, atr: float) -> Optional[SwingSetup]:
    """
    When price is within 3% of 52-week high with volume surge + healthy RSI.
    Mid-cap NSE stocks near their 52W high often signal fresh institutional buying,
    not an overbought condition.
    """
    if len(df) < 50:
        return None

    close = float(df.iloc[-1]["close"])
    rsi = float(df.iloc[-1].get("RSI_14", 50) or 50)
    ema20 = float(df.iloc[-1].get("EMA_20", 0) or 0)
    ema50 = float(df.iloc[-1].get("EMA_50", 0) or 0)
    vol = float(df.iloc[-1].get("volume", 0))
    vol_avg = float(df["volume"].tail(20).mean()) if "volume" in df.columns else 0

    # 52-week high from available history (up to 252 bars)
    lookback_52w = df.iloc[-min(252, len(df)):]
    high_52w = float(lookback_52w["high"].max())

    if high_52w <= 0:
        return None

    proximity_pct = (high_52w - close) / high_52w * 100  # % below 52W high

    # Conditions
    within_3pct = proximity_pct <= 3.0
    vol_strong = vol_avg > 0 and vol >= vol_avg * settings.SWING_VOL_52W_BREAKOUT
    rsi_healthy = 55 <= rsi <= 75
    trend_intact = ema20 > ema50 if ema20 > 0 and ema50 > 0 else True

    if not (within_3pct and vol_strong and rsi_healthy and trend_intact):
        return None

    sl = high_52w * 0.96
    tp1 = high_52w * 1.05
    tp2 = high_52w * 1.12

    strength = 65
    if proximity_pct < 1.0:
        strength += 15  # Almost touching or breaking 52W high
    if rsi > 65:
        strength += 5
    strength = min(90, strength + 20)  # Pattern bonus

    return SwingSetup(
        symbol=symbol,
        pattern="52-Week High Proximity Breakout",
        direction="LONG",
        strength=min(95, strength),
        entry_zone=round(close, 2),
        stop_loss=round(sl, 2),
        target_1=round(tp1, 2),
        target_2=round(tp2, 2),
        risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
        timeframe="5-10 days",
        description=f"Within {proximity_pct:.1f}% of 52W high ₹{high_52w:.0f}. "
                    f"Strong volume + healthy RSI at {rsi:.0f}. Institutional accumulation signal.",
        indicators={"high_52w": round(high_52w, 2), "proximity_pct": round(proximity_pct, 2),
                    "rsi": round(rsi, 1), "vol_ratio": round(vol / max(vol_avg, 1), 1)},
    )


# ── Pattern 8: 3-Candle Reversal at Support ───────────────────────────────────

def _check_support_reversal(df: pd.DataFrame, symbol: str, atr: float) -> Optional[SwingSetup]:
    """
    3 consecutive daily closes above a support zone, with shrinking sell volume.
    Best accumulation signal for mid-cap NSE stocks.
    """
    if len(df) < 10:
        return None

    close = float(df.iloc[-1]["close"])
    rsi = float(df.iloc[-1].get("RSI_14", 50) or 50)

    # Look for support in the dataframe
    support = df.iloc[-1].get("support")
    if support is None or not pd.notna(support):
        return None
    support = float(support)

    if support <= 0:
        return None

    # Within 2% of support
    proximity_pct = abs(close - support) / support * 100
    if proximity_pct > 2.0:
        return None

    # RSI in oversold territory (below 45)
    if rsi >= 45:
        return None

    # Last 3 bars must each close higher
    if len(df) < 4:
        return None
    closes_3 = [float(df.iloc[-3]["close"]), float(df.iloc[-2]["close"]), float(df.iloc[-1]["close"])]
    ascending = closes_3[0] < closes_3[1] < closes_3[2]
    if not ascending:
        return None

    # Volume pattern: shrinking (day1 > day2 > day3 — selling drying up)
    vols_3 = [float(df.iloc[-3].get("volume", 0)), float(df.iloc[-2].get("volume", 0)), float(df.iloc[-1].get("volume", 0))]
    vol_shrinking = vols_3[0] > 0 and vols_3[1] > 0 and vols_3[0] >= vols_3[1] >= vols_3[2]

    sl = support * 0.98  # 2% below support
    # Fibonacci TP from prior down-move
    prior_high = float(df.iloc[-20:]["high"].max()) if len(df) >= 20 else close * 1.10
    down_range = prior_high - support
    fib_38 = support + down_range * 0.382
    fib_62 = support + down_range * 0.618

    strength = 60
    if vol_shrinking:
        strength += 18
    if rsi < 35:
        strength += 10
    strength = min(95, strength)

    return SwingSetup(
        symbol=symbol,
        pattern="3-Candle Reversal at Support",
        direction="LONG",
        strength=strength,
        entry_zone=round(close, 2),
        stop_loss=round(sl, 2),
        target_1=round(fib_38, 2),
        target_2=round(fib_62, 2),
        risk_reward=round((fib_38 - close) / max(close - sl, 0.01), 1),
        timeframe="3-7 days",
        description=f"3 consecutive closes above support ₹{support:.0f}. "
                    f"{'Volume drying up ✓ ' if vol_shrinking else ''}RSI at {rsi:.0f} — oversold.",
        indicators={"support": round(support, 2), "rsi": round(rsi, 1),
                    "fib_38": round(fib_38, 2), "fib_62": round(fib_62, 2),
                    "vol_shrinking": vol_shrinking},
    )


# ── Pattern 9: Post-Result Gap-Up Momentum ────────────────────────────────────

def _check_post_result_gap(df: pd.DataFrame, symbol: str, atr: float) -> Optional[SwingSetup]:
    """
    Gap up 3-7% on high volume (earnings/results catalyst).
    If the gap holds for 2 bars, it sets up a momentum continuation.
    """
    if len(df) < 5:
        return None

    # Look at the most recent bars for a gap-up event
    for i in [-3, -2]:  # Gap up could have been 1-2 bars ago
        try:
            gap_open = float(df.iloc[i]["open"])
            prev_close = float(df.iloc[i - 1]["close"])
            gap_close = float(df.iloc[i]["close"])
            gap_vol = float(df.iloc[i].get("volume", 0))
            vol_avg = float(df["volume"].tail(20).mean()) if "volume" in df.columns else 0
        except (IndexError, KeyError):
            continue

        if prev_close <= 0:
            continue

        gap_pct = (gap_open - prev_close) / prev_close * 100

        # Must be a 3-7% gap up
        if not (3.0 <= gap_pct <= 7.0):
            continue

        # Gap must be held (close >= 99% of open — not filled)
        gap_held = gap_close >= gap_open * 0.99

        # Volume on gap day must be > 3x average
        vol_strong = vol_avg > 0 and gap_vol >= vol_avg * settings.SWING_VOL_POST_RESULT

        if not (gap_held and vol_strong):
            continue

        # Current price must still be above the gap
        current_close = float(df.iloc[-1]["close"])
        if current_close < prev_close:  # Gap filled → setup invalid
            continue

        sl = prev_close  # If gap fills → exit
        tp1 = round(gap_close * 1.06, 2)
        tp2 = round(gap_close * 1.12, 2)

        strength = min(95, 70 + 25)  # Pattern bonus = +25 (highest — earnings catalyst)

        return SwingSetup(
            symbol=symbol,
            pattern="Post-Result Gap-Up Momentum",
            direction="LONG",
            strength=strength,
            entry_zone=round(current_close, 2),
            stop_loss=round(sl, 2),
            target_1=tp1,
            target_2=tp2,
            risk_reward=round((tp1 - current_close) / max(current_close - sl, 0.01), 1),
            timeframe="5-15 days",
            description=f"Gap up {gap_pct:.1f}% on {round(gap_vol / max(vol_avg, 1), 1)}x avg volume. "
                        f"Gap held — momentum continuation expected.",
            indicators={"gap_pct": round(gap_pct, 1), "prev_close": round(prev_close, 2),
                        "gap_close": round(gap_close, 2),
                        "vol_ratio": round(gap_vol / max(vol_avg, 1), 1)},
        )
    return None


# ── Portfolio scan with sector momentum (Fix 5) ───────────────────────────────

def scan_all_symbols(
    symbol_data: dict[str, pd.DataFrame],
    market_context: dict | None = None,
) -> list[dict]:
    """
    Scan all symbols for swing setups with sector momentum context.

    Args:
        symbol_data: {display_name: daily_df_with_indicators}
        market_context: Optional index trend/regime for per-symbol regime gating.

    Returns:
        List of dicts (JSON-serializable swing setups), sorted by strength.
    """
    from config.settings import NSE_SECTOR_MAP

    # ── Pre-compute sector 5-day momentum ──────────────────────────────────
    # For each sector, compute the average 5-day % change of its member symbols.
    sector_5d: dict[str, list[float]] = {}
    for sym, df in symbol_data.items():
        sector = NSE_SECTOR_MAP.get(sym, "Other")
        if sector.startswith("_"):  # Skip indices
            continue
        if df is None or df.empty or len(df) < 6:
            continue
        try:
            pct_5d = (float(df.iloc[-1]["close"]) - float(df.iloc[-6]["close"])) / \
                     max(float(df.iloc[-6]["close"]), 0.01) * 100
            sector_5d.setdefault(sector, []).append(pct_5d)
        except Exception:
            pass

    sector_momentum_map: dict[str, float] = {
        sec: round(sum(vals) / len(vals), 2)
        for sec, vals in sector_5d.items() if vals
    }

    # ── Scan each symbol ───────────────────────────────────────────────────
    all_setups = []
    for symbol, df in symbol_data.items():
        sector = NSE_SECTOR_MAP.get(symbol, "Other")
        momentum = sector_momentum_map.get(sector, 0.0)
        setups = scan_swing_setups(
            df,
            symbol,
            sector_momentum=momentum,
            sector=sector,
            market_context=market_context,
        )
        for s in setups:
            all_setups.append(asdict(s))

    all_setups.sort(key=lambda x: x["strength"], reverse=True)
    return all_setups

