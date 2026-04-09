"""FastAPI application factory."""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from db.connection import init_db

from api.paths import STATIC_DIR
from api import service as svc
from api.routes import router


def _lifespan_context():
    from contextlib import asynccontextmanager
    import asyncio
    from agents.stock_screener import start_morning_scan_task, start_screener_task

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        svc.logger.info("Starting server — market data will load in background...")
        asyncio.create_task(svc._initial_load())
        asyncio.create_task(svc._background_refresh())
        asyncio.create_task(start_screener_task())
        asyncio.create_task(start_morning_scan_task())
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
