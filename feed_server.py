"""
TradeAI Pro — Dedicated Market Data Feed Service (Box C).
Serves real-time LTP snapshots and 5-minute rolling bars via lightweight HTTP API.
Runs completely isolated from Core API (Box A) to eliminate Yahoo latency and memory bloat.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, status

from feed.bar_builder import BarBuilder
from feed.shoonya_client import ShoonyaFeedManager
from feed.token_master import TokenMaster

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("feed_server")

# Global feed components
bar_builder = BarBuilder()
token_master = TokenMaster()
feed_manager: Optional[ShoonyaFeedManager] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global feed_manager
    logger.info("Starting TradeAI Market Data Feed Service (Box C)...")

    # Initial token master refresh (non-blocking fallback)
    try:
        token_master.refresh_from_shoonya(timeout=5.0)
    except Exception as exc:
        logger.warning("Token master refresh deferred: %s", exc)

    # Initialize and start feed manager
    feed_manager = ShoonyaFeedManager(
        bar_builder=bar_builder,
        token_master=token_master,
    )
    feed_manager.start()

    yield

    logger.info("Shutting down TradeAI Market Data Feed Service...")
    if feed_manager:
        feed_manager.stop()


app = FastAPI(
    title="TradeAI Feed Service",
    version="1.0.0",
    description="Dedicated microservice for real-time market data feed and rolling 5m bars",
    lifespan=lifespan,
)


@app.get("/health/live")
def health_live() -> Dict[str, str]:
    """
    Ultra-lightweight liveness probe for Render health check (< 1ms, zero CPU).
    Never touches token master, websocket client, or database.
    """
    return {"status": "alive"}


@app.get("/health")
def health() -> Dict[str, Any]:
    """Rich telemetry endpoint reporting memory RSS and feed health."""
    rss_mb = 0.0
    try:
        import psutil
        proc = psutil.Process()
        rss_mb = round(proc.memory_info().rss / 1024 / 1024, 2)
    except Exception:
        pass

    feed_state = "OFFLINE"
    if feed_manager:
        if feed_manager.is_simulated:
            feed_state = "SIMULATED"
        elif feed_manager.is_connected:
            feed_state = "LIVE"
        else:
            feed_state = "CONNECTING"

    stats = bar_builder.get_stats()

    return {
        "status": "ok",
        "service": "trading-feed",
        "feed_source": "SHOONYA" if feed_state == "LIVE" else "SIMULATED",
        "feed_state": feed_state,
        "memory_rss_mb": rss_mb,
        "symbols_tracked": stats["symbols_tracked"],
        "total_ticks": stats["total_ticks"],
        "last_tick_time": stats["last_tick_time"],
        "token_master_symbols": len(token_master.symbol_to_token),
    }


def _verify_auth(x_internal_token: Optional[str]) -> None:
    """Enforce fail-closed internal token security contract."""
    expected_token = os.environ.get("FEED_INTERNAL_TOKEN", "").strip()
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feed auth misconfigured: FEED_INTERNAL_TOKEN environment variable is not set",
        )
    if x_internal_token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid internal authorization token",
        )


@app.get("/snapshot")
def get_snapshot(
    symbols: str = Query(..., description="Comma-separated symbols, e.g. RELIANCE,TCS,NIFTY 50"),
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
) -> Dict[str, Any]:
    """
    Return real-time LTP quote snapshots and rolling 5-minute bars for requested symbols.
    """
    _verify_auth(x_internal_token)

    wanted = [s.strip() for s in symbols.split(",") if s.strip()]
    if not wanted:
        raise HTTPException(status_code=400, detail="symbols parameter is empty")

    snap = bar_builder.get_snapshot(wanted)
    if feed_manager and feed_manager.is_simulated:
        snap["source"] = "SHOONYA_SIMULATED"
    return snap


@app.get("/bars")
def get_bars(
    symbol: str = Query(..., description="Trading symbol, e.g. RELIANCE"),
    limit: int = Query(120, ge=1, le=240),
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
) -> Dict[str, Any]:
    """Return historical 5-minute bars for a specific symbol."""
    _verify_auth(x_internal_token)

    sym = symbol.strip().upper()
    bars = bar_builder.get_bars(sym, limit=limit)
    return {
        "symbol": sym,
        "timeframe": "5m",
        "count": len(bars),
        "bars": bars,
    }
