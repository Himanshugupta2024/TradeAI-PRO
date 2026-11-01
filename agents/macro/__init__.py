"""
Macro Intelligence Layer (Edge Engine)

Provides fundamental/macro economic scoring that sits ON TOP of technical signal
generation. Aggregates free data sources to produce an EdgeScore (-100 to +100)
that modifies technical signal confidence.

Components:
- MacroScorer: Macro environment scoring (GDP, CPI, rates)
- COTScorer: Institutional positioning (CFTC COT data)
- SentimentScorer: Retail sentiment (contrarian indicator)
- RiskGauge: Risk-on/risk-off environment
- SeasonalScorer: Historical seasonality patterns
- CarryScanner: Carry trade analysis (forex only)
- EdgeScoreEngine: Main orchestrator combining all components
"""

from agents.macro.macro_fetcher import MacroFetcher, MacroIndicators
from agents.macro.macro_scorer import MacroScorer
from agents.macro.cot_fetcher import COTFetcher, COTData
from agents.macro.cot_scorer import COTScorer
from agents.macro.retail_sentiment import SentimentScorer, RetailSentiment
from agents.macro.risk_gauge import RiskGauge, RiskIndicators
from agents.macro.seasonality import SeasonalScorer, SeasonalPattern
from agents.macro.carry_scanner import CarryScanner, CarryData
from agents.macro.edge_score_engine import EdgeScoreEngine, EdgeScoreBreakdown

__all__ = [
    # Fetchers
    "MacroFetcher",
    "COTFetcher",
    # Scorers
    "MacroScorer",
    "COTScorer",
    "SentimentScorer",
    "RiskGauge",
    "SeasonalScorer",
    "CarryScanner",
    # Main Engine
    "EdgeScoreEngine",
    # Data Classes
    "MacroIndicators",
    "COTData",
    "RetailSentiment",
    "RiskIndicators",
    "SeasonalPattern",
    "CarryData",
    "EdgeScoreBreakdown",
]
