"""
Basic smoke tests for macro fetcher and scorer.

These tests verify the basic structure and cache behavior without requiring
a real FRED API key.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.macro.macro_fetcher import MacroFetcher, MacroIndicators
from agents.macro.macro_scorer import MacroScorer


def test_macro_indicators_serialization():
    """Test MacroIndicators to_dict and from_dict"""
    indicators = MacroIndicators(
        gdp_growth_yoy=3.5,
        cpi_yoy=2.8,
        unemployment_rate=4.2,
        fed_funds_rate=1.5,
        treasury_10y=2.3,
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


def test_macro_fetcher_cache_behavior():
    """Test cache hit/miss behavior"""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "test_macro_cache.json"

        # Create fetcher without API key (cache-only mode)
        fetcher = MacroFetcher(
            api_key="",
            cache_file=str(cache_file),
            cache_ttl_seconds=3600,
            fallback_enabled=True,
        )

        # First fetch with no cache should return empty indicators
        indicators = fetcher.fetch_indicators()
        assert indicators.gdp_growth_yoy is None
        assert indicators.cpi_yoy is None

        # Manually create a cache file
        test_indicators = MacroIndicators(
            gdp_growth_yoy=3.0,
            cpi_yoy=2.5,
            unemployment_rate=4.0,
            fed_funds_rate=2.0,
            treasury_10y=2.5,
            timestamp=datetime.now(timezone.utc),
        )
        with open(cache_file, "w") as f:
            json.dump(test_indicators.to_dict(), f)

        # Second fetch should use cache
        indicators = fetcher.fetch_indicators()
        assert indicators.gdp_growth_yoy == 3.0
        assert indicators.cpi_yoy == 2.5


def test_macro_fetcher_stale_cache_fallback():
    """Test fallback to stale cache when API fails"""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "test_macro_cache.json"

        # Create stale cache (25 hours old)
        stale_indicators = MacroIndicators(
            gdp_growth_yoy=2.5,
            cpi_yoy=3.0,
            unemployment_rate=5.0,
            fed_funds_rate=3.0,
            treasury_10y=3.5,
            timestamp=datetime.now(timezone.utc) - timedelta(hours=25),
        )
        with open(cache_file, "w") as f:
            json.dump(stale_indicators.to_dict(), f)

        # Fetcher with no API key should use stale cache as fallback
        fetcher = MacroFetcher(
            api_key="",
            cache_file=str(cache_file),
            cache_ttl_seconds=3600,  # 1 hour TTL
            fallback_enabled=True,
        )

        indicators = fetcher.fetch_indicators()
        assert indicators.gdp_growth_yoy == 2.5  # Stale cache used


def test_macro_scorer_risk_on_conditions():
    """Test scoring for risk-on conditions"""
    # Create mock fetcher with risk-on indicators
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "test_macro_cache.json"

        risk_on_indicators = MacroIndicators(
            gdp_growth_yoy=4.0,   # Strong growth
            cpi_yoy=2.0,          # Low inflation
            unemployment_rate=3.5, # Low unemployment
            fed_funds_rate=1.0,   # Low rates
            treasury_10y=1.5,     # Low yields
            timestamp=datetime.now(timezone.utc),
        )
        with open(cache_file, "w") as f:
            json.dump(risk_on_indicators.to_dict(), f)

        fetcher = MacroFetcher(api_key="", cache_file=str(cache_file))
        scorer = MacroScorer(fetcher=fetcher)

        score = scorer.get_macro_score()
        assert score > 50, "Risk-on conditions should produce positive score"


def test_macro_scorer_risk_off_conditions():
    """Test scoring for risk-off conditions"""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "test_macro_cache.json"

        risk_off_indicators = MacroIndicators(
            gdp_growth_yoy=-1.0,  # Recession
            cpi_yoy=6.0,          # High inflation
            unemployment_rate=9.0, # High unemployment
            fed_funds_rate=6.0,   # High rates
            treasury_10y=6.0,     # High yields
            timestamp=datetime.now(timezone.utc),
        )
        with open(cache_file, "w") as f:
            json.dump(risk_off_indicators.to_dict(), f)

        fetcher = MacroFetcher(api_key="", cache_file=str(cache_file))
        scorer = MacroScorer(fetcher=fetcher)

        score = scorer.get_macro_score()
        assert score < -50, "Risk-off conditions should produce negative score"


def test_macro_scorer_insufficient_indicators():
    """Test behavior when insufficient indicators available"""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "test_macro_cache.json"

        # Only 2 indicators (need 3 minimum)
        partial_indicators = MacroIndicators(
            gdp_growth_yoy=3.0,
            cpi_yoy=2.5,
            unemployment_rate=None,
            fed_funds_rate=None,
            treasury_10y=None,
            timestamp=datetime.now(timezone.utc),
        )
        with open(cache_file, "w") as f:
            json.dump(partial_indicators.to_dict(), f)

        fetcher = MacroFetcher(api_key="", cache_file=str(cache_file))
        scorer = MacroScorer(fetcher=fetcher)

        score = scorer.get_macro_score()
        assert score == 0.0, "Insufficient indicators should return 0"
