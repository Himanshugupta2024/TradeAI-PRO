"""
CFTC Commitment of Traders (COT) scoring module.

Analyzes institutional positioning from COT data to generate contrarian
positioning scores. Large speculators heavily long = bullish, heavily short = bearish.
"""

import logging
from typing import Optional

from agents.macro.cot_fetcher import COTFetcher, COTData

logger = logging.getLogger(__name__)


class COTScorer:
    """Scores institutional positioning from CFTC COT data"""

    def __init__(
        self,
        cache_ttl_seconds: int = 604800,  # 7 days (COT weekly)
    ) -> None:
        """
        Initialize COT scorer.

        Args:
            cache_ttl_seconds: Cache TTL for COT data (default 7 days)
        """
        self.fetcher = COTFetcher(cache_ttl_seconds=cache_ttl_seconds)

    def get_cot_score(self, symbol) -> float:
        """
        Calculate COT positioning score for a symbol.

        Scoring logic:
        - Large speculators heavily long = +100 (bullish)
        - Large speculators heavily short = -100 (bearish)
        - Neutral positioning = 0
        - Contrarian: extreme positioning can signal reversal

        Preconditions:
        - symbol has COT data available (forex, commodities)
        - COT data is less than 14 days old

        Postconditions:
        - Returns score in range [-100, +100]
        - Score is cached for 7 days
        - Returns 0 if symbol not in COT database

        Args:
            symbol: Trading symbol (e.g., "EUR/USD", "GOLD") or a COTData object for testing.

        Returns:
            COT score from -100 (bearish) to +100 (bullish)
        """
        if isinstance(symbol, COTData):
            cot_data = symbol
            symbol_str = cot_data.symbol
        else:
            symbol_str = symbol
            # Fetch COT data
            cot_data = self.fetcher.fetch_cot_data(symbol_str)

        # Return 0 if no COT data available (crypto, NSE stocks, etc.)
        if not cot_data:
            logger.debug(f"No COT data for {symbol_str}, returning neutral score 0")
            return 0.0

        # Calculate score from net_position_pct
        # net_position_pct ranges roughly from -50% to +50% in extreme cases
        # We scale this to -100 to +100 range
        score = self._calculate_positioning_score(cot_data)

        logger.info(
            f"COT score for {symbol_str}: {score:.2f} "
            f"(net_position_pct: {cot_data.net_position_pct:.2f}%)"
        )

        return score

    def _calculate_positioning_score(self, cot_data: COTData) -> float:
        """
        Calculate positioning score from COT data using contrarian logic.

        Logic:
        - net_position_pct > +30% = -100 (extremely bullish positioning -> bearish reversal expected)
        - net_position_pct > +15% = -50 to -100 (bearish)
        - net_position_pct between -15% and +15% = -50 to +50 (neutral)
        - net_position_pct < -15% = +50 to +100 (bullish)
        - net_position_pct < -30% = +100 (extremely bearish positioning -> bullish reversal expected)

        Args:
            cot_data: COT data with net_position_pct

        Returns:
            Score from -100 to +100
        """
        net_pct = cot_data.net_position_pct

        # Extreme bullish positioning (>30%) -> Bearish score
        if net_pct >= 30.0:
            return -100.0

        # Extreme bearish positioning (<-30%) -> Bullish score
        if net_pct <= -30.0:
            return 100.0

        # Scale linearly between -30% and +30%
        # net_pct of +30% → -100
        # net_pct of 0% → 0
        # net_pct of -30% → +100
        score = -(net_pct / 30.0) * 100.0

        # Clamp to [-100, +100] range
        return max(-100.0, min(100.0, score))
