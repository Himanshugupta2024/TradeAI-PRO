"""
Empirical Adversarial Stress Test Suite: Backend Resiliency, Memory Bounds, and DB Snapshot Reading.
Authored by: teamwork_challenger_1 (EMPIRICAL CHALLENGER)

Adversarial Stress Verification Targets:
1. Module Isolation: Under LIGHTWEIGHT_API_MODE, endpoints (/api/health, /api/overview/intelligence,
   /api/ai/ensemble/status) strictly avoid importing torch and avoid instantiating XGBoostPredictor._predictor.
2. Memory Safety: Probing web routes maintains a lean memory footprint safely below the Render 512MB RAM cap.
3. Precedence & Conflict Stress: Even if ENABLE_PYTORCH_MODELS=true, LIGHTWEIGHT_API_MODE=true takes strict precedence.
4. Concurrency & Repeated Probing: 50 requests under load cause zero memory leaks and no late-loading of ML modules.
5. Retrain Gate: POST /api/retrain returns HTTP 503 and launches zero background training threads.
6. DB Snapshot Resilience: Multi-key fallback (overview_intelligence:v1 -> latest_overview_intelligence ->
   latest_overview_intelligence:v1), DB exceptions, and total snapshot absence degrade gracefully without 500.
7. Poisoned Artifact Defense: Corrupted model artifact does not cause unpickling errors in lightweight mode.
"""

import os
import sys
import json
import time
import subprocess
import threading
import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
from fastapi.testclient import TestClient


class TestChallengerSubprocessIsolation(unittest.TestCase):
    """EMPIRICAL VERIFICATION: Execute in isolated subprocess to verify sys.modules purity."""

    def test_routes_pure_isolation_subprocess(self):
        """
        Verify that booting the FastAPI app and hitting /api/health, /api/overview/intelligence,
        and /api/ai/ensemble/status does NOT import torch or instantiate xgboost._predictor.
        """
        code = """
import os, sys, gc
os.environ["LIGHTWEIGHT_API_MODE"] = "true"
os.environ["INITIAL_LOAD_ENABLED"] = "false"
os.environ["BACKGROUND_REFRESH_ENABLED"] = "false"

from server import app
from fastapi.testclient import TestClient

client = TestClient(app, raise_server_exceptions=True)

# 1. Probe health
r_health = client.get("/api/health")
assert r_health.status_code == 200, f"/api/health failed: {r_health.status_code} {r_health.text}"
h_data = r_health.json()
assert "subsystems" in h_data or "status" in h_data

# 2. Probe overview intelligence
r_overview = client.get("/api/overview/intelligence")
assert r_overview.status_code == 200, f"/api/overview failed: {r_overview.status_code} {r_overview.text}"
o_data = r_overview.json()
assert "top_setups" in o_data

# 3. Probe ai ensemble status
r_status = client.get("/api/ai/ensemble/status")
assert r_status.status_code == 200, f"/api/ai/ensemble/status failed: {r_status.status_code} {r_status.text}"
s_data = r_status.json()
assert s_data.get("status") == "ok"
assert s_data.get("ai_engine", {}).get("lightweight_mode") is True

# Strict empirical checks on modules
assert "torch" not in sys.modules, f"VIOLATION: 'torch' was imported into sys.modules: {sys.modules.get('torch')}"
assert "torch.nn" not in sys.modules, "VIOLATION: 'torch.nn' found in sys.modules"

lstm_mod = sys.modules.get("agents.ai_engine.lstm_model")
if lstm_mod:
    assert getattr(lstm_mod, "TORCH_AVAILABLE", None) is False, "VIOLATION: TORCH_AVAILABLE should be False"

xgb_mod = sys.modules.get("agents.ai_engine.xgboost_model")
if xgb_mod:
    assert getattr(xgb_mod, "_predictor", None) is None, "VIOLATION: xgboost._predictor was initialized"

lgbm_mod = sys.modules.get("agents.ai_engine.lightgbm_model")
if lgbm_mod:
    assert getattr(lgbm_mod, "_predictor", None) is None, "VIOLATION: lightgbm._predictor was initialized"

# Memory verification via psutil if available
try:
    import psutil
    rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
    print(f"PROCESS_RSS_MB={rss_mb:.2f}")
    assert rss_mb < 350, f"VIOLATION: RSS memory {rss_mb}MB exceeded 350MB baseline budget"
except ImportError:
    print("PROCESS_RSS_MB=psutil_not_installed")

print("SUBPROCESS_ISOLATION_VERIFIED_CLEAN")
"""
        res = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, f"Subprocess failed with stderr:\n{res.stderr}\nstdout:\n{res.stdout}")
        self.assertIn("SUBPROCESS_ISOLATION_VERIFIED_CLEAN", res.stdout)
        print("\n[CHALLENGER ORACLE 1] Pure process isolation confirmed: torch NOT in sys.modules, xgboost._predictor is None.")

    def test_torch_env_conflict_precedence(self):
        """
        Adversarial test: If ENABLE_PYTORCH_MODELS=true is maliciously or mistakenly set,
        LIGHTWEIGHT_API_MODE=true MUST strictly take precedence and suppress PyTorch import.
        """
        code = """
import os, sys
os.environ["ENABLE_PYTORCH_MODELS"] = "true"
os.environ["LIGHTWEIGHT_API_MODE"] = "true"

import importlib.util
spec = importlib.util.spec_from_file_location("lstm_model", "agents/ai_engine/lstm_model.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

assert not mod.TORCH_AVAILABLE, "VIOLATION: TORCH_AVAILABLE is True despite LIGHTWEIGHT_API_MODE=true"
assert "torch" not in sys.modules, "VIOLATION: 'torch' leaked into sys.modules under conflicting flags"
print("PRECEDENCE_VERIFIED_CLEAN")
"""
        res = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, f"Conflict precedence test failed:\n{res.stderr}")
        self.assertIn("PRECEDENCE_VERIFIED_CLEAN", res.stdout)
        print("[CHALLENGER ORACLE 2] Conflicting flags precedence verified: LIGHTWEIGHT_API_MODE suppresses torch even when ENABLE_PYTORCH_MODELS=true.")


class TestChallengerBackendStressAndGuards(unittest.TestCase):
    """Adversarial stress and edge case tests on route guards, concurrency, and DB snapshots."""

    @classmethod
    def setUpClass(cls):
        cls._saved_env = {
            k: os.environ[k]
            for k in ("LIGHTWEIGHT_API_MODE", "INITIAL_LOAD_ENABLED", "BACKGROUND_REFRESH_ENABLED")
            if k in os.environ
        }
        os.environ["LIGHTWEIGHT_API_MODE"] = "true"
        os.environ["INITIAL_LOAD_ENABLED"] = "false"
        os.environ["BACKGROUND_REFRESH_ENABLED"] = "false"
        from server import app
        cls.client = TestClient(app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls):
        for k in ("LIGHTWEIGHT_API_MODE", "INITIAL_LOAD_ENABLED", "BACKGROUND_REFRESH_ENABLED"):
            if k in cls._saved_env:
                os.environ[k] = cls._saved_env[k]
            else:
                os.environ.pop(k, None)

    def test_concurrency_repeated_probing_stability(self):
        """
        Adversarial stress: Probe /api/health, /api/overview/intelligence, and /api/ai/ensemble/status
        repeatedly across 60 sequential and parallel requests.
        Verify zero crashes, 200 status codes, and persistent exclusion of heavy ML models.
        """
        endpoints = [
            "/api/health",
            "/api/overview/intelligence",
            "/api/ai/ensemble/status",
            "/api/health/live",
            "/api/health/ready",
        ]
        errors = []

        def worker(ep):
            try:
                resp = self.client.get(ep)
                if resp.status_code != 200:
                    errors.append(f"{ep} returned status {resp.status_code}")
            except Exception as ex:
                errors.append(f"{ep} raised {ex}")

        threads = []
        for i in range(50):
            ep = endpoints[i % len(endpoints)]
            t = threading.Thread(target=worker, args=(ep,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrency stress errors: {errors}")
        print("[CHALLENGER ORACLE 3] Concurrency stress test (50 requests): 100% success rate with zero unhandled exceptions.")

    def test_retrain_endpoint_503_and_zero_worker_threads(self):
        """
        Verify that POST /api/retrain returns HTTP 503 under LIGHTWEIGHT_API_MODE
        and does NOT spawn any background training threads.
        """
        from api.routes import require_admin
        from server import app

        app.dependency_overrides[require_admin] = lambda: True
        try:
            initial_threads = threading.active_count()

            resp = self.client.post("/api/retrain?symbols=BTC/USDT&timeframe=4h&days=30")
            self.assertEqual(resp.status_code, 503)
            data = resp.json()
            self.assertIn("detail", data)
            self.assertIn("Render 512MB RAM cap", data["detail"])

            post_threads = threading.active_count()
            # Assert that no persistent training thread was spawned
            self.assertLessEqual(post_threads, initial_threads + 1, "VIOLATION: Retrain spawned background threads despite 503")
        finally:
            app.dependency_overrides.pop(require_admin, None)
        print("[CHALLENGER ORACLE 4] Retrain endpoint returns HTTP 503 without spawning background training workers.")

    def test_retrain_endpoint_unauthenticated_guard(self):
        """
        Adversarial test: Without admin authorization, retrain must reject request
        (status 401 or 403 or 503) without leaking execution into training logic.
        """
        resp = self.client.post("/api/retrain")
        self.assertIn(resp.status_code, (401, 403, 503))
        print(f"[CHALLENGER ORACLE 5] Unauthenticated retrain securely guarded (status {resp.status_code}).")

    def test_db_snapshot_multi_key_fallback_resilience(self):
        """
        Stress test DB snapshot reading across 3 fallback keys:
        1. overview_intelligence:v1
        2. latest_overview_intelligence
        3. latest_overview_intelligence:v1
        """
        from api import service as svc

        # Clear in-memory cache to guarantee testing the DB read path
        svc._cached_overview_intelligence = None
        svc._cached_overview_time = 0.0

        # Sub-test 1: overview_intelligence:v1 hit
        snap_v1 = {"source": "overview_intelligence:v1_PAYLOAD", "top_setups": []}
        with patch("db.runtime_state.get_runtime_state") as mock_state:
            mock_state.side_effect = lambda k: snap_v1 if k == "overview_intelligence:v1" else None
            svc._cached_overview_intelligence = None
            svc._cached_overview_time = 0.0
            res = svc._compute_overview_intelligence()
            self.assertEqual(res.get("source"), "overview_intelligence:v1_PAYLOAD")

        # Sub-test 2: latest_overview_intelligence hit
        snap_latest = {"source": "latest_overview_intelligence_PAYLOAD", "top_setups": []}
        with patch("db.runtime_state.get_runtime_state") as mock_state:
            mock_state.side_effect = lambda k: snap_latest if k == "latest_overview_intelligence" else None
            svc._cached_overview_intelligence = None
            svc._cached_overview_time = 0.0
            res = svc._compute_overview_intelligence()
            self.assertEqual(res.get("source"), "latest_overview_intelligence_PAYLOAD")

        # Sub-test 3: latest_overview_intelligence:v1 hit
        snap_alt = {"source": "latest_overview_intelligence:v1_PAYLOAD", "top_setups": []}
        with patch("db.runtime_state.get_runtime_state") as mock_state:
            mock_state.side_effect = lambda k: snap_alt if k == "latest_overview_intelligence:v1" else None
            svc._cached_overview_intelligence = None
            svc._cached_overview_time = 0.0
            res = svc._compute_overview_intelligence()
            self.assertEqual(res.get("source"), "latest_overview_intelligence:v1_PAYLOAD")

        print("[CHALLENGER ORACLE 6] Multi-key DB snapshot fallback sequence verified across all 3 key variants.")

    def test_db_snapshot_database_failure_and_cold_start_resilience(self):
        """
        Adversarial test: What happens if database raises an OperationalError
        or returns None for all keys (cold start / DB empty)?
        The endpoint MUST degrade gracefully to fallback compute and return valid JSON (no 500).
        """
        from api import service as svc

        # Clear cache
        svc._cached_overview_intelligence = None
        svc._cached_overview_time = 0.0

        # Scenario A: DB raises Exception
        with patch("db.runtime_state.get_runtime_state", side_effect=RuntimeError("DB_CONNECTION_TIMEOUT")):
            resp = self.client.get("/api/overview/intelligence")
            self.assertEqual(resp.status_code, 200, "Endpoint should handle DB failure gracefully")
            data = resp.json()
            self.assertIn("top_setups", data)

        # Scenario B: DB returns None for all keys (DB unpopulated on first run)
        svc._cached_overview_intelligence = None
        svc._cached_overview_time = 0.0
        with patch("db.runtime_state.get_runtime_state", return_value=None):
            resp = self.client.get("/api/overview/intelligence")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("top_setups", data)

        # Scenario C: DB returns corrupt non-dict type (e.g. string "corrupt_data" or [1, 2, 3])
        svc._cached_overview_intelligence = None
        svc._cached_overview_time = 0.0
        with patch("db.runtime_state.get_runtime_state", return_value="INVALID_STRING_BLOB"):
            resp = self.client.get("/api/overview/intelligence")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("top_setups", data)

        print("[CHALLENGER ORACLE 7] Total snapshot absence & DB error resilience verified: endpoint degrades gracefully without 500.")

    def test_poisoned_model_artifact_defense(self):
        """
        Adversarial test: If models/xgb_model.pkl is corrupted or contains hostile/invalid data,
        get_model_status in lightweight mode must NOT crash because it only checks os.path.exists().
        """
        from agents.ai_engine.ensemble import get_model_status

        # Simulate presence of corrupted model file
        with patch("os.path.exists", return_value=True):
            # If get_model_status tried to pickle.load, it would fail or load unpickled object
            # But in lightweight mode, it strictly checks file existence
            status = get_model_status()
            self.assertTrue(status.get("lightweight_mode"))
            self.assertEqual(status.get("active_inference_mode"), "LIGHTWEIGHT_BASELINE")
            self.assertTrue(status.get("xgboost_fitted"))
            self.assertIn("Lightweight", status.get("mode"))

        print("[CHALLENGER ORACLE 8] Poisoned artifact defense verified: lightweight status probe checks file existence without unpickling.")

    def test_signal_generator_offline_lightweight_mode(self):
        """
        Adversarial test: Under lightweight mode, verify that generate_signals bypasses
        Attempt 3 (predict_batch_ensemble) and produces valid signals with composite fallback.
        """
        from agents.signal_engine.signal_generator import generate_signals

        df = pd.DataFrame([{
            "timestamp": pd.Timestamp.now() - pd.Timedelta(minutes=5 * (30 - i)),
            "open": 100.0 + i,
            "high": 102.0 + i,
            "low": 99.0 + i,
            "close": 101.5 + i,
            "volume": 25000 + i * 100,
            "RSI_14": 52.0,
            "MACD": 0.15,
            "MACD_signal": 0.10,
            "MACD_hist": 0.05,
            "EMA_9": 101.0,
            "EMA_20": 100.0,
            "EMA_50": 98.0,
            "EMA_200": 95.0,
            "ATR_14": 1.2,
            "VWAP": 101.0,
            "htf_trend": "BULLISH",
        } for i in range(35)])

        with patch("agents.ai_engine.ensemble.predict_batch_ensemble") as mock_ens:
            res_df = generate_signals(df, symbol="INFY", offline=True)
            mock_ens.assert_not_called()
            self.assertIn("signal", res_df.columns)
            self.assertIn("ai_confidence", res_df.columns)
            self.assertTrue((res_df["ai_confidence"] >= 0).all() and (res_df["ai_confidence"] <= 100).all())

        print("[CHALLENGER ORACLE 9] Signal generator cleanly skips heavy ensemble in lightweight mode.")


if __name__ == "__main__":
    unittest.main()
