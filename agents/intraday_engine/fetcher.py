from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from agents.data_ingestion.nse_live_quote import NSELiveQuote, get_nse_live_quote

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


def fetch_5m(symbol: str) -> Optional[pd.DataFrame]:
    """5-minute historical bars via dashboard NSE fetcher."""
    try:
        from agents.market_intelligence.nse_fetcher import (
            NSE_SYMBOLS,
            _YF_TO_DISPLAY,
            fetch_ohlcv,
        )
        from api.service import TOP_NSE_SYMBOLS

        clean = symbol.strip().upper().replace(".NS", "").replace("^", "")
        # Allow symbols that are in TOP_NSE_SYMBOLS or NSE_SYMBOLS or _YF_TO_DISPLAY
        if (
            clean not in NSE_SYMBOLS
            and clean not in _YF_TO_DISPLAY
            and clean not in TOP_NSE_SYMBOLS
            and symbol not in NSE_SYMBOLS
            and symbol not in _YF_TO_DISPLAY
            and symbol not in TOP_NSE_SYMBOLS
        ):
            return None

        df = fetch_ohlcv(symbol, "7d", "5m")
        if df is None or df.empty:
            return None
        df = df.copy()

        # Ensure timestamps are properly aligned to IST naive
        ts = pd.to_datetime(df["timestamp"])
        if ts.dt.tz is not None:
            ts = ts.dt.tz_convert(_IST)

        df["timestamp"] = ts.dt.tz_localize(None)
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as exc:
        logger.debug("intraday fetch_5m %s: %s", symbol, exc)
        return None


def merge_live_candle_overlay(
    df: pd.DataFrame, quote: NSELiveQuote
) -> pd.DataFrame:
    """
    Merge the 0-delay NSE live quote into the latest 5m candle DataFrame.

    If the latest bar in df is older than the current 5m slot, appends a new forming
    bar with the live LTP, high, low, volume, and day VWAP.
    If the latest bar is in the current 5m slot, updates its close, high, low, and VWAP.
    Also attaches quote metadata (rvol, live_vwap, quote object) to df.attrs.
    """
    if df is None or df.empty or quote is None or quote.ltp <= 0:
        return df

    out = df.copy()

    last_ts = pd.to_datetime(out["timestamp"].iloc[-1])
    if getattr(last_ts, "tz", None) is not None:
        last_ts = last_ts.tz_convert(_IST).tz_localize(None)
    elif getattr(last_ts, "tzinfo", None) is not None:
        last_ts = last_ts.replace(tzinfo=None)
    now_ist = datetime.now(_IST).replace(tzinfo=None)

    # Round down current time to 5-minute bucket
    current_slot_min = (now_ist.minute // 5) * 5
    current_slot_ts = now_ist.replace(
        minute=current_slot_min, second=0, microsecond=0
    )

    # Check if latest candle is already in the current 5m slot
    diff_sec = (current_slot_ts - last_ts).total_seconds()

    if diff_sec <= 0:
        # Same bar: update in-place with latest tick
        idx = out.index[-1]
        out.loc[idx, "close"] = float(quote.ltp)
        out.loc[idx, "high"] = float(max(out.loc[idx, "high"], quote.ltp))
        out.loc[idx, "low"] = float(min(out.loc[idx, "low"], quote.ltp))
        if "VWAP" in out.columns and quote.vwap > 0:
            out.loc[idx, "VWAP"] = float(quote.vwap)
    else:
        # Forming candle in a newer 5m slot: synthesize and append forming bar
        open_price = float(out["close"].iloc[-1])
        high_price = float(max(open_price, quote.ltp))
        low_price = float(min(open_price, quote.ltp))
        close_price = float(quote.ltp)

        recent_vol = float(out["volume"].tail(5).mean()) if "volume" in out.columns else 1000.0
        if recent_vol <= 0:
            recent_vol = 1000.0

        new_row = {
            "timestamp": current_slot_ts,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": recent_vol,
        }
        if "VWAP" in out.columns and quote.vwap > 0:
            new_row["VWAP"] = float(quote.vwap)

        row_df = pd.DataFrame([new_row])
        out = pd.concat([out, row_df], ignore_index=True)

    # Inject VWAP column if not already present
    if "VWAP" not in out.columns and quote.vwap > 0:
        out["VWAP"] = float(quote.vwap)

    # Attach live metadata directly to output DataFrame
    out.attrs["live_quote"] = quote
    out.attrs["rvol"] = quote.rvol
    out.attrs["live_vwap"] = quote.vwap
    out.attrs["live_ltp"] = quote.ltp

    return out


def fetch_5m_with_live_overlay(
    symbol: str, enable_overlay: bool = True
) -> Optional[pd.DataFrame]:
    """
    Fetch 5m bars and overlay 0-delay NSE live quote for real-time strategy execution.
    """
    df = fetch_5m(symbol)
    if df is None or df.empty:
        return None

    if not enable_overlay:
        return df

    # Estimate 20-day average volume from available bars for RVOL calculation
    avg_vol = 0.0
    if "volume" in df.columns:
        avg_vol = float(df["volume"].mean() * 75.0)

    try:
        quote = get_nse_live_quote(symbol, avg_volume=avg_vol)
        if quote is not None:
            df = merge_live_candle_overlay(df, quote)
    except Exception as exc:
        logger.debug("Live overlay failed for %s (fallback to pure 5m): %s", symbol, exc)

    return df
