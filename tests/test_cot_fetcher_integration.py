"""
Integration tests for COT fetcher with actual CFTC data.

These tests are marked as integration and will be skipped in CI.
Run with: pytest tests/test_cot_fetcher_integration.py -v -m integration
"""

import tempfile
from pathlib import Path

import pytest

from agents.macro.cot_fetcher import COTFetcher


@pytest.mark.integration
class TestCOTFetcherIntegration:
    """Integration tests with actual CFTC API"""

    def test_fetch_eur_usd_data(self):
        """Test fetching real COT data for EUR/USD"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cot_cache.json"
            fetcher = COTFetcher(cache_file=str(cache_file))

            # Fetch EUR/USD data
            cot_data = fetcher.fetch_cot_data("EUR/USD")

            # May be None if cot-reports library has issues or network is down
            if cot_data is not None:
                assert cot_data.symbol == "EUR/USD"
                assert cot_data.commercial_long >= 0
                assert cot_data.commercial_short >= 0
                assert cot_data.non_commercial_long >= 0
                assert cot_data.non_commercial_short >= 0
                assert isinstance(cot_data.net_position, int)
                assert isinstance(cot_data.net_position_pct, float)
                print(f"\nEUR/USD COT Data:")
                print(f"  Commercial Long: {cot_data.commercial_long:,}")
                print(f"  Commercial Short: {cot_data.commercial_short:,}")
                print(f"  Non-Commercial Long: {cot_data.non_commercial_long:,}")
                print(f"  Non-Commercial Short: {cot_data.non_commercial_short:,}")
                print(f"  Net Position: {cot_data.net_position:,}")
                print(f"  Net Position %: {cot_data.net_position_pct:.2f}%")
            else:
                pytest.skip("COT data not available (network or library issue)")

    def test_fetch_gold_data(self):
        """Test fetching real COT data for GOLD"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cot_cache.json"
            fetcher = COTFetcher(cache_file=str(cache_file))

            # Fetch GOLD data
            cot_data = fetcher.fetch_cot_data("GOLD")

            if cot_data is not None:
                assert cot_data.symbol == "GOLD"
                assert cot_data.commercial_long >= 0
                assert cot_data.non_commercial_long >= 0
                print(f"\nGOLD COT Data:")
                print(f"  Net Position: {cot_data.net_position:,}")
                print(f"  Net Position %: {cot_data.net_position_pct:.2f}%")
            else:
                pytest.skip("COT data not available (network or library issue)")

    def test_cache_reuse(self):
        """Test that cached data is reused on second fetch"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cot_cache.json"
            fetcher = COTFetcher(cache_file=str(cache_file))

            # First fetch (from API)
            first_fetch = fetcher.fetch_cot_data("EUR/USD")
            if first_fetch is None:
                pytest.skip("COT data not available")

            # Second fetch (should use cache)
            second_fetch = fetcher.fetch_cot_data("EUR/USD")
            assert second_fetch is not None
            assert second_fetch.symbol == first_fetch.symbol
            assert second_fetch.net_position == first_fetch.net_position

            # Verify cache file exists
            assert cache_file.exists()
