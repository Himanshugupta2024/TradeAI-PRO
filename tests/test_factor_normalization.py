"""
Unit tests for Edge Engine 3.0 Dynamic Factor Normalization and Asset-Class Models.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from datetime import datetime, timezone

import pytest

from agents.macro.edge_score_engine import (
    EdgeScoreEngine,
    EdgeScoreBreakdown,
    FactorReading,
)


class TestDynamicFactorNormalization:
    """Tests for dynamic normalization and non-compressed scoring."""

    def test_dynamic_normalization_excludes_not_applicable(self):
        """Not applicable factors must not be in the denominator or compress the score."""
        engine = EdgeScoreEngine()
        factors = [
            FactorReading("momentum", "Technical Momentum", 40.0, "LIVE", 0.25, 0.25, "Detail", "Source"),
            FactorReading("sector_rs", "Sector RS", 20.0, "LIVE", 0.25, 0.25, "Detail", "Source"),
            FactorReading("cot", "COT Positioning", None, "NOT_APPLICABLE", 0.0, 0.0, "N/A", "Source"),
            FactorReading("carry", "Carry Trade", None, "NOT_APPLICABLE", 0.0, 0.0, "N/A", "Source"),
        ]
        edge_score, coverage_pct, conf = engine._dynamic_normalize(factors, seasonal_bonus=0.0, carry_bonus=0.0)

        # Total applicable weight is 0.25 + 0.25 = 0.50
        # Effective weights should be 0.5 and 0.5
        # Expected score: 40 * 0.5 + 20 * 0.5 = 30.0 (NOT compressed by missing 50% weights)
        assert edge_score == 30.0
        assert coverage_pct == 100.0
        assert conf > 0.9

    def test_equity_model_factor_generation(self):
        """Equity model must generate sector RS, sentiment, momentum, and mark COT as NOT_APPLICABLE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_edge_scores.json"
            engine = EdgeScoreEngine(cache_ttl_seconds=3600)
            engine.cache_file = cache_file

            with patch.object(engine, "_get_momentum_score", return_value=30.0), \
                 patch.object(engine, "_get_relative_strength_score", return_value=(20.0, 1.5)), \
                 patch.object(engine.gdelt_provider, "get_sentiment_sync", return_value={
                     "sentiment": "BULLISH", "tone_score": 5.0, "article_count": 3,
                     "top_headlines": ["Strong quarterly growth"], "source": "TEST_RSS"
                 }), \
                 patch.object(engine, "_get_market_breadth_score", return_value=(10.0, "Nifty 50 Bullish")), \
                 patch.object(engine.risk_gauge, "get_risk_score", return_value=15.0), \
                 patch.object(engine.seasonal_scorer, "get_seasonal_score", return_value=0.0):

                breakdown = engine.get_edge_score("RELIANCE", "nse")

                assert breakdown.asset_class == "nse"
                assert breakdown.momentum_score == 30.0
                assert breakdown.sentiment_score == 50.0  # 5.0 * 10
                assert breakdown.factors["cot"]["status"] == "NOT_APPLICABLE"
                assert breakdown.factors["carry"]["status"] == "NOT_APPLICABLE"
                assert breakdown.factors["sector_rs"]["status"] == "LIVE"
                assert breakdown.factors["sector_rs"]["value"] == 20.0
                assert breakdown.edge_score > 0.0
                assert breakdown.factor_coverage_pct == 100.0

    def test_crypto_model_factor_generation(self):
        """Crypto model must generate volume expansion and momentum without COT/Carry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_edge_scores.json"
            engine = EdgeScoreEngine(cache_ttl_seconds=3600)
            engine.cache_file = cache_file

            with patch.object(engine, "_get_momentum_score", return_value=40.0), \
                 patch.object(engine, "_get_crypto_volume_score", return_value=(25.0, "Volume ratio: 1.5x")), \
                 patch.object(engine.risk_gauge, "get_risk_score", return_value=10.0), \
                 patch.object(engine.gdelt_provider, "get_sentiment_sync", return_value={
                     "sentiment": "NEUTRAL", "tone_score": 0.0, "article_count": 0, "top_headlines": []
                 }), \
                 patch.object(engine.seasonal_scorer, "get_seasonal_score", return_value=0.0):

                breakdown = engine.get_edge_score("SOL/USDT", "crypto")

                assert breakdown.asset_class == "crypto"
                assert breakdown.momentum_score == 40.0
                assert breakdown.factors["volume_expansion"]["value"] == 25.0
                assert breakdown.factors["cot"]["status"] == "NOT_APPLICABLE"
                assert breakdown.factors["carry"]["status"] == "NOT_APPLICABLE"
                assert breakdown.edge_score > 0.0
