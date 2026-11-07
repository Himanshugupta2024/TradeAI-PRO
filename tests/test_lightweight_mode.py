"""
Unit tests for Lightweight Mode and Backend Resiliency (Milestone 1).

Tests coverage for:
- F1: Healthcheck Model Status Guard in agents/ai_engine/ensemble.py
- F2: PyTorch Lazy Import Guard in agents/ai_engine/lstm_model.py
- F3: Overview Intelligence DB Snapshots in api/service.py
- F4: Live ML Inference Short-Circuit & Mutual Recursion Fix
- F8: Origin Error Cache Header Middleware in api/app.py
"""

import asyncio
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
import pandas as pd
from fastapi.testclient import TestClient


class TestLightweightModeEnsemble(unittest.TestCase):
    """Test F1 & F4: Ensemble status guard and live inference bypass."""

    def test_get_model_status_lightweight_mode_genuine_file_check(self):
        """get_model_status in lightweight mode checks os.path.exists without unpickling models."""
        with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "true"}):
            from agents.ai_engine.ensemble import get_model_status

            status = get_model_status()
            self.assertTrue(status.get("lightweight_mode"))
            self.assertIn("fitted_count", status)
            self.assertIn("total_ml_models", status)
            self.assertEqual(status["total_ml_models"], 4)
            self.assertIn("xgboost_fitted", status)
            self.assertIn("lightgbm_fitted", status)
            self.assertIn("lstm_fitted", status)
            self.assertIn("badge_short", status)
            if os.path.exists("models/xgb_model.pkl"):
                self.assertTrue(status["xgboost_fitted"])

    def test_predict_advanced_lightweight_mode_fallback(self):
        """predict_advanced uses fallback predictor in lightweight mode."""
        df = pd.DataFrame({
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [104.0, 105.0, 106.0],
            "volume": [1000, 1200, 1100],
        })
        with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "true"}):
            from agents.ai_engine.ensemble import predict_advanced

            res = predict_advanced(df, symbol="BTC/USDT")
            self.assertTrue(res.get("lightweight_mode"))
            self.assertIn("direction", res)
            self.assertIn("calibrated_trade_win_prob", res)
            self.assertEqual(res.get("model_contributions", {}).get("xgboost"), 0.0)

    def test_predict_batch_ensemble_lightweight_mode(self):
        """predict_batch_ensemble returns statistical Series in lightweight mode."""
        df = pd.DataFrame({
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [104.0, 105.0, 106.0],
            "volume": [1000, 1200, 1100],
        })
        with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "true"}):
            from agents.ai_engine.ensemble import predict_batch_ensemble

            series = predict_batch_ensemble(df, symbol="BTC/USDT")
            self.assertEqual(len(series), len(df))
            self.assertTrue(isinstance(series, pd.Series))


class TestPyTorchLazyImport(unittest.TestCase):
    """Test F2: PyTorch is not imported when ENABLE_PYTORCH_MODELS is false or LIGHTWEIGHT_API_MODE is true."""

    def test_torch_not_imported_in_lightweight_subprocess(self):
        """In clean subprocess with LIGHTWEIGHT_API_MODE=true, lstm_model must not import torch."""
        code = (
            "import os, sys, importlib.util\n"
            "os.environ['LIGHTWEIGHT_API_MODE'] = 'true'\n"
            "os.environ.pop('ENABLE_PYTORCH_MODELS', None)\n"
            "spec = importlib.util.spec_from_file_location('lstm_model', 'agents/ai_engine/lstm_model.py')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "assert not mod.TORCH_AVAILABLE, 'TORCH_AVAILABLE should be False'\n"
            "assert 'torch' not in sys.modules, 'torch was unexpectedly imported into sys.modules'\n"
            "print('SUCCESS')\n"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=os.getcwd())
        self.assertEqual(res.returncode, 0, f"Subprocess failed with stderr:\n{res.stderr}")
        self.assertIn("SUCCESS", res.stdout)

    def test_torch_not_imported_when_torch_models_disabled(self):
        """In clean subprocess with ENABLE_PYTORCH_MODELS=false, lstm_model must not import torch."""
        code = (
            "import os, sys, importlib.util\n"
            "os.environ['ENABLE_PYTORCH_MODELS'] = 'false'\n"
            "os.environ['LIGHTWEIGHT_API_MODE'] = 'false'\n"
            "spec = importlib.util.spec_from_file_location('lstm_model', 'agents/ai_engine/lstm_model.py')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "assert not mod.TORCH_AVAILABLE, 'TORCH_AVAILABLE should be False'\n"
            "assert 'torch' not in sys.modules, 'torch was unexpectedly imported into sys.modules'\n"
            "print('SUCCESS')\n"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=os.getcwd())
        self.assertEqual(res.returncode, 0, f"Subprocess failed with stderr:\n{res.stderr}")
        self.assertIn("SUCCESS", res.stdout)


class TestOverviewIntelligenceSnapshots(unittest.TestCase):
    """Test F3: Overview intelligence DB snapshot reading and cache hydration."""

    def test_compute_overview_reads_db_snapshot_in_lightweight_mode(self):
        """_compute_overview_intelligence reads overview_intelligence:v1 DB snapshot under LIGHTWEIGHT_API_MODE."""
        mock_snapshot = {
            "top_setups": [{"symbol": "INFY", "signal": "LONG", "score": 85.0}],
            "avoid_list": [],
            "market_guardrails": {"mood": "BULLISH"},
            "system_state": {"capital": 100000},
            "source": "db_snapshot_test",
        }
        with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "true"}):
            with patch("db.runtime_state.get_runtime_state", return_value=mock_snapshot) as mock_get:
                from api.service import _compute_overview_intelligence

                result = _compute_overview_intelligence(force_refresh=False)
                mock_get.assert_called_with("overview_intelligence:v1")
                self.assertEqual(result.get("source"), "db_snapshot_test")
                self.assertEqual(len(result.get("top_setups", [])), 1)

    def test_hydrate_signals_cache_from_db_v1_and_snapshot(self):
        """_hydrate_signals_cache_from_db queries latest_signals:v1 and falls back to latest_signals_snapshot."""
        import api.service as svc

        # Test 1: latest_signals:v1 found
        with patch("db.runtime_state.get_runtime_state") as mock_get:
            mock_get.side_effect = lambda key: {"signals": [{"symbol": "TCS", "signal": "LONG"}], "timestamp": "2026-11-07T12:00:00Z"} if key == "latest_signals:v1" else None
            success = asyncio.run(svc._hydrate_signals_cache_from_db())
            self.assertTrue(success)
            self.assertEqual(len(svc._persisted_signals_snapshot), 1)
            self.assertEqual(svc._persisted_signals_snapshot[0]["symbol"], "TCS")

        # Test 2: latest_signals:v1 empty, latest_signals_snapshot found
        with patch("db.runtime_state.get_runtime_state") as mock_get:
            def _fake_get(key):
                if key == "latest_signals:v1":
                    return None
                if key == "latest_signals_snapshot":
                    return {"signals": [{"symbol": "RELIANCE", "signal": "SHORT"}], "updated_at": "2026-11-07T12:05:00Z"}
                return None
            mock_get.side_effect = _fake_get
            success = asyncio.run(svc._hydrate_signals_cache_from_db())
            self.assertTrue(success)
            self.assertEqual(len(svc._persisted_signals_snapshot), 1)
            self.assertEqual(svc._persisted_signals_snapshot[0]["symbol"], "RELIANCE")


class TestMarketMoodNoRecursion(unittest.TestCase):
    """Test F4: Break mutual recursion between _compute_market_mood and _get_signal_for_symbol."""

    def test_compute_market_mood_does_not_call_get_signal_for_symbol(self):
        """_compute_market_mood iterates over _signal_cache without calling _get_signal_for_symbol."""
        import api.service as svc

        orig_signal_cache = svc._signal_cache
        orig_market_data = svc._market_data
        test_df = pd.DataFrame({"signal": ["HOLD", "LONG"], "close": [100.0, 105.0], "timestamp": [pd.Timestamp.now(), pd.Timestamp.now()]})
        try:
            with patch.object(svc, "_get_signal_for_symbol") as mock_get_signal:
                svc._signal_cache = {"TEST_SYM": test_df}
                svc._market_data = {"TEST_SYM": test_df}

                mood = svc._compute_market_mood()
                # _get_signal_for_symbol must NOT have been called
                mock_get_signal.assert_not_called()
                self.assertIn("mood", mood)
                self.assertEqual(mood["signals"]["long"], 1)
        finally:
            svc._signal_cache = orig_signal_cache
            svc._market_data = orig_market_data


class TestWebRouteGuardsAndHeaders(unittest.TestCase):
    """Test F4 & F8: Route guards for retrain and anti-poisoning cache headers on errors."""

    @classmethod
    def setUpClass(cls):
        from server import app
        cls.client = TestClient(app, raise_server_exceptions=False)

    def test_retrain_returns_503_in_lightweight_mode(self):
        """POST /api/retrain returns 503 in LIGHTWEIGHT_API_MODE."""
        from api.routes import require_admin
        from server import app
        app.dependency_overrides[require_admin] = lambda: True
        try:
            with patch.dict(os.environ, {"LIGHTWEIGHT_API_MODE": "true"}):
                resp = self.client.post("/api/retrain")
                self.assertEqual(resp.status_code, 503)
                self.assertIn("Render 512MB RAM cap", resp.json().get("detail", ""))
        finally:
            app.dependency_overrides.pop(require_admin, None)

    def test_error_status_gets_anti_poisoning_cache_headers(self):
        """HTTP responses with status_code >= 400 receive private no-cache no-store headers."""
        resp = self.client.get("/api/non_existent_route_404_test")
        self.assertEqual(resp.status_code, 404)
        cc = resp.headers.get("Cache-Control", "")
        self.assertIn("no-store", cc)
        self.assertIn("no-cache", cc)
        self.assertIn("private", cc)
        self.assertEqual(resp.headers.get("Pragma"), "no-cache")


if __name__ == "__main__":
    unittest.main()
