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

    public_url = os.environ.get("RENDER_EXTERNAL_URL") or "https://trading-api-n1wt.onrender.com"
    url = f"{public_url.rstrip('/')}/api/health"



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
        is_lightweight = _env_flag("LIGHTWEIGHT_API_MODE", False) or not _env_flag("BACKGROUND_REFRESH_ENABLED", True)

        # Run DB hydration and model warmup in a non-blocking background task so
        # Uvicorn binds the port immediately (<0.1s). This prevents Render free-tier
        # edge routers from timing out with 'hibernate-wake-error (503)'.
        async def _async_warmup_and_hydrate():
            try:
                await asyncio.wait_for(asyncio.to_thread(init_db), timeout=5.0)
            except Exception as exc:
                svc.logger.warning("Database schema initialization warning (non-fatal): %s", exc)

            try:
                await svc._hydrate_swing_cache_from_db()
            except Exception as exc:
                svc.logger.warning("Swing cache hydration raised on startup: %s", exc)

            try:
                await svc._hydrate_signals_cache_from_db()
            except Exception as exc:
                svc.logger.warning("Signals cache hydration raised on startup: %s", exc)

            try:
                from agents.analytics.feedback_loop import backfill_closed_paper_trades
                from db.runtime_state import get_runtime_state
                from db.connection import SessionLocal
                from db.models import TradeOutcome

                def _check_and_backfill():
                    with SessionLocal() as s:
                        count = s.query(TradeOutcome).count()
                    if count == 0:
                        data = get_runtime_state("paper_trading_state:v1")
                        if data and isinstance(data, dict):
                            trades = data.get("trades", [])
                            if trades:
                                res = backfill_closed_paper_trades(trades)
                                svc.logger.info("Auto-backfilled paper trade outcomes on startup: %s", res)
                await asyncio.to_thread(_check_and_backfill)
            except Exception as bf_err:
                svc.logger.debug("Paper trade outcomes startup backfill skipped: %s", bf_err)

            is_lightweight = _env_flag("LIGHTWEIGHT_API_MODE", False) or not _env_flag("BACKGROUND_REFRESH_ENABLED", True)
            if not is_lightweight:
                try:
                    from agents.ai_engine.model_registry import get_model_registry
                    await asyncio.to_thread(get_model_registry().warmup_all)
                    svc._force_free_memory()
                except Exception as exc:
                    svc.logger.warning("ModelRegistry warmup raised on startup: %s", exc)
            else:
                svc.logger.info("LIGHTWEIGHT_API_MODE=true -> skipping ML model warmup to preserve RAM.")

            if not _env_flag("INITIAL_LOAD_ENABLED", True):
                svc._loading_status["ready"] = True
                svc._loading_status["phase"] = "ready_on_demand"
                svc.logger.info("Service initialized in on-demand mode. Readiness probe READY.")

        _spawn_task(_async_warmup_and_hydrate(), name="warmup_and_hydrate")
        _spawn_task(svc.broadcaster.broadcast_loop(), name="snapshot_broadcaster")
        # Enforce stops on bars that arrived while the process was down. Runs
        # regardless of INITIAL_LOAD_ENABLED: a restart must never leave a
        # position sitting past its stop.
        _spawn_task(svc._reconcile_open_positions_on_startup(), name="startup_reconcile")



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



        if not is_lightweight and _env_flag("NIGHTLY_SCAN_ENABLED", False):
            _spawn_task(svc._nightly_swing_scan_background(), name="nightly_swing_scan")
        else:
            svc.logger.info("NIGHTLY_SCAN_ENABLED=false (or lightweight mode) -> skipping nightly SwingSpectrum scan")



        if _env_flag("SELF_PING_ENABLED", True):

            _spawn_task(_keep_alive_task(), name="keep_alive_task")

        else:

            svc.logger.info("SELF_PING_ENABLED=false -> skipping self-ping keep-alive task")

        async def _warm_bar_probe():
            try:
                from agents.market_intelligence.nse_fetcher import fetch_ohlcv
                from db.runtime_state import set_runtime_state
                from api.data_honesty import IST
                from datetime import datetime, timezone
                df = await asyncio.to_thread(fetch_ohlcv, "NIFTY 50", "1d", "5m")
                if df is not None and not df.empty:
                    last_bar = df.iloc[-1]
                    ts = last_bar.get("timestamp") if "timestamp" in last_bar else None
                    if ts is None and hasattr(df, "index") and len(df.index) > 0:
                        ts = df.index[-1]
                    if ts is not None:
                        if isinstance(ts, str):
                            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        elif hasattr(ts, "to_pydatetime"):
                            ts_dt = ts.to_pydatetime()
                        elif isinstance(ts, datetime):
                            ts_dt = ts
                        else:
                            ts_dt = None
                        if ts_dt is not None:
                            if ts_dt.tzinfo is None:
                                ts_dt = ts_dt.replace(tzinfo=IST)
                            set_runtime_state("last_bar_probe:v1", {
                                "last_bar_at": ts_dt.isoformat(),
                                "symbol": "NIFTY 50",
                                "interval": "5m",
                                "written_at": datetime.now(timezone.utc).isoformat(),
                            })
                            svc.logger.info("Warm bar probe recorded last_bar_at: %s", ts_dt.isoformat())
            except Exception as e:
                svc.logger.debug("Warm bar probe non-blocking failure: %s", e)

        _spawn_task(_warm_bar_probe(), name="warm_bar_probe")



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





def _docs_enabled() -> bool:
    """Expose /docs, /redoc and /openapi.json only when explicitly allowed.

    In production the schema enumerates every route and request model, which is a
    free map for an attacker. Enable with ENABLE_API_DOCS=true for a debug session.
    """
    raw = os.getenv("ENABLE_API_DOCS")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return not bool(getattr(settings, "IS_PRODUCTION", False))


def create_app() -> FastAPI:

    settings.validate_runtime_config()

    lifespan = _lifespan_context()

    app = FastAPI(
        title="AI Trading System — Live",
        version="4.1.0",
        lifespan=lifespan,
        docs_url="/docs" if _docs_enabled() else None,
        redoc_url="/redoc" if _docs_enabled() else None,
        openapi_url="/openapi.json" if _docs_enabled() else None,
    )

    cors_origins_raw = os.getenv(

        "CORS_ALLOWED_ORIGINS",

        "http://localhost:8000,http://127.0.0.1:8000,http://localhost:3000,http://localhost:5173,https://tradeai-pro.pages.dev,https://tradeai-pro.tradingpro.workers.dev",

    )

    cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]

    app.add_middleware(

        CORSMiddleware,

        allow_origins=cors_origins or ["http://localhost:8000"],

        allow_credentials=True,

        allow_methods=["*"],

        allow_headers=["*"],

    )

    @app.middleware("http")
    async def add_security_and_cache_headers(request, call_next):
        response = await call_next(request)
        path = request.url.path
        # Enforce strict private, no-store policy on error responses (status >= 400) and sensitive / stateful endpoints
        if (
            response.status_code >= 400
            or any(k in path for k in ("/portfolio", "/trades", "/paper", "/capital", "/journal", "/risk", "/admin", "/orders", "/positions"))
            or request.method != "GET"
        ):
            response.headers["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        # Baseline hardening for JSON responses served directly from Render.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    app.include_router(router)

    if os.path.exists(STATIC_DIR):

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app

