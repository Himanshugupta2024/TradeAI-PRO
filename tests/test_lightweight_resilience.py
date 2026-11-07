"""
Unit tests verifying Priority 0 (P0) Stability & Render Resiliency:
1. Under LIGHTWEIGHT_API_MODE, web endpoints and signal generation strictly bypass
   heavy ML model inference (XGBoost, LightGBM, LSTM, CatBoost).
2. /api/overview/intelligence strictly reads cached DB snapshots.
3. Model status reports clean lightweight web mode without loading models into RAM.
"""

import os
import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np


class TestLightweightResilience(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.get("LIGHTWEIGHT_API_MODE")
        os.environ["LIGHTWEIGHT_API_MODE"] = "1"

    def tearDown(self):
        if self.orig_env is not None:
            os.environ["LIGHTWEIGHT_API_MODE"] = self.orig_env
        else:
            os.environ.pop("LIGHTWEIGHT_API_MODE", None)

    def test_ensemble_bypasses_heavy_ml_in_lightweight_mode(self):
        """In lightweight mode, predict_advanced must not call XGBoost or LightGBM."""
        from agents.ai_engine.ensemble import predict_advanced, _is_lightweight_mode
        self.assertTrue(_is_lightweight_mode())

        # Construct minimal sample candle dataframe
        df = pd.DataFrame([{
            "timestamp": pd.Timestamp.now() - pd.Timedelta(minutes=5 * (4 - i)),
            "open": 100.0 + i,
            "high": 105.0 + i,
            "low": 99.0 + i,
            "close": 103.0 + i,
            "volume": 10000,
            "RSI_14": 55.0,
            "MACD_hist": 0.5,
            "EMA_20": 101.0,
            "EMA_50": 98.0,
            "VWAP": 102.0,
            "htf_trend": "BULLISH",
        } for i in range(5)])

        with patch("agents.ai_engine.xgboost_model.get_xgb_predictor") as mock_xgb, \
             patch("agents.ai_engine.lightgbm_model.get_lgbm_predictor") as mock_lgbm:
            res = predict_advanced(df, symbol="TEST_SYM", force_refresh=True)

            # Heavy ML models must NEVER be fetched or run
            mock_xgb.assert_not_called()
            mock_lgbm.assert_not_called()

            self.assertTrue(res.get("lightweight_mode"))
            self.assertIn("direction", res)
            self.assertIn("calibrated_trade_win_prob", res)

    def test_batch_ensemble_bypasses_heavy_ml_in_lightweight_mode(self):
        """In lightweight mode, predict_batch_ensemble must use fallback predictor without loading XGB/LGBM."""
        from agents.ai_engine.ensemble import predict_batch_ensemble

        df = pd.DataFrame([{
            "close": 100.0 + i,
            "RSI_14": 50.0,
            "MACD_hist": 0.1,
            "EMA_20": 99.0,
            "EMA_50": 98.0,
        } for i in range(10)])

        with patch("agents.ai_engine.xgboost_model.get_xgb_predictor") as mock_xgb, \
             patch("agents.ai_engine.lightgbm_model.get_lgbm_predictor") as mock_lgbm:
            conf_series = predict_batch_ensemble(df, symbol="TEST_SYM")

            mock_xgb.assert_not_called()
            mock_lgbm.assert_not_called()

            self.assertEqual(len(conf_series), 10)
            self.assertTrue((conf_series >= 0).all() and (conf_series <= 100).all())

    def test_get_model_status_lightweight_mode(self):
        """In lightweight mode, get_model_status must return status without model imports."""
        from agents.ai_engine.ensemble import get_model_status

        with patch("agents.ai_engine.xgboost_model.get_xgb_predictor") as mock_xgb, \
             patch("agents.ai_engine.lightgbm_model.get_lgbm_predictor") as mock_lgbm:
            status = get_model_status()

            mock_xgb.assert_not_called()
            mock_lgbm.assert_not_called()

            self.assertTrue(status.get("lightweight_mode"))
            self.assertTrue(status.get("statistical_heuristic"))
            self.assertIn("badge_text", status)

            # When no model weights exist on disk
            with patch("os.path.exists", return_value=False):
                status_empty = get_model_status()
                self.assertEqual(status_empty.get("fitted_count"), 0)
                self.assertIn("Heuristic", status_empty.get("badge_text", ""))

    def test_signal_generator_bypasses_ensemble_in_lightweight_mode(self):
        """signal_generator must skip attempt 3 (predict_batch_ensemble) when in lightweight mode."""
        from agents.signal_engine.signal_generator import generate_signals

        df = pd.DataFrame([{
            "timestamp": pd.Timestamp.now(),
            "open": 100.0 + i,
            "high": 102.0 + i,
            "low": 99.0 + i,
            "close": 101.0 + i,
            "volume": 50000,
            "RSI_14": 45.0,
            "MACD": 0.2,
            "MACD_signal": 0.1,
            "MACD_hist": 0.1,
            "EMA_9": 100.5,
            "EMA_20": 100.0,
            "EMA_50": 98.0,
            "EMA_200": 95.0,
            "ATR_14": 1.5,
            "VWAP": 100.8,
            "htf_trend": "BULLISH",
        } for i in range(30)])

        with patch("agents.ai_engine.ensemble.predict_batch_ensemble") as mock_ens:
            res_df = generate_signals(df, symbol="RELIANCE", offline=True)
            mock_ens.assert_not_called()
            self.assertIn("signal", res_df.columns)
            self.assertIn("confidence", res_df.columns)

    def test_overview_intelligence_reads_db_snapshot_in_lightweight_mode(self):
        """_compute_overview_intelligence must read cached DB snapshot in lightweight mode."""
        from api import service as svc

        # Clear in-memory cache to guarantee testing the DB snapshot path
        svc._cached_overview_intelligence = None
        svc._cached_overview_time = 0.0

        mock_db_snapshot = {
            "top_setups": [{"symbol": "INFY", "signal": "LONG", "quality_score": 80, "confidence": 75.0}],
            "avoid_list": [],
            "ranking_meta": {"total_tracked": 1, "actionable": 1, "session_state": "LIVE"},
            "market_guardrails": {"mood": "BULLISH", "mood_score": 68.0},
            "system_state": {"capital": 100000, "open_trades": 0},
            "updated_at": "2026-11-07T12:00:00Z",
            "source": "DB_SNAPSHOT_MOCK"
        }

        with patch("db.runtime_state.get_runtime_state", return_value=mock_db_snapshot):
            # force_refresh=False must hit DB snapshot
            overview = svc._compute_overview_intelligence(force_refresh=False)
            self.assertEqual(overview.get("source"), "DB_SNAPSHOT_MOCK")
            self.assertEqual(len(overview.get("top_setups", [])), 1)
            self.assertEqual(overview["top_setups"][0]["symbol"], "INFY")


if __name__ == "__main__":
    unittest.main()
