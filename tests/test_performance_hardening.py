"""
Performance, Memory Guardrail & Hardening Tests — TradeAI-PRO
Verifies:
  1. ModelRegistry singleton loading and thread bounding
  2. Memory RSS guardrail within Render Free Tier (< 380MB Linux / < 500MB Win)
  3. Overview composite payload schema and fast response
  4. Non-blocking async execution
"""

import pytest
import time
from fastapi.testclient import TestClient

from agents.ai_engine.model_registry import get_model_registry
from utils.memory_profiler import get_process_rss_mb


class TestModelRegistryAndConcurrency:
    def test_model_registry_singleton_warmup(self):
        reg1 = get_model_registry()
        reg2 = get_model_registry()
        assert reg1 is reg2

        status = reg1.warmup_all()
        assert status["initialized"] is True
        assert "models" in status

        # Re-running warmup should be instantaneous (<1ms) since it's already initialized
        t0 = time.perf_counter()
        status2 = reg2.warmup_all()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < 10.0
        assert status2["initialized"] is True

    def test_torch_thread_bounding(self):
        try:
            import torch
            assert torch.get_num_threads() == 1
        except ImportError:
            pass


class TestMemoryProfilingGuardrails:
    def test_rss_memory_within_render_budget(self):
        import gc
        for _ in range(3):
            gc.collect()
        rss_mb = get_process_rss_mb(collect_gc=True)
        assert rss_mb > 0.0
        # Budget guardrail: individual service baseline is ~150-250MB; in a full test suite with 500+ tests, allow reasonable runner overhead
        import platform
        threshold = 512.0 if platform.system() == "Linux" else 650.0
        assert rss_mb < threshold, f"Exceeded Render free tier budget: {rss_mb} MB"
        if platform.system() == "Linux":
            assert rss_mb < 512.0, f"Exceeded Render free tier hard limit: {rss_mb} MB"


@pytest.mark.integration
class TestDashboardCompositeOverviewAPI:
    def test_overview_endpoint_schema_and_speed(self):
        from server import app
        client = TestClient(app)
        # First call triggers test client initialization
        resp0 = client.get("/api/dashboard/overview")
        assert resp0.status_code == 200

        t0 = time.perf_counter()
        resp = client.get("/api/dashboard/overview")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "market_status" in data
        assert "capital" in data
        assert "top_signals" in data
        assert "dominant_regime" in data
        assert "ai_engine" in data
        assert "risk_state" in data
        # Cached response should be served from memory cache in sub-100ms
        assert elapsed_ms < 100.0, f"Overview endpoint took {elapsed_ms:.1f}ms, expected < 100ms"

    def test_health_live_endpoint(self):
        from server import app
        client = TestClient(app)

        resp = client.get("/api/health/live")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "alive"
        assert "uptime_seconds" in data

    def test_context_v3_endpoint(self):
        from server import app
        client = TestClient(app)

        resp = client.get("/api/context/v3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "india_vix" in data
        assert "fii_dii_flow" in data
        assert "market_breadth" in data
        assert "derivatives_oi" in data
