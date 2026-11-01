"""
Basic smoke tests for EdgeScoreEngine.

Tests the main orchestrator without requiring real API keys.
"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, Mock

import pytest

from agents.macro.edge_score_engine import EdgeScoreEngine, EdgeScoreBreakdown


class TestEdgeScoreEngine:
    """Test EdgeScoreEngine orchestrator"""

    def test_edge_score_breakdown_serialization(self):
        """Test EdgeScoreBreakdown to_dict and from_dict"""
        breakdown = EdgeScoreBreakdown(
            macro_score=50.0,
            cot_score=30.0,
            sentiment_score=-20.0,
            risk_score=40.0,
            momentum_score=0.0,
            seasonal_bonus=10.0,
            carry_bonus=5.0,
            edge_score=55.0,
            timestamp=datetime.now(timezone.utc),
            cache_hit=False,
        )

        # Serialize and deserialize
        data = breakdown.to_dict()
        restored = EdgeScoreBreakdown.from_dict(data)

        assert restored.macro_score == breakdown.macro_score
        assert restored.cot_score == breakdown.cot_score
        assert restored.edge_score == breakdown.edge_score
        assert restored.cache_hit == breakdown.cache_hit

    def test_edge_score_calculation_mock(self):
        """Test EdgeScore calculation with mocked components"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_edge_scores.json"

            engine = EdgeScoreEngine(cache_ttl_seconds=3600)
            engine.cache_file = cache_file

            # Mock all component scorers
            with patch.object(engine.macro_scorer, "get_macro_score", return_value=60.0), \
                 patch.object(engine.cot_scorer, "get_cot_score", return_value=40.0), \
                 patch.object(engine.sentiment_scorer, "get_sentiment_score", return_value=-30.0), \
                 patch.object(engine.risk_gauge, "get_risk_score", return_value=50.0), \
                  patch.object(engine, "_get_momentum_score", return_value=0.0), \
                 patch.object(engine.risk_gauge, "fetch_risk_indicators") as mock_risk_indicators, \
                 patch.object(engine.seasonal_scorer, "get_seasonal_score", return_value=15.0), \
                 patch.object(engine.carry_scanner, "get_carry_score", return_value=8.0):

                # Mock risk indicators
                mock_indicators = Mock()
                mock_indicators.risk_regime = "RISK_ON"
                mock_risk_indicators.return_value = mock_indicators

                # Calculate EdgeScore
                breakdown = engine.get_edge_score("EUR/USD", "forex")

                # Verify component scores
                assert breakdown.macro_score == 60.0
                assert breakdown.cot_score == 40.0
                assert breakdown.sentiment_score == -30.0
                assert breakdown.risk_score == 50.0
                assert breakdown.seasonal_bonus == 15.0
                assert breakdown.carry_bonus == 8.0

                # Verify EdgeScore calculation
                # Base = 60*0.30 + 40*0.25 + (-30)*0.20 + 50*0.10 + 0*0.15
                #      = 18 + 10 + (-6) + 5 + 0 = 27
                # EdgeScore = 27 + 15 + 8 = 50
                assert breakdown.edge_score == 50.0

    def test_edge_score_clamping(self):
        """Test that EdgeScore is clamped to [-100, +100]"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_edge_scores.json"

            engine = EdgeScoreEngine(cache_ttl_seconds=3600)
            engine.cache_file = cache_file

            # Mock extreme scores that would exceed +100
            with patch.object(engine.macro_scorer, "get_macro_score", return_value=100.0), \
                 patch.object(engine.cot_scorer, "get_cot_score", return_value=100.0), \
                 patch.object(engine.sentiment_scorer, "get_sentiment_score", return_value=100.0), \
                 patch.object(engine.risk_gauge, "get_risk_score", return_value=100.0), \
                  patch.object(engine, "_get_momentum_score", return_value=0.0), \
                 patch.object(engine.risk_gauge, "fetch_risk_indicators") as mock_risk_indicators, \
                 patch.object(engine.seasonal_scorer, "get_seasonal_score", return_value=20.0), \
                 patch.object(engine.carry_scanner, "get_carry_score", return_value=10.0):

                mock_indicators = Mock()
                mock_indicators.risk_regime = "RISK_ON"
                mock_risk_indicators.return_value = mock_indicators

                breakdown = engine.get_edge_score("EUR/USD", "forex")

                # EdgeScore should be clamped to +100
                assert breakdown.edge_score == 100.0

    def test_neutral_edge_score(self):
        """Test neutral EdgeScore when all components return 0"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_edge_scores.json"

            engine = EdgeScoreEngine(cache_ttl_seconds=3600)
            engine.cache_file = cache_file

            # Mock all components returning 0
            with patch.object(engine.macro_scorer, "get_macro_score", return_value=0.0), \
                 patch.object(engine.cot_scorer, "get_cot_score", return_value=0.0), \
                 patch.object(engine.sentiment_scorer, "get_sentiment_score", return_value=0.0), \
                 patch.object(engine.risk_gauge, "get_risk_score", return_value=0.0), \
                  patch.object(engine, "_get_momentum_score", return_value=0.0), \
                 patch.object(engine.risk_gauge, "fetch_risk_indicators") as mock_risk_indicators, \
                 patch.object(engine.seasonal_scorer, "get_seasonal_score", return_value=0.0), \
                 patch.object(engine.carry_scanner, "get_carry_score", return_value=0.0):

                mock_indicators = Mock()
                mock_indicators.risk_regime = "NEUTRAL"
                mock_risk_indicators.return_value = mock_indicators

                breakdown = engine.get_edge_score("BTC/USDT", "crypto")

                # All scores should be 0
                assert breakdown.edge_score == 0.0
                assert breakdown.macro_score == 0.0
                assert breakdown.cot_score == 0.0

    def test_cache_behavior(self):
        """Test cache hit/miss behavior"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_edge_scores.json"

            engine = EdgeScoreEngine(cache_ttl_seconds=3600)
            engine.cache_file = cache_file

            # Mock components
            with patch.object(engine.macro_scorer, "get_macro_score", return_value=50.0), \
                 patch.object(engine.cot_scorer, "get_cot_score", return_value=30.0), \
                 patch.object(engine.sentiment_scorer, "get_sentiment_score", return_value=0.0), \
                 patch.object(engine.risk_gauge, "get_risk_score", return_value=40.0), \
                  patch.object(engine, "_get_momentum_score", return_value=0.0), \
                 patch.object(engine.risk_gauge, "fetch_risk_indicators") as mock_risk_indicators, \
                 patch.object(engine.seasonal_scorer, "get_seasonal_score", return_value=0.0), \
                 patch.object(engine.carry_scanner, "get_carry_score", return_value=0.0):

                mock_indicators = Mock()
                mock_indicators.risk_regime = "NEUTRAL"
                mock_risk_indicators.return_value = mock_indicators

                # First call - cache miss
                breakdown1 = engine.get_edge_score("EUR/USD", "forex")
                assert breakdown1.cache_hit is False

                # Second call - cache hit
                breakdown2 = engine.get_edge_score("EUR/USD", "forex")
                assert breakdown2.cache_hit is True

                # Scores should match
                assert breakdown1.edge_score == breakdown2.edge_score

    def test_momentum_weight_contribution(self):
        """Momentum score should contribute using configured weight."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_edge_scores.json"

            engine = EdgeScoreEngine(cache_ttl_seconds=3600)
            engine.cache_file = cache_file

            with patch.object(engine.macro_scorer, "get_macro_score", return_value=0.0), \
                 patch.object(engine.cot_scorer, "get_cot_score", return_value=0.0), \
                 patch.object(engine.sentiment_scorer, "get_sentiment_score", return_value=0.0), \
                 patch.object(engine.risk_gauge, "get_risk_score", return_value=0.0), \
                 patch.object(engine, "_get_momentum_score", return_value=40.0), \
                 patch.object(engine.risk_gauge, "fetch_risk_indicators") as mock_risk_indicators, \
                 patch.object(engine.seasonal_scorer, "get_seasonal_score", return_value=0.0), \
                 patch.object(engine.carry_scanner, "get_carry_score", return_value=0.0):

                mock_indicators = Mock()
                mock_indicators.risk_regime = "NEUTRAL"
                mock_risk_indicators.return_value = mock_indicators

                breakdown = engine.get_edge_score("EUR/USD", "forex")

                # 40 * EDGE_WEIGHT_MOMENTUM(0.15) = 6
                assert breakdown.momentum_score == 40.0
                assert breakdown.edge_score == 6.0

    def test_component_weights_sum_to_one(self):
        """Test that component weights sum to 1.0"""
        engine = EdgeScoreEngine()

        weight_sum = sum(engine.weights.values())
        assert abs(weight_sum - 1.0) < 0.01, f"Weights sum to {weight_sum}, not 1.0"

    def test_adaptive_momentum_scale_expands_with_volatility(self):
        """Adaptive normalization scale should expand for high-volatility series."""
        engine = EdgeScoreEngine()

        low_vol = [100.0 + (i * 0.01) for i in range(80)]
        high_vol = [100.0 + ((-1) ** i) * 2.0 + (i * 0.1) for i in range(80)]

        low_scale = engine._adaptive_momentum_scale(low_vol, roc_lookback=24, base_scale=1.5)
        high_scale = engine._adaptive_momentum_scale(high_vol, roc_lookback=24, base_scale=1.5)

        assert low_scale >= 1.5
        assert high_scale > low_scale

    def test_get_top_setups_mock(self):
        """Test get_top_setups with mocked data"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "test_edge_scores.json"

            engine = EdgeScoreEngine(cache_ttl_seconds=3600)
            engine.cache_file = cache_file

            # Mock get_edge_score to return different scores for different symbols
            def mock_get_edge_score(symbol, asset_class):
                scores = {
                    "EUR/USD": 75.0,
                    "GBP/USD": 60.0,
                    "USD/JPY": 45.0,
                    "AUD/USD": 80.0,
                }
                return EdgeScoreBreakdown(
                    macro_score=0.0,
                    cot_score=0.0,
                    sentiment_score=0.0,
                    risk_score=0.0,
                    momentum_score=0.0,
                    seasonal_bonus=0.0,
                    carry_bonus=0.0,
                    edge_score=scores.get(symbol, 0.0),
                    timestamp=datetime.now(timezone.utc),
                    cache_hit=False,
                )

            with patch.object(engine, "get_edge_score", side_effect=mock_get_edge_score):
                # Get top setups with min_score=50
                top_setups = engine.get_top_setups("forex", min_edge_score=50.0, limit=10)

                # Should return 3 symbols (EUR/USD, GBP/USD, AUD/USD) sorted by score
                assert len(top_setups) == 3
                assert top_setups[0] == ("AUD/USD", 80.0)  # Highest
                assert top_setups[1] == ("EUR/USD", 75.0)
                assert top_setups[2] == ("GBP/USD", 60.0)
                # USD/JPY (45.0) should be filtered out (< 50.0)
