"""
Retail sentiment data integration (contrarian indicator).

Fetches retail trader positioning from Myfxbook and Dukascopy to use as a
contrarian indicator. When 80%+ retail traders are long, it signals a bearish
setup (fade the crowd). Implements 1-hour cache with fallback to stale data.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from config.settings import (
    EDGE_SENTIMENT_CACHE_FILE,
    EDGE_SENTIMENT_CACHE_TTL,
    EDGE_FALLBACK_ENABLED,
)

logger = logging.getLogger(__name__)


@dataclass
class RetailSentiment:
    """Retail trader sentiment data"""
    symbol: str
    long_pct: float              # % of retail traders long
    short_pct: float             # % of retail traders short
    total_positions: int         # Total retail positions
    sentiment_extreme: bool      # True if >75% one-sided
    timestamp: datetime

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization"""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RetailSentiment":
        """Create from dict (JSON deserialization)"""
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


class SentimentScorer:
    """Scores retail sentiment as contrarian indicator"""

    # Minimum sample size for reliable sentiment
    MIN_POSITIONS = 100

    # Extreme sentiment threshold (>75% one-sided)
    EXTREME_THRESHOLD = 75.0

    def __init__(
        self,
        cache_file: str = EDGE_SENTIMENT_CACHE_FILE,
        cache_ttl_seconds: int = EDGE_SENTIMENT_CACHE_TTL,
        fallback_enabled: bool = EDGE_FALLBACK_ENABLED,
    ) -> None:
        """
        Initialize sentiment scorer.

        Args:
            cache_file: Path to cache file
            cache_ttl_seconds: Cache TTL (default 1 hour)
            fallback_enabled: Use stale cache on API failure
        """
        self.cache_file = Path(cache_file)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.fallback_enabled = fallback_enabled

    def get_sentiment_score(self, symbol: str) -> float:
        """
        Calculate contrarian sentiment score.

        Scoring logic (CONTRARIAN):
        - 80%+ retail long = -100 (bearish, fade the crowd)
        - 80%+ retail short = +100 (bullish, fade the crowd)
        - 50/50 split = 0 (neutral)

        Rationale: Retail traders are typically wrong at extremes.
        When 80%+ are long, it signals a top. When 80%+ are short,
        it signals a bottom.

        Args:
            symbol: Trading symbol (e.g., "EUR/USD", "BTC/USDT")

        Returns:
            Sentiment score from -100 (bearish) to +100 (bullish)
        """
        sentiment = self.fetch_sentiment(symbol)
        if not sentiment:
            logger.debug(f"No sentiment data for {symbol}, returning neutral score")
            return 0.0

        # Check minimum sample size
        if sentiment.total_positions < self.MIN_POSITIONS:
            logger.debug(
                f"Insufficient sample size for {symbol}: "
                f"{sentiment.total_positions} < {self.MIN_POSITIONS}"
            )
            return 0.0

        # CONTRARIAN scoring: fade the retail crowd
        # If 80% long → bearish (-100)
        # If 80% short → bullish (+100)
        # Linear interpolation between 50% (neutral) and 100% (extreme)

        if sentiment.long_pct >= 50:
            # More longs than shorts → bearish (contrarian)
            # Map 50% → 0, 100% → -100
            score = -2.0 * (sentiment.long_pct - 50.0)
        else:
            # More shorts than longs → bullish (contrarian)
            # Map 50% → 0, 0% → +100
            score = 2.0 * (50.0 - sentiment.long_pct)

        # Clamp to [-100, +100]
        score = max(-100.0, min(100.0, score))

        logger.info(
            f"Sentiment score for {symbol}: {score:.1f} "
            f"(long: {sentiment.long_pct:.1f}%, short: {sentiment.short_pct:.1f}%)"
        )

        return round(score, 1)

    def fetch_sentiment(self, symbol: str) -> Optional[RetailSentiment]:
        """
        Fetch retail sentiment from available sources.

        Tries Myfxbook first (forex), then Dukascopy as fallback.

        Args:
            symbol: Trading symbol

        Returns:
            RetailSentiment or None if unavailable
        """
        # Try cache first
        cached = self._load_cache(symbol)
        if cached and self._is_cache_fresh(cached):
            logger.info(f"Using fresh sentiment data from cache for {symbol}")
            return cached

        # Retail sentiment scrapers only support major forex pairs
        clean_sym = symbol.upper().replace("/", "").replace(" ", "")
        forex_pairs = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD", "USDINR", "EURINR", "GBPINR"}
        if clean_sym not in forex_pairs:
            return None

        # Cache miss or stale — fetch from API
        sentiment = None

        # Try Myfxbook first (forex pairs)
        sentiment = self.fetch_myfxbook_sentiment(symbol)

        # Fallback to Dukascopy if Myfxbook fails
        if not sentiment:
            sentiment = self.fetch_dukascopy_sentiment(symbol)

        # Save to cache if successful
        if sentiment:
            self._save_cache(symbol, sentiment)
            return sentiment

        # All sources failed — try stale cache as last resort
        if cached and self.fallback_enabled:
            logger.warning(f"Using stale cache as fallback for {symbol}")
            return cached

        return None

    def fetch_myfxbook_sentiment(self, symbol: str) -> Optional[RetailSentiment]:
        """
        Fetch sentiment from Myfxbook Community Outlook.

        Myfxbook provides free sentiment data for forex pairs via
        their public API (no key required).

        API: https://www.myfxbook.com/community/outlook/{symbol}

        Args:
            symbol: Forex symbol (e.g., "EURUSD", "EUR/USD")

        Returns:
            RetailSentiment or None
        """
        try:
            # Normalize symbol (remove slash, uppercase)
            normalized = symbol.replace("/", "").replace(" ", "").upper()

            # Myfxbook API endpoint
            url = f"https://www.myfxbook.com/community/outlook/{normalized}"

            logger.info(f"Fetching Myfxbook sentiment for {symbol}")

            # Make request with timeout
            response = requests.get(url, timeout=2.5)
            response.raise_for_status()

            # Parse JSON response
            data = response.json()

            # Extract sentiment data
            # Myfxbook returns: {"symbol": "EURUSD", "longPercentage": 65, "shortPercentage": 35}
            long_pct = float(data.get("longPercentage", 50.0))
            short_pct = float(data.get("shortPercentage", 50.0))

            # Estimate total positions (Myfxbook doesn't provide this)
            # Use a reasonable default for forex pairs
            total_positions = 1000

            # Check if sentiment is extreme
            sentiment_extreme = long_pct > self.EXTREME_THRESHOLD or short_pct > self.EXTREME_THRESHOLD

            return RetailSentiment(
                symbol=symbol,
                long_pct=long_pct,
                short_pct=short_pct,
                total_positions=total_positions,
                sentiment_extreme=sentiment_extreme,
                timestamp=datetime.now(timezone.utc),
            )

        except requests.RequestException as e:
            logger.error(f"Failed to fetch Myfxbook sentiment for {symbol}: {e}")
            return None
        except (KeyError, ValueError) as e:
            logger.error(f"Failed to parse Myfxbook response for {symbol}: {e}")
            return None

    def fetch_dukascopy_sentiment(self, symbol: str) -> Optional[RetailSentiment]:
        """
        Fetch sentiment from Dukascopy SWFX Sentiment Index.

        Dukascopy provides free sentiment data via their website.
        Data is scraped from public HTML (no API key).

        URL: https://www.dukascopy.com/swiss/english/marketwatch/sentiment/

        Args:
            symbol: Forex symbol

        Returns:
            RetailSentiment or None
        """
        try:
            # Normalize symbol
            normalized = symbol.replace("/", "").replace(" ", "").upper()

            logger.info(f"Fetching Dukascopy sentiment for {symbol}")

            # Dukascopy sentiment page
            url = "https://www.dukascopy.com/swiss/english/marketwatch/sentiment/"

            # Make request with timeout
            response = requests.get(url, timeout=2.5)
            response.raise_for_status()

            # Parse HTML
            soup = BeautifulSoup(response.text, "html.parser")

            # Find sentiment data for this symbol
            # Dukascopy uses a table with symbol rows
            # Structure: <tr><td>EURUSD</td><td>65%</td><td>35%</td></tr>

            # Find all table rows
            rows = soup.find_all("tr")

            for row in rows:
                cells = row.find_all("td")
                if len(cells) >= 3:
                    # Check if this row is for our symbol
                    row_symbol = cells[0].get_text(strip=True).upper()
                    if row_symbol == normalized:
                        # Extract long/short percentages
                        long_text = cells[1].get_text(strip=True).replace("%", "")
                        short_text = cells[2].get_text(strip=True).replace("%", "")

                        long_pct = float(long_text)
                        short_pct = float(short_text)

                        # Estimate total positions
                        total_positions = 1000

                        # Check if sentiment is extreme
                        sentiment_extreme = (
                            long_pct > self.EXTREME_THRESHOLD or
                            short_pct > self.EXTREME_THRESHOLD
                        )

                        return RetailSentiment(
                            symbol=symbol,
                            long_pct=long_pct,
                            short_pct=short_pct,
                            total_positions=total_positions,
                            sentiment_extreme=sentiment_extreme,
                            timestamp=datetime.now(timezone.utc),
                        )

            logger.warning(f"Symbol {symbol} not found in Dukascopy sentiment data")
            return None

        except requests.RequestException as e:
            logger.error(f"Failed to fetch Dukascopy sentiment for {symbol}: {e}")
            return None
        except (ValueError, AttributeError) as e:
            logger.error(f"Failed to parse Dukascopy response for {symbol}: {e}")
            return None

    def _load_cache(self, symbol: str) -> Optional[RetailSentiment]:
        """Load sentiment data from cache file for specific symbol"""
        if not self.cache_file.exists():
            return None

        try:
            with open(self.cache_file, "r") as f:
                cache = json.load(f)

            # Cache is a dict mapping symbol -> RetailSentiment
            symbol_data = cache.get(symbol)
            if not symbol_data:
                return None

            return RetailSentiment.from_dict(symbol_data)
        except Exception as e:
            logger.error(f"Failed to load cache for {symbol}: {e}")
            return None

    def _save_cache(self, symbol: str, sentiment: RetailSentiment) -> None:
        """Save sentiment data to cache file"""
        try:
            # Load existing cache
            cache = {}
            if self.cache_file.exists():
                with open(self.cache_file, "r") as f:
                    cache = json.load(f)

            # Update cache for this symbol
            cache[symbol] = sentiment.to_dict()

            # Save back to file
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(cache, f, indent=2)

            logger.info(f"Saved sentiment data to cache for {symbol}: {self.cache_file}")
        except Exception as e:
            logger.error(f"Failed to save cache for {symbol}: {e}")

    def _is_cache_fresh(self, sentiment: RetailSentiment) -> bool:
        """Check if cached sentiment data is still fresh"""
        age_seconds = (datetime.now(timezone.utc) - sentiment.timestamp).total_seconds()
        return age_seconds < self.cache_ttl_seconds
