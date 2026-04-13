"""
Binance Data Fetcher — Historical OHLCV via CCXT.

Uses the public API (no API key required for market data).
Handles pagination (Binance returns max 1000 candles per request).
"""

import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import ccxt

from config import settings
from config.logger import get_logger

log = get_logger(__name__)

_UNSUPPORTED_SYMBOL_UNTIL: dict[str, float] = {}
_UNSUPPORTED_SYMBOL_TTL_SECONDS = 3600


def _is_symbol_temporarily_unsupported(symbol: str) -> bool:
    key = (symbol or "").upper()
    until_ts = _UNSUPPORTED_SYMBOL_UNTIL.get(key)
    if not until_ts:
        return False
    now_ts = time.time()
    if now_ts >= until_ts:
        _UNSUPPORTED_SYMBOL_UNTIL.pop(key, None)
        return False
    return True


def _mark_symbol_unsupported(symbol: str) -> None:
    _UNSUPPORTED_SYMBOL_UNTIL[(symbol or "").upper()] = time.time() + _UNSUPPORTED_SYMBOL_TTL_SECONDS


def _init_exchange() -> ccxt.binance:
    """Create a Binance exchange instance."""
    return ccxt.binance({
        "enableRateLimit": settings.BINANCE_RATE_LIMIT,
        "options": {"defaultType": "spot"},
    })


def fetch_historical(
    symbol: str,
    timeframe: str,
    since_days: int = settings.HISTORICAL_DAYS,
) -> pd.DataFrame:
    """
    Fetch historical OHLCV candles from Binance.

    Args:
        symbol: Trading pair, e.g. "BTC/USDT"
        timeframe: Candle interval, e.g. "1d", "1h", "15m", "5m"
        since_days: How many days of history to fetch (default ~2 years)

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
    """
    if _is_symbol_temporarily_unsupported(symbol):
        log.debug("Skipping unsupported Binance symbol (cached): %s", symbol)
        return pd.DataFrame()

    exchange = _init_exchange()

    since_dt = datetime.now(timezone.utc) - timedelta(days=since_days)
    since_ms = int(since_dt.timestamp() * 1000)

    all_candles = []
    limit = 1000  # Binance max per request
    retries = 0
    max_retries = 5

    log.info("Fetching %s %s since %s", symbol, timeframe, since_dt.date())

    while True:
        try:
            candles = exchange.fetch_ohlcv(
                symbol, timeframe, since=since_ms, limit=limit
            )
        except ccxt.NetworkError as e:
            retries += 1
            if retries > max_retries:
                log.error("Max retries (%d) exceeded for %s: %s", max_retries, symbol, e)
                break
            wait = min(5 * retries, 30)
            log.warning("Network error (retry %d/%d), waiting %ds: %s", retries, max_retries, wait, e)
            time.sleep(wait)
            continue
        except ccxt.ExchangeError as e:
            msg = str(e).lower()
            if "does not have market symbol" in msg or "bad symbol" in msg or "symbol" in msg and "not" in msg and "found" in msg:
                _mark_symbol_unsupported(symbol)
                log.warning("Unsupported Binance symbol: %s (cached for %ds)", symbol, _UNSUPPORTED_SYMBOL_TTL_SECONDS)
            else:
                log.error("Exchange error: %s", e)
            break

        if not candles:
            break

        all_candles.extend(candles)

        # Move cursor forward past the last candle's timestamp
        since_ms = candles[-1][0] + 1

        # If we got fewer than `limit`, we've reached the end
        if len(candles) < limit:
            break

        # Rate limit courtesy
        time.sleep(exchange.rateLimit / 1000)

    if not all_candles:
        log.warning("No data returned for %s %s", symbol, timeframe)
        return pd.DataFrame()

    df = pd.DataFrame(
        all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    log.info("Fetched %d candles (%s → %s)", len(df), df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
    return df


def fetch_gap_fill(
    symbol: str,
    timeframe: str,
    since_dt: datetime,
    until_dt: datetime = None,
) -> pd.DataFrame:
    """
    Fetch candles for a specific time range (for gap filling after WebSocket reconnection).

    Args:
        symbol: Trading pair, e.g. "BTC/USDT"
        timeframe: Candle interval, e.g. "1h"
        since_dt: Start datetime (UTC)
        until_dt: End datetime (UTC), defaults to now

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
    """
    if _is_symbol_temporarily_unsupported(symbol):
        log.debug("Skipping gap-fill for unsupported Binance symbol (cached): %s", symbol)
        return pd.DataFrame()

    exchange = _init_exchange()
    since_ms = int(since_dt.timestamp() * 1000)
    until_ms = int((until_dt or datetime.now(timezone.utc)).timestamp() * 1000)

    all_candles = []
    limit = 1000
    retries = 0
    max_retries = 3

    log.info("Gap-filling %s %s from %s to %s", symbol, timeframe, since_dt, until_dt)

    while since_ms < until_ms:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
        except ccxt.NetworkError as e:
            retries += 1
            if retries > max_retries:
                log.error("Gap-fill failed after %d retries: %s", max_retries, e)
                break
            time.sleep(2 * retries)
            continue
        except ccxt.ExchangeError as e:
            msg = str(e).lower()
            if "does not have market symbol" in msg or "bad symbol" in msg or "symbol" in msg and "not" in msg and "found" in msg:
                _mark_symbol_unsupported(symbol)
                log.warning("Unsupported Binance symbol in gap-fill: %s (cached for %ds)", symbol, _UNSUPPORTED_SYMBOL_TTL_SECONDS)
            else:
                log.error("Gap-fill exchange error: %s", e)
            break

        if not candles:
            break

        # Filter candles within our range
        candles = [c for c in candles if c[0] <= until_ms]
        all_candles.extend(candles)
        since_ms = candles[-1][0] + 1

        if len(candles) < limit:
            break
        time.sleep(exchange.rateLimit / 1000)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(
        all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    log.info("Gap-filled %d candles for %s", len(df), symbol)
    return df
