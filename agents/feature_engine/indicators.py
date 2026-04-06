"""
Technical Indicator Calculator — Pure pandas/numpy implementation.

No external TA library needed. Calculates:
  - RSI, MACD, EMA (20/50/200), ATR (core)
  - VWAP, Bollinger Bands, ADX (Phase 2 additions)
"""

import pandas as pd
import numpy as np

from config import settings


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index.
    Uses Wilder's smoothing (exponential moving average of gains/losses).
    """
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    MACD (Moving Average Convergence Divergence).
    Returns: (macd_line, signal_line, histogram)
    """
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    Average True Range.
    True Range = max(H-L, |H-Prev_C|, |L-Prev_C|)
    ATR = EMA of True Range
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(span=period, adjust=False).mean()


def _vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    Volume Weighted Average Price.
    VWAP = cumsum(typical_price * volume) / cumsum(volume)
    Falls back to typical price when volume is zero (e.g. index data).
    """
    typical_price = (high + low + close) / 3.0

    # If total volume is zero (common for indices), use typical price as proxy
    if volume.sum() == 0:
        return typical_price

    cum_vol = volume.cumsum()
    cum_tp_vol = (typical_price * volume).cumsum()
    vwap = cum_tp_vol / cum_vol.replace(0, np.nan)
    return vwap


def _bollinger_bands(
    close: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Bollinger Bands.
    Returns: (upper_band, middle_band, lower_band)
    """
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = sma + (std_dev * std)
    lower = sma - (std_dev * std)
    return upper, sma, lower


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    Average Directional Index — measures trend strength (0–100).

    ADX > 25 = trending, ADX < 20 = ranging.
    Uses Wilder's smoothing.
    """
    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr_val = _atr(high, low, close, period)

    plus_di = 100 * _ema(plus_dm, period) / atr_val.replace(0, np.nan)
    minus_di = 100 * _ema(minus_dm, period) / atr_val.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _ema(dx, period)
    return adx


def _stoch_rsi(close: pd.Series, rsi_period: int = 14, stoch_period: int = 14,
               k_smooth: int = 3, d_smooth: int = 3) -> tuple[pd.Series, pd.Series]:
    """
    Stochastic RSI — applies stochastic formula on RSI values.
    More sensitive than plain RSI; great for catching early reversals.
    Returns: (K line, D line) both 0–100.
    """
    rsi = _rsi(close, rsi_period)
    rsi_min = rsi.rolling(stoch_period).min()
    rsi_max = rsi.rolling(stoch_period).max()
    rng = (rsi_max - rsi_min).replace(0, np.nan)
    k_raw = (rsi - rsi_min) / rng * 100
    k = k_raw.rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()
    return k, d


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    On-Balance Volume — cumulative volume with sign of price change.
    Rising OBV = accumulation; falling OBV = distribution.
    """
    direction = np.sign(close.diff().fillna(0))
    return (direction * volume).cumsum()


def _mfi(high: pd.Series, low: pd.Series, close: pd.Series,
         volume: pd.Series, period: int = 14) -> pd.Series:
    """
    Money Flow Index — volume-weighted RSI.
    MFI < 20 = oversold, MFI > 80 = overbought.
    """
    typical = (high + low + close) / 3.0
    raw_mf = typical * volume
    pos_mf = raw_mf.where(typical > typical.shift(1), 0.0)
    neg_mf = raw_mf.where(typical < typical.shift(1), 0.0)
    pos_sum = pos_mf.rolling(period).sum()
    neg_sum = neg_mf.rolling(period).sum()
    mfr = pos_sum / neg_sum.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + mfr))


def _roc(close: pd.Series, period: int) -> pd.Series:
    """Rate of Change — percentage price change over N bars."""
    return close.pct_change(period) * 100


def _williams_r(high: pd.Series, low: pd.Series, close: pd.Series,
                period: int = 14) -> pd.Series:
    """
    Williams %R — momentum oscillator (-100 to 0).
    < -80 = oversold, > -20 = overbought.
    """
    highest_high = high.rolling(period).max()
    lowest_low   = low.rolling(period).min()
    rng = (highest_high - lowest_low).replace(0, np.nan)
    return -100.0 * (highest_high - close) / rng


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate all technical indicators on an OHLCV DataFrame.

    Expected input columns: open, high, low, close, volume
    Added columns:
        Core:    RSI_14, MACD, MACD_signal, MACD_hist, EMA_20/50/200, ATR_14
        Bands:   VWAP, BB_upper/middle/lower, BB_width, ADX_14
        New:     StochRSI_K, StochRSI_D, OBV, MFI_14, ROC_5/10/20,
                 Williams_R, BB_position, ATR_pct, volume_ratio_20,
                 volume_ratio_tod, candle_body_ratio, upper_wick_ratio,
                 lower_wick_ratio, close_position_in_range,
                 breakout_from_range, candle_consecutive
    """
    if df.empty:
        return df

    result = df.copy()

    # ── RSI ───────────────────────────────────────────
    result[f"RSI_{settings.RSI_PERIOD}"] = _rsi(result["close"], settings.RSI_PERIOD)

    # ── MACD ──────────────────────────────────────────
    macd_line, signal_line, histogram = _macd(
        result["close"],
        fast=settings.MACD_FAST,
        slow=settings.MACD_SLOW,
        signal=settings.MACD_SIGNAL,
    )
    result["MACD"] = macd_line
    result["MACD_signal"] = signal_line
    result["MACD_hist"] = histogram

    # ── EMAs ──────────────────────────────────────────
    for period in settings.EMA_PERIODS:
        result[f"EMA_{period}"] = _ema(result["close"], period)

    # ── ATR ───────────────────────────────────────────
    result[f"ATR_{settings.ATR_PERIOD}"] = _atr(
        result["high"], result["low"], result["close"],
        period=settings.ATR_PERIOD,
    )

    # ── VWAP ─────────────────────────────────────────
    result["VWAP"] = _vwap(result["high"], result["low"], result["close"], result["volume"])

    # ── Bollinger Bands ──────────────────────────────
    bb_upper, bb_mid, bb_lower = _bollinger_bands(
        result["close"],
        period=settings.BB_PERIOD,
        std_dev=settings.BB_STD,
    )
    result["BB_upper"]  = bb_upper
    result["BB_middle"] = bb_mid
    result["BB_lower"]  = bb_lower

    # BB Width (normalized) — regime detection
    result["BB_width"] = (bb_upper - bb_lower) / bb_mid.replace(0, np.nan)

    # BB Position — where is close within the band? 0=lower, 1=upper
    bb_rng = (bb_upper - bb_lower).replace(0, np.nan)
    result["BB_position"] = ((result["close"] - bb_lower) / bb_rng).clip(0, 1)

    # ── ADX ──────────────────────────────────────────
    result[f"ADX_{settings.ADX_PERIOD}"] = _adx(
        result["high"], result["low"], result["close"],
        period=settings.ADX_PERIOD,
    )

    # ── Stochastic RSI ────────────────────────────────
    result["StochRSI_K"], result["StochRSI_D"] = _stoch_rsi(result["close"])

    # ── OBV ──────────────────────────────────────────
    result["OBV"] = _obv(result["close"], result["volume"])
    # OBV rate-of-change (5-bar) — easier to use in signals than raw OBV
    result["OBV_ROC5"] = result["OBV"].pct_change(5) * 100

    # ── MFI ──────────────────────────────────────────
    result["MFI_14"] = _mfi(result["high"], result["low"], result["close"], result["volume"])

    # ── Rate of Change ────────────────────────────────
    result["ROC_5"]  = _roc(result["close"], 5)
    result["ROC_10"] = _roc(result["close"], 10)
    result["ROC_20"] = _roc(result["close"], 20)

    # ── Williams %R ───────────────────────────────────
    result["Williams_R"] = _williams_r(result["high"], result["low"], result["close"])

    # ── ATR% (relative to price) ─────────────────────
    result["ATR_pct"] = (result[f"ATR_{settings.ATR_PERIOD}"] / result["close"].replace(0, np.nan)) * 100

    # ── Volume ratio (vs 20-bar avg) ─────────────────
    vol_avg_20 = result["volume"].rolling(20, min_periods=5).mean().replace(0, np.nan)
    result["volume_ratio_20"] = result["volume"] / vol_avg_20

    # ── Time-of-day volume ratio ──────────────────────
    # Compare volume to the average for the SAME hour-of-day across recent bars.
    # This is far more accurate than a flat rolling average:
    # market open / close hours naturally have 3-5x average volume.
    if "timestamp" in result.columns:
        try:
            ts = pd.to_datetime(result["timestamp"])
            result["_hour"] = ts.dt.hour
            # Group by hour, compute rolling mean of volume within each hour-bucket
            # Strategy: for each row, take the last 10 instances of the same hour
            tod_avg = pd.Series(np.nan, index=result.index)
            for hour, grp_idx in result.groupby("_hour").groups.items():
                grp_vol = result.loc[grp_idx, "volume"]
                # Expanding mean within this hour group (min 2 samples needed)
                tod_avg.loc[grp_idx] = grp_vol.expanding(min_periods=2).mean().shift(1)
            result["volume_ratio_tod"] = (result["volume"] / tod_avg.replace(0, np.nan)).clip(0, 10)
            result.drop(columns=["_hour"], inplace=True)
        except Exception:
            result["volume_ratio_tod"] = result["volume_ratio_20"]
    else:
        result["volume_ratio_tod"] = result["volume_ratio_20"]

    # ── Candlestick features ─────────────────────────
    candle_range = (result["high"] - result["low"]).replace(0, np.nan)
    body = (result["close"] - result["open"]).abs()
    upper_wick = result["high"] - result[["close", "open"]].max(axis=1)
    lower_wick = result[["close", "open"]].min(axis=1) - result["low"]

    result["candle_body_ratio"]          = (body / candle_range).clip(0, 1)
    result["upper_wick_ratio"]           = (upper_wick / candle_range).clip(0, 1)
    result["lower_wick_ratio"]           = (lower_wick / candle_range).clip(0, 1)
    result["close_position_in_range"]    = ((result["close"] - result["low"]) / candle_range).clip(0, 1)

    # ── Breakout from range ───────────────────────────
    # 1 = closed above recent 20-bar high, -1 = below recent 20-bar low, 0 = inside
    rolling_high = result["high"].rolling(20, min_periods=5).max().shift(1)
    rolling_low  = result["low"].rolling(20, min_periods=5).min().shift(1)
    result["breakout_from_range"] = np.where(
        result["close"] > rolling_high, 1,
        np.where(result["close"] < rolling_low, -1, 0)
    ).astype("int8")

    # ── Consecutive directional candles ───────────────
    # Positive = consecutive up candles, Negative = down candles
    # Helps detect exhaustion (>= 5 in a row) or nascent moves (2-3)
    # Safe from NaN: handle missing values gracefully
    close_open_diff = result["close"] - result["open"]
    direction = np.sign(close_open_diff.fillna(0))  # +1, -1, or 0 (NaN → 0)
    consecutive = pd.Series(0, index=result.index, dtype="int8")
    count = 0
    prev_dir = 0
    for i, d in enumerate(direction):
        d_val = int(d) if pd.notna(d) else 0  # Safe int conversion
        if d_val == prev_dir and d_val != 0:
            count += d_val
        elif d_val != 0:
            count = d_val
        else:
            count = 0
        consecutive.iloc[i] = max(-10, min(10, count))
        prev_dir = d_val if d_val != 0 else prev_dir
    result["candle_consecutive"] = consecutive

    # ── Advanced Features (Ichimoku, Pivot Points, Time, Volatility, etc.) ──
    try:
        from agents.feature_engine.advanced_features import calculate_advanced_features
        result = calculate_advanced_features(result)
    except Exception as _adv_err:
        import logging as _log
        _log.getLogger(__name__).debug("Advanced features skipped: %s", _adv_err)

    return result
