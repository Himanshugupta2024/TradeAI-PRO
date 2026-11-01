"""
Comprehensive tests for MacroFetcher.

Tests FRED API connection, data fetching, cache behavior (24hr TTL),
fallback to stale cache on API failure, and all 5 FRED series.

Uses mocking to avoid requiring a real FRED API key.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from agents.macro.macro_fetcher import MacroFetcher, MacroIndicators


# Mock FRED API responses for each series
MOCK_GDP_RESPONSE = {
    "observations": [
        {"date": "2023-01-01", "value": "20000.0"},
        {"date": "2023-04-01", "value": "20200.0"},
        {"date": "2023-07-01", "value": "20400.0"},
        {"date": "2023-10-01", "value": "20600.0"},
        {"date": "2024-01-01", "value": "20800.0"},
    ]
}

MOCK_CPI_RESPONSE = {
    "observations": [
        {"date": "2023-01-01", "value": "300.0"},
        {"date": "2023-02-01", "value": "301.0"},
        {"date": "2023-03-01", "value": "302.0"},
        # ... more months
        {"date": "2024-01-01", "value": "308.0"},
        {"date": "2024-02-01", "value": "309.0"},
    ]
}

MOCK_UNEMPLOYMENT_RESPONSE = {
    "observations": [
        {"date": "2023-12-01", "value": "4.2"},
        {"date": "2024-01-01", "value": "4.1"},
        {"date": "2024-02-01", "value": "4.0"},
    ]
}

MOCK_FED_FUNDS_RESPONSE = {
    "observations": [
        {"date": "2024-02-15", "value": "5.33"},
        {"date": "2024-02-16", "value": "5.33"},
        {"date": "2024-02-17", "value": "5.33"},
    ]
}

MOCK_TREASURY_10Y_RESPONSE = {
    "observations": [
        {"date": "2024-02-15", "value": "4.25"},
        {"date": "2024-02-16", "value": "4.28"},
        {"date": "2024-02-17", "value": "4.30"},
    ]
}


class TestMacroIndicators:
    """Test MacroIndicators dataclass"""

    def test_serialization_round_trip(self):
        """Test to_dict and from_dict preserve all fields"""
        indicators = MacroIndicators(
            gdp_growth_yoy=3.5,
            cpi_yoy=2.8,
            unemployment_rate=4.2,
            fed_funds_rate=5.33,
            treasury_10y=4.30,
            timestamp=datetime.now(timezone.utc),
        )

        # Serialize and deserialize
        data = indicators.to_dict()
        restored = MacroIndicators.from_dict(data)

        assert restored.gdp_growth_yoy == indicators.gdp_growth_yoy
        assert restored.cpi_yoy == indicators.cpi_yoy
        assert restored.unemployment_rate == indicators.unemployment_rate
        assert restored.fed_funds_rate == indicators.fed_funds_rate
        assert restored.treasury_10y == indicators.treasury_10y
        assert abs((restored.timestamp - indicators.timestamp).total_seconds()) < 1

    def test_serialization_with_none_values(self):
        """Test serialization handles None values"""
        indicators = MacroIndicators(
            gdp_growth_yoy=None,
            cpi_yoy=None,
            unemployment_rate=4.2,
            fed_funds_rate=None,
            treasury_10y=4.30,
            timestamp=datetime.now(timezone.utc),
        )

        data = indicators.to_dict()
        restored = MacroIndicators.from_dict(data)

        assert restored.gdp_growth_yoy is None
        assert restored.cpi_yoy is None
        assert restored.unemployment_rate == 4.2
        assert restored.fed_funds_rate is None
        assert restored.treasury_10y == 4.30


class TestMacroFetcherCache:
    """Test cache behavior (24hr TTL)"""

    def test_cache_hit_fresh_data(self):
        """Test cache hit when data is fresh (< 24 hours)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"

            # Create fresh cache (1 hour old)
            fresh_indicators = MacroIndicators(
                gdp_growth_yoy=3.0,
                cpi_yoy=2.5,
                unemployment_rate=4.0,
                fed_funds_rate=5.0,
                treasury_10y=4.0,
                timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
            )
            with open(cache_file, "w") as f:
                json.dump(fresh_indicators.to_dict(), f)

            fetcher = MacroFetcher(
                api_key="test_key",
                cache_file=str(cache_file),
                cache_ttl_seconds=86400,  # 24 hours
            )

            indicators = fetcher.fetch_indicators()

            # Should use cache (no API call)
            assert indicators.gdp_growth_yoy == 3.0
            assert indicators.cpi_yoy == 2.5

    def test_cache_miss_stale_data(self):
        """Test cache miss when data is stale (> 24 hours)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"

            # Create stale cache (25 hours old)
            stale_indicators = MacroIndicators(
                gdp_growth_yoy=2.0,
                cpi_yoy=3.0,
                unemployment_rate=5.0,
                fed_funds_rate=4.0,
                treasury_10y=3.5,
                timestamp=datetime.now(timezone.utc) - timedelta(hours=25),
            )
            with open(cache_file, "w") as f:
                json.dump(stale_indicators.to_dict(), f)

            fetcher = MacroFetcher(
                api_key="test_key",
                cache_file=str(cache_file),
                cache_ttl_seconds=86400,  # 24 hours
            )

            # Mock API calls
            with patch.object(fetcher, "_fetch_from_api") as mock_fetch:
                mock_fetch.return_value = MacroIndicators(
                    gdp_growth_yoy=3.5,
                    cpi_yoy=2.8,
                    unemployment_rate=4.2,
                    fed_funds_rate=5.33,
                    treasury_10y=4.30,
                    timestamp=datetime.now(timezone.utc),
                )

                indicators = fetcher.fetch_indicators()

                # Should fetch from API (cache is stale)
                mock_fetch.assert_called_once()
                assert indicators.gdp_growth_yoy == 3.5

    def test_cache_miss_no_cache_file(self):
        """Test cache miss when cache file doesn't exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "nonexistent_cache.json"

            fetcher = MacroFetcher(
                api_key="test_key",
                cache_file=str(cache_file),
            )

            with patch.object(fetcher, "_fetch_from_api") as mock_fetch:
                mock_fetch.return_value = MacroIndicators(
                    gdp_growth_yoy=3.0,
                    cpi_yoy=2.5,
                    unemployment_rate=4.0,
                    fed_funds_rate=5.0,
                    treasury_10y=4.0,
                    timestamp=datetime.now(timezone.utc),
                )

                indicators = fetcher.fetch_indicators()

                # Should fetch from API
                mock_fetch.assert_called_once()

    def test_cache_saves_after_api_fetch(self):
        """Test cache is saved after successful API fetch"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"

            fetcher = MacroFetcher(
                api_key="test_key",
                cache_file=str(cache_file),
            )

            with patch.object(fetcher, "_fetch_from_api") as mock_fetch:
                mock_fetch.return_value = MacroIndicators(
                    gdp_growth_yoy=3.5,
                    cpi_yoy=2.8,
                    unemployment_rate=4.2,
                    fed_funds_rate=5.33,
                    treasury_10y=4.30,
                    timestamp=datetime.now(timezone.utc),
                )

                indicators = fetcher.fetch_indicators()

                # Cache file should be created
                assert cache_file.exists()

                # Load cache and verify
                with open(cache_file, "r") as f:
                    cached_data = json.load(f)
                assert cached_data["gdp_growth_yoy"] == 3.5


class TestMacroFetcherFallback:
    """Test fallback to stale cache on API failure"""

    def test_fallback_on_api_error_with_stale_cache(self):
        """Test fallback to stale cache when API fails"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"

            # Create stale cache (25 hours old)
            stale_indicators = MacroIndicators(
                gdp_growth_yoy=2.5,
                cpi_yoy=3.0,
                unemployment_rate=5.0,
                fed_funds_rate=4.5,
                treasury_10y=3.8,
                timestamp=datetime.now(timezone.utc) - timedelta(hours=25),
            )
            with open(cache_file, "w") as f:
                json.dump(stale_indicators.to_dict(), f)

            fetcher = MacroFetcher(
                api_key="test_key",
                cache_file=str(cache_file),
                cache_ttl_seconds=86400,
                fallback_enabled=True,
            )

            # Mock API failure
            with patch.object(fetcher, "_fetch_from_api") as mock_fetch:
                mock_fetch.side_effect = requests.RequestException("API error")

                indicators = fetcher.fetch_indicators()

                # Should use stale cache as fallback
                assert indicators.gdp_growth_yoy == 2.5
                assert indicators.cpi_yoy == 3.0

    def test_no_fallback_when_disabled(self):
        """Test no fallback when fallback_enabled=False"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"

            # Create stale cache
            stale_indicators = MacroIndicators(
                gdp_growth_yoy=2.5,
                cpi_yoy=3.0,
                unemployment_rate=5.0,
                fed_funds_rate=4.5,
                treasury_10y=3.8,
                timestamp=datetime.now(timezone.utc) - timedelta(hours=25),
            )
            with open(cache_file, "w") as f:
                json.dump(stale_indicators.to_dict(), f)

            fetcher = MacroFetcher(
                api_key="test_key",
                cache_file=str(cache_file),
                cache_ttl_seconds=86400,
                fallback_enabled=False,
            )

            # Mock API failure
            with patch.object(fetcher, "_fetch_from_api") as mock_fetch:
                mock_fetch.side_effect = requests.RequestException("API error")

                indicators = fetcher.fetch_indicators()

                # Should return empty indicators (no fallback)
                assert indicators.gdp_growth_yoy is None
                assert indicators.cpi_yoy is None

    def test_fallback_no_api_key_with_cache(self):
        """Test fallback when no API key but cache exists"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"

            # Create stale cache
            stale_indicators = MacroIndicators(
                gdp_growth_yoy=2.5,
                cpi_yoy=3.0,
                unemployment_rate=5.0,
                fed_funds_rate=4.5,
                treasury_10y=3.8,
                timestamp=datetime.now(timezone.utc) - timedelta(hours=25),
            )
            with open(cache_file, "w") as f:
                json.dump(stale_indicators.to_dict(), f)

            fetcher = MacroFetcher(
                api_key="",  # No API key
                cache_file=str(cache_file),
                fallback_enabled=True,
            )

            indicators = fetcher.fetch_indicators()

            # Should use stale cache
            assert indicators.gdp_growth_yoy == 2.5

    def test_no_fallback_no_api_key_no_cache(self):
        """Test empty indicators when no API key and no cache"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "nonexistent_cache.json"

            fetcher = MacroFetcher(
                api_key="",  # No API key
                cache_file=str(cache_file),
                fallback_enabled=True,
            )

            indicators = fetcher.fetch_indicators()

            # Should return empty indicators
            assert indicators.gdp_growth_yoy is None
            assert indicators.cpi_yoy is None


class TestMacroFetcherFREDSeries:
    """Test fetching all 5 FRED series"""

    @patch("agents.macro.macro_fetcher.requests.get")
    def test_fetch_gdp_series(self, mock_get):
        """Test fetching GDP series (GDPC1)"""
        mock_response = Mock()
        mock_response.json.return_value = MOCK_GDP_RESPONSE
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            values = fetcher._fetch_series("GDPC1", lookback_days=365)

            assert values is not None
            assert len(values) == 5
            assert values[-1] == 20800.0  # Latest value

    @patch("agents.macro.macro_fetcher.requests.get")
    def test_fetch_cpi_series(self, mock_get):
        """Test fetching CPI series (CPIAUCSL)"""
        mock_response = Mock()
        mock_response.json.return_value = MOCK_CPI_RESPONSE
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            values = fetcher._fetch_series("CPIAUCSL", lookback_days=365)

            assert values is not None
            assert len(values) == 5
            assert values[-1] == 309.0

    @patch("agents.macro.macro_fetcher.requests.get")
    def test_fetch_unemployment_series(self, mock_get):
        """Test fetching unemployment series (UNRATE)"""
        mock_response = Mock()
        mock_response.json.return_value = MOCK_UNEMPLOYMENT_RESPONSE
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            values = fetcher._fetch_series("UNRATE", lookback_days=90)

            assert values is not None
            assert len(values) == 3
            assert values[-1] == 4.0

    @patch("agents.macro.macro_fetcher.requests.get")
    def test_fetch_fed_funds_series(self, mock_get):
        """Test fetching Fed Funds series (DFF)"""
        mock_response = Mock()
        mock_response.json.return_value = MOCK_FED_FUNDS_RESPONSE
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            values = fetcher._fetch_series("DFF", lookback_days=30)

            assert values is not None
            assert len(values) == 3
            assert values[-1] == 5.33

    @patch("agents.macro.macro_fetcher.requests.get")
    def test_fetch_treasury_10y_series(self, mock_get):
        """Test fetching 10Y Treasury series (DGS10)"""
        mock_response = Mock()
        mock_response.json.return_value = MOCK_TREASURY_10Y_RESPONSE
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            values = fetcher._fetch_series("DGS10", lookback_days=30)

            assert values is not None
            assert len(values) == 3
            assert values[-1] == 4.30

    @patch("agents.macro.macro_fetcher.requests.get")
    def test_fetch_all_series_integration(self, mock_get):
        """Test fetching all 5 series together"""
        # Mock responses for all series
        def mock_response_factory(*args, **kwargs):
            url = args[0] if args else kwargs.get("url", "")
            params = kwargs.get("params", {})
            series_id = params.get("series_id", "")

            mock_response = Mock()
            mock_response.raise_for_status = Mock()

            if series_id == "GDPC1":
                mock_response.json.return_value = MOCK_GDP_RESPONSE
            elif series_id == "CPIAUCSL":
                mock_response.json.return_value = MOCK_CPI_RESPONSE
            elif series_id == "UNRATE":
                mock_response.json.return_value = MOCK_UNEMPLOYMENT_RESPONSE
            elif series_id == "DFF":
                mock_response.json.return_value = MOCK_FED_FUNDS_RESPONSE
            elif series_id == "DGS10":
                mock_response.json.return_value = MOCK_TREASURY_10Y_RESPONSE
            else:
                mock_response.json.return_value = {"observations": []}

            return mock_response

        mock_get.side_effect = mock_response_factory

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            indicators = fetcher.fetch_indicators()

            # All indicators should be populated
            assert indicators.gdp_growth_yoy is not None
            assert indicators.cpi_yoy is not None
            assert indicators.unemployment_rate == 4.0
            assert indicators.fed_funds_rate == 5.33
            assert indicators.treasury_10y == 4.30

    @patch("agents.macro.macro_fetcher.requests.get")
    def test_fetch_series_handles_missing_data(self, mock_get):
        """Test handling of missing data (FRED returns '.')"""
        mock_response = Mock()
        mock_response.json.return_value = {
            "observations": [
                {"date": "2024-01-01", "value": "4.0"},
                {"date": "2024-01-02", "value": "."},  # Missing data
                {"date": "2024-01-03", "value": "4.2"},
            ]
        }
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            values = fetcher._fetch_series("TEST", lookback_days=30)

            # Should skip missing data
            assert values is not None
            assert len(values) == 2
            assert values == [4.0, 4.2]

    @patch("agents.macro.macro_fetcher.requests.get")
    def test_fetch_series_handles_api_error(self, mock_get):
        """Test handling of API errors"""
        mock_get.side_effect = requests.RequestException("Network error")

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            values = fetcher._fetch_series("TEST", lookback_days=30)

            # Should return None on error
            assert values is None

    @patch("agents.macro.macro_fetcher.requests.get")
    def test_fetch_series_handles_empty_response(self, mock_get):
        """Test handling of empty API response"""
        mock_response = Mock()
        mock_response.json.return_value = {"observations": []}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            values = fetcher._fetch_series("TEST", lookback_days=30)

            # Should return None for empty response
            assert values is None


class TestMacroFetcherYoYCalculation:
    """Test year-over-year growth calculation"""

    def test_calculate_yoy_growth_quarterly_data(self):
        """Test YoY calculation for quarterly data (GDP)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            # 5 quarters of data
            values = [20000.0, 20200.0, 20400.0, 20600.0, 20800.0]
            yoy_growth = fetcher._calculate_yoy_growth(values)

            # Should calculate growth from first to last
            assert yoy_growth is not None
            assert yoy_growth == 4.0  # (20800 - 20000) / 20000 * 100

    def test_calculate_yoy_growth_monthly_data(self):
        """Test YoY calculation for monthly data (CPI)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            # 13 months of data
            values = [300.0] + [300.0 + i for i in range(1, 13)]
            yoy_growth = fetcher._calculate_yoy_growth(values)

            # Should calculate growth from 12 months ago to now
            assert yoy_growth is not None
            assert yoy_growth == 4.0  # (312 - 300) / 300 * 100

    def test_calculate_yoy_growth_insufficient_data(self):
        """Test YoY calculation with insufficient data"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            values = [100.0]  # Only 1 data point
            yoy_growth = fetcher._calculate_yoy_growth(values)

            # Should return None
            assert yoy_growth is None

    def test_calculate_yoy_growth_zero_previous_value(self):
        """Test YoY calculation when previous value is zero"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            values = [0.0, 100.0, 200.0]
            yoy_growth = fetcher._calculate_yoy_growth(values)

            # Should return None (division by zero)
            assert yoy_growth is None


class TestMacroFetcherEdgeCases:
    """Test edge cases and error handling"""

    def test_corrupted_cache_file(self):
        """Test handling of corrupted cache file"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"

            # Create corrupted cache
            with open(cache_file, "w") as f:
                f.write("{ invalid json }")

            fetcher = MacroFetcher(
                api_key="test_key",
                cache_file=str(cache_file),
            )

            with patch.object(fetcher, "_fetch_from_api") as mock_fetch:
                mock_fetch.return_value = MacroIndicators(
                    gdp_growth_yoy=3.0,
                    cpi_yoy=2.5,
                    unemployment_rate=4.0,
                    fed_funds_rate=5.0,
                    treasury_10y=4.0,
                    timestamp=datetime.now(timezone.utc),
                )

                indicators = fetcher.fetch_indicators()

                # Should fetch from API (cache load failed)
                mock_fetch.assert_called_once()

    def test_cache_directory_creation(self):
        """Test cache directory is created if it doesn't exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "subdir" / "test_cache.json"

            fetcher = MacroFetcher(
                api_key="test_key",
                cache_file=str(cache_file),
            )

            with patch.object(fetcher, "_fetch_from_api") as mock_fetch:
                mock_fetch.return_value = MacroIndicators(
                    gdp_growth_yoy=3.0,
                    cpi_yoy=2.5,
                    unemployment_rate=4.0,
                    fed_funds_rate=5.0,
                    treasury_10y=4.0,
                    timestamp=datetime.now(timezone.utc),
                )

                indicators = fetcher.fetch_indicators()

                # Cache directory should be created
                assert cache_file.parent.exists()
                assert cache_file.exists()

    def test_partial_api_response(self):
        """Test handling when some series fail to fetch"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_cache.json"
            fetcher = MacroFetcher(api_key="test_key", cache_file=str(cache_file))

            # Mock partial failures
            with patch.object(fetcher, "_fetch_series") as mock_fetch_series:
                def side_effect(series_id, lookback_days):
                    if series_id == "GDPC1":
                        return [20000.0, 20800.0]
                    elif series_id == "CPIAUCSL":
                        return None  # Failed
                    elif series_id == "UNRATE":
                        return [4.0]
                    elif series_id == "DFF":
                        return None  # Failed
                    elif series_id == "DGS10":
                        return [4.30]
                    return None

                mock_fetch_series.side_effect = side_effect

                indicators = fetcher._fetch_from_api()

                # Should have some indicators populated, others None
                assert indicators.gdp_growth_yoy is not None
                assert indicators.cpi_yoy is None
                assert indicators.unemployment_rate == 4.0
                assert indicators.fed_funds_rate is None
                assert indicators.treasury_10y == 4.30
