"""
FastAPI Application — Trading Dashboard Backend.

Serves API endpoints for signals, trades, market data, dashboard metrics,
portfolio view, performance stats, and regime classification.
Also serves the static frontend dashboard.

Implementation is split under `api/` (service state, routes, app factory).
"""

import os
import sys
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)

_startup_error: str | None = None

try:
    from api.app import create_app
    from api import service as _service

    app = create_app()

    # Mutable aliases for tests (e.g. test_market_mood) that patch server module state
    _movers_cache = _service._movers_cache
    _signal_cache = _service._signal_cache
    _regime_cache = _service._regime_cache
    _compute_market_mood = _service._compute_market_mood
    _build_ai_market_summary = _service._build_ai_market_summary
except Exception as exc:
    _startup_error = f"{exc.__class__.__name__}: {exc}"
    logger.exception("Primary app bootstrap failed; starting degraded fallback app")

    app = FastAPI(title="Trading API (Degraded)", version="degraded-fallback")

    _movers_cache = {}
    _signal_cache = {}
    _regime_cache = {}

    def _compute_market_mood():
        return {"mood": "UNKNOWN", "score": 50}

    def _build_ai_market_summary(_payload):
        return "Service is in degraded mode. Check server logs for startup errors."

    @app.get("/api/health")
    async def degraded_health_check():
        return {
            "status": "degraded",
            "error": _startup_error,
            "message": "Primary app failed to boot; fallback app is serving health only.",
        }

    @app.get("/")
    async def degraded_root():
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": _startup_error,
                "message": "Service startup failed. Check Render logs and fix the primary bootstrap error.",
            },
        )

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
