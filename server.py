"""
FastAPI Application — Trading Dashboard Backend.

Serves API endpoints for signals, trades, market data, dashboard metrics,
portfolio view, performance stats, and regime classification.
Also serves the static frontend dashboard.

Implementation is split under `api/` (service state, routes, app factory).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.app import create_app
from api import service as _service

app = create_app()

# Mutable aliases for tests (e.g. test_market_mood) that patch server module state
_movers_cache = _service._movers_cache
_signal_cache = _service._signal_cache
_regime_cache = _service._regime_cache
_compute_market_mood = _service._compute_market_mood
_build_ai_market_summary = _service._build_ai_market_summary

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
