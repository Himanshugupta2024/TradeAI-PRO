"""
NSE Data Fetcher — Historical OHLCV via yfinance.

Free, no API key needed. Maps internal symbols to yfinance tickers.
"""

from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf
import signal
from contextlib import contextmanager

from config import settings
from config.logger import get_logger

log = get_logger(__name__)


class TimeoutException(Exception):
    """Raised when a yfinance call times out."""
    pass


@contextmanager
def timeout(seconds: int):
    """Context manager for per-call timeout on Unix/Windows.
    Note: On Windows, SIGALRM is not available; this is a best-effort.
    For production reliability, use async.wait_for() instead.
    """
    def _timeout_handler(signum, frame):
        raise TimeoutException(f"Operation timed out after {seconds}s")
    
    # Save original handler
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)  # Cancel alarm
        signal.signal(signal.SIGALRM, old_handler)  # Restore handler


# Timeframe mapping: our notation → yfinance interval
_TF_MAP = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "1d": "1d",
}

# yfinance has limits on intraday history:
#   5m  → max 60 days
#   15m → max 60 days
#   1h  → max 730 days
#   1d  → unlimited
_MAX_DAYS = {
    "5m": 59,
    "15m": 59,
    "1h": 729,
    "1d": settings.HISTORICAL_DAYS,
}


def fetch_historical(
    symbol: str,
    timeframe: str,
    since_days: int = settings.HISTORICAL_DAYS,
    max_retries: int = 2,
) -> pd.DataFrame:
    """
    Fetch historical OHLCV candles from NSE via yfinance with retries.

    Args:
        symbol: Internal name, e.g. "NIFTY 50", "BANKNIFTY"
        timeframe: Candle interval, e.g. "1d", "1h", "15m", "5m"
        since_days: How many days of history to fetch
        max_retries: Number of retry attempts on network errors

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
        Empty DataFrame on failure (caller must handle gracefully).
    """
    ticker = settings.NSE_TICKER_MAP.get(symbol)
    if not ticker:
        log.error("Unknown symbol: %s", symbol)
        return pd.DataFrame()

    yf_interval = _TF_MAP.get(timeframe)
    if not yf_interval:
        log.error("Unsupported timeframe: %s", timeframe)
        return pd.DataFrame()

    # Cap to yfinance's max history for this timeframe
    effective_days = min(since_days, _MAX_DAYS.get(timeframe, since_days))

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=effective_days)

    log.info("Fetching %s (%s) %s since %s", symbol, ticker, timeframe, start_dt.date())

    last_exc = None
    for attempt in range(max_retries):
        try:
            tk = yf.Ticker(ticker, session=None)  # Fresh session each attempt
            df = tk.history(
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                interval=yf_interval,
                auto_adjust=False,
                timeout=15,  # yfinance timeout per request
            )
            
            if df.empty:
                log.warning("No data returned for %s %s (attempt %d/%d)",
                           symbol, timeframe, attempt + 1, max_retries)
                return pd.DataFrame()
            
            # Success — return immediately
            return _finalize_dataframe(df)
            
        except (TimeoutException, TimeoutError) as e:
            last_exc = e
            log.warning("Timeout fetching %s (attempt %d/%d): %s",
                       symbol, attempt + 1, max_retries, str(e)[:100])
            if attempt < max_retries - 1:
                import time
                time.sleep(0.5 * (2 ** attempt))  # Exponential backoff
        except Exception as e:
            last_exc = e
            log.error("Error fetching %s (attempt %d/%d): %s",
                     symbol, attempt + 1, max_retries, str(e)[:100])
            if attempt < max_retries - 1:
                import time
                time.sleep(0.5 * (2 ** attempt))
    
    log.error("All retries exhausted for %s — last error: %s",
             symbol, str(last_exc)[:100] if last_exc else "Unknown")
    return pd.DataFrame()


def _finalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Standardize and validate a dataframe from yfinance.
    
    Args:
        df: DataFrame from yfinance.Ticker.history()
        
    Returns:
        Standardized DataFrame or empty on validation failure
    """
    if df.empty:
        return df
    
    try:
        # Standardize column names to lowercase
        df = df.rename(columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Adj Close": "adj_close",
            "Volume": "volume",
        })

        # yfinance uses DatetimeIndex — move to column
        df = df.reset_index()
        ts_col = "Datetime" if "Datetime" in df.columns else "Date"
        df = df.rename(columns={ts_col: "timestamp"})

        # Keep only the columns we need
        keep = ["timestamp", "open", "high", "low", "close", "volume"]
        if "adj_close" in df.columns:
            keep.append("adj_close")
        df = df[[c for c in keep if c in df.columns]].copy()
        
        # Validate required columns exist
        if not all(c in df.columns for c in ["timestamp", "open", "high", "low", "close", "volume"]):
            log.error("Missing required columns: %s", df.columns.tolist())
            return pd.DataFrame()
        
        df["timestamp"] = pd.to_datetime(df["timestamp"])

        # Remove timezone info for consistency
        if df["timestamp"].dt.tz is not None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(None)

        # Remove duplicates and sort
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        
        if len(df) > 0:
            log.info("Finalized %d candles (%s → %s)", len(df), df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
        return df
        
    except Exception as e:
        log.error("DataFrame finalization failed: %s", e)
        return pd.DataFrame()
