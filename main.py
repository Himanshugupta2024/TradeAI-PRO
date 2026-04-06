"""
AI Trading System — Main Pipeline Runner.

Orchestrates data ingestion → feature engineering for all
configured symbols and timeframes.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from config.logger import get_logger
from db.connection import init_db
from agents.data_ingestion import binance_fetcher, nse_fetcher
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.support_resistance import calculate_support_resistance
from agents.feature_engine.trend import classify_trend, propagate_trend_to_ltf

log = get_logger("pipeline")


def run_pipeline():
    """Run the full data ingestion + feature engineering pipeline."""
    log.info("═" * 50)
    log.info("AI Trading System — Data Pipeline")
    log.info("═" * 50)

    # 1. Initialize DB
    init_db()
    log.info("DB tables created / verified")

    # Storage: symbol → { timeframe → enriched_df }
    data_store = {}

    # ── Fetch Crypto Data ────────────────────────────
    for symbol in settings.CRYPTO_SYMBOLS:
        data_store[symbol] = {}
        for tf in settings.TIMEFRAMES:
            log.info("--- %s / %s ---", symbol, tf)
            df = binance_fetcher.fetch_historical(symbol, tf)
            if df.empty:
                continue
            df = calculate_indicators(df)
            df = calculate_support_resistance(df)
            data_store[symbol][tf] = df

    # ── Fetch NSE Data ───────────────────────────────
    for symbol in settings.NSE_SYMBOLS:
        data_store[symbol] = {}
        for tf in settings.TIMEFRAMES:
            log.info("--- %s / %s ---", symbol, tf)
            df = nse_fetcher.fetch_historical(symbol, tf)
            if df.empty:
                continue
            df = calculate_indicators(df)
            df = calculate_support_resistance(df)
            data_store[symbol][tf] = df

    # ── Higher Timeframe Trend Propagation ───────────
    htf = settings.HTF_TREND_TIMEFRAME  # "1h"
    log.info("HTF Trend Classification (%s)", htf)

    for symbol, timeframes in data_store.items():
        if htf not in timeframes:
            log.debug("SKIP %s: no %s data", symbol, htf)
            continue

        htf_df = classify_trend(timeframes[htf])
        timeframes[htf] = htf_df

        # Propagate to lower timeframes
        for tf, df in timeframes.items():
            if tf == htf:
                continue
            enriched = propagate_trend_to_ltf(df, htf_df)
            timeframes[tf] = enriched
            log.info("%s/%s: HTF trend propagated (%d rows)", symbol, tf, len(enriched))

    # ── Summary ──────────────────────────────────────
    total_rows = 0
    for symbol, timeframes in data_store.items():
        for tf, df in timeframes.items():
            row_count = len(df)
            total_rows += row_count
            cols = [c for c in df.columns if c not in ["timestamp", "open", "high", "low", "close", "volume"]]
            log.info("%15s | %5s | %7s rows | Features: %s",
                     symbol, tf, f"{row_count:,}", ", ".join(cols[:6]))

    log.info("Total: %s candle-feature rows", f"{total_rows:,}")
    log.info("✅ Pipeline complete")

    return data_store


if __name__ == "__main__":
    run_pipeline()
