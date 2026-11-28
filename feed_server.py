"""
TradeAI Pro — Dedicated Market Data Feed Service (Box C).
Serves real-time LTP snapshots and 5-minute rolling bars via lightweight HTTP API.
Runs completely isolated from Core API (Box A) to eliminate Yahoo latency and memory bloat.
"""
from __future__ import annotations

import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel

# Load local environment if present
load_dotenv()

from feed.bar_builder import BarBuilder
from feed.fno_analytics import FNOAnalyticsEngine
from feed.nfo_master import NFOMaster
from feed.shoonya_client import ShoonyaFeedManager
from feed.token_master import TokenMaster

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("feed_server")

# Global feed components
bar_builder = BarBuilder()
token_master = TokenMaster()
nfo_master = NFOMaster()
fno_engine = FNOAnalyticsEngine()
feed_manager: Optional[ShoonyaFeedManager] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global feed_manager
    logger.info("Starting TradeAI Market Data Feed Service (Box C)...")

    # Initial token master + NFO master refresh (non-blocking fallback)
    try:
        token_master.refresh_from_shoonya(timeout=5.0)
    except Exception as exc:
        logger.warning("Token master refresh deferred: %s", exc)

    def _refresh_nfo_bg():
        try:
            nfo_master.refresh_from_shoonya(timeout=10.0)
        except Exception as exc:
            logger.warning("NFO master refresh deferred: %s", exc)

    threading.Thread(target=_refresh_nfo_bg, daemon=True, name="NFOMasterRefresh").start()

    # Initialize and start feed manager
    feed_manager = ShoonyaFeedManager(
        bar_builder=bar_builder,
        token_master=token_master,
        nfo_master=nfo_master,
        fno_engine=fno_engine,
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
        "nfo_underlyings_indexed": len(nfo_master.contracts),
        "fno_live_chains": len(fno_engine.chains),
    }


def _verify_auth(x_internal_token: Optional[str]) -> None:
    """Enforce fail-closed internal token security contract."""
    expected_token = os.environ.get("FEED_INTERNAL_TOKEN", "").strip()
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feed auth misconfigured: FEED_INTERNAL_TOKEN environment variable is not set",
        )
    if not x_internal_token or not hmac.compare_digest(x_internal_token, expected_token):
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


@app.get("/fno/analytics")
def get_fno_analytics(
    symbol: str = Query(..., description="Underlying symbol, e.g. RELIANCE, NIFTY 50"),
    spot: float = Query(0.0, description="Current spot price"),
    days_to_expiry: float = Query(5.0, ge=0.5, le=90.0),
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
) -> Dict[str, Any]:
    """
    Return real-time options analytics: OI walls, PCR, ATM straddle implied move,
    and options reachability envelope.
    """
    _verify_auth(x_internal_token)
    sym = symbol.strip().upper().replace(".NS", "")
    resolved_spot = spot
    if resolved_spot <= 0:
        quote = bar_builder.get_snapshot([sym])
        if quote and "quotes" in quote and sym in quote["quotes"]:
            resolved_spot = float(quote["quotes"][sym].get("lp", 0.0))

    age_sec = fno_engine.get_chain_age_seconds(sym)
    if feed_manager and not feed_manager.is_simulated and (age_sec is None or age_sec > 90.0):
        feed_manager.refresh_fno_underlying(sym, spot=resolved_spot, count=10)

    return fno_engine.compute_symbol_analytics(
        symbol=sym,
        spot=resolved_spot,
        days_to_expiry=days_to_expiry,
    )


@app.get("/fno/underlyings")
def list_fno_underlyings(
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
) -> Dict[str, Any]:
    """Return full list of F&O underlyings indexed by NFOMaster."""
    _verify_auth(x_internal_token)
    priority_display = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
    items = [priority_display.get(u, u) for u in nfo_master.list_underlyings()]
    return {
        "underlyings": items,
        "count": len(items),
        "live_chains": list(fno_engine.chains.keys()),
    }


@app.post("/fno/update-chain")
def update_fno_chain(
    symbol: str = Query(..., description="Underlying symbol"),
    strikes_data: List[Dict[str, Any]] = ...,
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
) -> Dict[str, Any]:
    """Update strike-indexed option quotes in memory on Box C."""
    _verify_auth(x_internal_token)
    sym = symbol.strip().upper().replace(".NS", "")
    chain = fno_engine.get_or_create_chain(sym)
    updated = 0
    for row in strikes_data:
        strike = float(row.get("strike", 0.0))
        if strike <= 0:
            continue
        if "ce" in row and isinstance(row["ce"], dict):
            ce = row["ce"]
            chain.update_by_strike(
                strike=strike,
                opt_type="ce",
                ltp=ce.get("ltp"),
                oi=ce.get("oi"),
                volume=ce.get("volume"),
            )
        if "pe" in row and isinstance(row["pe"], dict):
            pe = row["pe"]
            chain.update_by_strike(
                strike=strike,
                opt_type="pe",
                ltp=pe.get("ltp"),
                oi=pe.get("oi"),
                volume=pe.get("volume"),
            )
        updated += 1
    if updated > 0:
        fno_engine.mark_updated(sym)
    return {"status": "ok", "symbol": sym, "strikes_updated": updated}


class ShoonyaSessionLoginRequest(BaseModel):
    userid: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None
    vendor_code: Optional[str] = None
    totp: Optional[str] = None
    usertoken: Optional[str] = None


def _session_status_payload() -> Dict[str, Any]:
    stats = bar_builder.get_stats()
    feed_state = "OFFLINE"
    if feed_manager:
        if feed_manager.is_simulated:
            feed_state = "SIMULATED"
        elif feed_manager.is_connected:
            feed_state = "LIVE"
        elif feed_manager.last_login_time is not None:
            feed_state = "CONNECTING"
    return {
        "status": "ok",
        "feed_source": "SHOONYA" if feed_state == "LIVE" else "SIMULATED",
        "feed_state": feed_state,
        "has_credentials": bool(feed_manager and feed_manager.has_credentials()),
        "masked_uid": feed_manager.masked_uid() if feed_manager else "",
        "watchlist_count": len(feed_manager.watchlist) if feed_manager else 0,
        "symbols_tracked": stats["symbols_tracked"],
        "total_ticks": stats["total_ticks"],
        "last_tick_time": stats["last_tick_time"],
        "last_error": feed_manager.last_error if feed_manager else None,
    }


@app.get("/session/status")
def session_status(
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
) -> Dict[str, Any]:
    """Return Shoonya feed authentication/connection state without exposing secrets."""
    _verify_auth(x_internal_token)
    return _session_status_payload()


@app.post("/session/login")
def session_login(
    req: ShoonyaSessionLoginRequest,
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
) -> Dict[str, Any]:
    """Authenticate Box C with operator-provided Shoonya credentials/OTP and start live WS."""
    _verify_auth(x_internal_token)
    if not feed_manager:
        raise HTTPException(status_code=503, detail="Feed manager not initialized")

    ok, msg = feed_manager.login_interactive(
        userid=req.userid or "",
        password=req.password or "",
        api_key=req.api_key or "",
        vendor_code=req.vendor_code or "",
        totp=req.totp or "",
        usertoken=req.usertoken or "",
    )
    if not ok:
        raise HTTPException(status_code=401, detail=msg)
    return {
        **_session_status_payload(),
        "feed_state": "LIVE",
        "feed_source": "SHOONYA",
        "message": msg,
    }


@app.get("/oauth/callback")
@app.post("/oauth/callback")
def oauth_callback(
    code: Optional[str] = Query(None),
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
) -> Dict[str, Any]:
    """
    OAuth redirect callback endpoint.
    Receives auth_code from Shoonya browser login redirect, exchanges it for susertoken,
    and initiates the live WebSocket feed. Protected by fail-closed X-Internal-Token auth.
    """
    _verify_auth(x_internal_token)
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code parameter 'code'")

    if not feed_manager:
        raise HTTPException(status_code=503, detail="Feed manager not initialized")

    token = feed_manager.exchange_auth_code(code)
    if not token:
        raise HTTPException(status_code=400, detail="Failed to exchange authorization code with Shoonya")

    success = feed_manager.login_with_token(token)
    if success:
        feed_manager._start_websocket()
        return {
            "status": "ok",
            "message": "Shoonya OAuth authentication successful! Live WebSocket connected.",
            "token_acquired": True,
        }
    raise HTTPException(status_code=500, detail="Failed to start WebSocket session with token")

