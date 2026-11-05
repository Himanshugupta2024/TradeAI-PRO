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
from dataclasses import asdict, dataclass, replace
from typing import Optional

import numpy as np
import pandas as pd

from config import settings
from risk.swing_risk import (
    calculate_swing_position_size,
    calculate_swing_sl_tp,
    overnight_risk_check,
    update_trailing_sl,
)

logger = logging.getLogger(__name__)


@dataclass
class SwingSetup:
    """A detected swing trading setup."""

    symbol: str
    pattern: str  # Pattern name
    direction: str  # "LONG" or "SHORT"
    strength: int  # 0-100 signal strength
    entry_zone: float  # Suggested entry price
    stop_loss: float  # Recommended SL (ATR or capped)
    target_1: float  # Primary target (ATR-based or Fib 38.2%)
    target_2: float  # Secondary target (ATR-based or Fib 61.8%)
    risk_reward: float  # Best R:R ratio
    timeframe: str  # "2-5 days", "5-10 days", etc.
    description: str  # Human-readable explanation
    indicators: dict  # Supporting indicator values
    # New v2 fields
    fib_tp1: float = 0.0  # Fibonacci 38.2% TP (LONG) / 61.8% (SHORT)
    fib_tp2: float = 0.0  # Fibonacci 61.8% TP (LONG) / 38.2% (SHORT)
    sl_capped: bool = False  # True if SL was wider than price-band cap
    sl_warning: str = ""  # "SL capped — reduce qty" if capped
    sector: str = ""  # Sector bucket from NSE_SECTOR_MAP
    sector_tag: str = ""  # "SECTOR TAILWIND 🟢" / "HEADWIND 🔴" / "NEUTRAL ⚪"
    setup_date: str = ""  # ISO date the setup was detected (for stale badge)
    data_as_of: str = ""  # ISO timestamp of last daily bar used for this scan
    data_stale: bool = False  # True if last bar exceeded SWING_MAX_BAR_AGE_DAYS (still returned if scanned)
    # Multi-pattern confluence (same-symbol merge in dedupe)
    confluence_count: int = 1  # How many patterns agreed (same direction)
    confluence_patterns: str = ""  # Semicolon-separated pattern names when merged
    # ── v3 Risk & Quality Fields ──────────────────────────────────────────────
    grade: str = "C"  # Signal grade: A (high conviction) / B (moderate) / C (watchlist)
    structural_sl: float = (
        0.0  # SL anchored to swing high/low (more reliable than ATR-only)
    )
    news_score: float = (
        0.0  # Context score from news (-1.0 to 1.0); negative = penalty applied
    )
    news_penalty_applied: bool = (
        False  # True if strength was reduced due to negative news
    )
    entry_note: str = ""  # Entry guidance e.g. "Wait for T+1 candle confirmation"
    confirm_next_day: bool = (
        True  # Always True for swing — entry on next bar open, not same-day close
    )
    adx_value: float = 0.0  # ADX at signal time — trend strength context
    # Execution-facing aliases and risk transparency fields.
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    qty: int = 0
    atr: float = 0.0
    score: float = 0.0
    capital_used: float = 0.0
    risk_amount: float = 0.0
    risk_pct: float = 0.0
    capital_pct: float = 0.0
    overnight_ok: bool = True
    risk_reason: str = ""
    trailing_sl: float = 0.0
    trailing_action: str = ""
    # ── 1H MTF Confirmation ──────────────────────────────────────────────────
    mtf_confirmed: bool = True  # False if 1H indicators oppose the LONG setup
    mtf_reason: str = ""  # Human-readable reason from _check_1h_mtf_bullish
    # ── Calibrated Win-Rate & Setup Strength Framing ────────────────────────
    setup_strength_display: str = ""
    calibrated_win_rate_pct: float = 0.0
    win_rate_expectation_bucket: str = ""
    confidence_disclaimer: str = (
        "Setup Strength reflects technical pattern confluence (0-100), not a guaranteed win probability. Risk management and SL discipline are mandatory."
    )

    def __post_init__(self):
        st = int(getattr(self, "strength", 50) or 50)
        if st >= 80:
            c_rate, bucket = 64.0, "80-100 Strength: ~64% Historical Win-Rate Expectation"
        elif st >= 66:
            c_rate, bucket = 56.0, "66-79 Strength: ~56% Historical Win-Rate Expectation"
        elif st >= 50:
            c_rate, bucket = 48.0, "50-65 Strength: ~48% Historical Win-Rate Expectation"
        else:
            c_rate, bucket = 40.0, "<50 Strength: ~40% Watchlist / Speculative"

        if not self.setup_strength_display:
            self.setup_strength_display = f"Setup Strength: {st}/100"
        if not self.calibrated_win_rate_pct:
            self.calibrated_win_rate_pct = c_rate
        if not self.win_rate_expectation_bucket:
            self.win_rate_expectation_bucket = bucket


def assign_calibrated_winrate(s: SwingSetup) -> SwingSetup:
    """Map setup strength confluence score to forward outcome calibrated win-rate expectations."""
    st = int(getattr(s, "strength", 50) or 50)
    if st >= 80:
        c_rate, bucket = 64.0, "80-100 Strength: ~64% Historical Win-Rate Expectation"
    elif st >= 66:
        c_rate, bucket = 56.0, "66-79 Strength: ~56% Historical Win-Rate Expectation"
    elif st >= 50:
        c_rate, bucket = 48.0, "50-65 Strength: ~48% Historical Win-Rate Expectation"
    else:
        c_rate, bucket = 40.0, "<50 Strength: ~40% Watchlist / Speculative"

    s.setup_strength_display = f"Setup Strength: {st}/100"
    s.calibrated_win_rate_pct = c_rate
    s.win_rate_expectation_bucket = bucket
    return s


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


def swing_price_history_context(df: pd.DataFrame, close: float) -> dict:
    """Where price sits vs ~4 weeks (20 sessions) and vs full *loaded* daily history.

    ``history_*`` values are max/min over whatever OHLCV is in ``df`` (typically ~1y
    from yFinance). They are **not** guaranteed NSE listing all-time high/low unless
    the upstream feed returns full listing history.
    """
    out: dict = {}
    if df is None or df.empty:
        return out
    if "high" not in df.columns or "low" not in df.columns:
        return out
    try:
        n = int(len(df))
        hh = float(df["high"].max())
        ll = float(df["low"].min())
        out["history_high_loaded"] = round(hh, 2)
        out["history_low_loaded"] = round(ll, 2)
        out["history_bars"] = n
        if hh > 0 and close > 0:
            out["pct_below_history_high"] = round((hh - close) / hh * 100.0, 2)
        if ll > 0 and close > 0:
            out["pct_above_history_low"] = round((close - ll) / ll * 100.0, 2)
        win = min(20, n)  # ~4 trading weeks
        tail = df.iloc[-win:]
        h20 = float(tail["high"].max())
        l20 = float(tail["low"].min())
        out["high_20d"] = round(h20, 2)
        out["low_20d"] = round(l20, 2)
        if h20 > 0:
            out["pct_from_20d_high"] = round((h20 - close) / h20 * 100.0, 2)
        if l20 > 0:
            out["pct_from_20d_low"] = round((close - l20) / l20 * 100.0, 2)
        out["price_context_scope"] = (
            "history_* = max/min over loaded daily bars only (not exchange ATH/ATL)"
        )
    except Exception:
        return {}
    return out


def _normalize_trend_label(raw: str | None) -> str:
    txt = str(raw or "").strip().upper()
    if txt in {"BULL", "BULLISH", "UP", "LONG", "BUY"}:
        return "BULLISH"
    if txt in {"BEAR", "BEARISH", "DOWN", "SHORT", "SELL"}:
        return "BEARISH"
    if txt in {"SIDEWAYS", "RANGE", "RANGING", "FLAT", "NEUTRAL", "NONE"}:
        return "NEUTRAL"
    return "UNKNOWN"


def _resolve_mtf_trend(df: pd.DataFrame, mtf_hint: str | None = None) -> str:
    hint = _normalize_trend_label(mtf_hint)
    if hint != "UNKNOWN":
        return hint

    if df is None or df.empty:
        return "UNKNOWN"

    if "htf_trend" in df.columns:
        htf = df["htf_trend"].dropna()
        if not htf.empty:
            if hasattr(htf.iloc[-1], "value"):
                return _normalize_trend_label(getattr(htf.iloc[-1], "value", ""))
            return _normalize_trend_label(str(htf.iloc[-1]))

    if "mtf_score" in df.columns:
        score = pd.to_numeric(df["mtf_score"], errors="coerce").dropna()
        if not score.empty:
            v = float(score.iloc[-1])
            neutral_band = float(
                getattr(settings, "SWING_MTF_SCORE_NEUTRAL_BAND", 15.0) or 15.0
            )
            if v > neutral_band:
                return "BULLISH"
            if v < -neutral_band:
                return "BEARISH"
            return "NEUTRAL"

    return "UNKNOWN"


def _mtf_confirms_direction(direction: str, mtf_trend: str) -> tuple[bool, str]:
    if not bool(getattr(settings, "SWING_MTF_CONFIRM_ENABLED", True)):
        return True, "disabled"

    trend = _normalize_trend_label(mtf_trend)
    if trend == "UNKNOWN":
        block_unknown = bool(getattr(settings, "SWING_MTF_BLOCK_ON_UNKNOWN", False))
        return (not block_unknown), "unknown"

    if trend == "NEUTRAL":
        allow_neutral = bool(getattr(settings, "SWING_MTF_ALLOW_NEUTRAL", True))
        return allow_neutral, "neutral"

    expected = "BULLISH" if str(direction).upper() == "LONG" else "BEARISH"
    return trend == expected, trend.lower()


def _price_band_bonus(close: float) -> int:
    """Tiered strength bonus based on price band (retail swing trade sweet spots)."""
    if 50 <= close < 200:
        return 20  # High volatility → big % moves possible
    elif 200 <= close < 500:
        return 15  # Sweet spot for retail swing trades
    elif 500 <= close < 1000:
        return 10  # Good liquidity + manageable lot size
    return 0  # No bonus above ₹1000


def _structural_sl(df: pd.DataFrame, direction: str, entry: float, atr: float) -> float:
    """
    Calculate SL anchored to recent swing high/low structure.

    LONG: Use the lowest low of the last 10 bars minus a small buffer.
    SHORT: Use the highest high of the last 10 bars plus a small buffer.
    Falls back to ATR-based SL if structural level is too far (> 8%) or
    too close (< 0.5%) from entry.
    """
    try:
        lookback = df.iloc[-10:]
        if direction == "LONG":
            swing_low = float(lookback["low"].min())
            # Buffer: 0.3% below the swing low to avoid wicks
            struct_sl = round(swing_low * 0.997, 2)
        else:
            swing_high = float(lookback["high"].max())
            struct_sl = round(swing_high * 1.003, 2)

        # Validate: structural SL must be within 0.5%–8% of entry
        if entry <= 0:
            return 0.0
        dist_pct = abs(entry - struct_sl) / entry
        if 0.005 <= dist_pct <= 0.08:
            return struct_sl
        # Too wide or too tight — blend with ATR-based fallback
        atr_sl = (entry - atr * 1.8) if direction == "LONG" else (entry + atr * 1.8)
        return round(atr_sl, 2)
    except Exception:
        return 0.0


def _apply_news_penalty(
    strength: int, news_score: float, direction: str
) -> tuple[int, bool]:
    """
    Apply strength penalty based on news context score.

    LONG signals with strongly negative news score (< -0.25) are penalised.
    SHORT signals with strongly positive news score (> 0.35) are penalised.
    Returns (adjusted_strength, penalty_applied).
    """
    penalty_applied = False
    if direction == "LONG" and news_score < -0.25:
        # Strong negative news — reduce strength proportionally
        penalty = int(abs(news_score) * 30)  # up to -30 pts at score=-1.0
        strength = max(0, strength - penalty)
        penalty_applied = True
    elif direction == "SHORT" and news_score > 0.35:
        penalty = int(news_score * 20)
        strength = max(0, strength - penalty)
        penalty_applied = True
    return strength, penalty_applied


def _assign_grade(
    strength: int,
    confluence_count: int,
    vol_confirmed: bool,
    sector_tag: str,
    news_score: float,
    news_penalty: bool,
    adx_value: float,
    sl_capped: bool = False,
    risk_reward: float = 0.0,
) -> str:
    """
    Assign signal grade A / B / C based on multi-factor quality.

    Grade A (High Conviction):
      - strength >= 80, volume confirmed, sector tailwind or neutral, news OK, ADX > 20
      - AND risk_reward >= 2.0 (meaningful R:R required — prevents high-strength/poor-RR setups)
    Grade B (Moderate — Trade with tight size):
      - strength >= 65, at least one quality factor confirmed
    Grade C (Watchlist — Do NOT trade, only watch):
      - everything else, OR when SL was artificially capped (wider natural SL = riskier)

    sl_capped=True: the ATR-based SL exceeded the price-band cap and was tightened.
    This means the true risk is HIGHER than what is shown — never grade above C.
    """
    if news_penalty:
        return "C"  # Never grade above C when news penalised

    if sl_capped:
        return "C"  # Never grade above C when SL was force-capped (hidden risk)

    tailwind = "TAILWIND" in sector_tag
    headwind = "HEADWIND" in sector_tag
    trend_ok = adx_value >= 20 or adx_value == 0.0  # 0.0 = ADX not available, skip

    if (
        strength >= 80
        and vol_confirmed
        and not headwind
        and news_score >= -0.10
        and trend_ok
        and risk_reward >= 2.0  # Meaningful R:R required for Grade A
    ):
        return "A"

    if (
        strength >= 65
        and (vol_confirmed or confluence_count >= 2 or tailwind)
        and news_score >= -0.25
    ):
        return "B"

    return "C"


def _entry_note_for_pattern(pattern: str, close: float, entry_zone: float) -> str:
    """
    Generate entry guidance note for the setup.
    Swing trades should always confirm on T+1 — never chase same-day close.
    """
    base = "Entry on next session open or limit order near entry zone."
    if "Breakout" in pattern or "Gap" in pattern:
        return f"{base} For breakouts: buy only if price holds above ₹{entry_zone:.0f} in first 30 min."
    if "Pullback" in pattern or "EMA" in pattern:
        return f"{base} Wait for bounce confirmation (green candle close above EMA20)."
    if "Reversal" in pattern or "Divergence" in pattern:
        return f"{base} Reversal setups: enter only after 2 consecutive green closes."
    return base


def _rolling_vol_ratio(
    df: pd.DataFrame, rolling_days: int | None = None
) -> tuple[float, float, float]:
    """
    Return (vol_ratio, vol_recent_avg, vol_20d_avg).

    vol_ratio = N-day rolling average / 20-day average, where N defaults to
    settings.SWING_VOL_ROLLING_DAYS (3). Smoother than single-bar ratio —
    resists one-day manipulation spikes common in mid/small-cap NSE names.

    When df has fewer than 20 bars, falls back to using all available bars for the
    baseline average (prevents silent "vol_ratio=1.0" bypass on short test/live DFs).
    Returns (1.0, 0.0, 0.0) only when the volume column is missing/empty.
    """
    if df is None or "volume" not in df.columns or len(df) == 0:
        return 1.0, 0.0, 0.0
    try:
        n = int(rolling_days or getattr(settings, "SWING_VOL_ROLLING_DAYS", 3) or 3)
        n = max(1, n)
        vol_recent = float(df["volume"].tail(n).mean())
        baseline_window = min(20, len(df))
        vol_20 = float(df["volume"].tail(baseline_window).mean())
        if vol_20 <= 0:
            return 1.0, vol_recent, 0.0
        return vol_recent / vol_20, vol_recent, vol_20
    except Exception:
        return 1.0, 0.0, 0.0


def _adx_gate_ok(df: pd.DataFrame) -> tuple[bool, float]:
    """
    Return (ok, adx_value). ok=False when the ADX floor is set and current ADX is below it.
    Used as a hard gate for breakout patterns (range, 52W high, BB squeeze, inside-bar).
    """
    try:
        adx = float(df.iloc[-1].get("ADX_14", 0) or 0)
    except Exception:
        adx = 0.0
    floor = float(getattr(settings, "SWING_ADX_MIN_FOR_BREAKOUT", 0) or 0)
    if floor <= 0 or adx <= 0:
        return True, adx
    return adx >= floor, adx


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
            return (
                round(entry - max_dist, 2),
                True,
                "SL capped — reduce qty to maintain risk",
            )
    else:
        actual_dist = raw_sl - entry
        if actual_dist > max_dist:
            return (
                round(entry + max_dist, 2),
                True,
                "SL capped — reduce qty to maintain risk",
            )
    return raw_sl, False, ""


def _fib_levels(
    df: pd.DataFrame, direction: str, entry: float = 0.0
) -> tuple[float, float]:
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
        min_rr = float(getattr(settings, "SWING_MIN_RISK_REWARD", 1.8) or 1.8)
        return (reward / risk) >= min_rr
    except Exception:
        return False  # Fail-safe: reject invalid or uncomputable levels


def _finalize_swing_targets(setup: SwingSetup, entry: float) -> None:
    """
    T1 = nearer profit exit, T2 = extended (same meaning as dashboard labels).

    Fix 1b: Fib TP1 can only IMPROVE R:R, never replace a better pattern target.
    If fib_tp1 meets min_rr but gives a WORSE R:R than the existing pattern target_1,
    the pattern target is preserved. This prevents fib from degrading 2x setups to 1.5x.

    Priority: preserve pattern RR → use fib only if it improves or equals pattern R:R.
    """
    e = float(entry)
    min_rr = float(getattr(settings, "SWING_MIN_RISK_REWARD", 1.5) or 1.5)
    sl_dist = abs(e - setup.stop_loss)

    if setup.direction == "LONG":
        # Compute existing pattern R:R before any fib override
        existing_t1_rr = (
            (setup.target_1 - e) / max(sl_dist, 1e-9) if setup.target_1 > e else 0.0
        )
        fib1_rr = (setup.fib_tp1 - e) / max(sl_dist, 1e-9) if setup.fib_tp1 > e else 0.0
        # Fix 1b: Only use fib_tp1 if it meets min_rr AND doesn't degrade the pattern's own R:R
        if setup.fib_tp1 > e and fib1_rr >= min_rr and fib1_rr >= existing_t1_rr:
            setup.target_1 = setup.fib_tp1
        # Always try to use fib_tp2 as T2 extension if it's further than T1
        if setup.fib_tp2 > setup.target_1:
            setup.target_2 = setup.fib_tp2
        elif setup.target_2 <= setup.target_1:
            # Ensure T2 is always further than T1
            setup.target_2 = round(setup.target_1 + sl_dist, 2)
    elif setup.direction == "SHORT":
        existing_t1_rr = (
            (e - setup.target_1) / max(sl_dist, 1e-9) if setup.target_1 < e else 0.0
        )
        fib1_rr = (
            (e - setup.fib_tp1) / max(sl_dist, 1e-9)
            if setup.fib_tp1 > 0 and setup.fib_tp1 < e
            else 0.0
        )
        # Fix 1b: Only use fib_tp1 if it meets min_rr AND doesn't degrade the pattern's own R:R
        if (
            setup.fib_tp1 > 0
            and setup.fib_tp1 < e
            and fib1_rr >= min_rr
            and fib1_rr >= existing_t1_rr
        ):
            setup.target_1 = setup.fib_tp1
        if setup.fib_tp2 > 0 and setup.fib_tp2 < setup.target_1:
            setup.target_2 = setup.fib_tp2
        elif setup.target_2 >= setup.target_1:
            setup.target_2 = round(setup.target_1 - sl_dist, 2)

    setup.fib_tp1 = 0.0
    setup.fib_tp2 = 0.0


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
    if (
        getattr(settings, "SWING_BLOCK_LONG_IN_BEARISH_TREND", True)
        and trend == "BEARISH"
    ):
        out = [s for s in out if s.direction != "LONG"]
    if (
        getattr(settings, "SWING_BLOCK_SHORT_IN_BULLISH_TREND", False)
        and trend == "BULLISH"
    ):
        out = [s for s in out if s.direction != "SHORT"]

    # ── Choppy/sideways filters (low directional edge) ─────────────────────
    if getattr(settings, "SWING_BLOCK_LONG_IN_CHOPPY_TREND", True) and trend in (
        "SIDEWAYS",
        "CHOPPY",
    ):
        out = [s for s in out if s.direction != "LONG"]
    if getattr(settings, "SWING_BLOCK_SHORT_IN_CHOPPY_TREND", True) and trend in (
        "SIDEWAYS",
        "CHOPPY",
    ):
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


def _apply_trade_risk_profile(
    setup: SwingSetup,
    *,
    atr: float,
    sector: str,
    capital: float,
    open_trades: list[dict] | None = None,
) -> tuple[bool, str]:
    """Normalize SL/TP, size the position, and run overnight swing guardrails.

    IMPORTANT: We do NOT overwrite the scanner's Fibonacci/structural SL+TP.
    Scanner already applied R:R gate before calling this. We only use ATR for
    sizing fallback when existing levels are invalid (zero/wrong side of entry).
    """
    min_rr = float(getattr(settings, "SWING_MIN_RISK_REWARD", 1.5) or 1.5)

    # Validate existing SL/TP from scanner
    if setup.direction == "LONG":
        sl_valid = setup.stop_loss > 0 and setup.stop_loss < setup.entry_zone
        t1_valid = setup.target_1 > 0 and setup.target_1 > setup.entry_zone
    else:
        sl_valid = setup.stop_loss > 0 and setup.stop_loss > setup.entry_zone
        t1_valid = setup.target_1 > 0 and setup.target_1 < setup.entry_zone

    if sl_valid and t1_valid:
        sl_dist = abs(setup.entry_zone - setup.stop_loss)
        t1_dist = abs(setup.target_1 - setup.entry_zone)
        existing_rr = round(t1_dist / max(sl_dist, 1e-9), 2)
    else:
        existing_rr = 0.0

    if sl_valid and t1_valid and existing_rr >= min_rr:
        # Preserve scanner-computed Fibonacci/structural SL+TP
        setup.risk_reward = round(existing_rr, 1)
        atr_for_sizing = atr
    else:
        # Fallback: scanner didn't produce valid levels — use ATR-based defaults
        levels = calculate_swing_sl_tp(
            entry=setup.entry_zone,
            atr=atr,
            direction=setup.direction,
        )
        stop = float(levels.get("stop", setup.stop_loss) or setup.stop_loss)
        target = float(levels.get("target", setup.target_1) or setup.target_1)
        tp_distance = float(levels.get("tp_distance", 0.0) or 0.0)
        rr = float(levels.get("risk_reward", 0.0) or 0.0)
        atr_for_sizing = float(levels.get("atr", atr) or atr)

        setup.stop_loss = round(stop, 2)
        setup.target_1 = round(target, 2)
        if setup.direction == "LONG":
            setup.target_2 = (
                round(setup.entry_zone + tp_distance * 1.5, 2)
                if tp_distance > 0
                else setup.target_1
            )
        else:
            setup.target_2 = (
                round(setup.entry_zone - tp_distance * 1.5, 2)
                if tp_distance > 0
                else setup.target_1
            )
        setup.risk_reward = round(rr, 1)

    sizing = calculate_swing_position_size(
        capital=capital,
        entry=setup.entry_zone,
        stop=setup.stop_loss,
        risk_pct=float(getattr(settings, "SWING_RISK_PER_TRADE_PCT", 0.015) or 0.015),
    )
    qty = int(sizing.get("qty", 0) or 0)
    if qty <= 0:
        return False, "Position size became zero"

    overnight_ok, risk_reason, adjusted_qty = overnight_risk_check(
        qty=qty,
        entry=setup.entry_zone,
        capital=capital,
        open_trades=open_trades,
        sector=sector,
    )
    if not overnight_ok:
        return False, risk_reason

    sl_distance = abs(setup.entry_zone - setup.stop_loss)
    if adjusted_qty != qty:
        sizing["qty"] = adjusted_qty
        sizing["capital_used"] = round(adjusted_qty * setup.entry_zone, 2)
        sizing["risk_amount"] = round(adjusted_qty * sl_distance, 2)
        if capital > 0:
            sizing["capital_pct"] = round(
                (float(sizing["capital_used"]) / capital) * 100, 2
            )

    setup.entry = round(setup.entry_zone, 2)
    setup.stop = round(setup.stop_loss, 2)
    setup.target = round(setup.target_1, 2)
    setup.atr = round(float(atr_for_sizing), 4)
    setup.score = float(setup.strength)
    setup.qty = int(sizing.get("qty", 0) or 0)
    setup.capital_used = float(sizing.get("capital_used", 0.0) or 0.0)
    setup.risk_amount = float(sizing.get("risk_amount", 0.0) or 0.0)
    setup.risk_pct = float(sizing.get("risk_pct", 0.0) or 0.0)
    setup.capital_pct = float(sizing.get("capital_pct", 0.0) or 0.0)
    setup.overnight_ok = True
    setup.risk_reason = risk_reason

    trail_preview = update_trailing_sl(
        entry=setup.entry_zone,
        current_price=setup.entry_zone,
        atr=setup.atr,
        original_sl=setup.stop_loss,
        direction=setup.direction,
    )
    setup.trailing_sl = float(
        trail_preview.get("new_sl", setup.stop_loss) or setup.stop_loss
    )
    setup.trailing_action = str(
        trail_preview.get("action", "Hold original SL") or "Hold original SL"
    )
    return True, risk_reason


# ── Main scan entry point ─────────────────────────────────────────────────────


def scan_swing_setups(
    df: pd.DataFrame,
    symbol: str = "",
    sector_momentum: float = 0.0,
    sector: str = "",
    market_context: dict | None = None,
    mtf_trend: str | None = None,
    stages_out: dict | None = None,
    allow_fallback: bool = False,
    news_context: dict | None = None,
    open_trades: list[dict] | None = None,
    capital: float | None = None,
    df_1h: "pd.DataFrame | None" = None,
) -> list[SwingSetup]:
    """
    Scan a daily OHLCV+indicators DataFrame for swing trading setups.

    Args:
        df: Daily OHLCV DataFrame with indicators (1y lookback recommended).
        symbol: Display name of the symbol (for logging).
        sector_momentum: Pre-computed 5-day % change avg across same sector symbols.
        sector: Sector bucket string from NSE_SECTOR_MAP.
        market_context: Optional index trend/regime from the API layer for LONG/SHORT gating.
        stages_out: If provided, filled with scan stage counts / block reasons for API diagnostics.
        allow_fallback: If True, emit a conservative trend-continuation watch setup when
            no strict pattern fires. Intended for single-symbol lookup UX.
        news_context: Optional dict with 'context_score' (float -1..1) from ContextFetcher.
            Used to penalise signals when news sentiment is strongly against the direction.
        open_trades: Optional current open positions used by overnight guardrails.
        capital: Optional portfolio capital override for swing position sizing.

    Returns list of SwingSetup objects, sorted by strength (strongest first).
    Only setups with R:R >= SWING_MIN_RISK_REWARD are included.
    """
    if stages_out is not None:
        stages_out.clear()
        stages_out.update(
            {
                "blocked": None,
                "bars": len(df)
                if df is not None and not getattr(df, "empty", True)
                else 0,
                "last_bar": None,
                "bar_age_days": None,
                "max_bar_age_days": float(
                    getattr(settings, "SWING_MAX_BAR_AGE_DAYS", 0) or 0
                ),
                "raw_pattern_count": 0,
                "after_risk_count": 0,
                "after_rr_count": 0,
                "after_mtf_count": 0,
                "after_regime_count": 0,
                "after_dedupe_count": 0,
                "fallback_used": False,
            }
        )

    if df is None or df.empty or len(df) < 30:
        if stages_out is not None:
            stages_out["blocked"] = "insufficient_bars"
        return []

    last_ts = _last_bar_timestamp(df)
    data_as_of_str = _data_as_of_iso(last_ts)
    if stages_out is not None:
        stages_out["last_bar"] = data_as_of_str or None
        if not pd.isna(last_ts):
            stages_out["bar_age_days"] = round(_bar_age_days(last_ts), 2)
    max_age = float(getattr(settings, "SWING_MAX_BAR_AGE_DAYS", 0) or 0)
    if max_age > 0:
        if pd.isna(last_ts):
            logger.debug("Swing skip %s: invalid or missing last-bar timestamp", symbol)
            if stages_out is not None:
                stages_out["blocked"] = "no_timestamp"
            return []
        age_days = _bar_age_days(last_ts)
        if age_days > max_age:
            logger.debug(
                "Swing skip %s: last bar age %.1f d > max %.1f d",
                symbol,
                age_days,
                max_age,
            )
            if stages_out is not None:
                stages_out["blocked"] = "stale_bars"
                stages_out["bar_age_days"] = round(age_days, 2)
            return []

    setups: list[SwingSetup] = []

    try:
        required = ["close", "high", "low", "RSI_14", "EMA_20", "EMA_50", "ATR_14"]
        if not all(c in df.columns for c in required):
            logger.debug("Missing required columns for swing scan on %s", symbol)
            if stages_out is not None:
                stages_out["blocked"] = "missing_columns"
            return []

        last = df.iloc[-1]
        atr = float(last["ATR_14"]) if pd.notna(last.get("ATR_14")) else 0
        close = float(last["close"])
        adx_val = float(last.get("ADX_14", 0) or 0)
        resolved_mtf_trend = _resolve_mtf_trend(df, mtf_trend)
        if stages_out is not None:
            stages_out["mtf_trend"] = resolved_mtf_trend

        if atr <= 0 or close <= 0:
            if stages_out is not None:
                stages_out["blocked"] = "bad_atr"
            return []

        # Run all pattern checks
        _disabled = getattr(settings, "SWING_DISABLED_PATTERNS", set())
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
                    # Drop patterns disabled by backtest evidence
                    if setup.pattern in _disabled:
                        logger.debug(
                            "Pattern '%s' skipped for %s (SWING_DISABLED_PATTERNS)",
                            setup.pattern, symbol,
                        )
                    else:
                        setups.append(setup)
            except Exception as e:
                logger.debug("%s failed for %s: %s", checker.__name__, symbol, e)


    except Exception as exc:
        logger.error("Swing scan failed for %s: %s", symbol, exc)
        if stages_out is not None:
            stages_out["blocked"] = "scan_error"
            stages_out["error"] = str(exc)
        return []

    if not setups and allow_fallback:
        fallback_setup = _check_trend_continuation_fallback(df, symbol, atr)
        if fallback_setup:
            setups.append(fallback_setup)
            if stages_out is not None:
                stages_out["fallback_used"] = True

    if stages_out is not None:
        stages_out["raw_pattern_count"] = len(setups)

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

    # ── Extract news score for this symbol ─────────────────────────────
    sym_news_score = 0.0
    if news_context is not None:
        sym_news_score = float(news_context.get("context_score", 0.0) or 0.0)

    portfolio_capital = float(
        capital or getattr(settings, "SWING_VIRTUAL_CAPITAL", 100000.0) or 100000.0
    )
    if portfolio_capital <= 0:
        portfolio_capital = 100000.0
    existing_open_trades = list(open_trades or [])

    filtered: list[SwingSetup] = []
    risk_pass_count = 0
    sector_hard_gate = bool(getattr(settings, "SWING_SECTOR_HARD_GATE_ENABLED", True))
    sector_headwind_block = float(
        getattr(settings, "SWING_SECTOR_HEADWIND_BLOCK_THRESHOLD", -1.5) or -1.5
    )
    sector_tailwind_block = float(
        getattr(settings, "SWING_SECTOR_TAILWIND_BLOCK_SHORT_THRESHOLD", 1.5) or 1.5
    )
    price_ctx = swing_price_history_context(df, close)
    for setup in setups:
        mtf_ok, mtf_reason = _mtf_confirms_direction(
            setup.direction, resolved_mtf_trend
        )
        if not mtf_ok:
            logger.debug(
                "Dropping %s %s: MTF mismatch (%s, trend=%s)",
                symbol,
                setup.pattern,
                mtf_reason,
                resolved_mtf_trend,
            )
            continue

        if setup.indicators is None:
            setup.indicators = {}
        if price_ctx:
            setup.indicators.update(price_ctx)
        setup.indicators["mtf_trend"] = resolved_mtf_trend
        setup.indicators["mtf_confirmation"] = mtf_reason
        if mtf_reason == "neutral":
            neutral_penalty = int(
                getattr(settings, "SWING_MTF_NEUTRAL_STRENGTH_PENALTY", 4) or 4
            )
            setup.strength = max(0, setup.strength - neutral_penalty)

        # Price-band bonus + liquidity penalty
        setup.strength = max(0, min(100, setup.strength + price_bonus + liq_penalty))

        # Sector momentum
        setup.sector = sector
        setup.sector_tag = sector_tag
        if sector_momentum < -1.5 and setup.direction == "LONG":
            setup.strength = max(0, setup.strength + sector_str_bonus)
        elif sector_momentum > 1.5:
            setup.strength = min(100, setup.strength + sector_str_bonus)

        # Optional hard gate: skip names fighting strong sector tape.
        if sector_hard_gate:
            if setup.direction == "LONG" and sector_momentum <= sector_headwind_block:
                logger.debug(
                    "Dropping %s %s: sector headwind %.2f <= %.2f",
                    symbol,
                    setup.pattern,
                    sector_momentum,
                    sector_headwind_block,
                )
                continue
            if setup.direction == "SHORT" and sector_momentum >= sector_tailwind_block:
                logger.debug(
                    "Dropping %s %s: sector tailwind %.2f >= %.2f",
                    symbol,
                    setup.pattern,
                    sector_momentum,
                    sector_tailwind_block,
                )
                continue

        # ── v3: Watchlist flag (needed for cap + news penalty) ───────────────
        watchlist_flag = bool(
            (getattr(setup, "indicators", None) or {}).get("watchlist")
        )

        # Watchlist strength cap — before news penalty
        if watchlist_flag:
            watch_cap = int(getattr(settings, "SWING_WATCHLIST_MAX_STRENGTH", 72) or 72)
            setup.strength = min(setup.strength, watch_cap)

        # SL cap — if natural ATR SL exceeds price-band max, cap it and flag
        capped_sl, was_capped, warn = _cap_sl(
            setup.entry_zone, setup.stop_loss, setup.direction
        )
        setup.stop_loss = capped_sl
        setup.sl_capped = was_capped
        setup.sl_warning = warn

        # When SL is capped: the trade is riskier than it looks (true risk is wider).
        # Penalise strength by 10 pts so the setup naturally ranks lower.
        if was_capped:
            setup.strength = max(0, setup.strength - 10)

        # ── v3: Structural SL (swing high/low anchored) ──────────────────────
        struct_sl = _structural_sl(df, setup.direction, setup.entry_zone, atr)
        if struct_sl > 0:
            setup.structural_sl = struct_sl
            # Fix 1a: Structural SL replaces ATR SL only when it is TIGHTER (closer to entry)
            # AND still respects the price-band cap. Prevents struct_sl from widening SL
            # beyond the cap, which would silently degrade R:R and bypass the cap guardrail.
            if setup.direction == "LONG" and struct_sl > setup.stop_loss:
                _, would_exceed_cap, _ = _cap_sl(
                    setup.entry_zone, struct_sl, setup.direction
                )
                if not would_exceed_cap:
                    setup.stop_loss = struct_sl  # tighten toward structure (cap-safe)
                    setup.sl_capped = False
                    setup.sl_warning = ""
                else:
                    logger.debug(
                        "%s: struct_sl ₹%.2f would exceed price-band cap — keeping capped ATR SL",
                        symbol,
                        struct_sl,
                    )
            elif setup.direction == "SHORT" and struct_sl < setup.stop_loss:
                _, would_exceed_cap, _ = _cap_sl(
                    setup.entry_zone, struct_sl, setup.direction
                )
                if not would_exceed_cap:
                    setup.stop_loss = struct_sl
                    setup.sl_capped = False
                    setup.sl_warning = ""
                else:
                    logger.debug(
                        "%s: struct_sl ₹%.2f would exceed price-band cap — keeping capped ATR SL",
                        symbol,
                        struct_sl,
                    )

        # ── v3: News context penalty (skipped for watchlist rows) ────────────
        # When SWING_NEWS_AFFECTS_STRENGTH=False (default), news does NOT reduce raw strength;
        # it only flows into grade assignment below. Google News RSS sentiment is too noisy
        # to double-count. Legacy strength penalty still available via env for backwards compat.
        news_penalised = False
        if not watchlist_flag and bool(
            getattr(settings, "SWING_NEWS_AFFECTS_STRENGTH", False)
        ):
            setup.strength, news_penalised = _apply_news_penalty(
                setup.strength, sym_news_score, setup.direction
            )
        else:
            # Compute penalty flag (for grade tagging) without touching strength.
            # ── Quick Win: Stricter News Penalty (Negative News = Auto Grade C) ──
            if not watchlist_flag and (
                (setup.direction == "LONG" and sym_news_score < -0.15)
                or (setup.direction == "SHORT" and sym_news_score > 0.20)
            ):
                news_penalised = True
        setup.news_score = sym_news_score
        setup.news_penalty_applied = news_penalised
        if news_context:
            setup.indicators["news_context_loaded"] = True
            for k in (
                "news_sentiment_24hr",
                "news_sentiment_7d",
                "weekly_trend_bias",
                "news_volume_spike",
                "event_risk_active",
            ):
                if k in news_context and news_context[k] is not None:
                    setup.indicators[k] = news_context[k]
        else:
            setup.indicators.setdefault("news_context_loaded", False)

        # Fibonacci targets — store as hints; _finalize_swing_targets will apply
        # them only if they don't degrade R:R below the minimum gate.
        fib1, fib2 = _fib_levels(df, setup.direction, entry=setup.entry_zone)
        setup.fib_tp1 = fib1
        setup.fib_tp2 = fib2
        # Do NOT shallow-cap target_1 with fib here — that causes RR degradation.
        # Only use fib2 as a T2 extension if it is FURTHER than current target_2.
        if setup.direction == "LONG":
            if fib2 > 0 and fib2 > setup.target_2 and fib2 > setup.entry_zone:
                setup.target_2 = fib2
        else:
            if fib2 > 0 and fib2 < setup.target_2 and fib2 < setup.entry_zone:
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

        _finalize_swing_targets(setup, e)

        risk_ok, risk_reason = _apply_trade_risk_profile(
            setup,
            atr=atr,
            sector=sector,
            capital=portfolio_capital,
            open_trades=existing_open_trades,
        )
        if not risk_ok:
            logger.debug(
                "Dropping %s %s: risk guardrail blocked (%s)",
                symbol,
                setup.pattern,
                risk_reason,
            )
            continue
        risk_pass_count += 1

        # Recalculate R:R after all adjustments
        risk = abs(setup.entry_zone - setup.stop_loss)
        if risk > 0:
            setup.risk_reward = round(abs(setup.target_1 - setup.entry_zone) / risk, 1)

        # R:R gate — drop setups below minimum (applied to ALL patterns including watchlist)
        if not _meets_min_rr(
            setup.entry_zone, setup.stop_loss, setup.target_1, setup.direction
        ):
            # Fix 1c: Detailed R:R diagnostic — shows exact numbers that failed the gate
            _risk = abs(setup.entry_zone - setup.stop_loss)
            _reward = abs(setup.target_1 - setup.entry_zone)
            _rr_actual = round(_reward / max(_risk, 1e-9), 2)
            logger.debug(
                "[RR-FAIL] %s | pattern=%s | entry=%.2f SL=%.2f TP1=%.2f "
                "| risk=%.2f reward=%.2f actual_rr=%.2f < required=%.1f",
                symbol,
                setup.pattern,
                setup.entry_zone,
                setup.stop_loss,
                setup.target_1,
                _risk,
                _reward,
                _rr_actual,
                settings.SWING_MIN_RISK_REWARD,
            )
            if stages_out is not None:
                stages_out.setdefault("rr_fail_details", []).append(
                    {
                        "pattern": setup.pattern,
                        "entry": round(setup.entry_zone, 2),
                        "sl": round(setup.stop_loss, 2),
                        "tp1": round(setup.target_1, 2),
                        "risk": round(_risk, 2),
                        "reward": round(_reward, 2),
                        "rr": _rr_actual,
                        "required": settings.SWING_MIN_RISK_REWARD,
                        "reason": "Entry too far from SL or target too tight",
                    }
                )
            continue

        setup.setup_date = today_str
        setup.data_as_of = data_as_of_str
        setup.data_stale = False

        # ── v3: ADX, entry note, confirm_next_day ───────────────────────
        setup.adx_value = adx_val
        setup.confirm_next_day = True
        setup.entry_note = _entry_note_for_pattern(
            setup.pattern, close, setup.entry_zone
        )

        # ── v3: Signal Grade A / B / C ──────────────────────────────
        vol_conf_ind = bool((setup.indicators or {}).get("vol_ratio", 0) >= 1.5)
        setup.grade = _assign_grade(
            strength=setup.strength,
            confluence_count=setup.confluence_count,
            vol_confirmed=vol_conf_ind,
            sector_tag=setup.sector_tag,
            news_score=sym_news_score,
            news_penalty=news_penalised,
            adx_value=adx_val,
            sl_capped=setup.sl_capped,  # Force C-grade when SL was capped
            risk_reward=setup.risk_reward,  # Grade A requires R:R >= 2.0
        )

        # 1H MTF Confirmation (LONG only; neutral when data unavailable)
        if setup.direction == "LONG":
            _1h_ok, _1h_why = _check_1h_mtf_bullish(df_1h)
            if _1h_ok is True:
                setup.strength = min(100, setup.strength + 5)
                setup.description += " \u2705 1H confirmed"
                setup.mtf_confirmed = True
                setup.mtf_reason = _1h_why
            elif _1h_ok is False:
                setup.strength = max(0, setup.strength - 15)
                setup.description += f" \u26a0\ufe0f 1H unconfirmed ({_1h_why})"
                setup.mtf_confirmed = False
                setup.mtf_reason = _1h_why
            else:
                setup.mtf_confirmed = False
                setup.mtf_reason = "1H data unavailable (neutral)"
        else:
            setup.mtf_confirmed = True
            setup.mtf_reason = "N/A (SHORT)"
        # MTF bonus/penalty runs after the earlier watchlist cap — re-apply cap.
        if bool((getattr(setup, "indicators", None) or {}).get("watchlist")):
            watch_cap2 = int(getattr(settings, "SWING_WATCHLIST_MAX_STRENGTH", 72) or 72)
            setup.strength = min(setup.strength, watch_cap2)

        filtered.append(setup)

    if stages_out is not None:
        stages_out["after_risk_count"] = risk_pass_count

    if stages_out is not None:
        stages_out["after_rr_count"] = len(filtered)

    if stages_out is not None:
        stages_out["after_mtf_count"] = len(filtered)

    filtered = apply_swing_regime_gate(filtered, market_context)
    if stages_out is not None:
        stages_out["after_regime_count"] = len(filtered)

    filtered = dedupe_swing_setups_one_per_symbol(filtered)
    if stages_out is not None:
        stages_out["after_dedupe_count"] = len(filtered)

    for setup in filtered:
        setup.score = float(setup.strength)
        assign_calibrated_winrate(setup)

    filtered.sort(key=lambda s: s.strength, reverse=True)
    return filtered


def _check_trend_continuation_fallback(
    df: pd.DataFrame,
    symbol: str,
    atr: float,
) -> Optional[SwingSetup]:
    """
    Conservative fallback for symbol lookup when strict pattern checks return none.

    Emits a lower-conviction trend-continuation watch setup if structure is clean:
    - LONG: close > EMA20 > EMA50 with RSI in a healthy momentum band
    - SHORT: close < EMA20 < EMA50 with RSI in a healthy downside band
    """
    if df is None or df.empty:
        return None

    last = df.iloc[-1]
    close = float(last.get("close", 0) or 0)
    ema20 = float(last.get("EMA_20", 0) or 0)
    ema50 = float(last.get("EMA_50", 0) or 0)
    rsi = float(last.get("RSI_14", 50) or 50)
    atr = float(atr or 0)

    if close <= 0 or ema20 <= 0 or ema50 <= 0 or atr <= 0:
        return None

    atr_pct = (atr / close) * 100.0
    if atr_pct < 0.4:
        return None

    # Long-side continuation watch.
    if close > ema20 > ema50 and 45 <= rsi <= 68:
        # SL = entry - ATR * 1.5  (tight, ATR-anchored — never use swing_low as primary)
        sl = close - atr * 1.5
        # T1 = entry + ATR * 2.5  (ensures RR ≈ 1.67x vs 1.5x SL)
        tp1 = close + atr * 2.5
        # T2 = entry + ATR * 4.0
        tp2 = close + atr * 4.0

        risk = close - sl
        rr = (tp1 - close) / max(risk, 0.01)

        # Hard RR gate — fallback setups must clear 1.5x minimum
        if rr < 1.5:
            logger.debug(
                "_check_trend_continuation_fallback LONG %s: rr=%.2f < 1.5 — skipping",
                symbol,
                rr,
            )
            return None

        strength = 58
        if rsi >= 52:
            strength += 5
        if (close - ema20) / ema20 < 0.03:
            strength += 4

        return SwingSetup(
            symbol=symbol,
            pattern="Trend Continuation (Watchlist)",
            direction="LONG",
            strength=min(72, strength),
            entry_zone=round(close, 2),
            stop_loss=round(sl, 2),
            target_1=round(tp1, 2),
            target_2=round(tp2, 2),
            risk_reward=round(rr, 1),
            timeframe="5-10 days",
            description=(
                "No strict pattern fired, but trend structure is constructive "
                "(close > EMA20 > EMA50). Treat as watchlist continuation candidate."
            ),
            indicators={
                "rsi": round(rsi, 1),
                "ema20": round(ema20, 2),
                "ema50": round(ema50, 2),
                "atr_pct": round(atr_pct, 2),
                "fallback": True,
                "watchlist": True,
                "watchlist_reason": "no_strict_pattern",
            },
        )

    # Short-side continuation watch.
    if close < ema20 < ema50 and 32 <= rsi <= 55:
        # SL = entry + ATR * 1.5
        sl = close + atr * 1.5
        # T1 = entry - ATR * 2.5  (ensures RR ≈ 1.67x)
        tp1 = close - atr * 2.5
        # T2 = entry - ATR * 4.0
        tp2 = close - atr * 4.0

        risk = sl - close
        rr = (close - tp1) / max(risk, 0.01)

        # Hard RR gate — fallback setups must clear 1.5x minimum
        if rr < 1.5:
            logger.debug(
                "_check_trend_continuation_fallback SHORT %s: rr=%.2f < 1.5 — skipping",
                symbol,
                rr,
            )
            return None

        strength = 58
        if rsi <= 48:
            strength += 5
        if (ema20 - close) / ema20 < 0.03:
            strength += 4

        return SwingSetup(
            symbol=symbol,
            pattern="Trend Continuation (Watchlist)",
            direction="SHORT",
            strength=min(72, strength),
            entry_zone=round(close, 2),
            stop_loss=round(sl, 2),
            target_1=round(tp1, 2),
            target_2=round(tp2, 2),
            risk_reward=round(rr, 1),
            timeframe="5-10 days",
            description=(
                "No strict pattern fired, but trend structure is bearish "
                "(close < EMA20 < EMA50). Treat as watchlist continuation candidate."
            ),
            indicators={
                "rsi": round(rsi, 1),
                "ema20": round(ema20, 2),
                "ema50": round(ema50, 2),
                "atr_pct": round(atr_pct, 2),
                "fallback": True,
                "watchlist": True,
                "watchlist_reason": "no_strict_pattern",
            },
        )

    return None


def _check_pullback_to_ema(
    df: pd.DataFrame, symbol: str, atr: float
) -> Optional[SwingSetup]:
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

    vol_ratio, _vol_recent, vol_avg = _rolling_vol_ratio(df)

    # ── HARD VOLUME BLOCK ──────────────────────────────────────────
    HARD_VOL_BLOCK = float(
        getattr(settings, "SWING_VOL_EMA_PULLBACK_HARD_BLOCK", 0.3) or 0.3
    )
    if vol_avg > 0 and vol_ratio < HARD_VOL_BLOCK:
        return None  # Block entirely for both longs and shorts
    # ──────────────────────────────────────────────────────────────

    vol_confirmed = vol_avg <= 0 or vol_ratio >= settings.SWING_VOL_EMA_PULLBACK
    min_actionable_ratio = float(
        getattr(settings, "SWING_VOL_EMA_PULLBACK_MIN_ACTIONABLE", 0.6) or 0.6
    )
    low_volume_watchlist = vol_avg > 0 and vol_ratio < min_actionable_ratio

    # LONG: Uptrend pullback
    if ema20 > ema50:
        # Price within 1.5% of EMA20 (touching the moving average)
        proximity = abs(close - ema20) / ema20 * 100
        if proximity < 1.5 and close > ema50:
            # Check bounce: last bar closed green or above previous low
            bouncing = close > float(df.iloc[-2]["close"]) or float(
                last["low"]
            ) > float(df.iloc[-2]["low"])

            if bouncing:
                sl = close - atr * 2
                tp1 = close + atr * 4
                tp2 = close + atr * 6

                strength = 60
                if has_ema200 and ema50 > ema200:  # Super uptrend
                    strength += 15
                if rsi > 40 and rsi < 60:  # Not overbought
                    strength += 10
                if proximity < 0.5:  # Very close to EMA
                    strength += 10

                pattern = "Pullback to EMA20 (Uptrend)"
                timeframe = "3-7 days"
                description = (
                    f"Price pulled back to EMA20 (₹{ema20:.0f}) in uptrend. "
                    f"EMA20 > EMA50 confirms. RSI at {rsi:.0f}. "
                    f"{'Volume confirmed ✓' if vol_confirmed else 'Volume weak ⚠️'}"
                )
                watchlist = False
                watch_reason = ""

                if low_volume_watchlist:
                    strength = min(strength, 68)
                    pattern = f"{pattern} (Watchlist)"
                    description += (
                        f" Volume ratio {vol_ratio:.1f}x wait for fresh confirmation."
                    )
                    watchlist = True
                    watch_reason = "low_volume"
                elif vol_confirmed:
                    strength = min(95, strength + 10)

                return SwingSetup(
                    symbol=symbol,
                    pattern=pattern,
                    direction="LONG",
                    strength=min(95, strength),
                    entry_zone=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(tp1, 2),
                    target_2=round(tp2, 2),
                    risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
                    timeframe=timeframe,
                    description=description,
                    indicators={
                        "rsi": round(rsi, 1),
                        "ema20": round(ema20, 2),
                        "ema50": round(ema50, 2),
                        "proximity_pct": round(proximity, 2),
                        "vol_ratio": round(vol_ratio, 2),
                        "watchlist": watchlist,
                        "watchlist_reason": watch_reason,
                        "min_actionable_vol_ratio": round(min_actionable_ratio, 1),
                    },
                )

    # SHORT: Downtrend rally
    if ema20 < ema50:
        proximity = abs(close - ema20) / ema20 * 100
        if proximity < 1.5 and close < ema50:
            rejecting = close < float(df.iloc[-2]["close"]) or float(
                last["high"]
            ) < float(df.iloc[-2]["high"])
            if rejecting:
                sl = close + atr * 2
                tp1 = close - atr * 4
                tp2 = close - atr * 6

                strength = 60
                if has_ema200 and ema50 < ema200:  # Super downtrend
                    strength += 15
                if rsi > 40 and rsi < 65:
                    strength += 10
                if proximity < 0.5:
                    strength += 10

                pattern = "Rally to EMA20 (Downtrend)"
                timeframe = "3-7 days"
                description = (
                    f"Price rallied to EMA20 (₹{ema20:.0f}) in downtrend. "
                    f"EMA20 < EMA50 bearish config. RSI at {rsi:.0f}. "
                    f"{'Volume confirmed ✓' if vol_confirmed else 'Volume weak ⚠️'}"
                )
                watchlist = False
                watch_reason = ""

                if low_volume_watchlist:
                    strength = min(strength, 68)
                    pattern = f"{pattern} (Watchlist)"
                    description += (
                        f" Volume ratio {vol_ratio:.1f}x wait for fresh confirmation."
                    )
                    watchlist = True
                    watch_reason = "low_volume"
                elif vol_confirmed:
                    strength = min(95, strength + 10)

                return SwingSetup(
                    symbol=symbol,
                    pattern=pattern,
                    direction="SHORT",
                    strength=min(95, strength),
                    entry_zone=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(tp1, 2),
                    target_2=round(tp2, 2),
                    risk_reward=round((close - tp1) / max(sl - close, 0.01), 1),
                    timeframe=timeframe,
                    description=description,
                    indicators={
                        "rsi": round(rsi, 1),
                        "ema20": round(ema20, 2),
                        "ema50": round(ema50, 2),
                        "proximity_pct": round(proximity, 2),
                        "vol_ratio": round(vol_ratio, 2),
                        "watchlist": watchlist,
                        "watchlist_reason": watch_reason,
                        "min_actionable_vol_ratio": round(min_actionable_ratio, 1),
                    },
                )
    return None


def _check_range_breakout(
    df: pd.DataFrame, symbol: str, atr: float
) -> Optional[SwingSetup]:
    """
    Breakout from 20-day consolidation range with volume confirmation.
    Must close above/below the range with above-average volume.
    """
    close = float(df.iloc[-1]["close"])

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

    vol_ratio, _vr, vol_avg = _rolling_vol_ratio(df)

    # ── HARD VOLUME BLOCK ──────────────────────────────────────────
    # ── Quick Win: Breakouts MUST have at least 1.5x average volume ──
    HARD_VOL_BLOCK = float(
        getattr(settings, "SWING_VOL_RANGE_BREAKOUT_HARD_BLOCK", 1.5) or 1.5
    )
    if vol_avg > 0 and vol_ratio < HARD_VOL_BLOCK:
        return None  # Block entirely (Fakeout protection)
    # ──────────────────────────────────────────────────────────────

    # ── ADX GATE (breakout patterns need a trending regime) ─────────
    adx_ok, adx_val = _adx_gate_ok(df)
    if not adx_ok:
        return None
    # ────────────────────────────────────────────────────────────────

    # ── 2-bar breakout confirmation (optional, controlled by setting) ──
    confirm_2bar = bool(getattr(settings, "SWING_REQUIRE_2BAR_BREAKOUT_CONFIRM", False))
    prev_close = float(df.iloc[-2]["close"]) if len(df) >= 2 else close

    min_actionable_ratio = float(
        getattr(settings, "SWING_VOL_RANGE_BREAKOUT_MIN_ACTIONABLE", 1.5) or 1.5
    )
    low_volume_watchlist = vol_avg > 0 and vol_ratio < min_actionable_ratio
    vol_confirmed = vol_avg <= 0 or vol_ratio >= settings.SWING_VOL_RANGE_BREAKOUT

    # BULLISH breakout
    if close > range_high:
        # Require previous close also above range_high for confirmation.
        if confirm_2bar and prev_close <= range_high:
            return None

        # ── Quick Win: Retest Entry instead of Direct Breakout ──
        entry_zone = range_high
        sl = entry_zone - atr * 0.5  # SL just below breakout level
        tp1 = entry_zone + range_width  # Measured move = range width
        tp2 = entry_zone + range_width * 1.5

        strength = 55
        if vol_confirmed:
            strength += 20
        if float(df.iloc[-1].get("ADX_14", 0) or 0) > 25:
            strength += 10
        rsi = float(df.iloc[-1].get("RSI_14", 50) or 50)
        if rsi < 75:
            strength += 5

        pattern = "20-Day Range Breakout (Bullish)"
        timeframe = "5-10 days"
        description = (
            f"Broke above 20-day range high ₹{range_high:.0f}. "
            f"{'Volume confirmed ✓' if vol_confirmed else 'Volume weak ⚠️'}. "
            f"Wait for retest entry near ₹{entry_zone:.0f}. Target: ₹{tp1:.0f}."
        )
        watchlist = False
        watch_reason = ""

        if low_volume_watchlist:
            strength = min(strength, 68)
            pattern = f"{pattern} (Watchlist)"
            timeframe = "6-12 days"
            description += (
                f" Volume ratio {vol_ratio:.1f}x is below actionable minimum "
                f"{min_actionable_ratio:.1f}x; wait for fresh confirmation."
            )
            watchlist = True
            watch_reason = "low_volume"
        else:
            strength = min(95, strength)

        return SwingSetup(
            symbol=symbol,
            pattern=pattern,
            direction="LONG",
            strength=strength,
            entry_zone=round(entry_zone, 2),
            stop_loss=round(sl, 2),
            target_1=round(tp1, 2),
            target_2=round(tp2, 2),
            risk_reward=round((tp1 - entry_zone) / max(entry_zone - sl, 0.01), 1),
            timeframe=timeframe,
            description=description,
            indicators={
                "range_high": round(range_high, 2),
                "range_low": round(range_low, 2),
                "vol_ratio": round(vol_ratio, 1),
                "range_width_pct": round(range_width / close * 100, 1),
                "watchlist": watchlist,
                "watchlist_reason": watch_reason,
                "min_actionable_vol_ratio": round(min_actionable_ratio, 1),
            },
        )

    # BEARISH breakdown
    if close < range_low:
        if confirm_2bar and prev_close >= range_low:
            return None

        # ── Quick Win: Retest Entry ──
        entry_zone = range_low
        sl = entry_zone + atr * 0.5
        tp1 = entry_zone - range_width
        tp2 = entry_zone - range_width * 1.5

        strength = 55
        if vol_confirmed:
            strength += 20
        if float(df.iloc[-1].get("ADX_14", 0) or 0) > 25:
            strength += 10

        pattern = "20-Day Range Breakdown (Bearish)"
        timeframe = "5-10 days"
        description = (
            f"Broke below 20-day range low ₹{range_low:.0f}. "
            f"{'Volume confirmed ✓' if vol_confirmed else 'Volume weak ⚠️'}. "
            f"Wait for retest entry near ₹{entry_zone:.0f}. Target: ₹{tp1:.0f}."
        )
        watchlist = False
        watch_reason = ""

        if low_volume_watchlist:
            strength = min(strength, 68)
            pattern = f"{pattern} (Watchlist)"
            timeframe = "6-12 days"
            description += (
                f" Volume ratio {vol_ratio:.1f}x is below actionable minimum "
                f"{min_actionable_ratio:.1f}x; wait for fresh confirmation."
            )
            watchlist = True
            watch_reason = "low_volume"
        else:
            strength = min(95, strength)

        return SwingSetup(
            symbol=symbol,
            pattern=pattern,
            direction="SHORT",
            strength=strength,
            entry_zone=round(entry_zone, 2),
            stop_loss=round(sl, 2),
            target_1=round(tp1, 2),
            target_2=round(tp2, 2),
            risk_reward=round((entry_zone - tp1) / max(sl - entry_zone, 0.01), 1),
            timeframe=timeframe,
            description=description,
            indicators={
                "range_high": round(range_high, 2),
                "range_low": round(range_low, 2),
                "vol_ratio": round(vol_ratio, 1),
                "watchlist": watchlist,
                "watchlist_reason": watch_reason,
                "min_actionable_vol_ratio": round(min_actionable_ratio, 1),
            },
        )
    return None


def _check_rsi_divergence(
    df: pd.DataFrame, symbol: str, atr: float
) -> Optional[SwingSetup]:
    """Detect RSI divergence using local swing pivots instead of broad min/max windows."""
    if len(df) < 30 or "RSI_14" not in df.columns:
        return None

    close = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
    rsi = pd.to_numeric(df["RSI_14"], errors="coerce").to_numpy(dtype=float)
    if len(close) != len(rsi) or np.isnan(close).all() or np.isnan(rsi).all():
        return None

    lookback = int(getattr(settings, "SWING_RSI_DIVERGENCE_LOOKBACK", 35) or 35)
    lookback = max(20, min(lookback, len(df)))
    window = int(getattr(settings, "SWING_RSI_DIVERGENCE_PIVOT_WINDOW", 2) or 2)
    window = max(1, min(window, 5))
    min_price_delta_pct = float(
        getattr(settings, "SWING_RSI_DIVERGENCE_MIN_PRICE_DELTA_PCT", 0.4) or 0.4
    )
    min_rsi_delta = float(
        getattr(settings, "SWING_RSI_DIVERGENCE_MIN_RSI_DELTA", 2.0) or 2.0
    )

    sub_close = close[-lookback:]
    sub_rsi = rsi[-lookback:]
    last_close = float(close[-1])

    def _pivot_idx(values: np.ndarray, kind: str) -> list[int]:
        n = len(values)
        if n < (window * 2 + 3):
            return []
        out: list[int] = []
        for i in range(window, n - window):
            seg = values[i - window : i + window + 1]
            if np.isnan(seg).any():
                continue
            v = float(values[i])
            left = values[i - window : i]
            right = values[i + 1 : i + window + 1]
            if kind == "low":
                if not (v < float(np.nanmin(left)) and v <= float(np.nanmin(right))):
                    continue
            else:
                if not (v > float(np.nanmax(left)) and v >= float(np.nanmax(right))):
                    continue

            if out and (i - out[-1]) <= window:
                prev = out[-1]
                better = (v < values[prev]) if kind == "low" else (v > values[prev])
                if better:
                    out[-1] = i
            else:
                out.append(i)
        return out

    lows = _pivot_idx(sub_close, "low")
    highs = _pivot_idx(sub_close, "high")

    # Bullish divergence: newer price pivot low is lower, RSI pivot at same bars is higher.
    if len(lows) >= 2:
        i1, i2 = lows[-2], lows[-1]
        price1, price2 = float(sub_close[i1]), float(sub_close[i2])
        rsi1, rsi2 = float(sub_rsi[i1]), float(sub_rsi[i2])
        bars_since_pivot = len(sub_close) - 1 - i2

        price_delta_pct = ((price1 - price2) / max(abs(price1), 1e-9)) * 100.0
        rsi_delta = rsi2 - rsi1
        momentum_turning = float(sub_rsi[-1]) >= rsi2 or float(sub_close[-1]) >= price2

        if (
            price_delta_pct >= min_price_delta_pct
            and rsi_delta >= min_rsi_delta
            and bars_since_pivot <= max(12, window * 5)
            and momentum_turning
            and rsi2 <= 50
        ):
            pivot_low = min(price1, price2)
            sl = pivot_low - atr * 1.2
            tp1 = last_close + atr * 3
            tp2 = last_close + atr * 5
            strength = min(
                88,
                58
                + int(min(15.0, price_delta_pct * 3.0))
                + int(min(12.0, rsi_delta * 1.8)),
            )
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
                description=(
                    "Lower price pivot low with higher RSI pivot low (bullish divergence). "
                    f"RSI improved {rsi1:.1f}→{rsi2:.1f}."
                ),
                indicators={
                    "pivot_price_1": round(price1, 2),
                    "pivot_price_2": round(price2, 2),
                    "pivot_rsi_1": round(rsi1, 1),
                    "pivot_rsi_2": round(rsi2, 1),
                    "bars_since_pivot": int(bars_since_pivot),
                },
            )

    # Bearish divergence: newer price pivot high is higher, RSI pivot at same bars is lower.
    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        price1, price2 = float(sub_close[i1]), float(sub_close[i2])
        rsi1, rsi2 = float(sub_rsi[i1]), float(sub_rsi[i2])
        bars_since_pivot = len(sub_close) - 1 - i2

        price_delta_pct = ((price2 - price1) / max(abs(price1), 1e-9)) * 100.0
        rsi_delta = rsi1 - rsi2
        momentum_turning = float(sub_rsi[-1]) <= rsi2 or float(sub_close[-1]) <= price2

        if (
            price_delta_pct >= min_price_delta_pct
            and rsi_delta >= min_rsi_delta
            and bars_since_pivot <= max(12, window * 5)
            and momentum_turning
            and rsi2 >= 50
        ):
            pivot_high = max(price1, price2)
            sl = pivot_high + atr * 1.2
            tp1 = last_close - atr * 3
            tp2 = last_close - atr * 5
            strength = min(
                88,
                58
                + int(min(15.0, price_delta_pct * 3.0))
                + int(min(12.0, rsi_delta * 1.8)),
            )
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
                description=(
                    "Higher price pivot high with lower RSI pivot high (bearish divergence). "
                    f"RSI faded {rsi1:.1f}→{rsi2:.1f}."
                ),
                indicators={
                    "pivot_price_1": round(price1, 2),
                    "pivot_price_2": round(price2, 2),
                    "pivot_rsi_1": round(rsi1, 1),
                    "pivot_rsi_2": round(rsi2, 1),
                    "bars_since_pivot": int(bars_since_pivot),
                },
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

    # ── Stricter history requirements ─────────────────────────────────
    # yFinance free data ke early bars me EMA_200 garbage hota hai — isliye
    # (a) full history minimum 220 bars, (b) last 20 bars non-NaN chahiye.
    min_bars = int(getattr(settings, "SWING_MIN_BARS_FOR_EMA200", 220) or 0)
    if min_bars > 0 and len(df) < min_bars:
        return None

    clean_tail = int(getattr(settings, "SWING_MIN_CLEAN_EMA200_TAIL", 20) or 0)
    if clean_tail > 0:
        tail = df["EMA_200"].tail(clean_tail)
        if tail.isna().any() or (tail == 0).any():
            return None

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

        if (
            pd.isna(curr_50)
            or pd.isna(curr_200)
            or pd.isna(prev_50)
            or pd.isna(prev_200)
        ):
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


def _check_inside_bar(
    df: pd.DataFrame, symbol: str, atr: float
) -> Optional[SwingSetup]:
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
            vol_ratio, _vr, vol_avg = _rolling_vol_ratio(df)

            # ── HARD VOLUME BLOCK ──────────────────────────────────────────
            # vol_ratio < 0.5x = extremely thin, skip completely (not even watchlist).
            HARD_VOL_BLOCK = float(
                getattr(settings, "SWING_VOL_INSIDE_BAR_HARD_BLOCK", 0.5) or 0.5
            )
            if vol_avg > 0 and vol_ratio < HARD_VOL_BLOCK:
                return None  # Block entirely — volume confirms nothing
            # ──────────────────────────────────────────────────────────────

            # ── ADX GATE ───────────────────────────────────────────────────
            adx_ok, _ = _adx_gate_ok(df)
            if not adx_ok:
                return None
            # ───────────────────────────────────────────────────────────────

            vol_confirmed = vol_avg <= 0 or vol_ratio >= settings.SWING_VOL_INSIDE_BAR
            min_actionable_ratio = float(
                getattr(settings, "SWING_VOL_INSIDE_BAR_MIN_ACTIONABLE", 1.5) or 1.5
            )
            low_volume_watchlist = vol_avg > 0 and vol_ratio < min_actionable_ratio
            # Breakout direction
            if close > mother_high:
                sl = mother_low
                tp1 = close + (mother_high - mother_low)
                tp2 = close + (mother_high - mother_low) * 1.5
                strength = min(90, 60 + inside_count * 10)
                pattern = f"Inside Bar Breakout ({inside_count} bars)"
                timeframe = "2-5 days"
                description = (
                    f"{inside_count} inside bars. Bullish breakout above ₹{mother_high:.0f}. "
                    f"{'Volume confirmed ✓' if vol_confirmed else 'Volume weak ⚠️'}."
                )
                watchlist = False
                watch_reason = ""
                if low_volume_watchlist:
                    strength = min(strength, 68)
                    pattern = f"{pattern} (Watchlist)"
                    timeframe = "3-8 days"
                    description += (
                        f" Volume ratio {vol_ratio:.1f}x is below actionable minimum "
                        f"{min_actionable_ratio:.1f}x; wait for fresh confirmation."
                    )
                    watchlist = True
                    watch_reason = "low_volume"
                elif vol_confirmed:
                    strength = min(95, strength + 10)
                return SwingSetup(
                    symbol=symbol,
                    pattern=pattern,
                    direction="LONG",
                    strength=strength,
                    entry_zone=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(tp1, 2),
                    target_2=round(tp2, 2),
                    risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
                    timeframe=timeframe,
                    description=description,
                    indicators={
                        "mother_high": round(mother_high, 2),
                        "mother_low": round(mother_low, 2),
                        "inside_bars": inside_count,
                        "vol_ratio": round(vol_ratio, 1),
                        "watchlist": watchlist,
                        "watchlist_reason": watch_reason,
                        "min_actionable_vol_ratio": round(min_actionable_ratio, 1),
                    },
                )
            elif close < mother_low:
                sl = mother_high
                tp1 = close - (mother_high - mother_low)
                tp2 = close - (mother_high - mother_low) * 1.5
                strength = min(90, 60 + inside_count * 10)
                pattern = f"Inside Bar Breakdown ({inside_count} bars)"
                timeframe = "2-5 days"
                description = (
                    f"{inside_count} inside bars. Bearish breakdown below ₹{mother_low:.0f}. "
                    f"{'Volume confirmed ✓' if vol_confirmed else 'Volume weak ⚠️'}."
                )
                watchlist = False
                watch_reason = ""
                if low_volume_watchlist:
                    strength = min(strength, 68)
                    pattern = f"{pattern} (Watchlist)"
                    timeframe = "3-8 days"
                    description += (
                        f" Volume ratio {vol_ratio:.1f}x is below actionable minimum "
                        f"{min_actionable_ratio:.1f}x; wait for fresh confirmation."
                    )
                    watchlist = True
                    watch_reason = "low_volume"
                elif vol_confirmed:
                    strength = min(95, strength + 10)
                return SwingSetup(
                    symbol=symbol,
                    pattern=pattern,
                    direction="SHORT",
                    strength=strength,
                    entry_zone=round(close, 2),
                    stop_loss=round(sl, 2),
                    target_1=round(tp1, 2),
                    target_2=round(tp2, 2),
                    risk_reward=round((close - tp1) / max(sl - close, 0.01), 1),
                    timeframe=timeframe,
                    description=description,
                    indicators={
                        "mother_high": round(mother_high, 2),
                        "mother_low": round(mother_low, 2),
                        "inside_bars": inside_count,
                        "vol_ratio": round(vol_ratio, 1),
                        "watchlist": watchlist,
                        "watchlist_reason": watch_reason,
                        "min_actionable_vol_ratio": round(min_actionable_ratio, 1),
                    },
                )
    return None


def _check_bb_squeeze(
    df: pd.DataFrame, symbol: str, atr: float
) -> Optional[SwingSetup]:
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
    was_squeezed = (
        min_width < avg_width * 0.7
    )  # Width was 70% below average (relaxed for real-world conditions)

    if was_squeezed and is_expanding:
        close = float(df.iloc[-1]["close"])
        vol_ratio, _vr, vol_avg = _rolling_vol_ratio(df)

        HARD_VOL_BLOCK = float(
            getattr(settings, "SWING_VOL_BB_SQUEEZE_HARD_BLOCK", 0.5) or 0.5
        )
        if vol_avg > 0 and vol_ratio < HARD_VOL_BLOCK:
            return None

        # ── ADX GATE ───────────────────────────────────────────────────
        adx_ok, _ = _adx_gate_ok(df)
        if not adx_ok:
            return None
        # ───────────────────────────────────────────────────────────────

        min_actionable_ratio = float(
            getattr(settings, "SWING_VOL_BB_SQUEEZE_MIN_ACTIONABLE", 1.0) or 1.0
        )
        low_volume_watchlist = vol_avg > 0 and vol_ratio < min_actionable_ratio
        vol_confirmed = vol_avg <= 0 or vol_ratio >= settings.SWING_VOL_BB_SQUEEZE

        bb_upper = float(df.iloc[-1].get("BB_upper", close + atr))
        bb_lower = float(df.iloc[-1].get("BB_lower", close - atr))

        # Breakout direction
        if close > bb_upper * 0.998:  # Near or above upper band
            sl = close - atr * 2
            tp1 = close + atr * 4
            tp2 = close + atr * 6

            strength = 70
            pattern = "BB Squeeze Breakout (Bullish)"
            timeframe = "3-7 days"
            description = (
                "Bollinger Bands squeezed to 20-day low and now expanding upward. "
                f"{'Volume confirmed ✓' if vol_confirmed else 'Volume weak ⚠️'}. "
                "Expect a strong directional move."
            )
            watchlist = False
            watch_reason = ""

            if low_volume_watchlist:
                strength = min(strength, 68)
                pattern = f"{pattern} (Watchlist)"
                timeframe = "4-10 days"
                description += (
                    f" Volume ratio {vol_ratio:.1f}x is below actionable minimum "
                    f"{min_actionable_ratio:.1f}x; wait for fresh confirmation."
                )
                watchlist = True
                watch_reason = "low_volume"
            elif vol_confirmed:
                strength = min(95, strength + 10)

            return SwingSetup(
                symbol=symbol,
                pattern=pattern,
                direction="LONG",
                strength=strength,
                entry_zone=round(close, 2),
                stop_loss=round(sl, 2),
                target_1=round(tp1, 2),
                target_2=round(tp2, 2),
                risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
                timeframe=timeframe,
                description=description,
                indicators={
                    "bb_width": round(current_width, 4),
                    "avg_width": round(avg_width, 4),
                    "squeeze_ratio": round(min_width / max(avg_width, 0.001), 2),
                    "vol_ratio": round(vol_ratio, 1),
                    "watchlist": watchlist,
                    "watchlist_reason": watch_reason,
                    "min_actionable_vol_ratio": round(min_actionable_ratio, 1),
                },
            )
        elif close < bb_lower * 1.002:
            sl = close + atr * 2
            tp1 = close - atr * 4
            tp2 = close - atr * 6

            strength = 70
            pattern = "BB Squeeze Breakout (Bearish)"
            timeframe = "3-7 days"
            description = (
                "BB Squeeze + bearish expansion. "
                f"{'Volume confirmed ✓' if vol_confirmed else 'Volume weak ⚠️'}. "
                "Expect continuation lower."
            )
            watchlist = False
            watch_reason = ""

            if low_volume_watchlist:
                strength = min(strength, 68)
                pattern = f"{pattern} (Watchlist)"
                timeframe = "4-10 days"
                description += (
                    f" Volume ratio {vol_ratio:.1f}x is below actionable minimum "
                    f"{min_actionable_ratio:.1f}x; wait for fresh confirmation."
                )
                watchlist = True
                watch_reason = "low_volume"
            elif vol_confirmed:
                strength = min(95, strength + 10)

            return SwingSetup(
                symbol=symbol,
                pattern=pattern,
                direction="SHORT",
                strength=strength,
                entry_zone=round(close, 2),
                stop_loss=round(sl, 2),
                target_1=round(tp1, 2),
                target_2=round(tp2, 2),
                risk_reward=round((close - tp1) / max(sl - close, 0.01), 1),
                timeframe=timeframe,
                description=description,
                indicators={
                    "bb_width": round(current_width, 4),
                    "avg_width": round(avg_width, 4),
                    "vol_ratio": round(vol_ratio, 1),
                    "watchlist": watchlist,
                    "watchlist_reason": watch_reason,
                    "min_actionable_vol_ratio": round(min_actionable_ratio, 1),
                },
            )
    return None


# ── Pattern 7: 52-Week High Proximity Breakout ────────────────────────────────


def _check_52w_high_breakout(
    df: pd.DataFrame, symbol: str, atr: float
) -> Optional[SwingSetup]:
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

    # 52-week high from prior history (excluding current candle being evaluated)
    lookback_52w = df.iloc[-min(252, len(df)) : -1]
    if lookback_52w.empty:
        return None
    high_52w = float(lookback_52w["high"].max())

    if high_52w <= 0:
        return None

    proximity_pct = (high_52w - close) / high_52w * 100  # % below 52W high

    vol_ratio, _vr, vol_avg = _rolling_vol_ratio(df)

    # ── HARD VOLUME BLOCK ──────────────────────────────────────────
    HARD_VOL_BLOCK = float(
        getattr(settings, "SWING_VOL_52W_BREAKOUT_HARD_BLOCK", 0.5) or 0.5
    )
    if vol_avg > 0 and vol_ratio < HARD_VOL_BLOCK:
        return None  # Block entirely
    # ──────────────────────────────────────────────────────────────

    # ── ADX GATE ───────────────────────────────────────────────────
    adx_ok, _ = _adx_gate_ok(df)
    if not adx_ok:
        return None
    # ───────────────────────────────────────────────────────────────

    # ── 2-bar breakout confirmation for 52W proximity ──────────────
    # When price is within 1% (i.e. effectively breaking out), require prior close
    # also within proximity OR above to avoid single-candle false breakouts.
    confirm_2bar = bool(getattr(settings, "SWING_REQUIRE_2BAR_BREAKOUT_CONFIRM", False))
    if confirm_2bar and proximity_pct < 1.0 and len(df) >= 2:
        prev_close = float(df.iloc[-2]["close"])
        prev_prox = (high_52w - prev_close) / high_52w * 100
        if prev_prox > 1.5:  # Yesterday wasn't also near the high = single-bar spike
            return None

    within_3pct = proximity_pct <= 3.0
    vol_strong = vol_avg <= 0 or vol_ratio >= settings.SWING_VOL_52W_BREAKOUT
    min_actionable_ratio = float(
        getattr(settings, "SWING_VOL_52W_BREAKOUT_MIN_ACTIONABLE", 1.0) or 1.0
    )
    low_volume_watchlist = vol_avg > 0 and vol_ratio < min_actionable_ratio

    rsi_healthy = 55 <= rsi <= 75
    trend_intact = ema20 > ema50 if ema20 > 0 and ema50 > 0 else True

    if not (within_3pct and rsi_healthy and trend_intact):
        return None

    sl = high_52w * 0.96
    tp1 = high_52w * 1.05
    tp2 = high_52w * 1.12

    strength = 65
    if proximity_pct < 1.0:
        strength += 15  # Almost touching or breaking 52W high
    if rsi > 65:
        strength += 5

    pattern = "52-Week High Proximity Breakout"
    timeframe = "5-10 days"
    description = (
        f"Within {proximity_pct:.1f}% of 52W high ₹{high_52w:.0f}. "
        f"Healthy RSI at {rsi:.0f}. Institutional accumulation signal. "
        f"{'Volume confirmed ✓' if vol_strong else 'Volume weak ⚠️'}"
    )
    watchlist = False
    watch_reason = ""

    if low_volume_watchlist:
        strength = min(strength + 5, 68)  # Capped strength
        pattern = f"{pattern} (Watchlist)"
        description += f" Volume ratio {vol_ratio:.1f}x is below actionable minimum {min_actionable_ratio:.1f}x; wait for volume."
        watchlist = True
        watch_reason = "low_volume"
    elif vol_strong:
        strength = min(90, strength + 20)  # Pattern bonus

    return SwingSetup(
        symbol=symbol,
        pattern=pattern,
        direction="LONG",
        strength=min(95, strength),
        entry_zone=round(close, 2),
        stop_loss=round(sl, 2),
        target_1=round(tp1, 2),
        target_2=round(tp2, 2),
        risk_reward=round((tp1 - close) / max(close - sl, 0.01), 1),
        timeframe=timeframe,
        description=description,
        indicators={
            "high_52w": round(high_52w, 2),
            "proximity_pct": round(proximity_pct, 2),
            "rsi": round(rsi, 1),
            "vol_ratio": round(vol_ratio, 1),
            "watchlist": watchlist,
            "watchlist_reason": watch_reason,
            "min_actionable_vol_ratio": round(min_actionable_ratio, 1),
        },
    )


# ── Pattern 8: 3-Candle Reversal at Support ───────────────────────────────────


def _check_support_reversal(
    df: pd.DataFrame, symbol: str, atr: float
) -> Optional[SwingSetup]:
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
    closes_3 = [
        float(df.iloc[-3]["close"]),
        float(df.iloc[-2]["close"]),
        float(df.iloc[-1]["close"]),
    ]
    ascending = closes_3[0] < closes_3[1] < closes_3[2]
    if not ascending:
        return None

    # Volume pattern: shrinking (day1 > day2 > day3 — selling drying up)
    vols_3 = [
        float(df.iloc[-3].get("volume", 0)),
        float(df.iloc[-2].get("volume", 0)),
        float(df.iloc[-1].get("volume", 0)),
    ]
    vol_shrinking = (
        vols_3[0] > 0 and vols_3[1] > 0 and vols_3[0] >= vols_3[1] >= vols_3[2]
    )

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
        indicators={
            "support": round(support, 2),
            "rsi": round(rsi, 1),
            "fib_38": round(fib_38, 2),
            "fib_62": round(fib_62, 2),
            "vol_shrinking": vol_shrinking,
        },
    )


# ── Pattern 9: Post-Result Gap-Up Momentum ────────────────────────────────────


def _check_post_result_gap(
    df: pd.DataFrame, symbol: str, atr: float
) -> Optional[SwingSetup]:
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
            vol_avg = (
                float(df["volume"].tail(20).mean()) if "volume" in df.columns else 0
            )
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

        gap_vol_ratio = gap_vol / max(vol_avg, 1) if vol_avg > 0 else 1.0

        # ── HARD VOLUME BLOCK ──────────────────────────────────────────
        HARD_VOL_BLOCK = float(
            getattr(settings, "SWING_VOL_POST_RESULT_HARD_BLOCK", 1.0) or 1.0
        )
        if vol_avg > 0 and gap_vol_ratio < HARD_VOL_BLOCK:
            continue  # Block entirely for this gap day
        # ──────────────────────────────────────────────────────────────

        vol_strong = vol_avg <= 0 or gap_vol_ratio >= settings.SWING_VOL_POST_RESULT
        min_actionable_ratio = float(
            getattr(settings, "SWING_VOL_POST_RESULT_MIN_ACTIONABLE", 2.0) or 2.0
        )
        low_volume_watchlist = vol_avg > 0 and gap_vol_ratio < min_actionable_ratio

        if not gap_held:
            continue

        # Current price must still be above the gap
        current_close = float(df.iloc[-1]["close"])
        if current_close < prev_close:  # Gap filled → setup invalid
            continue

        sl = prev_close  # If gap fills → exit
        tp1 = round(gap_close * 1.06, 2)
        tp2 = round(gap_close * 1.12, 2)

        strength = min(95, 70 + 25)  # Pattern bonus = +25 (highest — earnings catalyst)

        pattern = "Post-Result Gap-Up Momentum"
        timeframe = "5-15 days"
        description = (
            f"Gap up {gap_pct:.1f}% on {gap_vol_ratio:.1f}x avg volume. "
            f"Gap held — momentum continuation expected. "
            f"{'Volume confirmed ✓' if vol_strong else 'Volume weak ⚠️'}"
        )
        watchlist = False
        watch_reason = ""

        if low_volume_watchlist:
            strength = min(strength, 75)  # Capped strength
            pattern = f"{pattern} (Watchlist)"
            description += f" Volume ratio {gap_vol_ratio:.1f}x is below actionable minimum {min_actionable_ratio:.1f}x; wait for volume."
            watchlist = True
            watch_reason = "low_volume"

        return SwingSetup(
            symbol=symbol,
            pattern=pattern,
            direction="LONG",
            strength=strength,
            entry_zone=round(current_close, 2),
            stop_loss=round(sl, 2),
            target_1=tp1,
            target_2=tp2,
            risk_reward=round((tp1 - current_close) / max(current_close - sl, 0.01), 1),
            timeframe=timeframe,
            description=description,
            indicators={
                "gap_pct": round(gap_pct, 1),
                "prev_close": round(prev_close, 2),
                "gap_close": round(gap_close, 2),
                "vol_ratio": round(gap_vol_ratio, 1),
                "watchlist": watchlist,
                "watchlist_reason": watch_reason,
                "min_actionable_vol_ratio": round(min_actionable_ratio, 1),
            },
        )
    return None


# ── Portfolio scan with sector momentum (Fix 5) ───────────────────────────────


def compute_sector_momentum_map(
    symbol_data: dict[str, pd.DataFrame],
) -> dict[str, float]:
    """
    Average 5-day % change per sector across member symbols (excludes index names).

    Used by scan_all_symbols and single-symbol swing lookup for consistent tags.
    """
    from config.settings import NSE_SECTOR_MAP

    sector_5d: dict[str, list[float]] = {}
    for sym, df in symbol_data.items():
        sector = NSE_SECTOR_MAP.get(sym, "Other")
        if sector.startswith("_"):  # Skip indices
            continue
        if df is None or df.empty or len(df) < 6:
            continue
        try:
            pct_5d = (
                (float(df.iloc[-1]["close"]) - float(df.iloc[-6]["close"]))
                / max(float(df.iloc[-6]["close"]), 0.01)
                * 100
            )
            sector_5d.setdefault(sector, []).append(pct_5d)
        except Exception:
            pass

    agg = str(
        getattr(settings, "SWING_SECTOR_MOMENTUM_AGG", "median") or "median"
    ).lower()

    def _aggregate(vals: list[float]) -> float:
        if not vals:
            return 0.0
        if agg == "mean":
            return float(sum(vals) / len(vals))
        # Default: median — resists one heavyweight moving the whole sector tag.
        return float(np.median(vals))

    return {sec: round(_aggregate(vals), 2) for sec, vals in sector_5d.items() if vals}


def _check_1h_mtf_bullish(df_1h: "pd.DataFrame | None") -> "tuple[bool | None, str]":
    """
    Confirm bullish swing setup using live 1H OHLCV indicators.
    Requires >= 2 of 3 checks: EMA20 cross, RSI < 70, MACD hist up.
    Returns:
        (True, reason)  -> confirmed
        (False, reason) -> unconfirmed
        (None, reason)  -> data unavailable (neutral, 0 bonus)
    """
    if df_1h is None or len(df_1h) < 20:
        return None, "1H data unavailable (neutral)"
    try:
        last = df_1h.iloc[-1]
        ok, why = 0, []
        # 1. Price > EMA20
        if "EMA_20" in df_1h.columns:
            _c = "close" if "close" in df_1h.columns else "Close"
            _cl = float(last.get(_c, 0) or 0)
            _e20 = float(last.get("EMA_20", 0) or 0)
            if _cl > 0 and _e20 > 0 and _cl > _e20:
                ok += 1
                why.append("1H>EMA20")
        # 2. RSI not overbought
        if "RSI_14" in df_1h.columns:
            _rsi = float(last.get("RSI_14", 50) or 50)
            if _rsi < 70:
                ok += 1
                why.append(f"1H RSI {_rsi:.0f}")
        # 3. MACD hist positive or improving
        if "MACD_hist" in df_1h.columns and len(df_1h) >= 2:
            _h1 = float(df_1h["MACD_hist"].iloc[-1] or 0)
            _h0 = float(df_1h["MACD_hist"].iloc[-2] or 0)
            if _h1 > 0 or _h1 > _h0:
                ok += 1
                why.append("1H MACD up")
        return ok >= 2, (" | ".join(why) or "no 1H signals")
    except Exception as _e:
        return None, f"1H check skipped ({_e})"


def scan_all_symbols(
    symbol_data: dict[str, pd.DataFrame],
    market_context: dict | None = None,
    open_trades: list[dict] | None = None,
    capital: float | None = None,
    df_1h_cache: "dict | None" = None,
) -> list[dict]:
    """
    Scan all symbols for swing setups with sector momentum + news context.

    Args:
        symbol_data: {display_name: daily_df_with_indicators}
        market_context: Optional index trend/regime for per-symbol regime gating.
        open_trades: Optional list of open positions for overnight risk checks.
        capital: Optional portfolio capital override for position sizing.

    Returns:
        List of dicts (JSON-serializable swing setups), sorted by strength.
    """
    from config.settings import NSE_SECTOR_MAP

    sector_momentum_map = compute_sector_momentum_map(symbol_data)
    include_watchlist = bool(
        getattr(settings, "SWING_INCLUDE_WATCHLIST_IN_PORTFOLIO_SCAN", False)
    )

    # ── Fetch news context scores (cached — non-blocking) ──────────────────
    news_contexts: dict[str, dict] = {}
    try:
        from data.providers.gdelt import get_gdelt_provider
        _gd = get_gdelt_provider()
        for sym in symbol_data:
            clean_s = sym.replace(".NS", "").replace(".BO", "").strip().upper()
            cached_s = _gd._cache.get(clean_s)
            if cached_s:
                t_score = float(cached_s[1].get("tone_score", 0.0) or 0.0)
                news_contexts[sym] = {
                    "context_score": round(t_score / 10.0, 2),
                    "news_sentiment_24hr": round(t_score / 10.0, 2),
                    "news_sentiment_7d": round(t_score / 10.0, 2),
                    "weekly_trend_bias": cached_s[1].get("sentiment", "NEUTRAL"),
                    "news_volume_spike": False,
                    "event_risk_active": False,
                }
    except Exception:
        pass
    try:
        from agents.data_ingestion.context_fetcher import build_context_fetcher

        _cf = build_context_fetcher()
        if _cf is not None:
            for sym in symbol_data:
                try:
                    snap = _cf.get_snapshot_sync(sym)
                    news_contexts[sym] = {
                        "context_score": float(snap.context_score),
                        "news_sentiment_24hr": float(snap.news_sentiment_24hr),
                        "news_sentiment_7d": float(snap.news_sentiment_7d),
                        "weekly_trend_bias": str(snap.weekly_trend_bias),
                        "news_volume_spike": bool(snap.news_volume_spike),
                        "event_risk_active": bool(snap.event_risk_active),
                    }
                except Exception:
                    pass
    except Exception:
        pass  # Context layer optional — scanner works without it

    # ── Scan each symbol ───────────────────────────────────────────────────
    all_setups = []
    for symbol, df in symbol_data.items():
        sector = NSE_SECTOR_MAP.get(symbol, "Other")
        momentum = sector_momentum_map.get(sector, 0.0)
        sym_news = news_contexts.get(symbol)
        mtf_hint = _resolve_mtf_trend(df)
        _df_1h = (df_1h_cache or {}).get(symbol)
        try:
            setups = scan_swing_setups(
                df,
                symbol,
                sector_momentum=momentum,
                sector=sector,
                market_context=market_context,
                mtf_trend=mtf_hint,
                news_context=sym_news,
                open_trades=open_trades,
                capital=capital,
                df_1h=_df_1h,
            )
        except Exception as exc:
            logger.error("Swing scan failed for %s in full scan: %s", symbol, exc)
            continue
        for s in setups:
            # Portfolio shortlist defaults to actionable setups only.
            # Watchlist rows stay available in symbol lookup/debug paths.
            indicators = getattr(s, "indicators", None) or {}
            is_watchlist = bool(indicators.get("watchlist")) or (
                "Watchlist" in str(getattr(s, "pattern", ""))
            )
            if is_watchlist and not include_watchlist:
                continue
            all_setups.append(asdict(s))

    all_setups.sort(key=lambda x: x["strength"], reverse=True)
    return all_setups
