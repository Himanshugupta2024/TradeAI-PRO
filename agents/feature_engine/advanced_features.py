"""
Advanced Feature Engineering for Stock Market Prediction.

Adds high-signal features that significantly improve ML model accuracy:

  Module                    | Features Added
  ──────────────────────────┼──────────────────────────────────────────────
  Ichimoku Cloud            | Tenkan, Kijun, Senkou A/B, Chikou, cloud pos
  Pivot Points              | Classic + Fibonacci R1/R2/R3, S1/S2/S3
  Time-Based Features       | Hour, day-of-week, session, time-to-close
  Volatility Features       | Realized vol, Garman-Klass vol, vol ratio
  Price Action Features     | Swing H/L, HH/LL streak, price structure
  Heikin-Ashi Features      | HA body ratio, HA trend, HA reversal signal
  ──────────────────────────┴──────────────────────────────────────────────

All features are dimensionless / normalized so the model generalises
across different price levels and asset classes.

Usage:
    from agents.feature_engine.advanced_features import calculate_advanced_features
    df = calculate_advanced_features(df)   # adds ~30 new columns in-place
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import logging

log = logging.getLogger(__name__)


# ── Ichimoku Cloud ─────────────────────────────────────────────────────────────

def _ichimoku(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
    displacement: int = 26,
) -> dict[str, pd.Series]:
    """
    Ichimoku Kinko Hyo — complete cloud calculation.

    Components:
        Tenkan-sen  (Conversion Line): (9-period high + low) / 2
        Kijun-sen   (Base Line):       (26-period high + low) / 2
        Senkou A    (Leading Span A):  (Tenkan + Kijun) / 2, shifted +26
        Senkou B    (Leading Span B):  (52-period high + low) / 2, shifted +26
        Chikou Span (Lagging Span):    Close shifted -26

    Derived signals (dimensionless, model-friendly):
        cloud_position:  +1 = above cloud, -1 = below cloud, 0 = inside
        tk_cross:        +1 = Tenkan > Kijun (bullish), -1 = bearish
        cloud_thickness: (Senkou A - Senkou B) / close  — normalized
        price_vs_kijun:  (close - Kijun) / close * 100  — % distance
        price_vs_tenkan: (close - Tenkan) / close * 100 — % distance
    """
    tenkan = (high.rolling(tenkan_period).max() + low.rolling(tenkan_period).min()) / 2
    kijun  = (high.rolling(kijun_period).max()  + low.rolling(kijun_period).min())  / 2

    senkou_a = ((tenkan + kijun) / 2).shift(displacement)
    senkou_b = ((high.rolling(senkou_b_period).max() + low.rolling(senkou_b_period).min()) / 2).shift(displacement)
    chikou   = close.shift(-displacement)

    # Cloud position: is price above, inside, or below the cloud?
    cloud_top    = pd.concat([senkou_a, senkou_b], axis=1).max(axis=1)
    cloud_bottom = pd.concat([senkou_a, senkou_b], axis=1).min(axis=1)

    cloud_pos = pd.Series(0.0, index=close.index)
    cloud_pos[close > cloud_top]    =  1.0   # Above cloud — bullish
    cloud_pos[close < cloud_bottom] = -1.0   # Below cloud — bearish

    # TK Cross: Tenkan vs Kijun
    tk_cross = pd.Series(0.0, index=close.index)
    tk_cross[tenkan > kijun] =  1.0
    tk_cross[tenkan < kijun] = -1.0

    # Cloud thickness (normalized by price)
    safe_close = close.replace(0, np.nan)
    cloud_thickness = ((senkou_a - senkou_b) / safe_close * 100).fillna(0).clip(-20, 20)

    # Price distance from Kijun and Tenkan (%)
    price_vs_kijun  = ((close - kijun)  / safe_close * 100).fillna(0).clip(-20, 20)
    price_vs_tenkan = ((close - tenkan) / safe_close * 100).fillna(0).clip(-20, 20)

    return {
        "ichimoku_tenkan":       tenkan,
        "ichimoku_kijun":        kijun,
        "ichimoku_senkou_a":     senkou_a,
        "ichimoku_senkou_b":     senkou_b,
        "ichimoku_cloud_pos":    cloud_pos,
        "ichimoku_tk_cross":     tk_cross,
        "ichimoku_cloud_thick":  cloud_thickness,
        "ichimoku_vs_kijun":     price_vs_kijun,
        "ichimoku_vs_tenkan":    price_vs_tenkan,
    }


# ── Pivot Points ───────────────────────────────────────────────────────────────

def _pivot_points(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    lookback: int = 20,
) -> dict[str, pd.Series]:
    """
    Rolling Pivot Points (Classic + Fibonacci) using a lookback window.

    Classic:
        PP = (H + L + C) / 3
        R1 = 2*PP - L,  R2 = PP + (H - L),  R3 = H + 2*(PP - L)
        S1 = 2*PP - H,  S2 = PP - (H - L),  S3 = L - 2*(H - PP)

    Fibonacci:
        R1_fib = PP + 0.382*(H - L)
        R2_fib = PP + 0.618*(H - L)
        R3_fib = PP + 1.000*(H - L)
        S1_fib = PP - 0.382*(H - L)
        S2_fib = PP - 0.618*(H - L)
        S3_fib = PP - 1.000*(H - L)

    Derived (model-friendly, normalized):
        pivot_pos:      (close - PP) / close * 100  — % above/below pivot
        r1_dist:        (R1 - close) / close * 100  — % to nearest resistance
        s1_dist:        (close - S1) / close * 100  — % to nearest support
        fib_zone:       which Fibonacci zone price is in (-3 to +3)
    """
    roll_high = high.rolling(lookback, min_periods=5).max()
    roll_low  = low.rolling(lookback, min_periods=5).min()
    roll_close = close.rolling(lookback, min_periods=5).apply(lambda x: x.iloc[-1])

    pp = (roll_high + roll_low + roll_close) / 3
    hl_range = roll_high - roll_low

    # Classic levels
    r1 = 2 * pp - roll_low
    r2 = pp + hl_range
    r3 = roll_high + 2 * (pp - roll_low)
    s1 = 2 * pp - roll_high
    s2 = pp - hl_range
    s3 = roll_low - 2 * (roll_high - pp)

    # Fibonacci levels
    r1_fib = pp + 0.382 * hl_range
    r2_fib = pp + 0.618 * hl_range
    r3_fib = pp + 1.000 * hl_range
    s1_fib = pp - 0.382 * hl_range
    s2_fib = pp - 0.618 * hl_range
    s3_fib = pp - 1.000 * hl_range

    safe_close = close.replace(0, np.nan)

    # Normalized distances
    pivot_pos = ((close - pp) / safe_close * 100).fillna(0).clip(-10, 10)
    r1_dist   = ((r1 - close) / safe_close * 100).fillna(5).clip(0, 10)
    s1_dist   = ((close - s1) / safe_close * 100).fillna(5).clip(0, 10)

    # Fibonacci zone: which fib level is price closest to?
    fib_levels = pd.concat([s3_fib, s2_fib, s1_fib, pp, r1_fib, r2_fib, r3_fib], axis=1)
    fib_levels.columns = [-3, -2, -1, 0, 1, 2, 3]
    fib_dist = fib_levels.sub(close, axis=0).abs()
    fib_zone = pd.Series(0.0, index=fib_dist.index)
    valid_rows = fib_dist.notna().any(axis=1)
    fib_zone.loc[valid_rows] = fib_dist.loc[valid_rows].idxmin(axis=1).astype(float)

    return {
        "pivot_pp":       pp,
        "pivot_r1":       r1,
        "pivot_s1":       s1,
        "pivot_r1_fib":   r1_fib,
        "pivot_s1_fib":   s1_fib,
        "pivot_pos":      pivot_pos,
        "pivot_r1_dist":  r1_dist,
        "pivot_s1_dist":  s1_dist,
        "pivot_fib_zone": fib_zone,
    }


# ── Time-Based Features ────────────────────────────────────────────────────────

def _time_features(df: pd.DataFrame) -> dict[str, pd.Series]:
    """
    Extract time-based features from the timestamp column.

    Features:
        hour_sin / hour_cos:    Cyclical encoding of hour (0-23)
        dow_sin  / dow_cos:     Cyclical encoding of day-of-week (0-6)
        session:                0=Asia, 1=London, 2=NewYork, 3=Overlap
        is_monday / is_friday:  Day-of-week flags (high volatility days)
        month_sin / month_cos:  Cyclical encoding of month (1-12)

    Cyclical encoding prevents the model from treating hour 23 and hour 0
    as far apart — they are actually adjacent in time.
    """
    n = len(df)
    zeros = pd.Series(0.0, index=df.index)

    if "timestamp" not in df.columns:
        return {
            "hour_sin": zeros, "hour_cos": zeros,
            "dow_sin":  zeros, "dow_cos":  zeros,
            "session":  zeros,
            "is_monday": zeros, "is_friday": zeros,
            "month_sin": zeros, "month_cos": zeros,
        }

    try:
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")

        hour = ts.dt.hour.fillna(12).astype(float)
        dow  = ts.dt.dayofweek.fillna(2).astype(float)   # 0=Mon, 6=Sun
        month = ts.dt.month.fillna(6).astype(float)

        # Cyclical encoding
        hour_sin = np.sin(2 * np.pi * hour / 24)
        hour_cos = np.cos(2 * np.pi * hour / 24)
        dow_sin  = np.sin(2 * np.pi * dow  / 7)
        dow_cos  = np.cos(2 * np.pi * dow  / 7)
        month_sin = np.sin(2 * np.pi * (month - 1) / 12)
        month_cos = np.cos(2 * np.pi * (month - 1) / 12)

        # Trading session (UTC hours)
        # Asia:    00-08 UTC
        # London:  08-13 UTC
        # NY:      13-17 UTC
        # Overlap: 13-17 UTC (London + NY)
        session = pd.Series(0.0, index=df.index)  # Default: Asia
        session[(hour >= 8)  & (hour < 13)]  = 1.0   # London
        session[(hour >= 13) & (hour < 17)]  = 3.0   # Overlap (highest vol)
        session[(hour >= 17) & (hour < 22)]  = 2.0   # NY afternoon

        is_monday = (dow == 0).astype(float)
        is_friday = (dow == 4).astype(float)

        return {
            "hour_sin":   pd.Series(hour_sin.values,  index=df.index),
            "hour_cos":   pd.Series(hour_cos.values,  index=df.index),
            "dow_sin":    pd.Series(dow_sin.values,   index=df.index),
            "dow_cos":    pd.Series(dow_cos.values,   index=df.index),
            "session":    session,
            "is_monday":  is_monday,
            "is_friday":  is_friday,
            "month_sin":  pd.Series(month_sin.values, index=df.index),
            "month_cos":  pd.Series(month_cos.values, index=df.index),
        }
    except Exception as e:
        log.debug("Time features failed: %s", e)
        return {
            "hour_sin": zeros, "hour_cos": zeros,
            "dow_sin":  zeros, "dow_cos":  zeros,
            "session":  zeros,
            "is_monday": zeros, "is_friday": zeros,
            "month_sin": zeros, "month_cos": zeros,
        }


# ── Volatility Features ────────────────────────────────────────────────────────

def _volatility_features(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    open_: pd.Series,
) -> dict[str, pd.Series]:
    """
    Advanced volatility features for regime detection and risk sizing.

    Features:
        realized_vol_5:   5-bar realized volatility (annualized %)
        realized_vol_20:  20-bar realized volatility (annualized %)
        gk_vol:           Garman-Klass volatility estimator (more efficient than close-to-close)
        vol_ratio:        realized_vol_5 / realized_vol_20 — vol acceleration
        vol_zscore:       z-score of current vol vs 50-bar rolling mean
        parkinson_vol:    Parkinson volatility (uses H-L range, more efficient)
        vol_regime:       0=low, 1=normal, 2=high volatility (based on percentile)
    """
    safe_close = close.replace(0, np.nan)
    log_ret = np.log(safe_close / safe_close.shift(1))

    # Realized volatility (annualized, assuming 252 trading days, 24 bars/day for crypto)
    rv5  = log_ret.rolling(5,  min_periods=3).std() * np.sqrt(252 * 24) * 100
    rv20 = log_ret.rolling(20, min_periods=10).std() * np.sqrt(252 * 24) * 100

    # Garman-Klass volatility (uses OHLC — more efficient than close-to-close)
    # GK = 0.5*(ln(H/L))^2 - (2*ln(2)-1)*(ln(C/O))^2
    safe_open = open_.replace(0, np.nan)
    hl_log = np.log(high.replace(0, np.nan) / low.replace(0, np.nan))
    co_log = np.log(safe_close / safe_open)
    gk_daily = 0.5 * hl_log**2 - (2 * np.log(2) - 1) * co_log**2
    gk_vol = gk_daily.rolling(20, min_periods=5).mean().apply(
        lambda x: np.sqrt(max(x, 0)) * np.sqrt(252 * 24) * 100
    ).fillna(0)

    # Parkinson volatility (uses H-L range only)
    pk_daily = (1 / (4 * np.log(2))) * hl_log**2
    parkinson_vol = pk_daily.rolling(20, min_periods=5).mean().apply(
        lambda x: np.sqrt(max(x, 0)) * np.sqrt(252 * 24) * 100
    ).fillna(0)

    # Vol ratio: short-term vs long-term (>1 = vol expanding, <1 = contracting)
    vol_ratio = (rv5 / rv20.replace(0, np.nan)).fillna(1.0).clip(0, 5)

    # Vol z-score: how unusual is current vol vs recent history?
    rv20_mean = rv20.rolling(50, min_periods=20).mean()
    rv20_std  = rv20.rolling(50, min_periods=20).std().replace(0, np.nan)
    vol_zscore = ((rv20 - rv20_mean) / rv20_std).fillna(0).clip(-3, 3)

    # Vol regime: 0=low (<33rd pct), 1=normal (33-67th), 2=high (>67th)
    rv20_pct33 = rv20.rolling(100, min_periods=30).quantile(0.33)
    rv20_pct67 = rv20.rolling(100, min_periods=30).quantile(0.67)
    vol_regime = pd.Series(1.0, index=close.index)
    vol_regime[rv20 < rv20_pct33] = 0.0
    vol_regime[rv20 > rv20_pct67] = 2.0

    return {
        "realized_vol_5":  rv5.fillna(0).clip(0, 200),
        "realized_vol_20": rv20.fillna(0).clip(0, 200),
        "gk_vol":          gk_vol.clip(0, 200),
        "parkinson_vol":   parkinson_vol.clip(0, 200),
        "vol_ratio":       vol_ratio,
        "vol_zscore":      vol_zscore,
        "vol_regime":      vol_regime,
    }


# ── Price Action Features ──────────────────────────────────────────────────────

def _price_action_features(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    lookback: int = 20,
) -> dict[str, pd.Series]:
    """
    Price structure and swing point features.

    Features:
        swing_high_dist:  % distance from recent swing high (resistance)
        swing_low_dist:   % distance from recent swing low (support)
        hh_streak:        consecutive higher-highs count (trend strength)
        ll_streak:        consecutive lower-lows count (trend strength)
        price_percentile: where is close in the N-bar range? (0=bottom, 1=top)
        range_expansion:  current bar range vs avg range (>1 = expanding)
        close_vs_open_5:  close vs open 5 bars ago (short-term momentum)
    """
    safe_close = close.replace(0, np.nan)

    # Swing high/low (rolling max/min)
    swing_high = high.rolling(lookback, min_periods=5).max()
    swing_low  = low.rolling(lookback, min_periods=5).min()

    swing_high_dist = ((swing_high - close) / safe_close * 100).fillna(2).clip(0, 20)
    swing_low_dist  = ((close - swing_low)  / safe_close * 100).fillna(2).clip(0, 20)

    # Price percentile in N-bar range (0=at low, 1=at high)
    price_range = (swing_high - swing_low).replace(0, np.nan)
    price_percentile = ((close - swing_low) / price_range).fillna(0.5).clip(0, 1)

    # Higher-highs / lower-lows streak
    # Positive = consecutive HH, Negative = consecutive LL
    hh = (high > high.shift(1)).astype(int)
    ll = (low  < low.shift(1)).astype(int)

    hh_streak = pd.Series(0, index=close.index, dtype="int8")
    ll_streak = pd.Series(0, index=close.index, dtype="int8")
    hh_count = 0
    ll_count = 0
    for i in range(len(close)):
        if hh.iloc[i]:
            hh_count = min(hh_count + 1, 10)
        else:
            hh_count = 0
        if ll.iloc[i]:
            ll_count = min(ll_count + 1, 10)
        else:
            ll_count = 0
        hh_streak.iloc[i] = hh_count
        ll_streak.iloc[i] = ll_count

    # Range expansion: current bar range vs 20-bar avg range
    bar_range = high - low
    avg_range = bar_range.rolling(20, min_periods=5).mean().replace(0, np.nan)
    range_expansion = (bar_range / avg_range).fillna(1.0).clip(0, 5)

    # Close vs open 5 bars ago (short-term momentum)
    open_5_ago = close.shift(5)
    close_vs_open_5 = ((close - open_5_ago) / open_5_ago.replace(0, np.nan) * 100).fillna(0).clip(-10, 10)

    return {
        "swing_high_dist":   swing_high_dist,
        "swing_low_dist":    swing_low_dist,
        "price_percentile":  price_percentile,
        "hh_streak":         hh_streak.astype(float),
        "ll_streak":         ll_streak.astype(float),
        "range_expansion":   range_expansion,
        "close_vs_open_5":   close_vs_open_5,
    }


# ── Heikin-Ashi Features ───────────────────────────────────────────────────────

def _heikin_ashi_features(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
) -> dict[str, pd.Series]:
    """
    Heikin-Ashi candle features — smoothed price action signals.

    Heikin-Ashi removes noise by averaging OHLC values:
        HA_Close = (O + H + L + C) / 4
        HA_Open  = (prev_HA_Open + prev_HA_Close) / 2
        HA_High  = max(H, HA_Open, HA_Close)
        HA_Low   = min(L, HA_Open, HA_Close)

    Features:
        ha_body_ratio:    HA body / HA range (0=doji, 1=strong candle)
        ha_direction:     +1 = bullish HA candle, -1 = bearish
        ha_trend_streak:  consecutive same-direction HA candles
        ha_no_lower_wick: 1 if HA has no lower wick (strong uptrend signal)
        ha_no_upper_wick: 1 if HA has no upper wick (strong downtrend signal)
    """
    ha_close = (open_ + high + low + close) / 4

    # HA Open: iterative calculation
    ha_open = pd.Series(np.nan, index=open_.index)
    ha_open.iloc[0] = (open_.iloc[0] + close.iloc[0]) / 2
    for i in range(1, len(ha_open)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2

    ha_high = pd.concat([high, ha_open, ha_close], axis=1).max(axis=1)
    ha_low  = pd.concat([low,  ha_open, ha_close], axis=1).min(axis=1)

    ha_range = (ha_high - ha_low).replace(0, np.nan)
    ha_body  = (ha_close - ha_open).abs()

    ha_body_ratio = (ha_body / ha_range).fillna(0.5).clip(0, 1)
    ha_direction  = np.sign(ha_close - ha_open).fillna(0)

    # Trend streak: consecutive same-direction HA candles
    ha_trend_streak = pd.Series(0, index=open_.index, dtype="int8")
    streak = 0
    prev_dir = 0
    for i in range(len(ha_direction)):
        d = int(ha_direction.iloc[i])
        if d == prev_dir and d != 0:
            streak = min(streak + 1, 10) if d > 0 else max(streak - 1, -10)
        elif d != 0:
            streak = d
        else:
            streak = 0
        ha_trend_streak.iloc[i] = streak
        if d != 0:
            prev_dir = d

    # No lower wick = strong uptrend (HA low == HA open)
    ha_no_lower_wick = (ha_low >= ha_open - 1e-8).astype(float)
    # No upper wick = strong downtrend (HA high == HA open)
    ha_no_upper_wick = (ha_high <= ha_open + 1e-8).astype(float)

    return {
        "ha_body_ratio":      ha_body_ratio,
        "ha_direction":       ha_direction,
        "ha_trend_streak":    ha_trend_streak.astype(float),
        "ha_no_lower_wick":   ha_no_lower_wick,
        "ha_no_upper_wick":   ha_no_upper_wick,
    }


# ── Master Function ────────────────────────────────────────────────────────────

def calculate_advanced_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate all advanced features and add them to the DataFrame.

    Expected input columns: open, high, low, close, volume
    Optional: timestamp (for time-based features)

    Added columns (~30 new features):
        Ichimoku:      ichimoku_cloud_pos, ichimoku_tk_cross, ichimoku_cloud_thick,
                       ichimoku_vs_kijun, ichimoku_vs_tenkan
        Pivot Points:  pivot_pos, pivot_r1_dist, pivot_s1_dist, pivot_fib_zone
        Time:          hour_sin, hour_cos, dow_sin, dow_cos, session,
                       is_monday, is_friday, month_sin, month_cos
        Volatility:    realized_vol_5, realized_vol_20, gk_vol, parkinson_vol,
                       vol_ratio, vol_zscore, vol_regime
        Price Action:  swing_high_dist, swing_low_dist, price_percentile,
                       hh_streak, ll_streak, range_expansion, close_vs_open_5
        Heikin-Ashi:   ha_body_ratio, ha_direction, ha_trend_streak,
                       ha_no_lower_wick, ha_no_upper_wick

    Returns:
        DataFrame with all new columns added (original columns preserved).
    """
    if df.empty:
        return df

    result = df.copy()

    high   = result["high"]
    low    = result["low"]
    close  = result["close"]
    open_  = result["open"]

    # ── 1. Ichimoku Cloud ──────────────────────────────────────────────────
    try:
        ichi = _ichimoku(high, low, close)
        for col, series in ichi.items():
            result[col] = series
        log.debug("Ichimoku features added: %d cols", len(ichi))
    except Exception as e:
        log.warning("Ichimoku calculation failed: %s", e)

    # ── 2. Pivot Points ────────────────────────────────────────────────────
    try:
        pivots = _pivot_points(high, low, close)
        for col, series in pivots.items():
            result[col] = series
        log.debug("Pivot point features added: %d cols", len(pivots))
    except Exception as e:
        log.debug("Pivot point calculation skipped: %s", e)

    # ── 3. Time-Based Features ─────────────────────────────────────────────
    try:
        time_feats = _time_features(result)
        for col, series in time_feats.items():
            result[col] = series
        log.debug("Time features added: %d cols", len(time_feats))
    except Exception as e:
        log.warning("Time feature calculation failed: %s", e)

    # ── 4. Volatility Features ─────────────────────────────────────────────
    try:
        vol_feats = _volatility_features(high, low, close, open_)
        for col, series in vol_feats.items():
            result[col] = series
        log.debug("Volatility features added: %d cols", len(vol_feats))
    except Exception as e:
        log.warning("Volatility feature calculation failed: %s", e)

    # ── 5. Price Action Features ───────────────────────────────────────────
    try:
        pa_feats = _price_action_features(high, low, close)
        for col, series in pa_feats.items():
            result[col] = series
        log.debug("Price action features added: %d cols", len(pa_feats))
    except Exception as e:
        log.warning("Price action feature calculation failed: %s", e)

    # ── 6. Heikin-Ashi Features ────────────────────────────────────────────
    try:
        ha_feats = _heikin_ashi_features(open_, high, low, close)
        for col, series in ha_feats.items():
            result[col] = series
        log.debug("Heikin-Ashi features added: %d cols", len(ha_feats))
    except Exception as e:
        log.warning("Heikin-Ashi feature calculation failed: %s", e)

    total_new = len(result.columns) - len(df.columns)
    # Per-symbol churn: use DEBUG so hosted logs stay readable (set LOG_LEVEL=DEBUG when tuning).
    log.debug("Advanced features: +%d columns added (total: %d)", total_new, len(result.columns))

    return result


# ── Feature List (for ML models) ──────────────────────────────────────────────

ADVANCED_FEATURE_COLS = [
    # Ichimoku (5 features)
    "ichimoku_cloud_pos",
    "ichimoku_tk_cross",
    "ichimoku_cloud_thick",
    "ichimoku_vs_kijun",
    "ichimoku_vs_tenkan",
    # Pivot Points (4 features)
    "pivot_pos",
    "pivot_r1_dist",
    "pivot_s1_dist",
    "pivot_fib_zone",
    # Time-Based (9 features)
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "session",
    "is_monday",
    "is_friday",
    "month_sin",
    "month_cos",
    # Volatility (7 features)
    "realized_vol_5",
    "realized_vol_20",
    "gk_vol",
    "parkinson_vol",
    "vol_ratio",
    "vol_zscore",
    "vol_regime",
    # Price Action (7 features)
    "swing_high_dist",
    "swing_low_dist",
    "price_percentile",
    "hh_streak",
    "ll_streak",
    "range_expansion",
    "close_vs_open_5",
    # Heikin-Ashi (5 features)
    "ha_body_ratio",
    "ha_direction",
    "ha_trend_streak",
    "ha_no_lower_wick",
    "ha_no_upper_wick",
]
