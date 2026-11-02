"""
Unit & Integration Tests for TradeAI-PRO Data Brain V3
Tests the 5 orthogonal free data dimensions:
  1. India VIX & Dynamic Volatility Regimes
  2. FII / DII Institutional Flow & Rolling Momentum
  3. NSE Corporate Announcements & Event Risk Hard-Gate
  4. Security Delivery % & Market Breadth
  5. Derivatives F&O Open Interest Buildup & PCR Engine
  6. Multi-Dimensional Composite Decision Synthesizer
"""

import pytest
import pandas as pd
from datetime import datetime, timezone

from agents.data_ingestion.vix_fetcher import (
    _classify_vix_regime,
    _calculate_vix_modifiers,
)
from agents.data_ingestion.fii_dii_fetcher import _compute_flow_score
from agents.data_ingestion.corporate_events import (
    _evaluate_event_risk,
    check_symbol_event_risk,
    _events_by_symbol,
)
from agents.data_ingestion.delivery_breadth import (
    calculate_delivery_strength,
    compute_market_breadth_score,
    get_symbol_delivery_metrics,
)
from agents.data_ingestion.derivatives_oi import (
    classify_oi_buildup,
    interpret_pcr,
)
from agents.signal_engine.composite_decision import build_composite_decision


class TestIndiaVIXEngine:
    def test_vix_regime_classification(self):
        assert _classify_vix_regime(15.0) == "LOW"
        assert _classify_vix_regime(45.0) == "NORMAL"
        assert _classify_vix_regime(75.0) == "HIGH"
        assert _classify_vix_regime(92.0) == "EXTREME"

    def test_vix_sizing_and_penalties(self):
        # LOW regime: full size, no penalty
        mult, pen = _calculate_vix_modifiers(11.5, 15.0, "LOW")
        assert mult == 1.0
        assert pen == 0

        # NORMAL regime: full size, no penalty
        mult, pen = _calculate_vix_modifiers(14.0, 40.0, "NORMAL")
        assert mult == 1.0
        assert pen == 0

        # HIGH regime: reduced size, -3 penalty
        mult, pen = _calculate_vix_modifiers(18.5, 75.0, "HIGH")
        assert mult < 1.0
        assert pen == -3

        # EXTREME regime: defensive size (0.35x), -6 penalty
        mult, pen = _calculate_vix_modifiers(26.0, 95.0, "EXTREME")
        assert mult == 0.35
        assert pen == -6


class TestFIIDIIMomentumEngine:
    def test_fii_dii_flow_scoring(self):
        # Strongly positive flows
        score_bull = _compute_flow_score(fii_5d=2500.0, dii_5d=1200.0, fii_1d=800.0, dii_1d=400.0)
        assert score_bull >= 8

        # Strongly negative flows
        score_bear = _compute_flow_score(fii_5d=-3000.0, dii_5d=-500.0, fii_1d=-1200.0, dii_1d=-100.0)
        assert score_bear <= -8

        # Neutral / balanced
        score_neutral = _compute_flow_score(fii_5d=100.0, dii_5d=-100.0, fii_1d=50.0, dii_1d=-50.0)
        assert abs(score_neutral) <= 2


class TestCorporateEventRiskGate:
    def test_evaluate_event_risk_keywords(self):
        assert _evaluate_event_risk("Unaudited Financial Results for Q3") == "HIGH_RISK"
        assert _evaluate_event_risk("Notice of Board Meeting to consider Dividend") == "HIGH_RISK"
        assert _evaluate_event_risk("Resignation of Chief Financial Officer") == "HIGH_RISK"
        assert _evaluate_event_risk("Investor Presentation on Q2") == "LOW_RISK"
        assert _evaluate_event_risk("Routine Share Certificate loss") == "LOW_RISK"

    def test_symbol_event_risk_gating(self):
        # Inject mock high-risk event for RELIANCE
        _events_by_symbol["RELIANCE"] = [
            {"subject": "Board Meeting for Financial Results", "broadcast_time": "2026-11-02", "risk_level": "HIGH_RISK"}
        ]
        res = check_symbol_event_risk("RELIANCE.NS")
        assert res["has_event_risk"] is True
        assert res["hard_skip"] is True
        assert res["event_risk_penalty"] == 10

        # Symbol without events
        res_tcs = check_symbol_event_risk("TCS.NS")
        assert res_tcs["has_event_risk"] is False
        assert res_tcs["hard_skip"] is False


class TestDeliveryAndMarketBreadth:
    def test_delivery_strength_accumulation_vs_churn(self):
        # High delivery breakout -> Accumulation bonus (+7)
        bonus, interp = calculate_delivery_strength(
            delivery_pct=65.0, avg_delivery_20d=40.0, delivery_zscore=1.8, price_change_pct=3.0, volume_ratio=2.2
        )
        assert bonus == 7
        assert interp == "INSTITUTIONAL_ACCUMULATION"

        # High volume price spike with dismal delivery -> Retail churn penalty (-3)
        bonus, interp = calculate_delivery_strength(
            delivery_pct=18.0, avg_delivery_20d=42.0, delivery_zscore=-1.1, price_change_pct=3.5, volume_ratio=2.5
        )
        assert bonus == -3
        assert interp == "SPECULATIVE_RETAIL_CHURN"

    def test_market_breadth_scoring(self):
        ratio, mod = compute_market_breadth_score(advances=38, declines=12)
        assert ratio > 3.0
        assert mod >= 5

        ratio, mod = compute_market_breadth_score(advances=10, declines=40)
        assert ratio < 0.3
        assert mod <= -5


class TestDerivativesOIEngine:
    def test_four_quadrant_buildup_classification(self):
        assert classify_oi_buildup(price_change_pct=0.8, oi_change_pct=1.5) == "LONG_BUILDUP"
        assert classify_oi_buildup(price_change_pct=-0.9, oi_change_pct=2.1) == "SHORT_BUILDUP"
        assert classify_oi_buildup(price_change_pct=0.7, oi_change_pct=-1.2) == "SHORT_COVERING"
        assert classify_oi_buildup(price_change_pct=-0.6, oi_change_pct=-0.9) == "LONG_UNWINDING"

    def test_pcr_interpretation(self):
        interp, score = interpret_pcr(1.35)
        assert interp == "BULLISH_SUPPORT"
        assert score > 0

        interp, score = interpret_pcr(0.68)
        assert interp == "BEARISH_RESISTANCE"
        assert score < 0


class TestCompositeDecisionSynthesizer:
    def test_composite_decision_generation_and_gating(self):
        # Strong Long setup validated by breadth, FII, and delivery
        res = build_composite_decision(
            symbol="INFY.NS",
            raw_signal="LONG",
            technical_score=85.0,
            ai_confidence=78.0,
            sector="IT",
            sector_strength=5,
            vix_data={"vix_regime": "NORMAL", "confidence_penalty": 0, "vix_size_multiplier": 1.0},
            fii_dii_data={"institutional_flow_score": 6, "sentiment": "BULLISH"},
            breadth_data={"market_breadth_modifier": 7},
            delivery_data={"delivery_strength_bonus": 7},
            event_risk_data={"has_event_risk": False, "hard_skip": False},
            data_quality=98.5,
        )
        assert res["signal"] == "LONG"
        assert res["final_trade_score"] >= 80
        assert res["breakdown"]["technical_score"] == 85.0
        assert res["breakdown"]["ai_probability"] == 78.0
        assert res["breakdown"]["delivery_strength"] == 7
        assert "institutional delivery accumulation" in res["reasoning"]

    def test_event_risk_overrides_to_skip(self):
        # Even with 95% technical score, event risk overrides to SKIP
        res = build_composite_decision(
            symbol="TATAMOTORS.NS",
            raw_signal="LONG",
            technical_score=95.0,
            ai_confidence=90.0,
            event_risk_data={"has_event_risk": True, "hard_skip": True, "event_risk_penalty": 10},
        )
        assert res["signal"] == "SKIP"
        assert "Gated by Corporate Event Risk" in res["reasoning"]

