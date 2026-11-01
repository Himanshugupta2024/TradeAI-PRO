"""
Macro environment scoring logic.

Scores the macro environment from -100 (risk-off) to +100 (risk-on) based on
FRED indicators: GDP growth, CPI inflation, unemployment, Fed Funds rate, and
10-year Treasury yield.

Scoring Logic:
- Strong growth + low inflation + low rates = +100 (risk-on)
- Recession + high inflation + high rates = -100 (risk-off)
- Neutral conditions = 0
"""

import logging
from typing import Optional

from agents.macro.macro_fetcher import MacroFetcher, MacroIndicators
from config.settings import (
    MACRO_MIN_INDICATORS,
    MACRO_STRONG_GROWTH_GDP,
    MACRO_LOW_INFLATION_CPI,
    MACRO_HIGH_INFLATION_CPI,
    MACRO_LOW_RATES_FED,
    MACRO_HIGH_RATES_FED,
)

logger = logging.getLogger(__name__)


class MacroScorer:
    """Scores macro environment from FRED data"""

    def __init__(
        self,
        fetcher: Optional[MacroFetcher] = None,
    ) -> None:
        """
        Initialize macro scorer.

        Args:
            fetcher: MacroFetcher instance (creates default if None)
        """
        self.fetcher = fetcher or MacroFetcher()

    def get_macro_score(self, indicators: Optional[MacroIndicators] = None) -> float:
        """
        Calculate macro environment score.

        Scoring logic:
        - Strong growth + low inflation + low rates = +100 (risk-on)
        - Recession + high inflation + high rates = -100 (risk-off)
        - Neutral conditions = 0

        Requires at least 3 of 5 indicators available.

        Returns:
            Macro score from -100 (bearish) to +100 (bullish)
        """
        indicators = indicators or self.fetcher.fetch_indicators()

        # Count available indicators
        available = sum([
            indicators.gdp_growth_yoy is not None,
            indicators.cpi_yoy is not None,
            indicators.unemployment_rate is not None,
            indicators.fed_funds_rate is not None,
            indicators.treasury_10y is not None,
        ])

        if available < MACRO_MIN_INDICATORS:
            logger.warning(
                f"Insufficient macro indicators: {available}/{MACRO_MIN_INDICATORS} required"
            )
            return 0.0

        # Score each component
        gdp_score = self._score_gdp(indicators.gdp_growth_yoy)
        cpi_score = self._score_cpi(indicators.cpi_yoy)
        unemployment_score = self._score_unemployment(indicators.unemployment_rate)
        fed_funds_score = self._score_fed_funds(indicators.fed_funds_rate)
        treasury_score = self._score_treasury(indicators.treasury_10y)

        # Average non-None scores
        scores = [
            s for s in [gdp_score, cpi_score, unemployment_score, fed_funds_score, treasury_score]
            if s is not None
        ]

        if not scores:
            return 0.0

        macro_score = sum(scores) / len(scores)

        logger.info(
            f"Macro score: {macro_score:.1f} "
            f"(GDP: {gdp_score}, CPI: {cpi_score}, Unemp: {unemployment_score}, "
            f"Fed: {fed_funds_score}, 10Y: {treasury_score})"
        )

        return round(macro_score, 1)

    def _score_gdp(self, gdp_growth_yoy: Optional[float]) -> Optional[float]:
        """
        Score GDP growth.

        > 3% YoY = strong growth = +100
        < 0% YoY = recession = -100
        Linear interpolation between
        """
        if gdp_growth_yoy is None:
            return None

        if gdp_growth_yoy >= MACRO_STRONG_GROWTH_GDP:
            return 100.0
        elif gdp_growth_yoy <= 0:
            return -100.0
        else:
            # Linear interpolation: 0% → -100, 3% → +100
            return (gdp_growth_yoy / MACRO_STRONG_GROWTH_GDP) * 100

    def _score_cpi(self, cpi_yoy: Optional[float]) -> Optional[float]:
        """
        Score CPI inflation.

        < 3% YoY = low inflation = +100
        > 5% YoY = high inflation = -100
        Linear interpolation between
        """
        if cpi_yoy is None:
            return None

        if cpi_yoy <= MACRO_LOW_INFLATION_CPI:
            return 100.0
        elif cpi_yoy >= MACRO_HIGH_INFLATION_CPI:
            return -100.0
        else:
            # Linear interpolation: 3% → +100, 5% → -100
            return 100 - ((cpi_yoy - MACRO_LOW_INFLATION_CPI) /
                          (MACRO_HIGH_INFLATION_CPI - MACRO_LOW_INFLATION_CPI)) * 200

    def _score_unemployment(self, unemployment_rate: Optional[float]) -> Optional[float]:
        """
        Score unemployment rate.

        < 4% = low unemployment = +100
        > 8% = high unemployment = -100
        Linear interpolation between
        """
        if unemployment_rate is None:
            return None

        if unemployment_rate <= 4.0:
            return 100.0
        elif unemployment_rate >= 8.0:
            return -100.0
        else:
            # Linear interpolation: 4% → +100, 8% → -100
            return 100 - ((unemployment_rate - 4.0) / 4.0) * 200

    def _score_fed_funds(self, fed_funds_rate: Optional[float]) -> Optional[float]:
        """
        Score Fed Funds rate.

        < 2% = low rates = +100 (accommodative)
        > 5% = high rates = -100 (restrictive)
        Linear interpolation between
        """
        if fed_funds_rate is None:
            return None

        if fed_funds_rate <= MACRO_LOW_RATES_FED:
            return 100.0
        elif fed_funds_rate >= MACRO_HIGH_RATES_FED:
            return -100.0
        else:
            # Linear interpolation: 2% → +100, 5% → -100
            return 100 - ((fed_funds_rate - MACRO_LOW_RATES_FED) /
                          (MACRO_HIGH_RATES_FED - MACRO_LOW_RATES_FED)) * 200

    def _score_treasury(self, treasury_10y: Optional[float]) -> Optional[float]:
        """
        Score 10-year Treasury yield.

        < 2% = low yields = +100 (risk-on)
        > 5% = high yields = -100 (risk-off)
        Linear interpolation between
        """
        if treasury_10y is None:
            return None

        if treasury_10y <= 2.0:
            return 100.0
        elif treasury_10y >= 5.0:
            return -100.0
        else:
            # Linear interpolation: 2% → +100, 5% → -100
            return 100 - ((treasury_10y - 2.0) / 3.0) * 200

    def fetch_indicators(self) -> MacroIndicators:
        """
        Fetch latest macro indicators.

        Convenience method that delegates to fetcher.

        Returns:
            MacroIndicators dataclass
        """
        return self.fetcher.fetch_indicators()
