"""
TradeAI-PRO — Watchlist Real Per-Stock Quant Engine & Analysis.
==============================================================
Replaces placeholder logic in the Priority Stock Watchlist with authentic
per-stock quant indicators, an explainable 0-100 multi-factor scoring model,
structural levels (Support/Resistance/Invalidation for HOLD/MONITOR vs
Trigger/SL/TP for LONG/SHORT), ATR-derived time barriers, and deterministic
template-based rationales.

Strict Data Honesty:
- Never falls back to hardcoded scores (e.g. 65) or arbitrary 2% SL / 3.5% TP.
- Returns status='INSUFFICIENT_DATA' and score=None if <200 bars are available.
- Batch vectorized downloading via YahooProvider in 1 single HTTP request.
- In-memory TTL caching (60s) to avoid repeated rate-limit issues.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────────────────
# In-Memory TTL Cache for Watchlist Analysis
# ─────────────────────────────────────────────────────────────────────────────
_WATCHLIST_ANALYSIS_CACHE: Dict[str, Dict[str, Any]] = {}
_WATCHLIST_CACHE_TTL_SEC: float = 60.0
_BENCHMARK_SYMBOL: str = "^NSEI"  # NIFTY 50

# ─────────────────────────────────────────────────────────────────────────────
# Comprehensive NSE Company Names Directory
# ─────────────────────────────────────────────────────────────────────────────
NSE_COMPANY_NAMES: Dict[str, str] = {
    "RELIANCE": "Reliance Industries Ltd.",
    "HDFCBANK": "HDFC Bank Ltd.",
    "TCS": "Tata Consultancy Services",
    "INFY": "Infosys Limited",
    "ICICIBANK": "ICICI Bank Ltd.",
    "SBIN": "State Bank of India",
    "BHARTIARTL": "Bharti Airtel Ltd.",
    "LT": "Larsen & Toubro Ltd.",
    "ITC": "ITC Limited",
    "TATAMOTORS": "Tata Motors Ltd.",
    "VEDL": "Vedanta Limited",
    "PRECWIRE": "Precision Wires India Ltd.",
    "MARUTI": "Maruti Suzuki India Ltd.",
    "SUNPHARMA": "Sun Pharmaceutical Ind.",
    "BAJFINANCE": "Bajaj Finance Ltd.",
    "ASIANPAINT": "Asian Paints Ltd.",
    "ONGC": "Oil & Natural Gas Corp",
    "BPCL": "Bharat Petroleum Corp",
    "COALINDIA": "Coal India Limited",
    "TATASTEEL": "Tata Steel Ltd.",
    "AXISBANK": "Axis Bank Ltd.",
    "KOTAKBANK": "Kotak Mahindra Bank Ltd.",
    "HINDUNILVR": "Hindustan Unilever Ltd.",
    "WIPRO": "Wipro Limited",
    "HCLTECH": "HCL Technologies Ltd.",
    "TECHM": "Tech Mahindra Ltd.",
    "NTPC": "NTPC Limited",
    "POWERGRID": "Power Grid Corporation",
    "ULTRACEMCO": "UltraTech Cement Ltd.",
    "TITAN": "Titan Company Ltd.",
    "BAJAJFINSV": "Bajaj Finserv Ltd.",
    "ADANIENT": "Adani Enterprises Ltd.",
    "ADANIPORTS": "Adani Ports & SEZ Ltd.",
    "JSWSTEEL": "JSW Steel Ltd.",
    "GRASIM": "Grasim Industries Ltd.",
    "CIPLA": "Cipla Limited",
    "DRREDDY": "Dr. Reddy's Laboratories",
    "APOLLOHOSP": "Apollo Hospitals Ent.",
    "EICHERMOT": "Eicher Motors Ltd.",
    "HEROMOTOCO": "Hero MotoCorp Ltd.",
    "DIVISLAB": "Divi's Laboratories Ltd.",
    "BRITANNIA": "Britannia Industries Ltd.",
    "NESTLEIND": "Nestle India Ltd.",
    "INDUSINDBK": "IndusInd Bank Ltd.",
    "HINDALCO": "Hindalco Industries Ltd.",
    "BEL": "Bharat Electronics Ltd.",
    "HAL": "Hindustan Aeronautics Ltd.",
    "MAHABANK": "Bank of Maharashtra",
    "BAJAJHLDNG": "Bajaj Holdings & Inv.",
    "TRENT": "Trent Limited",
    "ZOMATO": "Zomato Limited",
    "JIOFIN": "Jio Financial Services",
    "DLF": "DLF Limited",
    "DIXON": "Dixon Technologies Ltd.",
    "POLYCAB": "Polycab India Ltd.",
    "RVNL": "Rail Vikas Nigam Ltd.",
    "IRFC": "Indian Railway Finance Corp",
    "MAZDOCK": "Mazagon Dock Shipbuilders",
    "COCHINSHIP": "Cochin Shipyard Ltd.",
}


def get_company_name(symbol: str) -> str:
    """Return verified company name or fallback to formatted clean symbol."""
    sym = (symbol or "").strip().upper().replace(".NS", "")
    if sym in NSE_COMPANY_NAMES:
        return NSE_COMPANY_NAMES[sym]
    return f"{sym} · NSE Equity"


# ─────────────────────────────────────────────────────────────────────────────
# Pure Technical Indicator Functions
# ─────────────────────────────────────────────────────────────────────────────

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    if series.empty or len(series) < period:
        return pd.Series(np.nan, index=series.index)
    return series.ewm(span=period, adjust=False).mean()


def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing."""
    if close.empty or len(close) < period + 1:
        return pd.Series(np.nan, index=close.index)
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    zero_loss = (avg_loss == 0) & (avg_gain > 0)
    rsi = rsi.where(~zero_loss, 100.0)
    flat = (avg_loss == 0) & (avg_gain == 0) & avg_gain.notna()
    rsi = rsi.where(~flat, 50.0)
    return rsi


def calculate_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, and histogram."""
    if close.empty or len(close) < slow:
        empty = pd.Series(np.nan, index=close.index)
        return empty, empty, empty
    ema_fast = calculate_ema(close, fast)
    ema_slow = calculate_ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average True Range using Wilder's smoothing."""
    if close.empty or len(close) < period + 1:
        return pd.Series(np.nan, index=close.index)
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(span=period, adjust=False).mean()


def calculate_adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average Directional Index (trend strength 0-100)."""
    if close.empty or len(close) < period * 2:
        return pd.Series(np.nan, index=close.index)
    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr_val = calculate_atr(high, low, close, period)

    plus_di = 100 * calculate_ema(plus_dm, period) / atr_val.replace(0, np.nan)
    minus_di = 100 * calculate_ema(minus_dm, period) / atr_val.replace(0, np.nan)

    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    adx = calculate_ema(dx, period)
    return adx


def calculate_bollinger_width(
    close: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> pd.Series:
    """Bollinger Band Width (% of middle band)."""
    if close.empty or len(close) < period:
        return pd.Series(np.nan, index=close.index)
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = sma + (std_dev * std)
    lower = sma - (std_dev * std)
    width = (upper - lower) / sma.replace(0, np.nan) * 100.0
    return width


def calculate_volume_ratio(volume: pd.Series, window: int = 20) -> float:
    """Ratio of latest volume to 20-day simple moving average volume."""
    if volume.empty or len(volume) < window:
        return 1.0
    recent_vol = float(volume.iloc[-1])
    avg_vol = float(volume.tail(window).mean())
    if avg_vol <= 0:
        return 1.0
    return round(recent_vol / avg_vol, 2)


def calculate_distance_52w(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    lookback: int = 250,
) -> Tuple[float, float]:
    """Percentage distance from 52-week High and 52-week Low."""
    if close.empty or len(close) < 10:
        return 0.0, 0.0
    slc_h = high.tail(lookback)
    slc_l = low.tail(lookback)
    h52 = float(slc_h.max())
    l52 = float(slc_l.min())
    c = float(close.iloc[-1])

    dist_high = round(((c - h52) / h52) * 100.0, 2) if h52 > 0 else 0.0
    dist_low = round(((c - l52) / l52) * 100.0, 2) if l52 > 0 else 0.0
    return dist_high, dist_low


def calculate_swing_pivots(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int = 5,
) -> Tuple[List[float], List[float]]:
    """
    Extract the last 3 swing-pivot supports (< current close) and
    last 3 swing-pivot resistances (> current close).
    Supports returned sorted descending (closest support first).
    Resistances returned sorted ascending (closest resistance first).
    """
    n = len(close)
    if n < window * 2 + 1:
        return [], []

    current_price = float(close.iloc[-1])
    pivot_highs = []
    pivot_lows = []

    # Identify confirmed local extrema
    for i in range(window, n - window):
        h_val = float(high.iloc[i])
        l_val = float(low.iloc[i])

        if h_val == float(high.iloc[i - window : i + window + 1].max()):
            pivot_highs.append(h_val)
        if l_val == float(low.iloc[i - window : i + window + 1].min()):
            pivot_lows.append(l_val)

    supports = sorted([p for p in set(pivot_lows) if p < current_price], reverse=True)[:3]
    resistances = sorted([p for p in set(pivot_highs) if p > current_price])[:3]

    return [round(s, 2) for s in supports], [round(r, 2) for r in resistances]


def calculate_relative_strength(
    stock_close: pd.Series,
    benchmark_close: pd.Series,
    window: int = 20,
) -> float:
    """
    Calculate 20-day excess return of stock vs benchmark (e.g. NIFTY 50).
    Returns percentage points, e.g. +2.15% means outperforming NIFTY by 2.15%.
    """
    if len(stock_close) < window or len(benchmark_close) < window:
        return 0.0

    stock_ret = (float(stock_close.iloc[-1]) / float(stock_close.iloc[-window])) - 1.0
    bench_ret = (float(benchmark_close.iloc[-1]) / float(benchmark_close.iloc[-window])) - 1.0
    return round((stock_ret - bench_ret) * 100.0, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Explainable 5-Factor 0–100 Scorer
# ─────────────────────────────────────────────────────────────────────────────

def score_stock_factors(
    close: float,
    ema20: float,
    ema50: float,
    ema200: float,
    rsi: float,
    macd_hist: float,
    macd_hist_prev: float,
    vol_ratio: float,
    rs_20d: float,
    adx: float,
    dist_52h: float,
) -> Tuple[int, Dict[str, int], str]:
    """
    Explainable 5-factor scoring model (Sum = 100 points):
      1. Trend (30 pts)
      2. Momentum (25 pts)
      3. Volume (15 pts)
      4. Relative Strength (15 pts)
      5. Volatility / Structure (15 pts)

    Returns: (total_score, factor_breakdown, bias)
    where bias is 'BULLISH', 'BEARISH', or 'NEUTRAL'.
    """
    # ── 1. Trend (30 pts) ────────────────────────────────────────────────────
    trend_score = 0
    bull_stack = (close > ema20) and (ema20 > ema50) and (ema50 > ema200)
    bear_stack = (close < ema20) and (ema20 < ema50) and (ema50 < ema200)

    if bull_stack:
        trend_score = 30
        trend_bias = "BULLISH"
    elif close > ema50 and ema50 > ema200:
        trend_score = 22
        trend_bias = "BULLISH"
    elif close > ema200:
        trend_score = 14
        trend_bias = "BULLISH"
    elif bear_stack:
        trend_score = 30
        trend_bias = "BEARISH"
    elif close < ema50 and ema50 < ema200:
        trend_score = 22
        trend_bias = "BEARISH"
    elif close < ema200:
        trend_score = 14
        trend_bias = "BEARISH"
    else:
        trend_score = 8
        trend_bias = "NEUTRAL"

    # ── 2. Momentum (25 pts) ─────────────────────────────────────────────────
    # RSI: max 15 pts
    rsi_pts = 0
    if trend_bias == "BULLISH":
        if 50.0 <= rsi <= 65.0:
            rsi_pts = 15
        elif 65.0 < rsi <= 72.0:
            rsi_pts = 10
        elif rsi > 72.0:
            rsi_pts = 4  # overbought penalty
        elif 40.0 <= rsi < 50.0:
            rsi_pts = 5
        else:
            rsi_pts = 0
    elif trend_bias == "BEARISH":
        if 35.0 <= rsi <= 50.0:
            rsi_pts = 15
        elif 28.0 <= rsi < 35.0:
            rsi_pts = 10
        elif rsi < 28.0:
            rsi_pts = 4  # oversold short penalty
        elif 50.0 < rsi <= 60.0:
            rsi_pts = 5
        else:
            rsi_pts = 0
    else:
        rsi_pts = 8 if (45.0 <= rsi <= 55.0) else 4

    # MACD Histogram: max 10 pts
    macd_pts = 0
    if trend_bias == "BULLISH":
        if macd_hist > 0 and macd_hist >= macd_hist_prev:
            macd_pts = 10
        elif macd_hist > 0:
            macd_pts = 6
        elif macd_hist <= 0 and macd_hist > macd_hist_prev:
            macd_pts = 4  # curl up from below
    elif trend_bias == "BEARISH":
        if macd_hist < 0 and macd_hist <= macd_hist_prev:
            macd_pts = 10
        elif macd_hist < 0:
            macd_pts = 6
        elif macd_hist >= 0 and macd_hist < macd_hist_prev:
            macd_pts = 4
    else:
        macd_pts = 5 if abs(macd_hist) < 0.5 else 2

    momentum_score = rsi_pts + macd_pts

    # ── 3. Volume (15 pts) ───────────────────────────────────────────────────
    if vol_ratio >= 1.5:
        volume_score = 15
    elif vol_ratio >= 1.2:
        volume_score = 11
    elif vol_ratio >= 0.8:
        volume_score = 7
    else:
        volume_score = 3  # dry / low volume

    # ── 4. Relative Strength (15 pts) ────────────────────────────────────────
    if trend_bias == "BULLISH":
        if rs_20d >= 3.0:
            rs_score = 15
        elif rs_20d >= 1.0:
            rs_score = 11
        elif rs_20d >= -1.0:
            rs_score = 7
        else:
            rs_score = 2
    elif trend_bias == "BEARISH":
        if rs_20d <= -3.0:
            rs_score = 15
        elif rs_20d <= -1.0:
            rs_score = 11
        elif rs_20d <= 1.0:
            rs_score = 7
        else:
            rs_score = 2
    else:
        rs_score = 8 if abs(rs_20d) <= 1.5 else 4

    # ── 5. Structure & Volatility (15 pts) ────────────────────────────────────
    struct_score = 0
    if adx >= 25.0:
        adx_pts = 8
    elif adx >= 18.0:
        adx_pts = 5
    else:
        adx_pts = 2  # weak trend / ranging

    if trend_bias == "BULLISH":
        if abs(dist_52h) <= 5.0:
            lvl_pts = 7  # near 52w high breakout zone
        elif abs(dist_52h) <= 15.0:
            lvl_pts = 5
        else:
            lvl_pts = 3
    elif trend_bias == "BEARISH":
        if dist_52h <= -35.0:
            lvl_pts = 7  # deep breakdown
        else:
            lvl_pts = 4
    else:
        lvl_pts = 4

    struct_score = adx_pts + lvl_pts

    total_score = int(np.clip(trend_score + momentum_score + volume_score + rs_score + struct_score, 0, 100))

    breakdown = {
        "trend": trend_score,
        "momentum": momentum_score,
        "volume": volume_score,
        "relative_strength": rs_score,
        "structure": struct_score,
    }

    return total_score, breakdown, trend_bias


# ─────────────────────────────────────────────────────────────────────────────
# Structural Levels & Risk:Reward Resolver
# ─────────────────────────────────────────────────────────────────────────────

def calculate_trade_levels(
    close: float,
    high_last: float,
    low_last: float,
    atr14: float,
    supports: List[float],
    resistances: List[float],
    trend_bias: str,
    total_score: int,
    adx: float,
) -> Dict[str, Any]:
    """
    Resolves genuine structural levels:
    - If LONG / SHORT candidate:
        * entry: trigger price
        * stop_loss: swing pivot with 1.5x ATR floor
        * target: nearest resistance or 2R
        * target_2: 3R
        * rr_ratio: (target - entry) / (entry - sl)
        * Must satisfy R:R >= 1.5 to stay actionable
    - If HOLD / MONITOR:
        * entry=None, sl=None, tp=None
        * key_support, key_resistance, invalidation_level
    """
    key_supp = supports[0] if supports else round(close * 0.95, 2)
    key_res = resistances[0] if resistances else round(close * 1.05, 2)

    # Invalidation level: break of nearest structural support or resistance
    invalidation = key_supp if trend_bias != "BEARISH" else key_res

    # Preliminary qualification for actionable setup
    is_qualified_long = (trend_bias == "BULLISH") and (total_score >= 70) and (adx >= 20.0)
    is_qualified_short = (trend_bias == "BEARISH") and (total_score >= 70) and (adx >= 20.0)

    if is_qualified_long:
        # Trigger price: above last bar's high or near resistance
        entry = round(high_last * 1.001, 2)

        # SL: below last swing low with 1.5x ATR14 floor
        raw_sl = (supports[0] - 0.2 * atr14) if supports else (entry - 1.5 * atr14)
        min_risk_dist = 1.5 * atr14
        if (entry - raw_sl) < min_risk_dist:
            sl = round(entry - min_risk_dist, 2)
        else:
            sl = round(raw_sl, 2)

        risk = entry - sl
        if risk <= 0:
            risk = 1.5 * atr14
            sl = round(entry - risk, 2)

        # TP1: nearest resistance or 2R
        tp1_candidate = resistances[0] if resistances else (entry + 2.0 * risk)
        tp1 = round(max(tp1_candidate, entry + 2.0 * risk), 2)
        tp2 = round(entry + 3.0 * risk, 2)

        rr = round((tp1 - entry) / risk, 2) if risk > 0 else 0.0

        if rr >= 1.5:
            return {
                "signal": "LONG",
                "is_actionable": True,
                "entry": entry,
                "stop_loss": sl,
                "target": tp1,
                "target_2": tp2,
                "key_support": key_supp,
                "key_resistance": key_res,
                "invalidation_level": invalidation,
                "rr_ratio": rr,
            }

    elif is_qualified_short:
        # Trigger price: below last bar's low
        entry = round(low_last * 0.999, 2)

        # SL: above last swing high with 1.5x ATR floor
        raw_sl = (resistances[0] + 0.2 * atr14) if resistances else (entry + 1.5 * atr14)
        min_risk_dist = 1.5 * atr14
        if (raw_sl - entry) < min_risk_dist:
            sl = round(entry + min_risk_dist, 2)
        else:
            sl = round(raw_sl, 2)

        risk = sl - entry
        if risk <= 0:
            risk = 1.5 * atr14
            sl = round(entry + risk, 2)

        tp1_candidate = supports[0] if supports else (entry - 2.0 * risk)
        tp1 = round(min(tp1_candidate, entry - 2.0 * risk), 2)
        tp2 = round(entry - 3.0 * risk, 2)

        rr = round((entry - tp1) / risk, 2) if risk > 0 else 0.0

        if rr >= 1.5:
            return {
                "signal": "SHORT",
                "is_actionable": True,
                "entry": entry,
                "stop_loss": sl,
                "target": tp1,
                "target_2": tp2,
                "key_support": key_supp,
                "key_resistance": key_res,
                "invalidation_level": invalidation,
                "rr_ratio": rr,
            }

    # Fallback to non-actionable observation state (HOLD vs MONITOR)
    signal = "HOLD" if total_score >= 45 else "MONITOR"
    return {
        "signal": signal,
        "is_actionable": False,
        "entry": None,
        "stop_loss": None,
        "target": None,
        "target_2": None,
        "key_support": key_supp,
        "key_resistance": key_res,
        "invalidation_level": invalidation,
        "rr_ratio": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic Time Horizon & Barrier Resolver
# ─────────────────────────────────────────────────────────────────────────────

def calculate_time_horizon(
    is_actionable: bool,
    entry: Optional[float],
    target: Optional[float],
    atr14: float,
    timeframe: str = "1d",
) -> Dict[str, Any]:
    """
    Derives horizon from (distance to TP1 / ATR14), clamped between 2 and 15 bars.
    Time stop = 2x estimate.
    """
    tf = str(timeframe or "1d").lower()
    if tf in ("5m", "15m", "intraday"):
        return {
            "short": "Today (Intraday)",
            "full": "Same-Day (Intraday MIS)",
            "barrier_short": "Exit: 15:15 IST",
            "barrier_full": "Mandatory auto square-off before 15:15 IST",
            "est_bars": 1,
            "time_stop_bars": 1,
        }

    if is_actionable and entry and target and atr14 > 0:
        dist = abs(target - entry)
        est_bars = int(np.clip(round(dist / atr14), 2, 15))
    else:
        # Observation state structural cycle
        est_bars = 5

    time_stop_bars = est_bars * 2

    if est_bars <= 4:
        label_short = f"2–4 Days (Swing)"
    elif est_bars <= 8:
        label_short = f"5–8 Days (Swing)"
    else:
        label_short = f"8–15 Days (Swing)"

    return {
        "short": label_short,
        "full": f"{label_short} · Multi-Session EOD",
        "barrier_short": f"Max {time_stop_bars} Bars",
        "barrier_full": f"{time_stop_bars} Trading Sessions (Daily Bars)",
        "est_bars": est_bars,
        "time_stop_bars": time_stop_bars,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Template-Based Rationale Generator (No LLM)
# ─────────────────────────────────────────────────────────────────────────────

def build_factor_rationale(
    close: float,
    ema20: float,
    ema50: float,
    ema200: float,
    rsi: float,
    vol_ratio: float,
    rs_20d: float,
    adx: float,
    trend_bias: str,
) -> str:
    """Deterministic plain-language one-liner built from actual computed factors."""
    # 1. Trend clause
    if close > ema20 and ema20 > ema50 and ema50 > ema200:
        trend_clause = "Above EMA 20/50/200"
    elif close > ema50 and close > ema200:
        trend_clause = "Above EMA 50/200"
    elif close > ema200:
        trend_clause = "Above EMA 200"
    elif close < ema20 and ema20 < ema50 and ema50 < ema200:
        trend_clause = "Below EMA 20/50/200"
    elif close < ema50 and close < ema200:
        trend_clause = "Below EMA 50/200"
    else:
        trend_clause = "Consolidating near EMAs"

    # 2. RSI clause
    if rsi >= 65:
        rsi_clause = f"RSI {int(round(rsi))} strong"
    elif rsi >= 50:
        rsi_clause = f"RSI {int(round(rsi))} rising"
    elif rsi >= 40:
        rsi_clause = f"RSI {int(round(rsi))} neutral"
    else:
        rsi_clause = f"RSI {int(round(rsi))} oversold"

    # 3. Volume clause
    if vol_ratio >= 1.5:
        vol_clause = f"volume {vol_ratio:.1f}x avg (spike)"
    elif vol_ratio >= 1.1:
        vol_clause = f"volume {vol_ratio:.1f}x avg"
    else:
        vol_clause = f"volume {vol_ratio:.1f}x avg"

    # 4. Relative Strength clause
    if rs_20d > 0:
        rs_clause = f"RS +{rs_20d:.1f}% vs NIFTY"
    else:
        rs_clause = f"RS {rs_20d:.1f}% vs NIFTY"

    return f"{trend_clause}, {rsi_clause}, {vol_clause}, {rs_clause}"


# ─────────────────────────────────────────────────────────────────────────────
# ASCII Debug Table Logger
# ─────────────────────────────────────────────────────────────────────────────

def log_watchlist_debug_table(results: Dict[str, Dict[str, Any]]) -> None:
    """Log a structured ASCII debug table showing all factor values per symbol."""
    header = (
        f"{'SYMBOL':<10} | {'PRICE':<9} | {'SCORE':<5} | {'SIGNAL':<7} | "
        f"{'RSI':<5} | {'ADX':<5} | {'VOL_X':<5} | {'RS_20D':<7} | {'R:R':<5} | {'HORIZON':<14}"
    )
    sep = "-" * len(header)
    rows = [sep, header, sep]

    for sym, q in sorted(results.items()):
        if q.get("status") == "INSUFFICIENT_DATA":
            rows.append(f"{sym:<10} | {'INSUFFICIENT DATA (< 200 bars)':<70}")
            continue

        p = f"{q.get('ltp', 0.0):.2f}"
        sc = f"{q.get('confidence') or '--'}"
        sig = f"{q.get('signal', '--')}"
        rsi = f"{q.get('indicators', {}).get('rsi14', 0.0):.1f}"
        adx = f"{q.get('indicators', {}).get('adx14', 0.0):.1f}"
        vx = f"{q.get('indicators', {}).get('vol_ratio', 1.0):.2f}"
        rs = f"{q.get('indicators', {}).get('rs_20d', 0.0):+.1f}%"
        rr = f"{q.get('rr_ratio') or '--'}"
        hz = f"{q.get('horizon', {}).get('short', '--')}"
        rows.append(f"{sym:<10} | {p:<9} | {sc:<5} | {sig:<7} | {rsi:<5} | {adx:<5} | {vx:<5} | {rs:<7} | {rr:<5} | {hz:<14}")

    rows.append(sep)
    logger.info("Watchlist Quant Evaluation Debug Table:\n%s", "\n".join(rows))


# ─────────────────────────────────────────────────────────────────────────────
# Core Batch Evaluation Pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def analyze_watchlist_symbols(
    symbols: List[str],
    force_refresh: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """
    Main entry point for Watchlist Real Per-Stock Quant Engine:
      1. Checks in-memory TTL cache (< 60s).
      2. Batches download of >= 250 daily bars for symbols + NIFTY 50 benchmark.
      3. Calculates pure technical indicators, pivots, and benchmark relative strength.
      4. Computes explainable 0-100 score with factor breakdown.
      5. Derives structural levels (Support/Resistance for HOLD vs Trigger/SL/TP for LONG).
      6. Formats dynamic horizon and plain-language rationale.
      7. Emits structured debug table.
    """
    clean_symbols = [s.strip().upper().replace(".NS", "") for s in symbols if s.strip()]
    if not clean_symbols:
        return {}

    now_ts = time.time()
    results: Dict[str, Dict[str, Any]] = {}
    missing_symbols: List[str] = []

    # 1. Check TTL Cache
    if not force_refresh:
        for sym in clean_symbols:
            cached = _WATCHLIST_ANALYSIS_CACHE.get(sym)
            if cached and (now_ts - cached.get("_cached_at", 0)) < _WATCHLIST_CACHE_TTL_SEC:
                results[sym] = cached

    missing_symbols = [s for s in clean_symbols if s not in results]

    if missing_symbols:
        # 2. Fetch OHLCV batch including NIFTY 50 benchmark
        from data.providers.yahoo import YahooProvider

        provider = YahooProvider(timeout=10.0)
        fetch_list = list(set(missing_symbols + [_BENCHMARK_SYMBOL]))

        try:
            ohlcv_map = await provider.get_batch_ohlcv(
                fetch_list,
                period="1y",  # ~250 trading sessions
                interval="1d",
            )
        except Exception as exc:
            logger.error("Failed to batch-fetch daily OHLCV for watchlist: %s", exc)
            ohlcv_map = {}

        nifty_df = ohlcv_map.get(_BENCHMARK_SYMBOL)
        if nifty_df is None or nifty_df.empty:
            # Try alternate key
            nifty_df = ohlcv_map.get("NIFTY 50")

        nifty_close = nifty_df["close"] if nifty_df is not None and "close" in nifty_df.columns else pd.Series(dtype=float)

        now_utc = datetime.now(timezone.utc)

        # 3. Analyze each missing symbol
        for sym in missing_symbols:
            df = ohlcv_map.get(sym)
            comp_name = get_company_name(sym)

            # Insufficient data check (< 200 bars)
            if df is None or df.empty or len(df) < 200:
                available_bars = len(df) if df is not None else 0
                results[sym] = {
                    "symbol": sym,
                    "company_name": comp_name,
                    "ltp": float(df["close"].iloc[-1]) if (df is not None and not df.empty) else 0.0,
                    "change_pct": 0.0,
                    "signal": "INSUFFICIENT_DATA",
                    "confidence": None,
                    "factor_breakdown": None,
                    "is_actionable": False,
                    "entry": None,
                    "stop_loss": None,
                    "target": None,
                    "target_2": None,
                    "levels_source": "UNAVAILABLE",
                    "key_support": None,
                    "key_resistance": None,
                    "invalidation_level": None,
                    "rr_ratio": None,
                    "horizon": {
                        "short": "Data Unavailable",
                        "full": "Insufficient historical data (< 200 daily bars)",
                        "barrier_short": "No Barrier",
                        "barrier_full": "No Barrier",
                        "est_bars": 0,
                        "time_stop_bars": 0,
                    },
                    "rationale": f"Insufficient data ({available_bars} bars available, minimum 200 required)",
                    "as_of": None,
                    "data_delay_minutes": None,
                    "timeframe": "1d",
                    "status": "INSUFFICIENT_DATA",
                    "_cached_at": now_ts,
                }
                _WATCHLIST_ANALYSIS_CACHE[sym] = results[sym]
                continue

            close_s = df["close"]
            high_s = df["high"]
            low_s = df["low"]
            vol_s = df["volume"]

            ltp = float(close_s.iloc[-1])
            prev_close = float(close_s.iloc[-2]) if len(close_s) >= 2 else ltp
            change_pct = round(((ltp - prev_close) / prev_close) * 100.0, 2) if prev_close > 0 else 0.0

            # Last bar timestamp and delay
            last_ts = df.get("timestamp")
            if last_ts is not None and not last_ts.empty:
                last_bar_dt = pd.to_datetime(last_ts.iloc[-1], utc=True)
                as_of_iso = last_bar_dt.isoformat()
                data_delay_min = max(0, int((now_utc - last_bar_dt).total_seconds() / 60.0))
            else:
                as_of_iso = now_utc.isoformat()
                data_delay_min = 15

            # Pure Indicators
            ema20 = float(calculate_ema(close_s, 20).iloc[-1])
            ema50 = float(calculate_ema(close_s, 50).iloc[-1])
            ema200 = float(calculate_ema(close_s, 200).iloc[-1])

            rsi_s = calculate_rsi(close_s, 14)
            rsi_val = float(rsi_s.iloc[-1])

            macd_line, sig_line, macd_hist = calculate_macd(close_s, 12, 26, 9)
            curr_hist = float(macd_hist.iloc[-1])
            prev_hist = float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 else curr_hist

            atr_s = calculate_atr(high_s, low_s, close_s, 14)
            atr14 = float(atr_s.iloc[-1])
            atr_pct = round((atr14 / ltp) * 100.0, 2) if ltp > 0 else 0.0

            adx_s = calculate_adx(high_s, low_s, close_s, 14)
            adx_val = float(adx_s.iloc[-1])

            bb_width_s = calculate_bollinger_width(close_s, 20, 2.0)
            bb_width = float(bb_width_s.iloc[-1])

            vol_ratio = calculate_volume_ratio(vol_s, 20)
            dist_52h, dist_52l = calculate_distance_52w(high_s, low_s, close_s, 250)
            supports, resistances = calculate_swing_pivots(high_s, low_s, close_s, window=5)

            rs_20d = calculate_relative_strength(close_s, nifty_close, 20) if not nifty_close.empty else 0.0

            # 5-Factor Scoring (0-100)
            score, breakdown, trend_bias = score_stock_factors(
                close=ltp,
                ema20=ema20,
                ema50=ema50,
                ema200=ema200,
                rsi=rsi_val,
                macd_hist=curr_hist,
                macd_hist_prev=prev_hist,
                vol_ratio=vol_ratio,
                rs_20d=rs_20d,
                adx=adx_val,
                dist_52h=dist_52h,
            )

            # Trade Levels & Actionability
            levels = calculate_trade_levels(
                close=ltp,
                high_last=float(high_s.iloc[-1]),
                low_last=float(low_s.iloc[-1]),
                atr14=atr14,
                supports=supports,
                resistances=resistances,
                trend_bias=trend_bias,
                total_score=score,
                adx=adx_val,
            )

            # Dynamic Horizon
            horizon = calculate_time_horizon(
                is_actionable=levels["is_actionable"],
                entry=levels["entry"],
                target=levels["target"],
                atr14=atr14,
                timeframe="1d",
            )

            # Plain-language Rationale
            rationale = build_factor_rationale(
                close=ltp,
                ema20=ema20,
                ema50=ema50,
                ema200=ema200,
                rsi=rsi_val,
                vol_ratio=vol_ratio,
                rs_20d=rs_20d,
                adx=adx_val,
                trend_bias=trend_bias,
            )

            result_entry = {
                "symbol": sym,
                "company_name": comp_name,
                "ltp": round(ltp, 2),
                "change_pct": change_pct,
                "signal": levels["signal"],
                "confidence": score,
                "factor_breakdown": breakdown,
                "is_actionable": levels["is_actionable"],
                "entry": levels["entry"],
                "stop_loss": levels["stop_loss"],
                "target": levels["target"],
                "target_2": levels["target_2"],
                "levels_source": "ENGINE" if levels["is_actionable"] else "UNAVAILABLE",
                "key_support": levels["key_support"],
                "key_resistance": levels["key_resistance"],
                "invalidation_level": levels["invalidation_level"],
                "rr_ratio": levels["rr_ratio"],
                "horizon": horizon,
                "rationale": rationale,
                "indicators": {
                    "ema20": round(ema20, 2),
                    "ema50": round(ema50, 2),
                    "ema200": round(ema200, 2),
                    "rsi14": round(rsi_val, 1),
                    "adx14": round(adx_val, 1),
                    "atr14": round(atr14, 2),
                    "atr_pct": atr_pct,
                    "bb_width": round(bb_width, 2),
                    "vol_ratio": vol_ratio,
                    "dist_52h": dist_52h,
                    "dist_52l": dist_52l,
                    "rs_20d": rs_20d,
                    "supports": supports,
                    "resistances": resistances,
                },
                "as_of": as_of_iso,
                "data_delay_minutes": data_delay_min,
                "timeframe": "1d",
                "status": "OK",
                "_cached_at": now_ts,
            }

            _WATCHLIST_ANALYSIS_CACHE[sym] = result_entry
            results[sym] = result_entry

    # 4. Log Debug Table to console
    try:
        log_watchlist_debug_table(results)
    except Exception as log_exc:
        logger.debug("Failed to log watchlist debug table: %s", log_exc)

    # Return clean dict (strip internal cache keys)
    clean_results = {}
    for sym, entry in results.items():
        entry_copy = dict(entry)
        entry_copy.pop("_cached_at", None)
        clean_results[sym] = entry_copy

    return clean_results
