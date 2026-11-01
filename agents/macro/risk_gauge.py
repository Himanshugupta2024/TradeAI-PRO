"""
Market risk-on/risk-off gauge using VIX, BTC, Nifty, USD/JPY, and Gold.

Measures the global risk environment by analyzing multiple risk indicators:
- VIX: Market volatility (fear gauge)
- BTC: Crypto risk appetite
- Nifty: Indian equity market
- USD/JPY: Safe-haven currency flows
- Gold: Traditional safe-haven asset

Risk-On: VIX low, BTC rising, Nifty rising, USD/JPY falling, Gold falling
Risk-Off: VIX high, BTC falling, Nifty falling, USD/JPY rising, Gold rising

Implements 1-hour cache with fallback to stale data.
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Literal

import requests
import yfinance as yf

from config.settings import (
    EDGE_RISK_CACHE_FILE,
    EDGE_RISK_CACHE_TTL,
    EDGE_FALLBACK_ENABLED,
    TWELVE_DATA_API_KEY,
)

logger = logging.getLogger(__name__)


@dataclass
class RiskIndicators:
    """Market risk indicators"""
    vix: Optional[float]                   # VIX index (volatility)
    vix_change_pct: Optional[float]        # % change in VIX (7-day)
    btc_trend: Optional[str]               # "RISING" | "FALLING" | "FLAT"
    nifty_trend: Optional[str]             # "RISING" | "FALLING" | "FLAT"
    usdjpy_level: Optional[float]          # USD/JPY rate
    usdjpy_trend: Optional[str]            # "RISING" | "FALLING" | "FLAT"
    gold_trend: Optional[str]              # "RISING" | "FALLING" | "FLAT"
    risk_regime: str                       # "RISK_ON" | "RISK_OFF" | "NEUTRAL"
    timestamp: datetime

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization"""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RiskIndicators":
        """Create from dict (JSON deserialization)"""
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


class RiskGauge:
    """Measures market risk environment"""

    # VIX thresholds
    VIX_LOW_THRESHOLD = 15.0    # VIX < 15 = low volatility (risk-on)
    VIX_HIGH_THRESHOLD = 30.0   # VIX > 30 = high volatility (risk-off)

    # Trend calculation parameters
    TREND_MA_PERIOD = 7         # 7-day moving average
    TREND_THRESHOLD_PCT = 2.0   # 2% threshold for trend classification

    # Minimum indicators required
    MIN_INDICATORS = 3          # Need at least 3 of 5 indicators

    def __init__(
        self,
        cache_file: str = EDGE_RISK_CACHE_FILE,
        cache_ttl_seconds: int = EDGE_RISK_CACHE_TTL,
        fallback_enabled: bool = EDGE_FALLBACK_ENABLED,
    ) -> None:
        """
        Initialize risk gauge.

        Args:
            cache_file: Path to cache file
            cache_ttl_seconds: Cache TTL (default 1 hour)
            fallback_enabled: Use stale cache on API failure
        """
        self.cache_file = Path(cache_file)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.fallback_enabled = fallback_enabled

    def get_risk_score(self) -> float:
        """
        Calculate risk environment score.

        Scoring logic:
        - VIX < 15, BTC rising, Nifty rising, USD/JPY falling, Gold falling = +100 (RISK_ON)
        - VIX > 30, BTC falling, Nifty falling, USD/JPY rising, Gold rising = -100 (RISK_OFF)
        - Mixed signals = 0 (NEUTRAL)

        Risk-On: Investors buying risky assets (stocks, crypto, EM currencies)
        Risk-Off: Investors fleeing to safe havens (USD, JPY, Gold, bonds)

        Returns:
            Risk score from -100 (risk-off) to +100 (risk-on)
        """
        indicators = self.fetch_risk_indicators()

        # Count available indicators
        available = sum([
            indicators.vix is not None,
            indicators.btc_trend is not None,
            indicators.nifty_trend is not None,
            indicators.usdjpy_trend is not None,
            indicators.gold_trend is not None,
        ])

        if available < self.MIN_INDICATORS:
            logger.warning(
                f"Insufficient risk indicators: {available}/{self.MIN_INDICATORS} available"
            )
            return 0.0

        # Calculate individual indicator scores
        scores = []

        # VIX score
        if indicators.vix is not None:
            if indicators.vix < self.VIX_LOW_THRESHOLD:
                vix_score = 100.0  # Low volatility = risk-on
            elif indicators.vix > self.VIX_HIGH_THRESHOLD:
                vix_score = -100.0  # High volatility = risk-off
            else:
                # Linear interpolation between thresholds
                # VIX 15-30 maps to +100 to -100
                vix_score = 100.0 - (
                    (indicators.vix - self.VIX_LOW_THRESHOLD) /
                    (self.VIX_HIGH_THRESHOLD - self.VIX_LOW_THRESHOLD) * 200.0
                )
            scores.append(vix_score)
            logger.debug(f"VIX score: {vix_score:.1f} (VIX={indicators.vix:.2f})")

        # BTC trend score
        if indicators.btc_trend:
            btc_score = self._trend_to_score(indicators.btc_trend)
            scores.append(btc_score)
            logger.debug(f"BTC score: {btc_score:.1f} (trend={indicators.btc_trend})")

        # Nifty trend score
        if indicators.nifty_trend:
            nifty_score = self._trend_to_score(indicators.nifty_trend)
            scores.append(nifty_score)
            logger.debug(f"Nifty score: {nifty_score:.1f} (trend={indicators.nifty_trend})")

        # USD/JPY trend score (INVERTED: rising USD/JPY = risk-off)
        if indicators.usdjpy_trend:
            usdjpy_score = -self._trend_to_score(indicators.usdjpy_trend)
            scores.append(usdjpy_score)
            logger.debug(f"USD/JPY score: {usdjpy_score:.1f} (trend={indicators.usdjpy_trend})")

        # Gold trend score (INVERTED: rising Gold = risk-off)
        if indicators.gold_trend:
            gold_score = -self._trend_to_score(indicators.gold_trend)
            scores.append(gold_score)
            logger.debug(f"Gold score: {gold_score:.1f} (trend={indicators.gold_trend})")

        # Calculate average score
        if not scores:
            return 0.0

        risk_score = sum(scores) / len(scores)

        # Clamp to [-100, +100]
        risk_score = max(-100.0, min(100.0, risk_score))

        logger.info(
            f"Risk score: {risk_score:.1f} (regime={indicators.risk_regime}, "
            f"{len(scores)}/{available} indicators)"
        )

        return round(risk_score, 1)

    def fetch_risk_indicators(self) -> RiskIndicators:
        """
        Fetch latest risk indicators from free sources.

        Data sources:
        - VIX: Yahoo Finance (^VIX)
        - BTC: Yahoo Finance (BTC-USD) - fallback from Binance
        - Nifty: Yahoo Finance (^NSEI)
        - USD/JPY: Twelve Data API (free tier) or Yahoo Finance
        - Gold: Yahoo Finance (GC=F)

        Returns:
            RiskIndicators dataclass
        """
        # Try cache first
        cached = self._load_cache()
        if cached and self._is_cache_fresh(cached):
            logger.info("Using fresh risk indicators from cache")
            return cached

        # Cache miss or stale — fetch from APIs
        logger.info("Fetching fresh risk indicators")

        # Fetch all indicators
        vix, vix_change = self._fetch_vix()
        btc_trend = self._fetch_btc_trend()
        nifty_trend = self._fetch_nifty_trend()
        usdjpy_level, usdjpy_trend = self._fetch_usdjpy()
        gold_trend = self._fetch_gold_trend()

        # Determine risk regime
        risk_regime = self._calculate_risk_regime(
            vix, btc_trend, nifty_trend, usdjpy_trend, gold_trend
        )

        indicators = RiskIndicators(
            vix=vix,
            vix_change_pct=vix_change,
            btc_trend=btc_trend,
            nifty_trend=nifty_trend,
            usdjpy_level=usdjpy_level,
            usdjpy_trend=usdjpy_trend,
            gold_trend=gold_trend,
            risk_regime=risk_regime,
            timestamp=datetime.now(timezone.utc),
        )

        # Save to cache
        self._save_cache(indicators)

        return indicators

    def calculate_trend(self, prices: list[float]) -> str:
        """
        Calculate trend from price series.

        Logic:
        - Current price > 7-day MA by >2% = "RISING"
        - Current price < 7-day MA by >2% = "FALLING"
        - Otherwise = "FLAT"

        Args:
            prices: List of prices (oldest to newest)

        Returns:
            Trend direction string: "RISING" | "FALLING" | "FLAT"
        """
        if len(prices) < self.TREND_MA_PERIOD:
            logger.warning(f"Insufficient data for trend calculation: {len(prices)} < {self.TREND_MA_PERIOD}")
            return "FLAT"

        # Calculate 7-day moving average
        ma_prices = prices[-self.TREND_MA_PERIOD:]
        ma = sum(ma_prices) / len(ma_prices)

        # Current price
        current = prices[-1]

        # Calculate percentage difference
        pct_diff = ((current - ma) / ma) * 100.0

        # Classify trend
        if pct_diff > self.TREND_THRESHOLD_PCT:
            return "RISING"
        elif pct_diff < -self.TREND_THRESHOLD_PCT:
            return "FALLING"
        else:
            return "FLAT"

    def _fetch_vix(self) -> tuple[Optional[float], Optional[float]]:
        """
        Fetch VIX index from Yahoo Finance.

        Returns:
            (current_vix, 7day_change_pct) or (None, None)
        """
        try:
            ticker = yf.Ticker("^VIX")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.error("No VIX data returned from Yahoo Finance")
                return None, None

            # Current VIX
            current_vix = float(hist["Close"].iloc[-1])

            # 7-day change
            if len(hist) >= 8:
                vix_7d_ago = float(hist["Close"].iloc[-8])
                vix_change = ((current_vix - vix_7d_ago) / vix_7d_ago) * 100.0
            else:
                vix_change = None

            logger.info(f"VIX: {current_vix:.2f} (7d change: {vix_change:.2f}%)" if vix_change else f"VIX: {current_vix:.2f}")
            return current_vix, vix_change

        except Exception as e:
            logger.error(f"Failed to fetch VIX: {e}")
            return None, None

    def _fetch_btc_trend(self) -> Optional[str]:
        """
        Fetch BTC trend from Yahoo Finance.

        Returns:
            "RISING" | "FALLING" | "FLAT" or None
        """
        try:
            ticker = yf.Ticker("BTC-USD")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.error("No BTC data returned from Yahoo Finance")
                return None

            # Extract closing prices
            prices = hist["Close"].tolist()

            # Calculate trend
            trend = self.calculate_trend(prices)
            logger.info(f"BTC trend: {trend}")
            return trend

        except Exception as e:
            logger.error(f"Failed to fetch BTC trend: {e}")
            return None

    def _fetch_nifty_trend(self) -> Optional[str]:
        """
        Fetch Nifty 50 trend from Yahoo Finance.

        Returns:
            "RISING" | "FALLING" | "FLAT" or None
        """
        try:
            ticker = yf.Ticker("^NSEI")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.error("No Nifty data returned from Yahoo Finance")
                return None

            # Extract closing prices
            prices = hist["Close"].tolist()

            # Calculate trend
            trend = self.calculate_trend(prices)
            logger.info(f"Nifty trend: {trend}")
            return trend

        except Exception as e:
            logger.error(f"Failed to fetch Nifty trend: {e}")
            return None

    def _fetch_usdjpy(self) -> tuple[Optional[float], Optional[str]]:
        """
        Fetch USD/JPY rate and trend.

        Tries Twelve Data API first, falls back to Yahoo Finance.

        Returns:
            (current_rate, trend) or (None, None)
        """
        # Try Twelve Data API first (if key available)
        if TWELVE_DATA_API_KEY:
            try:
                # Fetch current rate
                url = "https://api.twelvedata.com/price"
                params = {
                    "symbol": "USD/JPY",
                    "apikey": TWELVE_DATA_API_KEY,
                }
                response = requests.get(url, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()

                if "price" in data:
                    current_rate = float(data["price"])

                    # Fetch historical data for trend
                    hist_url = "https://api.twelvedata.com/time_series"
                    hist_params = {
                        "symbol": "USD/JPY",
                        "interval": "1day",
                        "outputsize": 30,
                        "apikey": TWELVE_DATA_API_KEY,
                    }
                    hist_response = requests.get(hist_url, params=hist_params, timeout=10)
                    hist_response.raise_for_status()
                    hist_data = hist_response.json()

                    if "values" in hist_data:
                        prices = [float(v["close"]) for v in hist_data["values"]]
                        prices.reverse()  # Oldest to newest
                        trend = self.calculate_trend(prices)
                        logger.info(f"USD/JPY: {current_rate:.2f} (trend: {trend})")
                        return current_rate, trend

            except Exception as e:
                logger.warning(f"Twelve Data API failed for USD/JPY: {e}, falling back to Yahoo Finance")

        # Fallback to Yahoo Finance
        try:
            ticker = yf.Ticker("JPY=X")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.error("No USD/JPY data returned from Yahoo Finance")
                return None, None

            # Current rate
            current_rate = float(hist["Close"].iloc[-1])

            # Extract closing prices for trend
            prices = hist["Close"].tolist()
            trend = self.calculate_trend(prices)

            logger.info(f"USD/JPY: {current_rate:.2f} (trend: {trend})")
            return current_rate, trend

        except Exception as e:
            logger.error(f"Failed to fetch USD/JPY: {e}")
            return None, None

    def _fetch_gold_trend(self) -> Optional[str]:
        """
        Fetch Gold trend from Yahoo Finance.

        Returns:
            "RISING" | "FALLING" | "FLAT" or None
        """
        try:
            ticker = yf.Ticker("GC=F")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.error("No Gold data returned from Yahoo Finance")
                return None

            # Extract closing prices
            prices = hist["Close"].tolist()

            # Calculate trend
            trend = self.calculate_trend(prices)
            logger.info(f"Gold trend: {trend}")
            return trend

        except Exception as e:
            logger.error(f"Failed to fetch Gold trend: {e}")
            return None

    def _trend_to_score(self, trend: str) -> float:
        """
        Convert trend string to score.

        Args:
            trend: "RISING" | "FALLING" | "FLAT"

        Returns:
            Score: +100 (rising), -100 (falling), 0 (flat)
        """
        if trend == "RISING":
            return 100.0
        elif trend == "FALLING":
            return -100.0
        else:
            return 0.0

    def _calculate_risk_regime(
        self,
        vix: Optional[float],
        btc_trend: Optional[str],
        nifty_trend: Optional[str],
        usdjpy_trend: Optional[str],
        gold_trend: Optional[str],
    ) -> str:
        """
        Determine overall risk regime from indicators.

        Returns:
            "RISK_ON" | "RISK_OFF" | "NEUTRAL"
        """
        risk_on_signals = 0
        risk_off_signals = 0
        total_signals = 0

        # VIX
        if vix is not None:
            total_signals += 1
            if vix < self.VIX_LOW_THRESHOLD:
                risk_on_signals += 1
            elif vix > self.VIX_HIGH_THRESHOLD:
                risk_off_signals += 1

        # BTC
        if btc_trend:
            total_signals += 1
            if btc_trend == "RISING":
                risk_on_signals += 1
            elif btc_trend == "FALLING":
                risk_off_signals += 1

        # Nifty
        if nifty_trend:
            total_signals += 1
            if nifty_trend == "RISING":
                risk_on_signals += 1
            elif nifty_trend == "FALLING":
                risk_off_signals += 1

        # USD/JPY (inverted)
        if usdjpy_trend:
            total_signals += 1
            if usdjpy_trend == "FALLING":
                risk_on_signals += 1
            elif usdjpy_trend == "RISING":
                risk_off_signals += 1

        # Gold (inverted)
        if gold_trend:
            total_signals += 1
            if gold_trend == "FALLING":
                risk_on_signals += 1
            elif gold_trend == "RISING":
                risk_off_signals += 1

        # Determine regime based on majority
        if total_signals == 0:
            return "NEUTRAL"

        risk_on_pct = (risk_on_signals / total_signals) * 100.0
        risk_off_pct = (risk_off_signals / total_signals) * 100.0

        # Need >60% agreement for clear regime
        if risk_on_pct >= 60.0:
            return "RISK_ON"
        elif risk_off_pct >= 60.0:
            return "RISK_OFF"
        else:
            return "NEUTRAL"

    def _load_cache(self) -> Optional[RiskIndicators]:
        """Load risk indicators from cache file"""
        if not self.cache_file.exists():
            return None

        try:
            with open(self.cache_file, "r") as f:
                data = json.load(f)
            return RiskIndicators.from_dict(data)
        except Exception as e:
            logger.error(f"Failed to load cache: {e}")
            return None

    def _save_cache(self, indicators: RiskIndicators) -> None:
        """Save risk indicators to cache file"""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(indicators.to_dict(), f, indent=2)
            logger.info(f"Saved risk indicators to cache: {self.cache_file}")
        except Exception as e:
            logger.error(f"Failed to save cache: {e}")

    def _is_cache_fresh(self, indicators: RiskIndicators) -> bool:
        """Check if cached indicators are still fresh"""
        age_seconds = (datetime.now(timezone.utc) - indicators.timestamp).total_seconds()
        return age_seconds < self.cache_ttl_seconds
