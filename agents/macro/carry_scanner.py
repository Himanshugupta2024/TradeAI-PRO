"""
Carry trade scanner for forex pairs.

Calculates carry bonus based on interest rate differentials between currency pairs.
Only applies to forex pairs in risk-on environments. Uses FRED API for central bank
policy rates.

Carry trade logic:
- Positive carry (higher interest rate currency) = bonus in risk-on environment
- Negative carry (lower interest rate currency) = penalty in risk-on environment
- Risk-off environment = no carry bonus (carry trades unwound)
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

from config.settings import (
    FRED_API_KEY,
    EDGE_CARRY_CACHE_FILE,
    EDGE_CARRY_CACHE_TTL,
    EDGE_FALLBACK_ENABLED,
)

logger = logging.getLogger(__name__)


# FRED series IDs for central bank policy rates
FRED_INTEREST_RATES = {
    "USD": "DFF",           # Federal Funds Rate (daily)
    "EUR": "ECBDFR",        # ECB Deposit Facility Rate
    "GBP": "BOERUKQ",       # Bank of England Official Bank Rate
    "JPY": "IRSTCB01JPM156N",  # Japan Policy Rate (monthly)
    "AUD": "IRSTCB01AUM156N",  # Australia Cash Rate (monthly)
    "CAD": "IRSTCB01CAM156N",  # Canada Overnight Rate (monthly)
    "INR": "IRSTCB01INM156N",  # India Repo Rate (monthly)
}


@dataclass
class CarryData:
    """Carry trade data for a forex pair"""
    symbol: str
    base_currency: str
    quote_currency: str
    base_rate: Optional[float]      # Base currency interest rate (%)
    quote_rate: Optional[float]     # Quote currency interest rate (%)
    carry_differential: Optional[float]  # base_rate - quote_rate (%)
    carry_bonus: float              # -10 to +10
    risk_regime: str                # "RISK_ON" | "RISK_OFF" | "NEUTRAL"
    timestamp: datetime

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization"""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "CarryData":
        """Create from dict (JSON deserialization)"""
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


class CarryScanner:
    """Calculates carry bonus for forex pairs"""

    # Carry differential thresholds (percentage points)
    STRONG_CARRY_THRESHOLD = 3.0    # >3% differential = strong carry
    WEAK_CARRY_THRESHOLD = 1.0      # >1% differential = weak carry

    def __init__(
        self,
        api_key: str = FRED_API_KEY,
        cache_file: str = EDGE_CARRY_CACHE_FILE,
        cache_ttl_seconds: int = EDGE_CARRY_CACHE_TTL,
        fallback_enabled: bool = EDGE_FALLBACK_ENABLED,
    ) -> None:
        """
        Initialize carry scanner.

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
            logger.warning("FRED_API_KEY not set — carry scanner will use cache only")

    def get_carry_score(
        self,
        symbol: str,
        risk_regime: str = "NEUTRAL",
    ) -> float:
        """
        Calculate carry bonus for a symbol.

        Only applies to forex pairs in risk-on environments.
        Returns 0 for:
        - Non-forex symbols (crypto, NSE stocks)
        - Risk-off environments (carry trades unwound)
        - Neutral risk environments

        Scoring logic (risk-on only):
        - Strong positive carry (>3% differential) = +10
        - Weak positive carry (1-3% differential) = +5
        - Neutral carry (-1% to +1%) = 0
        - Weak negative carry (-3% to -1%) = -5
        - Strong negative carry (<-3% differential) = -10

        Args:
            symbol: Trading symbol (e.g., "EUR/USD", "BTC/USDT")
            risk_regime: Current risk environment ("RISK_ON" | "RISK_OFF" | "NEUTRAL")

        Returns:
            Carry bonus from -10 to +10
        """
        # Check if symbol is a forex pair
        if not self._is_forex_pair(symbol):
            logger.debug(f"{symbol} is not a forex pair, returning 0 carry bonus")
            return 0.0

        # Only apply carry bonus in risk-on environments
        if risk_regime != "RISK_ON":
            logger.debug(
                f"Risk regime is {risk_regime}, not RISK_ON — returning 0 carry bonus"
            )
            return 0.0

        # Fetch carry data
        carry_data = self._fetch_carry_data(symbol, risk_regime)

        if not carry_data or carry_data.carry_differential is None:
            logger.warning(f"No carry data for {symbol}, returning 0")
            return 0.0

        logger.info(
            f"Carry bonus for {symbol}: {carry_data.carry_bonus:.1f} "
            f"(differential: {carry_data.carry_differential:.2f}%)"
        )

        return carry_data.carry_bonus

    def _fetch_carry_data(
        self,
        symbol: str,
        risk_regime: str,
    ) -> Optional[CarryData]:
        """
        Fetch carry data for a forex pair.

        Args:
            symbol: Forex pair (e.g., "EUR/USD")
            risk_regime: Current risk environment

        Returns:
            CarryData or None
        """
        # Try cache first
        cached = self._load_cache(symbol)
        if cached and self._is_cache_fresh(cached):
            logger.info(f"Using fresh carry data from cache for {symbol}")
            # Update risk regime (may have changed)
            cached.risk_regime = risk_regime
            cached.carry_bonus = self._calculate_carry_bonus(
                cached.carry_differential, risk_regime
            )
            return cached

        # Cache miss or stale — fetch from API
        if not self.api_key:
            if cached and self.fallback_enabled:
                logger.warning(f"No FRED API key — using stale cache for {symbol}")
                cached.risk_regime = risk_regime
                cached.carry_bonus = self._calculate_carry_bonus(
                    cached.carry_differential, risk_regime
                )
                return cached
            logger.error(f"No FRED API key and no cache for {symbol}")
            return None

        try:
            logger.info(f"Fetching carry data from FRED API for {symbol}")
            carry_data = self._fetch_from_api(symbol, risk_regime)
            if carry_data:
                self._save_cache(carry_data)
            return carry_data
        except Exception as e:
            logger.error(f"Failed to fetch carry data for {symbol}: {e}")
            if cached and self.fallback_enabled:
                logger.warning(f"Using stale cache as fallback for {symbol}")
                cached.risk_regime = risk_regime
                cached.carry_bonus = self._calculate_carry_bonus(
                    cached.carry_differential, risk_regime
                )
                return cached
            return None

    def _fetch_from_api(
        self,
        symbol: str,
        risk_regime: str,
    ) -> Optional[CarryData]:
        """
        Fetch interest rates from FRED API and calculate carry.

        Args:
            symbol: Forex pair (e.g., "EUR/USD")
            risk_regime: Current risk environment

        Returns:
            CarryData or None
        """
        # Parse currency pair
        base_currency, quote_currency = self._parse_forex_pair(symbol)
        if not base_currency or not quote_currency:
            logger.error(f"Failed to parse forex pair: {symbol}")
            return None

        # Fetch interest rates
        base_rate = self._fetch_interest_rate(base_currency)
        quote_rate = self._fetch_interest_rate(quote_currency)

        # Calculate carry differential
        if base_rate is not None and quote_rate is not None:
            carry_differential = base_rate - quote_rate
        else:
            carry_differential = None
            logger.warning(
                f"Missing interest rate data for {symbol}: "
                f"base={base_rate}, quote={quote_rate}"
            )

        # Calculate carry bonus
        carry_bonus = self._calculate_carry_bonus(carry_differential, risk_regime)

        return CarryData(
            symbol=symbol,
            base_currency=base_currency,
            quote_currency=quote_currency,
            base_rate=base_rate,
            quote_rate=quote_rate,
            carry_differential=carry_differential,
            carry_bonus=carry_bonus,
            risk_regime=risk_regime,
            timestamp=datetime.now(timezone.utc),
        )

    def _fetch_interest_rate(self, currency: str) -> Optional[float]:
        """
        Fetch latest interest rate for a currency from FRED.

        Args:
            currency: Currency code (e.g., "USD", "EUR")

        Returns:
            Interest rate as percentage or None
        """
        series_id = FRED_INTEREST_RATES.get(currency)
        if not series_id:
            logger.warning(f"No FRED series for currency: {currency}")
            return None

        # Fetch last 30 days of data
        start_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": start_date,
            "sort_order": "desc",  # Most recent first
            "limit": 1,  # Only need latest value
        }

        try:
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            observations = data.get("observations", [])
            if not observations:
                logger.warning(f"No observations for {currency} rate (series {series_id})")
                return None

            # Get latest value
            latest = observations[0]
            value = latest.get("value")

            if not value or value == ".":
                logger.warning(f"Missing value for {currency} rate")
                return None

            rate = float(value)
            logger.info(f"{currency} interest rate: {rate:.2f}%")
            return rate

        except requests.RequestException as e:
            logger.error(f"Failed to fetch {currency} rate: {e}")
            return None
        except (ValueError, KeyError) as e:
            logger.error(f"Failed to parse {currency} rate: {e}")
            return None

    def _calculate_carry_bonus(
        self,
        carry_differential: Optional[float],
        risk_regime: str,
    ) -> float:
        """
        Calculate carry bonus from interest rate differential.

        Only applies in risk-on environments.

        Args:
            carry_differential: Interest rate differential (base - quote) in %
            risk_regime: Current risk environment

        Returns:
            Carry bonus from -10 to +10
        """
        # No carry bonus in risk-off or neutral environments
        if risk_regime != "RISK_ON":
            return 0.0

        # No carry bonus if differential is unknown
        if carry_differential is None:
            return 0.0

        # Strong positive carry (>3%)
        if carry_differential >= self.STRONG_CARRY_THRESHOLD:
            return 10.0

        # Strong negative carry (<-3%)
        if carry_differential <= -self.STRONG_CARRY_THRESHOLD:
            return -10.0

        # Weak positive carry (1-3%)
        if carry_differential >= self.WEAK_CARRY_THRESHOLD:
            # Linear interpolation: 1% → +5, 3% → +10
            bonus = 5.0 + (carry_differential - self.WEAK_CARRY_THRESHOLD) / \
                    (self.STRONG_CARRY_THRESHOLD - self.WEAK_CARRY_THRESHOLD) * 5.0
            return round(bonus, 1)

        # Weak negative carry (-3% to -1%)
        if carry_differential <= -self.WEAK_CARRY_THRESHOLD:
            # Linear interpolation: -1% → -5, -3% → -10
            bonus = -5.0 + (carry_differential + self.WEAK_CARRY_THRESHOLD) / \
                    (self.STRONG_CARRY_THRESHOLD - self.WEAK_CARRY_THRESHOLD) * 5.0
            return round(bonus, 1)

        # Neutral carry (-1% to +1%)
        return 0.0

    def _is_forex_pair(self, symbol: str) -> bool:
        """
        Check if symbol is a forex pair.

        Forex pairs have format: "XXX/YYY" where XXX and YYY are 3-letter currency codes.

        Args:
            symbol: Trading symbol

        Returns:
            True if forex pair, False otherwise
        """
        if "/" not in symbol:
            return False

        parts = symbol.split("/")
        if len(parts) != 2:
            return False

        base, quote = parts
        # Currency codes are 3 letters
        if len(base) != 3 or len(quote) != 3:
            return False

        # Check if both are known currencies
        return base in FRED_INTEREST_RATES and quote in FRED_INTEREST_RATES

    def _parse_forex_pair(self, symbol: str) -> tuple[Optional[str], Optional[str]]:
        """
        Parse forex pair into base and quote currencies.

        Args:
            symbol: Forex pair (e.g., "EUR/USD")

        Returns:
            (base_currency, quote_currency) or (None, None)
        """
        if "/" not in symbol:
            return None, None

        parts = symbol.split("/")
        if len(parts) != 2:
            return None, None

        base, quote = parts
        if len(base) != 3 or len(quote) != 3:
            return None, None

        return base, quote

    def _load_cache(self, symbol: str) -> Optional[CarryData]:
        """Load carry data from cache file"""
        if not self.cache_file.exists():
            return None

        try:
            with open(self.cache_file, "r") as f:
                cache = json.load(f)

            # Cache is a dict mapping symbol to carry data
            if symbol not in cache:
                return None

            return CarryData.from_dict(cache[symbol])
        except Exception as e:
            logger.error(f"Failed to load cache: {e}")
            return None

    def _save_cache(self, carry_data: CarryData) -> None:
        """Save carry data to cache file"""
        try:
            # Load existing cache
            cache = {}
            if self.cache_file.exists():
                with open(self.cache_file, "r") as f:
                    cache = json.load(f)

            # Update cache for this symbol
            cache[carry_data.symbol] = carry_data.to_dict()

            # Save cache
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(cache, f, indent=2)

            logger.info(f"Saved carry data to cache: {carry_data.symbol}")
        except Exception as e:
            logger.error(f"Failed to save cache: {e}")

    def _is_cache_fresh(self, carry_data: CarryData) -> bool:
        """Check if cached carry data is still fresh"""
        age_seconds = (datetime.now(timezone.utc) - carry_data.timestamp).total_seconds()
        return age_seconds < self.cache_ttl_seconds
