"""
FRED API integration for macro economic indicators.

Fetches GDP, CPI, unemployment, Fed Funds rate, and 10-year Treasury yield
from the Federal Reserve Economic Data (FRED) API. Implements 24-hour cache
with fallback to stale data on API failure.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from config.settings import (
    FRED_API_KEY,
    FRED_SERIES_GDP,
    FRED_SERIES_CPI,
    FRED_SERIES_UNEMPLOYMENT,
    FRED_SERIES_FED_FUNDS,
    FRED_SERIES_TREASURY_10Y,
    EDGE_MACRO_CACHE_FILE,
    EDGE_MACRO_CACHE_TTL,
    EDGE_FALLBACK_ENABLED,
)

logger = logging.getLogger(__name__)


@dataclass
class MacroIndicators:
    """FRED macro indicators"""
    gdp_growth_yoy: Optional[float]      # % YoY GDP growth
    cpi_yoy: Optional[float]             # % YoY inflation
    unemployment_rate: Optional[float]   # % unemployment
    fed_funds_rate: Optional[float]      # % Fed Funds rate
    treasury_10y: Optional[float]        # % 10-year yield
    timestamp: datetime

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization"""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MacroIndicators":
        """Create from dict (JSON deserialization)"""
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


class MacroFetcher:
    """Fetches macro indicators from FRED API with caching"""

    def __init__(
        self,
        api_key: str = FRED_API_KEY,
        cache_file: str = EDGE_MACRO_CACHE_FILE,
        cache_ttl_seconds: int = EDGE_MACRO_CACHE_TTL,
        fallback_enabled: bool = EDGE_FALLBACK_ENABLED,
    ) -> None:
        """
        Initialize macro fetcher.

        Args:
            api_key: FRED API key (free from stlouisfed.org)
            cache_file: Path to cache file
            cache_ttl_seconds: Cache TTL (default 24 hours)
            fallback_enabled: Use stale cache on API failure
        """
        self.api_key = api_key
        self.cache_file = Path(cache_file)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.fallback_enabled = fallback_enabled
        self.base_url = "https://api.stlouisfed.org/fred/series/observations"

        if not self.api_key:
            logger.warning("FRED_API_KEY not set — macro fetcher will use cache only")

    def fetch_indicators(self) -> MacroIndicators:
        """
        Fetch latest macro indicators from FRED.

        Returns cached data if fresh (< 24 hours old).
        Falls back to stale cache on API failure if fallback_enabled.

        Returns:
            MacroIndicators dataclass
        """
        # Try cache first
        cached = self._load_cache()
        if cached and self._is_cache_fresh(cached):
            logger.info("Using fresh macro indicators from cache")
            return cached

        # Cache miss or stale — fetch from API
        if not self.api_key:
            if cached and self.fallback_enabled:
                logger.warning("No FRED API key — using stale cache")
                return cached
            logger.error("No FRED API key and no cache available")
            return self._empty_indicators()

        try:
            logger.info("Fetching macro indicators from FRED API")
            indicators = self._fetch_from_api()
            self._save_cache(indicators)
            return indicators
        except Exception as e:
            logger.error(f"Failed to fetch from FRED API: {e}")
            if cached and self.fallback_enabled:
                logger.warning("Using stale cache as fallback")
                return cached
            return self._empty_indicators()

    def _fetch_from_api(self) -> MacroIndicators:
        """Fetch all indicators from FRED API"""
        gdp = self._fetch_series(FRED_SERIES_GDP, lookback_days=365)
        cpi = self._fetch_series(FRED_SERIES_CPI, lookback_days=365)
        unemployment = self._fetch_series(FRED_SERIES_UNEMPLOYMENT, lookback_days=90)
        fed_funds = self._fetch_series(FRED_SERIES_FED_FUNDS, lookback_days=30)
        treasury_10y = self._fetch_series(FRED_SERIES_TREASURY_10Y, lookback_days=30)

        # Calculate YoY growth for GDP and CPI
        gdp_growth = self._calculate_yoy_growth(gdp) if gdp else None
        cpi_yoy = self._calculate_yoy_growth(cpi) if cpi else None

        return MacroIndicators(
            gdp_growth_yoy=gdp_growth,
            cpi_yoy=cpi_yoy,
            unemployment_rate=unemployment[-1] if unemployment else None,
            fed_funds_rate=fed_funds[-1] if fed_funds else None,
            treasury_10y=treasury_10y[-1] if treasury_10y else None,
            timestamp=datetime.now(timezone.utc),
        )

    def _fetch_series(
        self,
        series_id: str,
        lookback_days: int = 365,
    ) -> Optional[list[float]]:
        """
        Fetch a single FRED series.

        Args:
            series_id: FRED series ID (e.g., "GDPC1")
            lookback_days: Days of history to fetch

        Returns:
            List of values (oldest to newest) or None on failure
        """
        start_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": start_date,
            "sort_order": "asc",
        }

        try:
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            observations = data.get("observations", [])
            if not observations:
                logger.warning(f"No observations for series {series_id}")
                return None

            # Extract numeric values, skip "." (missing data)
            values = []
            for obs in observations:
                val = obs.get("value")
                if val and val != ".":
                    try:
                        values.append(float(val))
                    except ValueError:
                        continue

            if not values:
                logger.warning(f"No valid values for series {series_id}")
                return None

            logger.info(f"Fetched {len(values)} observations for {series_id}")
            return values

        except requests.RequestException as e:
            logger.error(f"Failed to fetch series {series_id}: {e}")
            return None

    def _calculate_yoy_growth(self, values: list[float]) -> Optional[float]:
        """
        Calculate year-over-year growth rate.

        Args:
            values: List of values (oldest to newest)

        Returns:
            YoY growth rate as percentage or None
        """
        if len(values) < 2:
            return None

        # Use last value vs value from ~1 year ago
        current = values[-1]
        # For quarterly data (GDP), 4 periods = 1 year
        # For monthly data (CPI), 12 periods = 1 year
        # Use min(len-1, 12) to handle both cases
        lookback_idx = max(0, len(values) - min(13, len(values)))
        previous = values[lookback_idx]

        if previous == 0:
            return None

        yoy_growth = ((current - previous) / previous) * 100
        return round(yoy_growth, 2)

    def _load_cache(self) -> Optional[MacroIndicators]:
        """Load indicators from cache file"""
        if not self.cache_file.exists():
            return None

        try:
            with open(self.cache_file, "r") as f:
                data = json.load(f)
            return MacroIndicators.from_dict(data)
        except Exception as e:
            logger.error(f"Failed to load cache: {e}")
            return None

    def _save_cache(self, indicators: MacroIndicators) -> None:
        """Save indicators to cache file"""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(indicators.to_dict(), f, indent=2)
            logger.info(f"Saved macro indicators to cache: {self.cache_file}")
        except Exception as e:
            logger.error(f"Failed to save cache: {e}")

    def _is_cache_fresh(self, indicators: MacroIndicators) -> bool:
        """Check if cached indicators are still fresh"""
        age_seconds = (datetime.now(timezone.utc) - indicators.timestamp).total_seconds()
        return age_seconds < self.cache_ttl_seconds

    def _empty_indicators(self) -> MacroIndicators:
        """Return empty indicators (all None)"""
        return MacroIndicators(
            gdp_growth_yoy=None,
            cpi_yoy=None,
            unemployment_rate=None,
            fed_funds_rate=None,
            treasury_10y=None,
            timestamp=datetime.now(timezone.utc),
        )
