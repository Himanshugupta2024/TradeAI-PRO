"""
CFTC Commitment of Traders (COT) data integration.

Fetches institutional positioning data from CFTC reports using the cot-reports
library. Implements 7-day cache with fallback to stale data on API failure.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.settings import (
    EDGE_COT_CACHE_FILE,
    EDGE_COT_CACHE_TTL,
    EDGE_FALLBACK_ENABLED,
)

logger = logging.getLogger(__name__)


@dataclass
class COTData:
    """CFTC Commitment of Traders data"""
    symbol: str
    commercial_long: int         # Commercial hedgers long contracts
    commercial_short: int        # Commercial hedgers short contracts
    non_commercial_long: int     # Large speculators long
    non_commercial_short: int    # Large speculators short
    net_position: int            # Net non-commercial position
    net_position_pct: float      # Net as % of open interest
    timestamp: datetime

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization"""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "COTData":
        """Create from dict (JSON deserialization)"""
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


class COTFetcher:
    """Fetches COT data from CFTC with caching"""

    # Symbol to CFTC contract code mapping
    SYMBOL_MAP = {
        # Forex pairs
        "EUR/USD": "092741",  # Euro FX
        "EURUSD": "092741",
        "GBP/USD": "096742",  # British Pound
        "GBPUSD": "096742",
        "USD/JPY": "097741",  # Japanese Yen
        "USDJPY": "097741",
        "AUD/USD": "232741",  # Australian Dollar
        "AUDUSD": "232741",
        "USD/CAD": "090741",  # Canadian Dollar
        "USDCAD": "090741",
        "NZD/USD": "112741",  # New Zealand Dollar
        "NZDUSD": "112741",
        "USD/CHF": "092741",  # Swiss Franc
        "USDCHF": "092741",
        # Commodities
        "GOLD": "088691",     # Gold
        "XAUUSD": "088691",
        "SILVER": "084691",   # Silver
        "XAGUSD": "084691",
        "CRUDE": "067651",    # Crude Oil WTI
        "WTI": "067651",
        "BRENT": "067651",
        "NATGAS": "023651",   # Natural Gas
        "COPPER": "085692",   # Copper
    }

    def __init__(
        self,
        cache_file: str = EDGE_COT_CACHE_FILE,
        cache_ttl_seconds: int = EDGE_COT_CACHE_TTL,
        fallback_enabled: bool = EDGE_FALLBACK_ENABLED,
    ) -> None:
        """
        Initialize COT fetcher.

        Args:
            cache_file: Path to cache file
            cache_ttl_seconds: Cache TTL (default 7 days)
            fallback_enabled: Use stale cache on API failure
        """
        self.cache_file = Path(cache_file)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.fallback_enabled = fallback_enabled

    def fetch_cot_data(self, symbol: str) -> Optional[COTData]:
        """
        Fetch latest COT data from CFTC.

        CFTC publishes COT reports every Friday (3:30 PM ET) with data
        as of Tuesday close. Data is free via CFTC website download.

        Args:
            symbol: Trading symbol (e.g., "EUR/USD", "GOLD")

        Returns:
            COTData or None if symbol not tracked by CFTC
        """
        # Check if symbol has COT data
        cftc_code = self.map_symbol_to_cftc_code(symbol)
        if not cftc_code:
            logger.debug(f"No COT data available for symbol: {symbol}")
            return None

        # Try cache first
        cached = self._load_cache(symbol)
        if cached and self._is_cache_fresh(cached):
            logger.info(f"Using fresh COT data from cache for {symbol}")
            return cached

        # Cache miss or stale — fetch from API
        try:
            logger.info(f"Fetching COT data from CFTC for {symbol}")
            cot_data = self._fetch_from_cftc(symbol, cftc_code)
            if cot_data:
                self._save_cache(symbol, cot_data)
            return cot_data
        except Exception as e:
            logger.error(f"Failed to fetch COT data for {symbol}: {e}")
            if cached and self.fallback_enabled:
                logger.warning(f"Using stale cache as fallback for {symbol}")
                return cached
            return None

    def map_symbol_to_cftc_code(self, symbol: str) -> Optional[str]:
        """
        Map trading symbol to CFTC contract code.

        Examples:
        - "EUR/USD" → "092741" (Euro FX)
        - "GBP/USD" → "096742" (British Pound)
        - "USD/JPY" → "097741" (Japanese Yen)
        - "GOLD" → "088691" (Gold)
        - "BTC/USDT" → None (no COT data for crypto)

        Args:
            symbol: Trading symbol

        Returns:
            CFTC contract code or None
        """
        if symbol is None:
            return None
        # Normalize symbol (remove spaces, uppercase)
        normalized = symbol.replace(" ", "").upper()
        return self.SYMBOL_MAP.get(normalized)

    def _fetch_from_cftc(self, symbol: str, cftc_code: str) -> Optional[COTData]:
        """
        Fetch COT data from CFTC using cot-reports library.

        Args:
            symbol: Trading symbol
            cftc_code: CFTC contract code

        Returns:
            COTData or None on failure
        """
        try:
            # Import cot_reports library (lazy import to avoid startup overhead)
            from cot_reports import cot_year

            # Get current year's COT data
            current_year = datetime.now(timezone.utc).year
            df = cot_year(current_year, cot_report_type="legacy_fut")

            # Filter for specific contract code
            contract_data = df[df["CFTC Contract Market Code"] == cftc_code]

            if contract_data.empty:
                logger.warning(f"No COT data found for CFTC code {cftc_code}")
                return None

            # Get most recent report (last row)
            latest = contract_data.iloc[-1]

            # Extract positioning data
            commercial_long = int(latest.get("Commercial Long", 0))
            commercial_short = int(latest.get("Commercial Short", 0))
            non_commercial_long = int(latest.get("Noncommercial Long", 0))
            non_commercial_short = int(latest.get("Noncommercial Short", 0))
            open_interest = int(latest.get("Open Interest", 1))  # Avoid division by zero

            # Calculate net position
            net_position = non_commercial_long - non_commercial_short
            net_position_pct = (net_position / open_interest * 100) if open_interest > 0 else 0.0

            return COTData(
                symbol=symbol,
                commercial_long=commercial_long,
                commercial_short=commercial_short,
                non_commercial_long=non_commercial_long,
                non_commercial_short=non_commercial_short,
                net_position=net_position,
                net_position_pct=round(net_position_pct, 2),
                timestamp=datetime.now(timezone.utc),
            )

        except ImportError:
            logger.error("cot-reports library not installed. Run: pip install cot-reports")
            return None
        except Exception as e:
            logger.error(f"Failed to fetch COT data from CFTC: {e}")
            return None

    def _load_cache(self, symbol: str) -> Optional[COTData]:
        """Load COT data from cache file for specific symbol"""
        if not self.cache_file.exists():
            return None

        try:
            with open(self.cache_file, "r") as f:
                cache = json.load(f)

            # Cache is a dict mapping symbol -> COTData
            symbol_data = cache.get(symbol)
            if not symbol_data:
                return None

            return COTData.from_dict(symbol_data)
        except Exception as e:
            logger.error(f"Failed to load cache for {symbol}: {e}")
            return None

    def _save_cache(self, symbol: str, cot_data: COTData) -> None:
        """Save COT data to cache file"""
        try:
            # Load existing cache
            cache = {}
            if self.cache_file.exists():
                with open(self.cache_file, "r") as f:
                    cache = json.load(f)

            # Update cache for this symbol
            cache[symbol] = cot_data.to_dict()

            # Save back to file
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(cache, f, indent=2)

            logger.info(f"Saved COT data to cache for {symbol}: {self.cache_file}")
        except Exception as e:
            logger.error(f"Failed to save cache for {symbol}: {e}")

    def _is_cache_fresh(self, cot_data: COTData) -> bool:
        """Check if cached COT data is still fresh"""
        age_seconds = (datetime.now(timezone.utc) - cot_data.timestamp).total_seconds()
        return age_seconds < self.cache_ttl_seconds
