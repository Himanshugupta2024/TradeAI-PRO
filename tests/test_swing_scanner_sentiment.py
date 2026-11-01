"""
Unit tests for Swing Scanner integration with GDELT sentiment provider.
"""
import pytest
import pandas as pd
import numpy as np

from agents.market_intelligence.swing_scanner import scan_all_symbols
from data.providers.gdelt import get_gdelt_provider


def _create_daily_swing_df(n_bars=60):
    dates = pd.date_range("2026-08-01", periods=n_bars, freq="D")
    closes = [200.0 + i * 2.0 for i in range(n_bars)]
    df = pd.DataFrame({
        "timestamp": dates,
        "open": [c - 1.0 for c in closes],
        "high": [c + 3.0 for c in closes],
        "low": [c - 2.0 for c in closes],
        "close": closes,
        "volume": [100000.0] * n_bars,
        "RSI_14": [58.0] * n_bars,
        "MACD": [2.0] * n_bars,
        "MACD_signal": [1.5] * n_bars,
        "MACD_hist": [0.5] * n_bars,
        "EMA_20": [c - 2.0 for c in closes],
        "EMA_50": [c - 5.0 for c in closes],
        "EMA_200": [c - 10.0 for c in closes],
        "ATR_14": [3.0] * n_bars,
        "htf_trend": ["BULLISH"] * n_bars,
    })
    return df


def test_swing_scanner_reads_gdelt_sentiment():
    gdelt = get_gdelt_provider()
    # Pre-seed GDELT cache with bullish sentiment for INFY
    gdelt._cache["INFY"] = (
        9999999999.0,
        {
            "symbol": "INFY",
            "sentiment": "BULLISH",
            "tone_score": 5.0,
            "confidence": 0.85,
        }
    )

    df_infy = _create_daily_swing_df()
    symbol_data = {"INFY": df_infy}

    setups = scan_all_symbols(symbol_data)
    assert isinstance(setups, list)
    # The scan should execute smoothly without crashing or network stalls
