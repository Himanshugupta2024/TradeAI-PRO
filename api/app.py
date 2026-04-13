"""FastAPI application factory."""
import asyncio
import logging
import os

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from db.connection import init_db

from api.paths import STATIC_DIR
from api import service as svc
from api.routes import router

logger = logging.getLogger(__name__)

# Self-ping interval: every 10 minutes to prevent Render free-tier spin-down.
# Render spins down after 15 min of inactivity; 10 min gives comfortable margin.
_KEEP_ALIVE_INTERVAL = 600


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _keep_alive_task():
    """Periodically ping our own /api/health to prevent Render free-tier spin-down."""
    port = int(os.environ.get("PORT", 10000))
    url = f"http://localhost:{port}/api/health"
    await asyncio.sleep(60)  # Wait for startup to settle before first ping
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(url)
        except Exception as exc:
            logger.debug("Keep-alive ping failed (non-critical): %s", exc)
        await asyncio.sleep(_KEEP_ALIVE_INTERVAL)


def _lifespan_context():
    from contextlib import asynccontextmanager
    from agents.stock_screener import start_morning_scan_task, start_screener_task

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(init_db)
        svc.logger.info("Starting server — market data will load in background...")
        if _env_flag("INITIAL_LOAD_ENABLED", True):
            asyncio.create_task(svc._initial_load())
        else:
            svc.logger.info("INITIAL_LOAD_ENABLED=false -> skipping initial data warm-up task")

        if _env_flag("BACKGROUND_REFRESH_ENABLED", True):
            asyncio.create_task(svc._background_refresh())
        else:
            svc.logger.info("BACKGROUND_REFRESH_ENABLED=false -> skipping periodic background refresh")

        if _env_flag("SCREENER_TASK_ENABLED", True):
            asyncio.create_task(start_screener_task())
        else:
            svc.logger.info("SCREENER_TASK_ENABLED=false -> skipping stock screener loop")

        if _env_flag("MORNING_SCAN_TASK_ENABLED", True):
            asyncio.create_task(start_morning_scan_task())
        else:
            svc.logger.info("MORNING_SCAN_TASK_ENABLED=false -> skipping morning scan scheduler")

        if _env_flag("SELF_PING_ENABLED", False):
            asyncio.create_task(_keep_alive_task())
        else:
            svc.logger.info("SELF_PING_ENABLED=false -> skipping self-ping keep-alive task")

        svc.logger.info("Server ready — accepting connections. Data loading in background.")
        yield

    return lifespan


def create_app() -> FastAPI:
    lifespan = _lifespan_context()
    app = FastAPI(title="AI Trading System — Live", version="4.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    if os.path.exists(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
