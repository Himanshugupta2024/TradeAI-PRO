"""
Unit tests for the enhanced "Best Right Now" setup ranking, scoring, risk geometry,
filtering, and decision intelligence in api/service.py (_compute_overview_intelligence).

Verifies:
1. R:R Expectancy scoring & asymmetric rewards (>=2.5x, >=2.0x vs <1.3x).
2. Trade lifecycle gates: TARGET_HIT and STOPPED_OUT setups are blocked and moved to avoid_list.
3. Severe chasing gate (entry_distance_pct >= 2.0% during live market) moved to avoid_list.
4. Optimal entry zone recognition (in_entry_zone when <= 0.35%) and priority sorting.
5. Macro trend conflict gate during live market.
6. Multi-factor confluence score and technical conflict integration into ranking.
7. Volume confirmation for NSE equity signals.
8. Inverted / invalid risk geometry directional validation and gating.
9. Forward expectancy exhaustion gating (effective R:R < 1.0 after price move).
10. Dynamic trade lifecycle transition from ACTIVE to TARGET_HIT / STOPPED_OUT on current price.
11. Correct schema, metadata (phase7_v3), and sorting of top_setups.
"""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pandas as pd
import numpy as np

import api.service as svc
from api.service import (
    _phase2_composite_score,
    _compute_overview_intelligence,
    _confluence_for_setup,
    _ensure_top_setups_fallback,
)


class TestBestRightNowScoring(unittest.TestCase):
    """Test the enhanced multi-factor composite scoring engine."""

    def setUp(self):
        self.neutral_mood = {
            "mood": "NEUTRAL",
            "score": 50.0,
            "signals": {"bias_pct": 0.0},
        }

    def test_rr_expectancy_bonus(self):
        """High R:R setups (>= 2.5x) receive higher composite rank than marginal R:R setups."""
        high_rr_sig = {
            "symbol": "INFY",
            "signal": "LONG",
            "quality_score": 75,
            "confidence": 70.0,
            "context_score": 0.1,
            "entry": 100.0,
            "stop_loss": 98.0,   # Risk = 2.0
            "target": 106.0,      # Reward = 6.0 -> R:R = 3.0x
        }
        low_rr_sig = {
            "symbol": "TCS",
            "signal": "LONG",
            "quality_score": 75,
            "confidence": 70.0,
            "context_score": 0.1,
            "entry": 100.0,
            "stop_loss": 95.0,   # Risk = 5.0
            "target": 106.0,      # Reward = 6.0 -> R:R = 1.2x
        }
        score_high, exp_high = _phase2_composite_score(high_rr_sig, self.neutral_mood)
        score_low, exp_low = _phase2_composite_score(low_rr_sig, self.neutral_mood)

        self.assertGreater(score_high, score_low)
        self.assertGreater(exp_high["rr_alignment_pts"], exp_low["rr_alignment_pts"])
        self.assertEqual(exp_high["rr_alignment_pts"], 4.0)
        self.assertEqual(exp_low["rr_alignment_pts"], -6.0)

    def test_directional_level_inversion_penalized(self):
        """Signals with inverted risk geometry (e.g. SL > Entry for LONG) receive penalty, not reward."""
        inverted_sig = {
            "symbol": "INVERTED_STOCK",
            "signal": "LONG",
            "quality_score": 75,
            "confidence": 70.0,
            "entry": 100.0,
            "stop_loss": 105.0,   # SL above entry for LONG!
            "target": 90.0,       # TP below entry for LONG!
        }
        score, exp = _phase2_composite_score(inverted_sig, self.neutral_mood)
        self.assertEqual(exp["rr_alignment_pts"], -10.0)
        penalty_names = [p["name"] for p in exp["penalty_detail"]]
        self.assertIn("Invalid risk geometry", penalty_names)

    def test_optimal_entry_zone_bonus_vs_chasing_penalty(self):
        """Signals sitting in the entry pocket receive bonus; chased signals receive penalty."""
        in_zone_sig = {
            "symbol": "RELIANCE",
            "signal": "LONG",
            "quality_score": 70,
            "confidence": 65.0,
            "entry_distance_pct": 0.20,  # <= 0.35% sweet spot
        }
        chased_sig = {
            "symbol": "HDFCBANK",
            "signal": "LONG",
            "quality_score": 70,
            "confidence": 65.0,
            "entry_distance_pct": 1.60,  # >= 1.2% chased
        }
        score_zone, exp_zone = _phase2_composite_score(in_zone_sig, self.neutral_mood)
        score_chased, exp_chased = _phase2_composite_score(chased_sig, self.neutral_mood)

        self.assertGreater(score_zone, score_chased)
        self.assertEqual(exp_zone["entry_zone_bonus"], 2.0)
        self.assertGreater(exp_chased["penalty_total"], 0)

    def test_confluence_integration_and_technical_conflicts(self):
        """High confluence boosts score; technical conflicts (macro conflict, RSI extreme) penalize score."""
        high_conf_sig = {
            "symbol": "SBIN",
            "signal": "LONG",
            "quality_score": 70,
            "confidence": 65.0,
            "confluence_score": 80,
            "confluence_penalties": [],
        }
        macro_conflict_sig = {
            "symbol": "ICICIBANK",
            "signal": "LONG",
            "quality_score": 70,
            "confidence": 65.0,
            "confluence_score": 40,
            "confluence_penalties": ["macro_conflict", "rsi_overbought_long"],
        }
        score_conf, exp_conf = _phase2_composite_score(high_conf_sig, self.neutral_mood)
        score_conflict, exp_conflict = _phase2_composite_score(macro_conflict_sig, self.neutral_mood)

        self.assertGreater(score_conf, score_conflict)
        self.assertEqual(exp_conf["confluence_boost_pts"], 4.0)
        self.assertGreaterEqual(exp_conflict["penalty_total"], 13.0)  # 8 macro + 5 rsi

    def test_volume_surge_bonus(self):
        """Setups with confirmed volume surge (>= 1.3x) receive positive contributor points."""
        surge_sig = {
            "symbol": "TATAMOTORS",
            "signal": "LONG",
            "quality_score": 70,
            "confidence": 65.0,
            "volume_ratio": 1.65,
        }
        normal_sig = {
            "symbol": "MARUTI",
            "signal": "LONG",
            "quality_score": 70,
            "confidence": 65.0,
            "volume_ratio": 0.90,
        }
        score_surge, exp_surge = _phase2_composite_score(surge_sig, self.neutral_mood)
        score_normal, exp_normal = _phase2_composite_score(normal_sig, self.neutral_mood)

        self.assertGreater(score_surge, score_normal)
        contributors = [c["factor"] for c in exp_surge.get("top_contributors", [])]
        self.assertIn("Volume Surge", contributors)


class TestBestRightNowDecisionCockpitGates(unittest.TestCase):
    """Test that _compute_overview_intelligence enforces capital safety gates on top_setups."""

    def setUp(self):
        svc._cached_overview_intelligence = None
        svc._cached_overview_time = 0.0

    def test_target_hit_and_stopped_out_routed_to_avoid_list(self):
        """Setups whose targets or stops were already hit are moved to avoid_list."""
        mock_signals = [
            {
                "symbol": "STOCK_TARGET_HIT",
                "signal": "LONG",
                "quality_score": 85,
                "confidence": 80.0,
                "entry": 100.0,
                "stop_loss": 95.0,
                "target": 110.0,
                "close": 110.5,  # Above target -> TARGET_HIT
                "signal_status": "TARGET_HIT",
                "stale": False,
            },
            {
                "symbol": "STOCK_STOPPED_OUT",
                "signal": "LONG",
                "quality_score": 82,
                "confidence": 75.0,
                "entry": 100.0,
                "stop_loss": 95.0,
                "target": 110.0,
                "close": 94.0,   # Below stop -> STOPPED_OUT
                "signal_status": "STOPPED_OUT",
                "stale": False,
            },
            {
                "symbol": "STOCK_ACTIVE",
                "signal": "LONG",
                "quality_score": 80,
                "confidence": 75.0,
                "entry": 100.0,
                "stop_loss": 96.0,
                "target": 108.0,
                "close": 100.2,  # Inside valid entry zone
                "signal_status": "ACTIVE",
                "stale": False,
            },
        ]

        with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "false"}):
            with patch.object(svc, "_signal_cache", {s["symbol"]: None for s in mock_signals}):
                with patch.object(svc, "_get_signal_for_symbol", side_effect=lambda sym, mood=None: next((s for s in mock_signals if s["symbol"] == sym), None)):
                    with patch.object(svc, "_get_feed_status", return_value={"is_market_open": True, "state": "LIVE"}):
                        payload = _compute_overview_intelligence(force_refresh=True)

        top_syms = [s["symbol"] for s in payload["top_setups"]]
        avoid_syms = [s["symbol"] for s in payload["avoid_list"]]

        self.assertIn("STOCK_ACTIVE", top_syms)
        self.assertNotIn("STOCK_TARGET_HIT", top_syms)
        self.assertNotIn("STOCK_STOPPED_OUT", top_syms)
        self.assertIn("STOCK_TARGET_HIT", avoid_syms)
        self.assertIn("STOCK_STOPPED_OUT", avoid_syms)

        # Check blocker tags
        th_item = next(s for s in payload["avoid_list"] if s["symbol"] == "STOCK_TARGET_HIT")
        so_item = next(s for s in payload["avoid_list"] if s["symbol"] == "STOCK_STOPPED_OUT")
        self.assertIn("target_already_hit", th_item["blockers"])
        self.assertIn("stopped_out", so_item["blockers"])

    def test_dynamic_transition_from_active_to_target_hit(self):
        """A signal cached as ACTIVE transitions to TARGET_HIT if current close >= target."""
        cached_active_signal = {
            "symbol": "STOCK_DYNAMIC_HIT",
            "signal": "LONG",
            "quality_score": 85,
            "confidence": 80.0,
            "entry": 100.0,
            "stop_loss": 95.0,
            "target": 110.0,
            "close": 110.2,  # Price crossed target after signal generation
            "signal_status": "ACTIVE",  # Stored as ACTIVE
            "stale": False,
        }
        with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "false"}):
            with patch.object(svc, "_signal_cache", {"STOCK_DYNAMIC_HIT": None}):
                with patch.object(svc, "_get_signal_for_symbol", return_value=cached_active_signal):
                    with patch.object(svc, "_get_feed_status", return_value={"is_market_open": True, "state": "LIVE"}):
                        payload = _compute_overview_intelligence(force_refresh=True)

        top_syms = [s["symbol"] for s in payload["top_setups"]]
        avoid_syms = [s["symbol"] for s in payload["avoid_list"]]

        self.assertNotIn("STOCK_DYNAMIC_HIT", top_syms)
        self.assertIn("STOCK_DYNAMIC_HIT", avoid_syms)
        item = next(s for s in payload["avoid_list"] if s["symbol"] == "STOCK_DYNAMIC_HIT")
        self.assertEqual(item["signal_status"], "TARGET_HIT")
        self.assertIn("target_already_hit", item["blockers"])

    def test_severe_chase_gate_during_live_market(self):
        """Setups with entry_distance_pct >= 2.0% are blocked as chase_extended during live hours."""
        chased_setup = {
            "symbol": "STOCK_CHASED",
            "signal": "LONG",
            "quality_score": 88,
            "confidence": 85.0,
            "entry": 100.0,
            "stop_loss": 96.0,
            "target": 110.0,
            "close": 102.5,  # +2.5% above entry -> severe chase
            "signal_status": "ACTIVE",
            "stale": False,
        }
        with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "false"}):
            with patch.object(svc, "_signal_cache", {"STOCK_CHASED": None}):
                with patch.object(svc, "_get_signal_for_symbol", return_value=chased_setup):
                    with patch.object(svc, "_get_feed_status", return_value={"is_market_open": True, "state": "LIVE"}):
                        payload = _compute_overview_intelligence(force_refresh=True)

        top_syms = [s["symbol"] for s in payload["top_setups"]]
        avoid_syms = [s["symbol"] for s in payload["avoid_list"]]

        self.assertNotIn("STOCK_CHASED", top_syms)
        self.assertIn("STOCK_CHASED", avoid_syms)
        avoid_item = next(s for s in payload["avoid_list"] if s["symbol"] == "STOCK_CHASED")
        self.assertIn("chase_extended", avoid_item["blockers"])

    def test_forward_expectancy_exhaustion_gate(self):
        """A setup where price has run close to target so effective R:R < 1.0 is blocked."""
        exhausted_setup = {
            "symbol": "STOCK_EXHAUSTED",
            "signal": "LONG",
            "quality_score": 85,
            "confidence": 80.0,
            "entry": 100.0,
            "stop_loss": 97.0,   # Risk at entry = 3.0
            "target": 104.0,      # Planned R:R = 4/3 = 1.33x
            "close": 102.8,      # +2.8% run: remaining reward = 1.2, current risk = 5.8 -> eff R:R = 0.21x
            "signal_status": "ACTIVE",
            "stale": False,
        }
        with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "false"}):
            with patch.object(svc, "_signal_cache", {"STOCK_EXHAUSTED": None}):
                with patch.object(svc, "_get_signal_for_symbol", return_value=exhausted_setup):
                    with patch.object(svc, "_get_feed_status", return_value={"is_market_open": True, "state": "LIVE"}):
                        payload = _compute_overview_intelligence(force_refresh=True)

        top_syms = [s["symbol"] for s in payload["top_setups"]]
        avoid_syms = [s["symbol"] for s in payload["avoid_list"]]

        self.assertNotIn("STOCK_EXHAUSTED", top_syms)
        self.assertIn("STOCK_EXHAUSTED", avoid_syms)
        avoid_item = next(s for s in payload["avoid_list"] if s["symbol"] == "STOCK_EXHAUSTED")
        self.assertIn("forward_expectancy_exhausted", avoid_item["blockers"])
        self.assertLess(avoid_item["effective_rr"], 1.0)

    def test_poor_rr_gate_during_live_market(self):
        """Setups with planned R:R < 1.3 are blocked as poor_risk_reward during live hours."""
        poor_rr_setup = {
            "symbol": "STOCK_POOR_RR",
            "signal": "LONG",
            "quality_score": 85,
            "confidence": 80.0,
            "entry": 100.0,
            "stop_loss": 90.0,   # Risk = 10
            "target": 108.0,      # Reward = 8 -> R:R = 0.8x
            "close": 100.1,
            "signal_status": "ACTIVE",
            "stale": False,
        }
        with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "false"}):
            with patch.object(svc, "_signal_cache", {"STOCK_POOR_RR": None}):
                with patch.object(svc, "_get_signal_for_symbol", return_value=poor_rr_setup):
                    with patch.object(svc, "_get_feed_status", return_value={"is_market_open": True, "state": "LIVE"}):
                        payload = _compute_overview_intelligence(force_refresh=True)

        top_syms = [s["symbol"] for s in payload["top_setups"]]
        avoid_syms = [s["symbol"] for s in payload["avoid_list"]]

        self.assertNotIn("STOCK_POOR_RR", top_syms)
        self.assertIn("STOCK_POOR_RR", avoid_syms)
        avoid_item = next(s for s in payload["avoid_list"] if s["symbol"] == "STOCK_POOR_RR")
        self.assertIn("poor_risk_reward", avoid_item["blockers"])

    def test_ranking_meta_scoring_version(self):
        """Overview payload contains enhanced scoring version and summary."""
        with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "false"}):
            with patch.object(svc, "_signal_cache", {}):
                with patch.object(svc, "_persisted_signals_snapshot", []):
                    payload = _compute_overview_intelligence(force_refresh=True)

        self.assertEqual(payload["ranking_meta"]["scoring_version"], "phase7_v3")
        self.assertIn("R:R expectancy", payload["ranking_meta"]["scoring_summary"])


if __name__ == "__main__":
    unittest.main()
