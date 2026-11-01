"""
Basic tests for COT fetcher implementation.

Tests the COTData dataclass, symbol mapping, and cache behavior.
"""

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from agents.macro.cot_fetcher import COTData, COTFetcher


class TestCOTData:
    """Test COTData dataclass"""

    def test_cot_data_creation(self):
        """Test creating COTData instance"""
        now = datetime.now(timezone.utc)
        cot_data = COTData(
            symbol="EUR/USD",
            commercial_long=100000,
            commercial_short=80000,
            non_commercial_long=50000,
            non_commercial_short=30000,
            net_position=20000,
            net_position_pct=5.5,
            timestamp=now,
        )

        assert cot_data.symbol == "EUR/USD"
        assert cot_data.commercial_long == 100000
        assert cot_data.net_position == 20000
        assert cot_data.net_position_pct == 5.5
        assert cot_data.timestamp == now

    def test_cot_data_serialization(self):
        """Test COTData to_dict and from_dict"""
        now = datetime.now(timezone.utc)
        original = COTData(
            symbol="GOLD",
            commercial_long=150000,
            commercial_short=120000,
            non_commercial_long=60000,
            non_commercial_short=40000,
            net_position=20000,
            net_position_pct=4.2,
            timestamp=now,
        )

        # Serialize to dict
        data_dict = original.to_dict()
        assert isinstance(data_dict, dict)
        assert data_dict["symbol"] == "GOLD"
        assert isinstance(data_dict["timestamp"], str)

        # Deserialize from dict
        restored = COTData.from_dict(data_dict)
        assert restored.symbol == original.symbol
        assert restored.commercial_long == original.commercial_long
        assert restored.net_position == original.net_position
        assert restored.timestamp == original.timestamp


class TestCOTFetcher:
    """Test COTFetcher class"""

    def test_symbol_mapping_forex(self):
        """Test symbol to CFTC code mapping for forex pairs"""
        fetcher = COTFetcher()

        # Test with slash notation
        assert fetcher.map_symbol_to_cftc_code("EUR/USD") == "092741"
        assert fetcher.map_symbol_to_cftc_code("GBP/USD") == "096742"
        assert fetcher.map_symbol_to_cftc_code("USD/JPY") == "097741"

        # Test without slash
        assert fetcher.map_symbol_to_cftc_code("EURUSD") == "092741"
        assert fetcher.map_symbol_to_cftc_code("GBPUSD") == "096742"

        # Test case insensitivity
        assert fetcher.map_symbol_to_cftc_code("eur/usd") == "092741"
        assert fetcher.map_symbol_to_cftc_code("Eur/Usd") == "092741"

    def test_symbol_mapping_commodities(self):
        """Test symbol to CFTC code mapping for commodities"""
        fetcher = COTFetcher()

        assert fetcher.map_symbol_to_cftc_code("GOLD") == "088691"
        assert fetcher.map_symbol_to_cftc_code("XAUUSD") == "088691"
        assert fetcher.map_symbol_to_cftc_code("SILVER") == "084691"
        assert fetcher.map_symbol_to_cftc_code("CRUDE") == "067651"

    def test_symbol_mapping_unsupported(self):
        """Test symbol mapping returns None for unsupported symbols"""
        fetcher = COTFetcher()

        # Crypto (no COT data)
        assert fetcher.map_symbol_to_cftc_code("BTC/USDT") is None
        assert fetcher.map_symbol_to_cftc_code("ETH/USDT") is None

        # NSE stocks (no COT data)
        assert fetcher.map_symbol_to_cftc_code("RELIANCE") is None
        assert fetcher.map_symbol_to_cftc_code("TCS") is None

        # Random invalid symbol
        assert fetcher.map_symbol_to_cftc_code("INVALID") is None

    def test_cache_save_and_load(self):
        """Test cache save and load functionality"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cot_cache.json"
            fetcher = COTFetcher(cache_file=str(cache_file))

            # Create test data
            now = datetime.now(timezone.utc)
            cot_data = COTData(
                symbol="EUR/USD",
                commercial_long=100000,
                commercial_short=80000,
                non_commercial_long=50000,
                non_commercial_short=30000,
                net_position=20000,
                net_position_pct=5.5,
                timestamp=now,
            )

            # Save to cache
            fetcher._save_cache("EUR/USD", cot_data)
            assert cache_file.exists()

            # Load from cache
            loaded = fetcher._load_cache("EUR/USD")
            assert loaded is not None
            assert loaded.symbol == "EUR/USD"
            assert loaded.net_position == 20000
            assert loaded.net_position_pct == 5.5

    def test_cache_freshness(self):
        """Test cache freshness check"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cot_cache.json"
            # 1 hour TTL for testing
            fetcher = COTFetcher(cache_file=str(cache_file), cache_ttl_seconds=3600)

            # Fresh data (just created)
            fresh_data = COTData(
                symbol="EUR/USD",
                commercial_long=100000,
                commercial_short=80000,
                non_commercial_long=50000,
                non_commercial_short=30000,
                net_position=20000,
                net_position_pct=5.5,
                timestamp=datetime.now(timezone.utc),
            )
            assert fetcher._is_cache_fresh(fresh_data) is True

            # Stale data (2 hours old)
            stale_data = COTData(
                symbol="EUR/USD",
                commercial_long=100000,
                commercial_short=80000,
                non_commercial_long=50000,
                non_commercial_short=30000,
                net_position=20000,
                net_position_pct=5.5,
                timestamp=datetime.now(timezone.utc) - timedelta(hours=2),
            )
            assert fetcher._is_cache_fresh(stale_data) is False

    def test_fetch_cot_data_unsupported_symbol(self):
        """Test fetch_cot_data returns None for unsupported symbols"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cot_cache.json"
            fetcher = COTFetcher(cache_file=str(cache_file))

            # Crypto should return None
            result = fetcher.fetch_cot_data("BTC/USDT")
            assert result is None

            # NSE stock should return None
            result = fetcher.fetch_cot_data("RELIANCE")
            assert result is None

    def test_multiple_symbols_in_cache(self):
        """Test that cache can store multiple symbols"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cot_cache.json"
            fetcher = COTFetcher(cache_file=str(cache_file))

            now = datetime.now(timezone.utc)

            # Save EUR/USD
            eur_data = COTData(
                symbol="EUR/USD",
                commercial_long=100000,
                commercial_short=80000,
                non_commercial_long=50000,
                non_commercial_short=30000,
                net_position=20000,
                net_position_pct=5.5,
                timestamp=now,
            )
            fetcher._save_cache("EUR/USD", eur_data)

            # Save GOLD
            gold_data = COTData(
                symbol="GOLD",
                commercial_long=150000,
                commercial_short=120000,
                non_commercial_long=60000,
                non_commercial_short=40000,
                net_position=20000,
                net_position_pct=4.2,
                timestamp=now,
            )
            fetcher._save_cache("GOLD", gold_data)

            # Load both
            loaded_eur = fetcher._load_cache("EUR/USD")
            loaded_gold = fetcher._load_cache("GOLD")

            assert loaded_eur is not None
            assert loaded_eur.symbol == "EUR/USD"
            assert loaded_gold is not None
            assert loaded_gold.symbol == "GOLD"
