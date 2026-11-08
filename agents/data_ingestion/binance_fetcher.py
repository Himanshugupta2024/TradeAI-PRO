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
_SPOT_MARKETS_CACHE_TTL_SECONDS = 1800
_SPOT_MARKETS_CACHE: set[str] = set()
_SPOT_MARKETS_CACHE_AT: float = 0.0
_UNSUPPORTED_SYMBOL_WARNED: set[str] = set()


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
    key = (symbol or "").upper()
    _UNSUPPORTED_SYMBOL_UNTIL[key] = time.time() + _UNSUPPORTED_SYMBOL_TTL_SECONDS


def _warn_unsupported_once(symbol: str, context: str = "") -> None:
    key = (symbol or "").upper()
    if key in _UNSUPPORTED_SYMBOL_WARNED:
        return
    _UNSUPPORTED_SYMBOL_WARNED.add(key)
    suffix = f" ({context})" if context else ""
    log.warning("Unsupported Binance symbol: %s%s", symbol, suffix)


def _get_spot_markets(exchange: ccxt.binance) -> set[str]:
    global _SPOT_MARKETS_CACHE, _SPOT_MARKETS_CACHE_AT
    now_ts = time.time()
    if _SPOT_MARKETS_CACHE and now_ts - _SPOT_MARKETS_CACHE_AT < _SPOT_MARKETS_CACHE_TTL_SECONDS:
        return _SPOT_MARKETS_CACHE
    try:
        markets = exchange.load_markets()
        _SPOT_MARKETS_CACHE = set(markets.keys()) if isinstance(markets, dict) else set()
        _SPOT_MARKETS_CACHE_AT = now_ts
    except Exception as exc:
        log.debug("Failed to refresh Binance markets cache: %s", exc)
    return _SPOT_MARKETS_CACHE


def _is_supported_spot_symbol(exchange: ccxt.binance, symbol: str) -> bool:
    markets = _get_spot_markets(exchange)
    if not markets:
        return True
    return symbol in markets


def _init_exchange() -> ccxt.binance:
    """Create a Binance exchange instance with short timeout for fast fallback."""
    return ccxt.binance({
        "enableRateLimit": settings.BINANCE_RATE_LIMIT,
        "options": {"defaultType": "spot"},
        "timeout": 3500,
    })


def _fetch_binance_futures_fallback(
    symbol: str, timeframe: str, since_days: int = 30
) -> pd.DataFrame:
    """Fallback fetcher for Binance Futures public REST (no API key required)."""
    import urllib.request
    import json
    clean_sym = symbol.replace("/", "").replace("-", "").upper()
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={clean_sym}&interval={timeframe}&limit=500"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 TradeAI-PRO"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                raw = json.loads(resp.read().decode())
                if isinstance(raw, list) and len(raw) > 0:
                    df = pd.DataFrame(
                        raw,
                        columns=[
                            "timestamp", "open", "high", "low", "close", "volume",
                            "close_time", "qav", "num_trades", "taker_base_vol", "taker_quote_vol", "ignore"
                        ]
                    )
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                    for col in ["open", "high", "low", "close", "volume"]:
                        df[col] = df[col].astype(float)
                    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
                    log.info("[Binance Futures Fallback] Fetched %d candles for %s (%s)", len(df), clean_sym, timeframe)
                    return df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        log.debug("Binance futures fallback failed for %s: %s", symbol, e)
    return pd.DataFrame()


def _fetch_yahoo_crypto_fallback(
    symbol: str, timeframe: str, since_days: int = settings.HISTORICAL_DAYS
) -> pd.DataFrame:
    """
    Zero-cost, unblocked Yahoo Finance fallback for crypto in case Binance CCXT is
    throttled or blocked by Indian ISPs (Jio/Airtel/ACT).
    Maps e.g. BTC/USDT -> BTC-USD, ETH/USDT -> ETH-USD, SOL/USDT -> SOL-USD.
    """
    try:
        import yfinance as yf

        clean = symbol.replace("-", "/").split("/")[0].upper().strip()
        yf_sym = f"{clean}-USD"

        # Map interval
        tf_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "4h": "1h", "1d": "1d", "1w": "1wk"
        }
        interval = tf_map.get(timeframe, "1h")
        period = f"{min(since_days, 30)}d" if since_days > 0 else "7d"
        if interval == "1d":
            period = f"{min(since_days, 730)}d"

        df = yf.download(yf_sym, period=period, interval=interval, progress=False)
        if df is None or df.empty:
            return pd.DataFrame()

        df = df.reset_index()
        # Flatten columns
        df.columns = [str(c[0] if isinstance(c, tuple) else c).lower() for c in df.columns]
        date_col = next((c for c in df.columns if c in ("datetime", "date", "timestamp")), None)
        if date_col:
            df = df.rename(columns={date_col: "timestamp"})

        cols_needed = ["timestamp", "open", "high", "low", "close", "volume"]
        df = df[[c for c in cols_needed if c in df.columns]]
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        log.info("[Yahoo Crypto Fallback] Fetched %d candles for %s (%s)", len(df), yf_sym, timeframe)
        return df
    except Exception as exc:
        log.warning("Yahoo crypto fallback failed for %s: %s", symbol, exc)
        return pd.DataFrame()


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
    if not _is_supported_spot_symbol(exchange, symbol):
        # Try Binance Futures public REST for futures-listed tokens (e.g. CHILLGUY, PNUT, MOODENG)
        df_futures = _fetch_binance_futures_fallback(symbol, timeframe, since_days)
        if not df_futures.empty:
            return df_futures
        _mark_symbol_unsupported(symbol)
        _warn_unsupported_once(symbol, "not listed on Binance spot or futures")
        return pd.DataFrame()

    since_dt = datetime.now(timezone.utc) - timedelta(days=since_days)
    since_ms = int(since_dt.timestamp() * 1000)

    all_candles = []
    limit = 1000  # Binance max per request
    retries = 0
    max_retries = 1

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
            wait = 1
            log.warning("Network error on Binance (retry %d/%d): %s — falling back soon", retries, max_retries, e)
            time.sleep(wait)
            continue
        except ccxt.ExchangeError as e:
            msg = str(e).lower()
            if "does not have market symbol" in msg or "bad symbol" in msg or "symbol" in msg and "not" in msg and "found" in msg:
                _mark_symbol_unsupported(symbol)
                _warn_unsupported_once(symbol, f"cached for {_UNSUPPORTED_SYMBOL_TTL_SECONDS}s")
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
        log.warning("Binance returned no data for %s %s; trying Yahoo crypto fallback...", symbol, timeframe)
        return _fetch_yahoo_crypto_fallback(symbol, timeframe, since_days)

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
    if not _is_supported_spot_symbol(exchange, symbol):
        _mark_symbol_unsupported(symbol)
        _warn_unsupported_once(symbol, "gap-fill skipped; not listed on Binance spot")
        return pd.DataFrame()
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
                _warn_unsupported_once(symbol, f"gap-fill cached for {_UNSUPPORTED_SYMBOL_TTL_SECONDS}s")
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
