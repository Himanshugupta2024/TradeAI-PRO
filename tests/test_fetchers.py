"""
Integration tests for data fetchers.
These hit real APIs — run with: pytest tests/test_fetchers.py -v -m integration
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agents.data_ingestion.binance_fetcher import fetch_historical as fetch_binance
from agents.data_ingestion.nse_fetcher import fetch_historical as fetch_nse


@pytest.mark.integration
class TestBinanceFetcher:
    """Integration tests for Binance data fetcher."""

    def test_fetch_btc_daily(self):
        """Should fetch non-empty BTC/USDT daily candles."""
        df = fetch_binance("BTC/USDT", "1d", since_days=30)
        assert not df.empty, "Expected data for BTC/USDT 1d"
        assert len(df) >= 20, f"Expected at least 20 rows, got {len(df)}"

        expected_cols = ["timestamp", "open", "high", "low", "close", "volume"]
        for col in expected_cols:
            assert col in df.columns, f"Missing column: {col}"

    def test_fetch_eth_hourly(self):
        """Should fetch ETH/USDT hourly candles."""
        df = fetch_binance("ETH/USDT", "1h", since_days=7)
        assert not df.empty, "Expected data for ETH/USDT 1h"
        assert len(df) >= 100, f"Expected at least 100 rows, got {len(df)}"


@pytest.mark.integration
class TestNSEFetcher:
    """Integration tests for NSE data fetcher via yfinance."""

    def test_fetch_nifty_daily(self):
        """Should fetch NIFTY 50 daily candles."""
        df = fetch_nse("NIFTY 50", "1d", since_days=60)
        assert not df.empty, "Expected data for NIFTY 50 1d"
        assert len(df) >= 30, f"Expected at least 30 rows, got {len(df)}"

        expected_cols = ["timestamp", "open", "high", "low", "close", "volume"]
        for col in expected_cols:
            assert col in df.columns, f"Missing column: {col}"

    def test_fetch_banknifty_daily(self):
        """Should fetch BANKNIFTY daily candles."""
        df = fetch_nse("BANKNIFTY", "1d", since_days=60)
        assert not df.empty, "Expected data for BANKNIFTY 1d"

    def test_unknown_symbol_returns_empty(self):
        """Unknown symbol should return an empty DataFrame."""
        df = fetch_nse("NONEXISTENT_SYMBOL", "1d", since_days=30)
        assert df.empty
