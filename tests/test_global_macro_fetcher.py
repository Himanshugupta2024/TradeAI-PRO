"""
Unit tests for Global Macro & Commodity Intelligence Fetcher.
Verifies:
- Live/Fallback quote extraction
- Brent and WTI crude barrel calculations
- Sector correlation risk gates (Crude spike on Paints/OMCs, US Yield on IT)
- Polarity scoring on breaking news
"""

import pytest
from unittest.mock import patch, MagicMock

from agents.market_intelligence.global_macro_fetcher import (
    GlobalMacroFetcher,
    CommodityQuote,
    GlobalMacroIntelligence,
    SECTOR_CORRELATIONS,
    get_global_macro_fetcher,
)


def test_singleton_instance():
    f1 = get_global_macro_fetcher()
    f2 = get_global_macro_fetcher()
    assert f1 is f2


def test_sector_correlations_integrity():
    assert "ASIANPAINT" in SECTOR_CORRELATIONS["CRUDE_BEARISH_IMPACT"]
    assert "INDIGO" in SECTOR_CORRELATIONS["CRUDE_BEARISH_IMPACT"]
    assert "BPCL" in SECTOR_CORRELATIONS["CRUDE_BEARISH_IMPACT"]
    assert "ONGC" in SECTOR_CORRELATIONS["CRUDE_BULLISH_IMPACT"]
    assert "TCS" in SECTOR_CORRELATIONS["US_YIELD_SENSITIVE"]


def test_headline_polarity_scoring():
    fetcher = GlobalMacroFetcher()
    score_bull, sent_bull = fetcher._compute_headline_polarity("Crude oil plunges as supply surges and demand drops")
    assert sent_bull in ("BEARISH", "BULLISH")

    score_rec, sent_rec = fetcher._compute_headline_polarity("Economy records strong rebound and profit surge")
    assert sent_rec == "BULLISH"
    assert score_rec > 0


def test_fallback_intelligence_structure():
    fetcher = GlobalMacroFetcher()
    intel = fetcher._build_fallback_intel()
    assert isinstance(intel, GlobalMacroIntelligence)
    assert intel.brent_crude.symbol == "BZ=F"
    assert intel.wti_crude.symbol == "CL=F"
    assert intel.brent_crude.price > 0
    assert intel.wti_crude.price > 0
    assert intel.macro_regime in ("NEUTRAL", "RISK_ON", "RISK_OFF", "HIGH_CRUDE_RISK")


def test_macro_sector_gate_blocks_asian_paints_on_high_crude(monkeypatch):
    fetcher = GlobalMacroFetcher()
    # Mock high crude ($105/bbl, +4%)
    mock_intel = fetcher._build_fallback_intel()
    mock_intel.brent_crude.price = 105.0
    mock_intel.brent_crude.change_pct = 4.2

    monkeypatch.setattr(fetcher, "get_intelligence", lambda force_refresh=False: mock_intel)

    allowed, reason = fetcher.evaluate_macro_sector_gate("ASIANPAINT", "LONG")
    assert allowed is False
    assert "Macro Risk Gate" in reason
    assert "Crude oil elevated" in reason

    # Should allow SHORT on Asian Paints during high crude
    allowed_short, _ = fetcher.evaluate_macro_sector_gate("ASIANPAINT", "SHORT")
    assert allowed_short is True

    # Should allow LONG on ONGC (beneficiary of high crude)
    allowed_ongc, _ = fetcher.evaluate_macro_sector_gate("ONGC", "LONG")
    assert allowed_ongc is True


def test_macro_sector_gate_blocks_it_on_yield_surge(monkeypatch):
    fetcher = GlobalMacroFetcher()
    mock_intel = fetcher._build_fallback_intel()
    mock_intel.brent_crude.price = 70.0
    mock_intel.brent_crude.change_pct = -1.0
    mock_intel.us_10y_yield.price = 4.85
    mock_intel.us_10y_yield.change_pct = 3.5

    monkeypatch.setattr(fetcher, "get_intelligence", lambda force_refresh=False: mock_intel)

    allowed, reason = fetcher.evaluate_macro_sector_gate("TCS", "LONG")
    assert allowed is False
    assert "US 10-Year Yield surged" in reason
