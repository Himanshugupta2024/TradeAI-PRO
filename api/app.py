"""FastAPI application factory with structured background task lifecycle management."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import warnings

# Suppress known 3rd-party deprecation warnings (e.g. yfinance with pandas 3.x / Pandas4Warning) in production
warnings.filterwarnings("ignore", module=r"yfinance(\..*)?")
warnings.filterwarnings("ignore", message=r".*The inplace keyword.*")
warnings.filterwarnings("ignore", message=r".*mode\.copy_on_write.*")
warnings.filterwarnings("ignore", message=r".*Timestamp\.utcnow is deprecated.*")
warnings.filterwarnings("ignore", message=r".*If you are loading a serialized model.*", category=UserWarning, module=r"xgboost.*")
try:
    import pandas.errors as _pde
    for _w_name in ("Pandas4Warning", "PandasDeprecationWarning", "PandasChangeWarning", "PandasFutureWarning"):
        if hasattr(_pde, _w_name):
            warnings.filterwarnings("ignore", category=getattr(_pde, _w_name))
except Exception:
    pass

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings
from db.connection import init_db

from api.paths import STATIC_DIR
from api import service as svc
from api.routes import router

logger = logging.getLogger(__name__)

# Self-ping interval: every 10 minutes to prevent Render free-tier spin-down.
_KEEP_ALIVE_INTERVAL = 600

# Retain strong references to background tasks to prevent premature garbage collection
# and to support clean cancellation and graceful shutdown.
_background_tasks: set[asyncio.Task] = set()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        if ("pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST")) and name in (
            "INITIAL_LOAD_ENABLED",
            "BACKGROUND_REFRESH_ENABLED",
            "SWING_OUTCOME_TASK_ENABLED",
        ):
            return False
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _spawn_task(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _keep_alive_task():
    """Periodically ping public URL or localhost fallback to prevent Render free-tier spin-down."""
    public_url = os.environ.get("RENDER_EXTERNAL_URL")
    if public_url:
        url = f"{public_url.rstrip('/')}/api/health"
    else:
        port = int(os.environ.get("PORT", 10000))
        url = f"http://localhost:{port}/api/health"

    try:
        await asyncio.sleep(30)  # Wait for startup to settle before first ping
        while True:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.get(url)
            except Exception as exc:
                logger.debug("Keep-alive ping failed (non-critical): %s", exc)
            await asyncio.sleep(_KEEP_ALIVE_INTERVAL)
    except asyncio.CancelledError:
        logger.debug("Keep-alive ping task cancelled.")


def _lifespan_context():
    from contextlib import asynccontextmanager
    from agents.stock_screener import start_morning_scan_task, start_screener_task

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc._SERVER_START_TIME = time.time()
        try:
            await asyncio.wait_for(asyncio.to_thread(init_db), timeout=5.0)
        except Exception as exc:
            svc.logger.warning("Database schema initialization warning (non-fatal): %s", exc)

        # Hydrate the in-memory swing cache from the latest persisted scan so
        # the dashboard shows yesterday's shortlist immediately. Non-fatal.
        try:
            await svc._hydrate_swing_cache_from_db()
        except Exception as exc:
            svc.logger.warning("Swing cache hydration raised on startup: %s", exc)

        # Hydrate the latest signals snapshot from DB so /api/signals/all is instant on boot.
        try:
            await svc._hydrate_signals_cache_from_db()
        except Exception as exc:
            svc.logger.warning("Signals cache hydration raised on startup: %s", exc)

        # Warmup all ML models once at startup via ModelRegistry off the event loop
        try:
            from agents.ai_engine.model_registry import get_model_registry
            await asyncio.to_thread(get_model_registry().warmup_all)
        except Exception as exc:
            svc.logger.warning("ModelRegistry warmup raised on startup: %s", exc)

        svc.logger.info("Starting server — market data will load in background...")
        if _env_flag("INITIAL_LOAD_ENABLED", True):
            _spawn_task(svc._initial_load(), name="initial_load")
        else:
            svc.logger.info("INITIAL_LOAD_ENABLED=false -> skipping initial data warm-up task")

        background_refresh_enabled = _env_flag("BACKGROUND_REFRESH_ENABLED", True)
        if background_refresh_enabled:
            _spawn_task(svc._background_refresh(), name="background_refresh")
        else:
            svc.logger.info("BACKGROUND_REFRESH_ENABLED=false -> skipping periodic background refresh")

        outcome_task_enabled = _env_flag("SWING_OUTCOME_TASK_ENABLED", True) and bool(
            getattr(settings, "SWING_OUTCOME_EVAL_ENABLED", True)
        )
        if background_refresh_enabled and outcome_task_enabled:
            _spawn_task(svc._swing_outcome_background(), name="swing_outcome_background")
        else:
            svc.logger.info(
                "Swing outcome task disabled (background=%s, outcome_enabled=%s)",
                background_refresh_enabled,
                outcome_task_enabled,
            )

        if _env_flag("SCREENER_TASK_ENABLED", False):
            _spawn_task(start_screener_task(), name="stock_screener_task")
        else:
            svc.logger.info("SCREENER_TASK_ENABLED=false -> skipping stock screener loop")

        if _env_flag("MORNING_SCAN_TASK_ENABLED", False):
            _spawn_task(start_morning_scan_task(), name="morning_scan_task")
        else:
            svc.logger.info("MORNING_SCAN_TASK_ENABLED=false -> skipping morning scan scheduler")

        if _env_flag("NIGHTLY_SCAN_ENABLED", True):
            _spawn_task(svc._nightly_swing_scan_background(), name="nightly_swing_scan")
        else:
            svc.logger.info("NIGHTLY_SCAN_ENABLED=false -> skipping nightly SwingSpectrum scan")

        if _env_flag("SELF_PING_ENABLED", False):
            _spawn_task(_keep_alive_task(), name="keep_alive_task")
        else:
            svc.logger.info("SELF_PING_ENABLED=false -> skipping self-ping keep-alive task")

        svc.logger.info("Server ready — accepting connections. Data loading in background.")
        try:
            yield
        finally:
            svc.logger.info("Shutting down: cancelling %d active background task(s)...", len(_background_tasks))
            for task in list(_background_tasks):
                task.cancel()
            if _background_tasks:
                await asyncio.gather(*_background_tasks, return_exceptions=True)
            svc.logger.info("All background tasks gracefully terminated.")

    return lifespan


def create_app() -> FastAPI:
    settings.validate_runtime_config()
    lifespan = _lifespan_context()
    app = FastAPI(title="AI Trading System — Live", version="4.1.0", lifespan=lifespan)
    cors_origins_raw = os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000,http://localhost:3000,http://localhost:5173",
    )
    cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["http://localhost:8000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    if os.path.exists(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
