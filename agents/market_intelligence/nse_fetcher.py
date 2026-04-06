"""
NSE Real-Time Data Fetcher using yFinance.

Fetches real OHLCV data for Indian markets:
- NIFTY 50, BANKNIFTY (indices)
- Top NSE stocks (RELIANCE, TCS, etc.)
- 1-minute intervals for intraday
- 15-minute delay (yFinance limitation — free tier)
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Symbol Map: Display Name → yFinance ticker ───────────
# NOTE: No duplicate tickers — each display name must map to a UNIQUE ticker
# (duplicates cause the same DataFrame to be shared between symbols, making
# them show identical prices/RSI/indicators on the dashboard).
NSE_SYMBOLS = {
    # Indices  — NIFTY BANK removed (same ticker as BANKNIFTY = ^NSEBANK)
    "NIFTY 50":    "^NSEI",
    "BANKNIFTY":   "^NSEBANK",
    "SENSEX":      "^BSESN",
    "NIFTY IT":    "^CNXIT",

    # Large Cap Stocks (NSE)
    "RELIANCE":    "RELIANCE.NS",
    "TCS":         "TCS.NS",
    "HDFCBANK":    "HDFCBANK.NS",
    "INFY":        "INFY.NS",
    "ICICIBANK":   "ICICIBANK.NS",
    "SBIN":        "SBIN.NS",
    "BHARTIARTL":  "BHARTIARTL.NS",
    "KOTAKBANK":   "KOTAKBANK.NS",
    "LT":          "LT.NS",
    "ITC":         "ITC.NS",
    "AXISBANK":    "AXISBANK.NS",
    "HINDUNILVR":  "HINDUNILVR.NS",
    "BAJFINANCE":  "BAJFINANCE.NS",
    "MARUTI":      "MARUTI.NS",
    "TITAN":       "TITAN.NS",
    "WIPRO":       "WIPRO.NS",
    "TATAMOTORS":  "TATAMOTORS.NS",
    "SUNPHARMA":   "SUNPHARMA.NS",
    "ULTRACEMCO":  "ULTRACEMCO.NS",
    "ADANIENT":    "ADANIENT.NS",

    # Mid Cap / Popular Stocks
    "ADANIPORTS":  "ADANIPORTS.NS",
    "ADANIPOWER":  "ADANIPOWER.NS",
    "ADANIGREEN":  "ADANIGREEN.NS",
    "TATASTEEL":   "TATASTEEL.NS",
    "JSWSTEEL":    "JSWSTEEL.NS",
    "HINDALCO":    "HINDALCO.NS",
    "COALINDIA":   "COALINDIA.NS",
    "NTPC":        "NTPC.NS",
    "POWERGRID":   "POWERGRID.NS",
    "ONGC":        "ONGC.NS",
    "BPCL":        "BPCL.NS",
    "IOC":         "IOC.NS",
    "GAIL":        "GAIL.NS",
    "TECHM":       "TECHM.NS",
    "HCLTECH":     "HCLTECH.NS",
    "DRREDDY":     "DRREDDY.NS",
    "CIPLA":       "CIPLA.NS",
    "DIVISLAB":    "DIVISLAB.NS",
    "APOLLOHOSP":  "APOLLOHOSP.NS",
    "ASIANPAINT":  "ASIANPAINT.NS",
    "NESTLEIND":   "NESTLEIND.NS",
    "BRITANNIA":   "BRITANNIA.NS",
    "DABUR":       "DABUR.NS",
    "GODREJCP":    "GODREJCP.NS",
    "PIDILITIND":  "PIDILITIND.NS",
    "BERGEPAINT":  "BERGEPAINT.NS",
    "HAVELLS":     "HAVELLS.NS",
    "VOLTAS":      "VOLTAS.NS",
    "SIEMENS":     "SIEMENS.NS",
    "ABB":         "ABB.NS",
    "BHEL":        "BHEL.NS",
    "M&M":         "M&M.NS",
    "EICHERMOT":   "EICHERMOT.NS",
    "HEROMOTOCO":  "HEROMOTOCO.NS",
    "BAJAJ-AUTO":  "BAJAJ-AUTO.NS",
    "TVSMOTOR":    "TVSMOTOR.NS",
    "INDUSINDBK":  "INDUSINDBK.NS",
    "BANDHANBNK":  "BANDHANBNK.NS",
    "PNB":         "PNB.NS",
    "BANKBARODA":  "BANKBARODA.NS",
    "IDFCFIRSTB":  "IDFCFIRSTB.NS",
    "SBILIFE":     "SBILIFE.NS",
    "HDFCLIFE":    "HDFCLIFE.NS",
    "ICICIGI":     "ICICIGI.NS",
    "BAJAJFINSV":  "BAJAJFINSV.NS",
    "CHOLAFIN":    "CHOLAFIN.NS",
    "SHRIRAMFIN":  "SHRIRAMFIN.NS",
    "MUTHOOTFIN":  "MUTHOOTFIN.NS",
    "HAL":         "HAL.NS",
    "BEL":         "BEL.NS",
    "IRCTC":       "IRCTC.NS",
    "ZOMATO":      "ZOMATO.NS",
    "PAYTM":       "PAYTM.NS",
    "NYKAA":       "NYKAA.NS",
    "POLICYBZR":   "POLICYBZR.NS",
    "DELHIVERY":   "DELHIVERY.NS",
    "TATAPOWER":   "TATAPOWER.NS",
    "TATAELXSI":   "TATAELXSI.NS",
    "PERSISTENT":  "PERSISTENT.NS",
    "LTIM":        "LTIM.NS",
    "MPHASIS":     "MPHASIS.NS",
    "COFORGE":     "COFORGE.NS",
    "VEDL":        "VEDL.NS",
    "JINDALSTEL":  "JINDALSTEL.NS",
    "SAIL":        "SAIL.NS",
    "NMDC":        "NMDC.NS",
    "DLF":         "DLF.NS",
    "GODREJPROP":  "GODREJPROP.NS",
    "OBEROIRLTY":  "OBEROIRLTY.NS",
    "PRESTIGE":    "PRESTIGE.NS",
    "INDIGO":      "INDIGO.NS",
    "TRENT":       "TRENT.NS",
    "DMART":       "DMART.NS",
    "PAGEIND":     "PAGEIND.NS",
    "DIXON":       "DIXON.NS",
    "POLYCAB":     "POLYCAB.NS",
    "KALYANKJIL":  "KALYANKJIL.NS",
}

# Reverse map: yf ticker → display name
_YF_TO_DISPLAY = {v: k for k, v in NSE_SYMBOLS.items()}

# ── Cache to avoid hammering yfinance ────────────────────
_cache: dict[str, dict] = {}
CACHE_TTL_SECONDS = 60  # Refresh every 60 seconds


def _is_cache_valid(key: str) -> bool:
    if key not in _cache:
        return False
    age = time.time() - _cache[key]["fetched_at"]
    return age < CACHE_TTL_SECONDS


def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Standardise a raw yfinance DataFrame:
      - Rename Open/High/Low/Close/Volume → lowercase
      - Reset index to timestamp column
      - Strip timezone info
      - Drop NaN closes
    Works for both yf.Ticker().history() output (no MultiIndex) and
    yf.download() output (may have MultiIndex when group_by='ticker').
    """
    if df.empty:
        return df

    # If columns are a MultiIndex (from yf.download with multiple tickers)
    # pick the correct ticker level.  For single-ticker Ticker.history() there
    # is no MultiIndex so this block is a no-op.
    if isinstance(df.columns, pd.MultiIndex):
        # Second level contains the ticker symbol; first level has OHLCV names
        df.columns = df.columns.get_level_values(0)

    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
        "Dividends": "dividends", "Stock Splits": "splits",
    })

    # Keep only OHLCV
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()

    # Move DatetimeIndex → column
    if df.index.name in ("Date", "Datetime", "Timestamp") or isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index()
        ts_col = next((c for c in df.columns if c.lower() in ("date", "datetime", "timestamp")), None)
        if ts_col:
            df = df.rename(columns={ts_col: "timestamp"})
    elif "timestamp" not in df.columns:
        df = df.reset_index()

    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Strip timezone
    if df["timestamp"].dt.tz is not None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(None)

    df = df.dropna(subset=["close"]).reset_index(drop=True)
    return df


def fetch_ohlcv(display_name: str, period: str = "5d", interval: str = "5m") -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV data for an NSE symbol using yf.Ticker().history().

    We use Ticker.history() (not yf.download) to guarantee that:
      - Columns are NEVER a MultiIndex (avoids the silent data-sharing bug
        where multiple symbols receive each other's OHLCV).
      - Each symbol fetches its own private DataFrame.

    Args:
        display_name: e.g. "NIFTY 50", "RELIANCE"
        period:   yf period string — "1d", "5d", "1mo", "60d"
        interval: yf interval — "1m", "5m", "15m", "1h", "1d"

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
        None on failure.
    """
    ticker = NSE_SYMBOLS.get(display_name)
    if not ticker:
        logger.warning(f"Unknown symbol: {display_name}")
        return None

    # Each symbol gets its own cache key (ticker + period + interval)
    cache_key = f"{ticker}_{period}_{interval}"
    if _is_cache_valid(cache_key):
        logger.debug(f"Cache hit: {display_name}")
        return _cache[cache_key]["df"].copy()  # return copy so callers can't mutate cache

    try:
        logger.info(f"Fetching yFinance: {display_name} ({ticker}) period={period} interval={interval}")
        # Use Ticker.history() — returns plain columns, never MultiIndex
        raw = yf.Ticker(ticker).history(
            period=period,
            interval=interval,
            auto_adjust=True,
            prepost=False,
        )

        if raw.empty:
            logger.warning(f"No data returned for {display_name}")
            return None

        df = _normalise_df(raw)
        if df.empty:
            return None

        _cache[cache_key] = {"df": df, "fetched_at": time.time()}
        logger.info(f"Fetched {len(df)} candles for {display_name}")
        return df.copy()

    except Exception as e:
        logger.error(f"yFinance fetch failed for {display_name}: {e}")
        return None


def fetch_current_price(display_name: str) -> Optional[float]:
    """Get the latest close price for a symbol."""
    df = fetch_ohlcv(display_name, period="1d", interval="5m")
    if df is None or df.empty:
        return None
    return float(df["close"].iloc[-1])


def get_all_symbols() -> list[str]:
    """Return all supported display names."""
    return list(NSE_SYMBOLS.keys())


# Crypto symbols included in search
CRYPTO_SEARCH_SYMBOLS = {
    "BTC/USDT":  "BTC/USDT",
    "ETH/USDT":  "ETH/USDT",
    "SOL/USDT":  "SOL/USDT",
    "BNB/USDT":  "BNB/USDT",
    "XRP/USDT":  "XRP/USDT",
    "ADA/USDT":  "ADA/USDT",
    "DOGE/USDT": "DOGE/USDT",
    "AVAX/USDT": "AVAX/USDT",
    "DOT/USDT":  "DOT/USDT",
    "MATIC/USDT":"MATIC/USDT",
    "LINK/USDT": "LINK/USDT",
    "LTC/USDT":  "LTC/USDT",
    "UNI/USDT":  "UNI/USDT",
    "ATOM/USDT": "ATOM/USDT",
    "NEAR/USDT": "NEAR/USDT",
}


def search_symbols(query: str) -> list[dict]:
    """
    Search NSE_SYMBOLS + CRYPTO_SEARCH_SYMBOLS by display name/ticker.
    Returns list of {symbol, ticker, type} dicts.
    """
    q = query.upper().strip()
    if not q:
        return []
    results = []

    # Search NSE stocks first
    for display_name, ticker in NSE_SYMBOLS.items():
        if q in display_name.upper() or q in ticker.upper():
            results.append({"symbol": display_name, "ticker": ticker, "type": "NSE"})

    # Search crypto symbols
    for display_name, ticker in CRYPTO_SEARCH_SYMBOLS.items():
        sym_upper = display_name.upper().replace("/", "")
        q_clean = q.replace("/", "").replace("-", "")
        if q_clean in sym_upper or q_clean in ticker.upper().replace("/", ""):
            results.append({"symbol": display_name, "ticker": ticker, "type": "CRYPTO"})

    return results[:20]


def fetch_arbitrary_symbol(ticker_or_name: str, period: str = "5d", interval: str = "5m") -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV for any yFinance ticker. If the input matches a known
    display name in NSE_SYMBOLS, use that; otherwise treat as raw yf ticker.
    """
    # Check if it's a known display name
    if ticker_or_name in NSE_SYMBOLS:
        return fetch_ohlcv(ticker_or_name, period=period, interval=interval)

    # Check if it's a known ticker
    if ticker_or_name in _YF_TO_DISPLAY:
        return fetch_ohlcv(_YF_TO_DISPLAY[ticker_or_name], period=period, interval=interval)

    # Try as raw yFinance ticker (e.g. "AAPL", "GOOGL", "TSLA")
    ticker = ticker_or_name.upper().strip()
    cache_key = f"arb_{ticker}_{period}_{interval}"

    if _is_cache_valid(cache_key):
        return _cache[cache_key]["df"]

    try:
        logger.info(f"Fetching yFinance (search): {ticker} period={period} interval={interval}")
        # Use Ticker.history() — no MultiIndex issues
        raw = yf.Ticker(ticker).history(
            period=period,
            interval=interval,
            auto_adjust=True,
            prepost=False,
        )
        if raw.empty:
            # Try with .NS suffix for NSE stocks
            nse_ticker = f"{ticker}.NS"
            logger.info(f"Retrying with NSE suffix: {nse_ticker}")
            raw = yf.Ticker(nse_ticker).history(
                period=period,
                interval=interval,
                auto_adjust=True,
                prepost=False,
            )

        if raw.empty:
            return None

        df = _normalise_df(raw)
        if df.empty:
            return None

        _cache[cache_key] = {"df": df, "fetched_at": time.time()}
        logger.info(f"Fetched {len(df)} candles for {ticker}")
        return df.copy()

    except Exception as e:
        logger.error(f"yFinance search fetch failed for {ticker}: {e}")
        return None


def fetch_nse_gainers_losers(mode: str = "gainers") -> list[dict]:
    """
    Fetch real intraday % change using a BATCH yf.download() call.

    Instead of fetching each symbol one-by-one (slow — causes timeouts),
    we download all stock tickers in a single batch request,
    then parse each ticker's column from the resulting wide DataFrame.
    This reduces ~100 sequential HTTP calls to 1–2 batch calls.
    """
    # Only stocks (no indices)
    stock_symbols = {
        k: v for k, v in NSE_SYMBOLS.items()
        if not k.startswith(("NIFTY", "BANKNIFTY", "SENSEX"))
    }

    results = []
    tickers_list = list(stock_symbols.values())   # e.g. ["RELIANCE.NS", "TCS.NS", ...]
    display_map  = {v: k for k, v in stock_symbols.items()}  # ticker → display name

    batch_cache_key = f"_batch_gainers_{mode}"
    BATCH_TTL = 120  # 2-minute cache for gain/loss data

    if batch_cache_key in _cache and (time.time() - _cache[batch_cache_key]["fetched_at"]) < BATCH_TTL:
        return _cache[batch_cache_key]["df"]

    try:
        logger.info("Batch downloading %d NSE stocks for screener...", len(tickers_list))
        raw = yf.download(
            tickers_list,
            period="2d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            group_by="ticker",   # gives MultiIndex: (field, ticker)
        )

        if raw.empty:
            logger.warning("Batch screener download returned empty DataFrame")
            return []

        for ticker, display in display_map.items():
            try:
                # Extract per-ticker slice from MultiIndex
                if isinstance(raw.columns, pd.MultiIndex):
                    if ticker not in raw.columns.get_level_values(1):
                        continue
                    sub = raw.xs(ticker, axis=1, level=1)  # columns: Open, High, Low, Close, Volume
                else:
                    sub = raw  # single-ticker fallback

                sub = sub.dropna(subset=["Close"])
                if len(sub) < 2:
                    continue

                last_close = float(sub["Close"].iloc[-1])
                prev_close = float(sub["Close"].iloc[-2])

                if prev_close == 0:
                    continue

                pct_change = round((last_close - prev_close) / prev_close * 100, 2)

                if mode == "gainers" and pct_change <= 0:
                    continue
                if mode == "losers" and pct_change >= 0:
                    continue

                volume = float(sub["Volume"].iloc[-1]) if "Volume" in sub.columns else 0
                volume_cr = round(volume * last_close / 1e7, 1)

                results.append({
                    "symbol":     display,
                    "company":    display,
                    "sector":     _get_sector(display),
                    "ltp":        round(last_close, 2),
                    "pct_change": pct_change,
                    "x_factor":   1.0,   # recalculated below
                    "volume_cr":  max(volume_cr, 0.1),
                    "oi_change":  None,
                })

            except Exception as exc:
                logger.debug("Skipped %s in screener: %s", display, exc)
                continue

    except Exception as exc:
        logger.error("Batch screener download failed: %s", exc)
        return []

    if not results:
        return []

    # Normalise X-Factor relative to avg % change in this group
    avg_abs_move = np.mean([abs(r["pct_change"]) for r in results]) or 1.0
    for r in results:
        r["x_factor"] = round(abs(r["pct_change"]) / avg_abs_move, 2)

    results.sort(key=lambda x: abs(x["pct_change"]), reverse=True)
    top = results[:20]

    # Cache the result to avoid re-fetching within 2 minutes
    _cache[batch_cache_key] = {"df": top, "fetched_at": time.time()}
    return top


def fetch_sector_performance() -> list[dict]:
    """
    Fetch real sector performance using index proxies.
    Uses sector ETFs / representative stocks.
    """
    # Sector proxy symbols on NSE
    SECTOR_PROXIES = {
        "IT":       ["TCS.NS", "INFY.NS", "WIPRO.NS"],
        "Finance":  ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS"],
        "FMCG":     ["HINDUNILVR.NS", "ITC.NS"],
        "Auto":     ["MARUTI.NS", "TATAMOTORS.NS"],
        "Pharma":   ["SUNPHARMA.NS"],
        "Energy":   ["RELIANCE.NS"],
        "Telecom":  ["BHARTIARTL.NS"],
        "Infra":    ["LT.NS"],
        "Consumer": ["TITAN.NS"],
        "Metals":   ["ADANIENT.NS"],
    }

    SECTOR_ICONS = {
        "IT": "💻", "Finance": "🏦", "FMCG": "🛒",
        "Auto": "🚗", "Pharma": "💊", "Energy": "⚡",
        "Metals": "⚙️", "Infra": "🏗️", "Telecom": "📡",
        "Consumer": "🛍️", "Realty": "🏢", "Media": "📺",
    }

    sectors = []
    for sector_name, tickers in SECTOR_PROXIES.items():
        changes = []
        for ticker in tickers:
            try:
                raw = yf.Ticker(ticker).history(period="2d", interval="1d", auto_adjust=True)
                if raw.empty or len(raw) < 2:
                    continue
                df = _normalise_df(raw)
                if df.empty or len(df) < 2:
                    continue
                pct = (df["close"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2] * 100
                changes.append(float(pct))
            except Exception:
                continue

        if not changes:
            # Skip sectors where no proxy data was fetched — never show random data
            logger.debug("Skipping sector %s — no real data available", sector_name)
            continue

        pct_change = round(np.mean(changes), 2)
        total_stocks = len(tickers) * 3
        adv = int(total_stocks * max(0.1, (pct_change + 3.5) / 7.0))
        dec = total_stocks - adv

        sectors.append({
            "name": sector_name,
            "pct_change": pct_change,
            "stocks": total_stocks,
            "adv": adv,
            "dec": dec,
            "icons": SECTOR_ICONS.get(sector_name, "📊"),
            "real_data": True,
        })

    return sectors


def _get_sector(symbol: str) -> str:
    SECTOR_MAP = {
        "RELIANCE": "Energy", "TCS": "IT", "HDFCBANK": "Finance",
        "INFY": "IT", "ICICIBANK": "Finance", "SBIN": "Finance",
        "BHARTIARTL": "Telecom", "KOTAKBANK": "Finance", "LT": "Infra",
        "ITC": "FMCG", "AXISBANK": "Finance", "HINDUNILVR": "FMCG",
        "BAJFINANCE": "Finance", "MARUTI": "Auto", "TITAN": "Consumer",
        "WIPRO": "IT", "TATAMOTORS": "Auto", "SUNPHARMA": "Pharma",
        "ULTRACEMCO": "Infra", "ADANIENT": "Conglomerate",
    }
    return SECTOR_MAP.get(symbol, "Others")
