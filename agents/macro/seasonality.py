"""
Seasonality pattern analysis for trading symbols.

Analyzes historical price patterns by month to identify seasonal trends.
Uses 10 years of historical data from ohlcv_cache database to calculate
average returns and win rates for each month.

Strong bullish months (>70% win rate, >3% avg return) get +20 bonus.
Strong bearish months (<30% win rate, <-3% avg return) get -20 penalty.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from config.settings import (
    EDGE_SEASONAL_CACHE_FILE,
    EDGE_SEASONAL_CACHE_TTL,
    EDGE_FALLBACK_ENABLED,
)
from db.ohlcv_cache import read_cached_ohlcv

logger = logging.getLogger(__name__)


@dataclass
class SeasonalPattern:
    """Historical seasonality data"""
    symbol: str
    month: int                   # 1-12
    avg_return_pct: float        # Average % return for this month
    win_rate_pct: float          # % of years this month was positive
    sample_size: int             # Number of years in sample
    is_strong: bool              # True if win_rate > 70% and sample > 10
    timestamp: datetime

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization"""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SeasonalPattern":
        """Create from dict (JSON deserialization)"""
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


class SeasonalScorer:
    """Analyzes historical seasonality patterns"""

    # Minimum data requirements
    MIN_YEARS = 5               # Minimum 5 years of data
    MIN_SAMPLE_SIZE = 10        # Minimum 10 years for "strong" pattern

    # Strong pattern thresholds
    STRONG_BULLISH_WIN_RATE = 70.0      # >70% win rate
    STRONG_BULLISH_AVG_RETURN = 3.0     # >3% avg return
    STRONG_BEARISH_WIN_RATE = 30.0      # <30% win rate
    STRONG_BEARISH_AVG_RETURN = -3.0    # <-3% avg return

    # Weak pattern thresholds
    WEAK_BULLISH_WIN_RATE = 60.0        # >60% win rate
    WEAK_BULLISH_AVG_RETURN = 1.0       # >1% avg return
    WEAK_BEARISH_WIN_RATE = 40.0        # <40% win rate
    WEAK_BEARISH_AVG_RETURN = -1.0      # <-1% avg return

    def __init__(
        self,
        lookback_years: int = 10,
        cache_file: str = EDGE_SEASONAL_CACHE_FILE,
        cache_ttl_seconds: int = EDGE_SEASONAL_CACHE_TTL,
        fallback_enabled: bool = EDGE_FALLBACK_ENABLED,
    ) -> None:
        """
        Initialize seasonal scorer.

        Args:
            lookback_years: Years of history to analyze (default 10)
            cache_file: Path to cache file
            cache_ttl_seconds: Cache TTL (default 30 days)
            fallback_enabled: Use stale cache on failure
        """
        self.lookback_years = lookback_years
        self.cache_file = Path(cache_file)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.fallback_enabled = fallback_enabled

    def get_seasonal_score(
        self,
        symbol: str,
        month: Optional[int] = None,
    ) -> float:
        """
        Calculate seasonal bonus for current month.

        Scoring logic:
        - Strong bullish month (>70% win rate, avg return >3%) = +20
        - Weak bullish month (>60% win rate, avg return >1%) = +10
        - Neutral month = 0
        - Weak bearish month (<40% win rate, avg return <-1%) = -10
        - Strong bearish month (<30% win rate, avg return <-3%) = -20

        Args:
            symbol: Trading symbol
            month: Month to analyze (1-12), defaults to current month

        Returns:
            Seasonal bonus from -20 to +20
        """
        # Use current month if not specified
        if month is None:
            month = datetime.now(timezone.utc).month

        # Validate month
        if not 1 <= month <= 12:
            logger.error(f"Invalid month: {month}")
            return 0.0

        # Get seasonality patterns
        patterns = self.calculate_seasonality(symbol)

        # Get pattern for this month
        pattern = patterns.get(month)
        if not pattern:
            logger.debug(f"No seasonal pattern for {symbol} month {month}")
            return 0.0

        # Check minimum sample size
        if pattern.sample_size < self.MIN_YEARS:
            logger.debug(
                f"Insufficient sample size for {symbol}: "
                f"{pattern.sample_size} < {self.MIN_YEARS} years"
            )
            return 0.0

        # Calculate seasonal score
        score = self._calculate_score(pattern)

        logger.info(
            f"Seasonal score for {symbol} (month {month}): {score:.1f} "
            f"(win_rate: {pattern.win_rate_pct:.1f}%, avg_return: {pattern.avg_return_pct:.2f}%)"
        )

        return score

    def calculate_seasonality(
        self,
        symbol: str,
    ) -> dict[int, SeasonalPattern]:
        """
        Calculate seasonality for all 12 months.

        Algorithm:
        1. Fetch historical daily data (10 years) from ohlcv_cache
        2. Group by month (1-12)
        3. Calculate avg return and win rate per month
        4. Identify strong patterns (>70% win rate, >10 samples)

        Args:
            symbol: Trading symbol

        Returns:
            Dict of {month: SeasonalPattern}
        """
        # Try cache first
        cached = self._load_cache(symbol)
        if cached and self._is_cache_fresh(cached):
            logger.info(f"Using fresh seasonality data from cache for {symbol}")
            return cached

        # Cache miss or stale — calculate from database
        try:
            logger.info(f"Calculating seasonality from database for {symbol}")
            patterns = self._calculate_from_db(symbol)

            if patterns:
                self._save_cache(symbol, patterns)
                return patterns

        except Exception as e:
            logger.error(f"Failed to calculate seasonality for {symbol}: {e}")

        # Fallback to stale cache
        if cached and self.fallback_enabled:
            logger.warning(f"Using stale cache as fallback for {symbol}")
            return cached

        return {}

    def _calculate_from_db(
        self,
        symbol: str,
    ) -> dict[int, SeasonalPattern]:
        """
        Calculate seasonality from ohlcv_cache database.

        Args:
            symbol: Trading symbol

        Returns:
            Dict of {month: SeasonalPattern}
        """
        # Fetch historical data (10 years)
        period = f"{self.lookback_years}y"
        df = read_cached_ohlcv(symbol, "1d", period=period)

        if df is None or df.empty:
            logger.warning(f"No historical data for {symbol}")
            return {}

        # Ensure timestamp is datetime
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"])

        # Calculate monthly returns
        df = df.sort_values("timestamp").reset_index(drop=True)
        df["month"] = df["timestamp"].dt.month
        df["year"] = df["timestamp"].dt.year

        # Group by year and month, get first and last close
        monthly = df.groupby(["year", "month"]).agg({
            "close": ["first", "last"]
        }).reset_index()

        monthly.columns = ["year", "month", "open", "close"]

        # Calculate monthly return
        monthly["return_pct"] = ((monthly["close"] - monthly["open"]) / monthly["open"]) * 100.0

        # Group by month and calculate statistics
        patterns = {}

        for month in range(1, 13):
            month_data = monthly[monthly["month"] == month]

            if month_data.empty:
                continue

            sample_size = len(month_data)

            # Calculate average return
            avg_return = month_data["return_pct"].mean()

            # Calculate win rate (% of positive months)
            wins = (month_data["return_pct"] > 0).sum()
            win_rate = (wins / sample_size) * 100.0

            # Check if strong pattern
            is_strong = (
                sample_size >= self.MIN_SAMPLE_SIZE and
                (
                    (win_rate > self.STRONG_BULLISH_WIN_RATE and avg_return > self.STRONG_BULLISH_AVG_RETURN) or
                    (win_rate < self.STRONG_BEARISH_WIN_RATE and avg_return < self.STRONG_BEARISH_AVG_RETURN)
                )
            )

            pattern = SeasonalPattern(
                symbol=symbol,
                month=month,
                avg_return_pct=round(avg_return, 2),
                win_rate_pct=round(win_rate, 1),
                sample_size=sample_size,
                is_strong=is_strong,
                timestamp=datetime.now(timezone.utc),
            )

            patterns[month] = pattern

            logger.debug(
                f"{symbol} month {month}: win_rate={win_rate:.1f}%, "
                f"avg_return={avg_return:.2f}%, sample={sample_size}"
            )

        return patterns

    def _calculate_score(self, pattern: SeasonalPattern) -> float:
        """
        Calculate seasonal score from pattern.

        Args:
            pattern: SeasonalPattern for a specific month

        Returns:
            Score from -20 to +20
        """
        win_rate = pattern.win_rate_pct
        avg_return = pattern.avg_return_pct

        # Strong bullish month
        if (win_rate >= self.STRONG_BULLISH_WIN_RATE and
            avg_return >= self.STRONG_BULLISH_AVG_RETURN):
            return 20.0

        # Strong bearish month
        if (win_rate <= self.STRONG_BEARISH_WIN_RATE and
            avg_return <= self.STRONG_BEARISH_AVG_RETURN):
            return -20.0

        # Weak bullish month
        if (win_rate >= self.WEAK_BULLISH_WIN_RATE and
            avg_return >= self.WEAK_BULLISH_AVG_RETURN):
            return 10.0

        # Weak bearish month
        if (win_rate <= self.WEAK_BEARISH_WIN_RATE and
            avg_return <= self.WEAK_BEARISH_AVG_RETURN):
            return -10.0

        # Neutral month
        return 0.0

    def _load_cache(self, symbol: str) -> Optional[dict[int, SeasonalPattern]]:
        """Load seasonality patterns from cache file"""
        if not self.cache_file.exists():
            return None

        try:
            with open(self.cache_file, "r") as f:
                cache = json.load(f)

            # Cache is a dict mapping symbol -> {month: pattern}
            if symbol not in cache:
                return None

            # Deserialize patterns
            patterns = {}
            for month_str, pattern_data in cache[symbol].items():
                month = int(month_str)
                patterns[month] = SeasonalPattern.from_dict(pattern_data)

            return patterns

        except Exception as e:
            logger.error(f"Failed to load cache for {symbol}: {e}")
            return None

    def _save_cache(
        self,
        symbol: str,
        patterns: dict[int, SeasonalPattern],
    ) -> None:
        """Save seasonality patterns to cache file"""
        try:
            # Load existing cache
            cache = {}
            if self.cache_file.exists():
                with open(self.cache_file, "r") as f:
                    cache = json.load(f)

            # Serialize patterns
            serialized = {}
            for month, pattern in patterns.items():
                serialized[str(month)] = pattern.to_dict()

            # Update cache for this symbol
            cache[symbol] = serialized

            # Save cache
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(cache, f, indent=2)

            logger.info(f"Saved seasonality data to cache for {symbol}")

        except Exception as e:
            logger.error(f"Failed to save cache for {symbol}: {e}")

    def _is_cache_fresh(
        self,
        patterns: dict[int, SeasonalPattern],
    ) -> bool:
        """Check if cached patterns are still fresh"""
        if not patterns:
            return False

        # Check first pattern's timestamp
        first_pattern = next(iter(patterns.values()))
        age_seconds = (datetime.now(timezone.utc) - first_pattern.timestamp).total_seconds()
        return age_seconds < self.cache_ttl_seconds
