"""Shared state, background tasks, and domain helpers for the trading API."""

import asyncio
import json
import logging
import os
import sys
import time

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT_DIR)

from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from fastapi import WebSocket

from agents.ai_engine.ensemble import get_dynamic_weight_state
from agents.ai_engine.fallback_predictor import predict_batch
from agents.backtester.engine import Backtester
from agents.execution.paper_trading import PaperTradingEngine
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.mtf_scorer import calculate_mtf_score
from agents.feature_engine.regime_detector import MarketRegime, detect_regime
from agents.feature_engine.trend import classify_trend, propagate_trend_to_ltf
from agents.market_intelligence.breakout_scanner import (
    scan_all_symbols as breakout_scan_all,
)
from agents.market_intelligence.index_mover import (
    calculate_index_movers,
)
from agents.market_intelligence.market_data import fetch_fii_dii_flow
from agents.market_intelligence.swing_scanner import (
    scan_all_symbols as swing_scan_all,
)
from agents.signal_engine.signal_generator import generate_signals
from config import settings

try:
    from agents.alerts.telegram_bot import send_signal_alert as _tg_signal_alert
except ImportError:
    _tg_signal_alert = None  # type: ignore

# Signal win rate tracker + new Telegram alerts (non-blocking, safe)
try:
    from agents.analytics.signal_tracker import log_signal, score_outcomes
    from agents.notifications.telegram_alerts import send_signal_alert as _tg_new_alert
    from agents.notifications.telegram_alerts import send_whale_alert

    _TRACKER_ENABLED = True
except ImportError:
    log_signal = score_outcomes = _tg_new_alert = send_whale_alert = None  # type: ignore
    _TRACKER_ENABLED = False

# Swing scan persistence (SQLite via SQLAlchemy). Import defensively so a DB
# failure never prevents the service from booting — all call sites wrap these
# in try/except and degrade gracefully when the store is unavailable.
try:
    from db import swing_store as _swing_store

    _SWING_STORE_ENABLED = True
except Exception as _swing_store_exc:  # pragma: no cover — import-time safety net
    _swing_store = None  # type: ignore[assignment]
    _SWING_STORE_ENABLED = False
    logging.getLogger(__name__).warning(
        "db.swing_store unavailable — swing persistence disabled: %s",
        _swing_store_exc,
    )

from agents.analytics.trade_journal import get_journal
from agents.data_ingestion.context_fetcher import build_context_fetcher
from agents.data_ingestion.validator import validate as validate_ohlcv
from agents.market_intelligence.nse_fetcher import (
    NSE_SYMBOLS,
    fetch_arbitrary_symbol,
    fetch_nse_gainers_losers,
    fetch_ohlcv,
    fetch_sector_performance,
    search_symbols,
    validate_nse_symbol,
)
from agents.stock_screener import (
    opportunity_stocks,
    start_morning_scan_task,
    start_screener_task,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_SERVER_START_TIME = time.time()

# ── Real data stores ──────────────────────────────────────
_market_data: dict[str, pd.DataFrame] = {}
_signal_cache: dict[str, pd.DataFrame] = {}
_regime_cache: dict[str, str] = {}
_last_refresh: datetime = datetime.min

# Daily data cache for swing scanner & breakout scanner
_daily_data_cache: dict[str, pd.DataFrame] = {}

# Pre-computed swing scanner results (updated during background refresh)
_swing_cache: dict = {
    "setups": [],
    "total_found": 0,
    "symbols_scanned": 0,
    "real_data": False,
    "updated_at": None,
    "market_context": None,
}

# Prevents concurrent refresh cycles from producing inconsistent partial snapshots
_refresh_lock: asyncio.Lock | None = None

# Guard: track the last calendar date on which swing scan results were persisted
# to DB. Periodic refreshes (every 60s) must not create a new SwingScan row on
# every cycle — one save per day is enough. Manual / nightly triggers always
# bypass this guard via trigger="manual" or trigger="nightly_*".
_last_swing_persist_date: date | None = None


def _get_refresh_lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


def _get_providers_telemetry() -> dict:
    """Return health and latency telemetry from MarketDataRouter and providers."""
    try:
        from data.router import get_default_router
        status = get_default_router().get_health_status()
        try:
            from data.providers.gdelt import get_gdelt_provider
            gdelt = get_gdelt_provider()
            status["gdelt"] = {
                "active": True,
                "cached_symbols": len(gdelt._cache),
            }
        except Exception:
            pass
        return status
    except Exception as exc:
        logger.debug("Failed to get providers telemetry: %s", exc)
        return {}


# Paper trading engine (loads real trade data from paper_trades.json)
_engine: PaperTradingEngine | None = None

# WebSocket clients for real-time updates
_ws_clients: list[WebSocket] = []

# ── Symbols to track ─────────────────────────────────────
# Priority symbols loaded first (indices + top stocks), rest loaded progressively
_ALL_NSE_SYMBOLS = list(NSE_SYMBOLS.keys())
NSE_SYMBOL_LIMIT = max(1, int(os.getenv("NSE_SYMBOL_LIMIT", "20")))
_TRACKED_NSE_SYMBOLS = _ALL_NSE_SYMBOLS[:NSE_SYMBOL_LIMIT]
_PRIORITY_COUNT = min(15, len(_TRACKED_NSE_SYMBOLS))
_PRIORITY_SYMBOLS = _TRACKED_NSE_SYMBOLS[:_PRIORITY_COUNT]  # Fast initial load
_REMAINING_SYMBOLS = _TRACKED_NSE_SYMBOLS[
    _PRIORITY_COUNT:
]  # Loaded in background after priority
TOP_NSE_SYMBOLS = _TRACKED_NSE_SYMBOLS

# Track loading progress for health endpoint
_loading_status = {
    "phase": "starting",
    "loaded": 0,
    "total": len(TOP_NSE_SYMBOLS),
    "ready": False,
}

# Previous signal state per symbol — used to detect NEW signals for Telegram alerts
_prev_signal_cache: dict[str, str] = {}

# Data quality scores per symbol (updated each refresh cycle)
_data_quality_cache: dict[str, float] = {}

# Feed heartbeat status for dashboard/API consumers.
_last_feed_update: datetime = datetime.min
# Throttle SwingSpectrum SQLite outcome evaluation (yFinance budget).
_last_swing_outcome_eval_monotonic: float = 0.0

# Precomputed snapshots for fast dashboard responses
_movers_cache: dict[str, object] = {"gainers": [], "losers": [], "updated_at": None}
_market_mood_cache: dict[str, object] = {
    "score": 50,
    "mood": "NEUTRAL",
    "updated_at": None,
}
_lazy_bootstrap_started = False
# Fast-boot snapshot for instant dashboard delivery before first refresh cycle completes
_persisted_signals_snapshot: list[dict] = []
_persisted_signals_updated_at: str | None = None

# Regime hysteresis: (confirmed_regime, tentative_regime, consecutive_count)
_regime_hysteresis: dict[str, tuple[str, str, int]] = {}
REGIME_HYSTERESIS_MIN = 3  # Must see new regime N times before switching
MAX_NSE_CONCURRENCY = int(os.getenv("MAX_NSE_CONCURRENCY", "3"))
SYMBOL_FETCH_TIMEOUT_SECONDS = float(
    getattr(settings, "NSE_SYMBOL_TIMEOUT_SECONDS", 150.0)
)
REFRESH_INTERVAL_SECONDS = max(
    10, int(getattr(settings, "REFRESH_INTERVAL_SEC", 60) or 60)
)
REFRESH_CYCLE_TIMEOUT_SECONDS = max(
    30, int(getattr(settings, "REFRESH_CYCLE_TIMEOUT_SEC", 120) or 120)
)
BACKGROUND_READY_WAIT_SECONDS = max(
    30, int(getattr(settings, "BACKGROUND_READY_WAIT_SEC", 180) or 180)
)
SWING_OUTCOME_TASK_INTERVAL_SECONDS = max(
    30,
    int(getattr(settings, "SWING_OUTCOME_TASK_CHECK_SEC", 60) or 60),
)


def _force_free_memory() -> None:
    """Force Python gc and Linux glibc malloc_trim to return unmapped heap pages to OS."""
    try:
        import gc
        gc.collect()
        if hasattr(sys, "platform") and sys.platform.startswith("linux"):
            import ctypes
            try:
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except Exception:
                pass
    except Exception:
        pass

# ── Phase 3 / 4: delta tracking + opportunity timeline ───────────────────────
# Snapshot of last intelligence payload for delta computation
_last_intelligence_snapshot: dict = {
    "signals": {},  # symbol → {signal, confidence, quality_score}
    "mood_score": None,
    "captured_at": None,
}
# Phase 4: first-seen timestamps for actionable setups (symbol → ISO timestamp)
_setup_first_seen: dict[str, str] = {}


def _build_swing_symbol_universe(default_symbols: list[str]) -> list[str]:
    """Return validated swing scan universe with required symbols always included."""
    watchlist = list(getattr(settings, "SWING_WATCHLIST", []))
    must_include = ["OLAELEC", "COALINDIA", "PRECWIRE", "TATASTEEL"]
    merged = list(dict.fromkeys([*watchlist, *must_include]))
    valid = [s for s in merged if s in NSE_SYMBOLS]
    return valid or default_symbols


def _build_swing_market_context() -> dict[str, object]:
    """
    Benchmark trend/regime for swing LONG/SHORT gating.
    Uses the same cache keys as the live 5m pipeline (htf_trend on the index).
    """
    ix = getattr(settings, "SWING_INDEX_SYMBOL", "NIFTY 50") or "NIFTY 50"
    regime = str(_regime_cache.get(ix) or "").upper() or "UNKNOWN"
    trend = "NEUTRAL"
    choppy_threshold = float(
        getattr(settings, "SWING_CHOPPY_ATR_RATIO_THRESHOLD", 0.7) or 0.7
    )
    atr_ratio: float | None = None
    atr_today: float | None = None
    atr_20d_avg: float | None = None
    atr_source = "none"

    df = _signal_cache.get(ix)
    if df is None:
        df = _market_data.get(ix)
    if df is not None and not df.empty:
        try:
            htf = str(df.iloc[-1].get("htf_trend", "") or "").upper()
            if "BULLISH" in htf:
                trend = "BULLISH"
            elif "BEARISH" in htf:
                trend = "BEARISH"
        except Exception:
            pass

    # Detect low-volatility/choppy benchmark sessions via ATR compression.
    atr_df = _daily_data_cache.get(ix)
    if atr_df is not None and not atr_df.empty:
        atr_source = "daily_cache"
    else:
        atr_df = df
        atr_source = "signal_cache"

    if atr_df is not None and not atr_df.empty and "ATR_14" in atr_df.columns:
        try:
            atr_series = pd.to_numeric(atr_df["ATR_14"], errors="coerce").dropna()
            if len(atr_series) >= 5:
                atr_today = float(atr_series.iloc[-1])
                atr_20d_avg = float(atr_series.tail(min(20, len(atr_series))).mean())
                if atr_20d_avg > 0:
                    atr_ratio = atr_today / atr_20d_avg
                    if atr_ratio < choppy_threshold:
                        trend = "CHOPPY"
        except Exception:
            pass

    return {
        "index_symbol": ix,
        "index_regime": regime,
        "index_trend": trend,
        "index_atr_ratio": round(atr_ratio, 4) if atr_ratio is not None else None,
        "index_atr_today": round(atr_today, 4) if atr_today is not None else None,
        "index_atr_20d_avg": round(atr_20d_avg, 4) if atr_20d_avg is not None else None,
        "index_choppy_atr_threshold": choppy_threshold,
        "index_atr_source": atr_source,
    }


async def _persist_swing_scan(
    setups: list[dict],
    market_context: dict | None,
    *,
    symbols_scanned: int,
    trigger: str,
) -> int | None:
    """Save a scan snapshot to SQLite without blocking the event loop.

    Always returns the scan_id on success, or None on any failure. Errors are
    logged at WARNING level but never propagated — persistence is best-effort
    and must never take down the live refresh loop.

    Once-per-day guard: periodic refreshes (trigger="periodic") will only
    write a new SwingScan row once per calendar day. Manual and nightly
    triggers always persist immediately, regardless of the guard.
    """
    global _last_swing_persist_date

    if not _SWING_STORE_ENABLED or _swing_store is None:
        return None

    today = date.today()
    _periodic_triggers = {"periodic"}  # triggers subject to the once-per-day gate

    if trigger in _periodic_triggers and _last_swing_persist_date == today:
        logger.debug(
            "Swing persist skipped — already saved today (%s) for trigger=%s.",
            today,
            trigger,
        )
        return None

    try:
        scan_id = await asyncio.to_thread(
            _swing_store.save_scan,
            setups,
            market_context,
            symbols_scanned=symbols_scanned,
            trigger=trigger,
        )
        if scan_id is not None:
            _last_swing_persist_date = today
            logger.info(
                "Swing scan persisted: scan_id=%s setups=%d trigger=%s date=%s",
                scan_id,
                len(setups or []),
                trigger,
                today,
            )
        return scan_id
    except Exception as exc:
        logger.warning("Swing scan persistence failed (non-fatal): %s", exc)
        return None


async def _hydrate_signals_cache_from_db() -> bool:
    """On startup, load the latest persisted signals snapshot from DB so /api/signals/all is instant."""
    global _persisted_signals_snapshot, _persisted_signals_updated_at
    try:
        from db.runtime_state import get_runtime_state
        data = await asyncio.to_thread(get_runtime_state, "latest_signals_snapshot")
        if isinstance(data, dict) and data.get("signals"):
            _persisted_signals_snapshot = data["signals"]
            _persisted_signals_updated_at = data.get("updated_at")
            logger.info("Hydrated %d signals from DB snapshot on startup", len(_persisted_signals_snapshot))
            return True
    except Exception as e:
        logger.warning("Failed to hydrate signals snapshot from DB: %s", e)
    return False


async def _hydrate_swing_cache_from_db() -> bool:
    """On startup, seed _swing_cache from the best available source.
    Checks both nightly JSON and DB, picks whichever has more setups.
    Returns True if hydration placed setups into the cache, False otherwise.
    """
    global _swing_cache

    best_source = None
    best_setups = []
    best_meta = {}

    # Source 1: Nightly scan JSON (usually has the full 500-stock scan)
    try:
        import json as _json

        json_path = os.path.join(
            os.path.dirname(__file__), "..", "data", "swing_shortlist_latest.json"
        )
        json_path = os.path.abspath(json_path)
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                raw = _json.load(f)
            shortlist = raw.get("shortlist") or raw.get("raw_setups") or []
            if len(shortlist) > len(best_setups):
                best_source = "json"
                best_setups = shortlist
                best_meta = {
                    "total_found": raw.get("setups_found", len(shortlist)),
                    "symbols_scanned": raw.get("symbols_scanned", 0),
                    "updated_at": raw.get("scan_timestamp"),
                }
    except Exception as exc:
        logger.warning("Swing hydration JSON read failed: %s", exc)

    # Source 2: DB persisted scan
    if _SWING_STORE_ENABLED and _swing_store is not None:
        try:
            latest = await asyncio.to_thread(_swing_store.load_latest_scan)
            if latest:
                db_setups = latest.get("setups") or []
                if len(db_setups) > len(best_setups):
                    best_source = "db"
                    best_setups = db_setups
                    best_meta = {
                        "total_found": int(
                            latest.get("total_setups") or len(db_setups)
                        ),
                        "symbols_scanned": int(latest.get("symbols_scanned") or 0),
                        "updated_at": latest.get("scan_time"),
                        "market_context": latest.get("market_context"),
                    }
        except Exception as exc:
            logger.warning("Swing hydration DB read failed: %s", exc)

    if not best_setups:
        logger.info("Swing cache hydration: no prior scan found — starting cold.")
        return False

    max_setups = max(1, int(getattr(settings, "SWING_MAX_SETUPS_RETURN", 100) or 100))
    _swing_cache["setups"] = best_setups[:max_setups]
    _swing_cache["total_found"] = best_meta.get("total_found", len(best_setups))
    _swing_cache["symbols_scanned"] = best_meta.get("symbols_scanned", 0)
    _swing_cache["real_data"] = True
    _swing_cache["updated_at"] = best_meta.get("updated_at")
    _swing_cache["market_context"] = best_meta.get("market_context")
    _swing_cache[f"hydrated_from_{best_source}"] = True
    logger.info(
        "Swing cache hydrated from %s: %d setups (scanned=%s).",
        best_source.upper(),
        len(_swing_cache["setups"]),
        _swing_cache["symbols_scanned"],
    )
    return True


async def _recompute_swing_cache(
    timeout_sec: float = 120.0, trigger: str = "periodic"
) -> bool:
    """Compute swing setups from current daily cache and store the latest snapshot.

    Args:
        timeout_sec: Hard ceiling on the pattern-scan worker (seconds).
        trigger: Audit label persisted with the scan — "manual" / "periodic" /
            "startup" / "cron" / any short string. Controls only the DB row,
            not behavior.
    """
    global _swing_cache
    if not _daily_data_cache:
        logger.debug("Swing cache recompute skipped — daily data cache is empty.")
        return False
    try:
        ctx = _build_swing_market_context()
        logger.info(
            "Swing scan starting: %d symbols | index_trend=%s | index_regime=%s",
            len(_daily_data_cache),
            ctx.get("index_trend"),
            ctx.get("index_regime"),
        )

        def _run_scan() -> list[dict]:
            return swing_scan_all(dict(_daily_data_cache), market_context=ctx)

        raw_setups = await asyncio.wait_for(
            asyncio.to_thread(_run_scan),
            timeout=timeout_sec,
        )
        # Enrich each setup with current live price from the 5m pipeline
        for setup in raw_setups:
            sym = setup.get("symbol", "")
            df_live = _market_data.get(sym)
            if df_live is not None and not df_live.empty:
                try:
                    ltp = float(df_live.iloc[-1]["close"])
                    entry = float(setup.get("entry_zone", 0))
                    setup["current_price"] = round(ltp, 2)
                    if entry > 0:
                        setup["price_vs_entry_pct"] = round(
                            (ltp - entry) / entry * 100, 2
                        )
                    else:
                        setup["price_vs_entry_pct"] = 0.0
                except Exception:
                    setup["current_price"] = None
                    setup["price_vs_entry_pct"] = None
            else:
                setup["current_price"] = None
                setup["price_vs_entry_pct"] = None

        max_setups = max(
            1, int(getattr(settings, "SWING_MAX_SETUPS_RETURN", 100) or 100)
        )

        # Guard: don't overwrite nightly scan (many setups) with periodic (few)
        existing_count = len(_swing_cache.get("setups") or [])
        is_nightly = (
            _swing_cache.get("hydrated_from_json")
            or _swing_cache.get("last_scan_source") == "nightly_auto"
        )
        if is_nightly and len(raw_setups) < existing_count and trigger == "periodic":
            logger.info(
                "Swing periodic recompute has %d setups vs %d from nightly — keeping nightly data.",
                len(raw_setups),
                existing_count,
            )
            return True

        _swing_cache["setups"] = raw_setups[:max_setups]
        _swing_cache["total_found"] = len(raw_setups)
        _swing_cache["symbols_scanned"] = len(_daily_data_cache)
        _swing_cache["real_data"] = True  # Always mark real after a completed scan
        _swing_cache["updated_at"] = datetime.now().isoformat()
        _swing_cache["market_context"] = ctx
        logger.info(
            "Swing cache updated: %d setups from %d symbols (trend=%s).",
            len(raw_setups),
            len(_daily_data_cache),
            ctx.get("index_trend"),
        )
        if len(raw_setups) == 0:
            logger.warning(
                "Swing scan found 0 setups — possible causes: "
                "regime gate blocking LONGs (trend=%s), strict R:R gate (%.1f), "
                "or insufficient pattern conditions met.",
                ctx.get("index_trend"),
                getattr(settings, "SWING_MIN_RISK_REWARD", 1.8),
            )

        # Persist snapshot (best-effort, never raises). We persist the FULL
        # raw_setups list (not the UI-truncated one) so the outcome tracker
        # and stats endpoint have the complete picture.
        try:
            await _persist_swing_scan(
                raw_setups,
                ctx,
                symbols_scanned=len(_daily_data_cache),
                trigger=trigger,
            )
        except Exception as exc:
            logger.warning("Swing persist call raised (non-fatal): %s", exc)

        return True
    except asyncio.TimeoutError:
        logger.warning(
            "Swing scan timed out after %.0fs — marking real_data=True with empty setups.",
            timeout_sec,
        )
        # Even on timeout, mark as real so UI stops showing "warming up"
        _swing_cache["real_data"] = True
        _swing_cache["symbols_scanned"] = len(_daily_data_cache)
        _swing_cache["updated_at"] = datetime.now().isoformat()
        _swing_cache["market_context"] = _build_swing_market_context()
        return False
    except Exception as exc:
        logger.error("Swing scan failed: %s", exc, exc_info=True)
        _swing_cache["real_data"] = True  # Avoid permanent "warming up" on error
        _swing_cache["symbols_scanned"] = len(_daily_data_cache)
        _swing_cache["updated_at"] = datetime.now().isoformat()
        return False


def _get_feed_status() -> dict[str, object]:
    """
    Decoupled feed status sourced from durable state (Supabase runtime_state / intraday_store)
    with fallback to local in-memory timestamp when running monolithically.
    """
    now_utc = datetime.now(timezone.utc)
    try:
        from agents.intraday_engine.session import evaluate_market_session
        session = evaluate_market_session()
        is_mkt_open = session.is_trading_active
    except Exception:
        is_mkt_open = is_market_open()

    # 1. Try durable latest_market_snapshot from runtime_state
    snapshot = None
    worker_status = None
    try:
        from db.runtime_state import get_runtime_state
        snapshot = get_runtime_state("latest_market_snapshot")
        worker_status = get_runtime_state("autonomous_worker:latest_status")
    except Exception as exc:
        logger.debug("Failed reading durable feed state: %s", exc)

    # 2. Extract timestamp from durable snapshot or fallback to local in-memory
    snap_dt: datetime | None = None
    source = "NONE"
    if snapshot and isinstance(snapshot, dict) and snapshot.get("timestamp"):
        try:
            raw_ts = snapshot["timestamp"]
            if isinstance(raw_ts, str):
                snap_dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if snap_dt.tzinfo is None:
                    snap_dt = snap_dt.replace(tzinfo=timezone.utc)
            source = str(snapshot.get("source", "WORKER_DB"))
        except Exception:
            snap_dt = None

    if snap_dt is None:
        # Fallback to intraday_store latest scan
        try:
            import db.intraday_store as ids
            latest_scan = ids.load_latest_scan()
            if latest_scan and latest_scan.get("scan_time"):
                st = latest_scan["scan_time"]
                if isinstance(st, str):
                    snap_dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
                    if snap_dt.tzinfo is None:
                        snap_dt = snap_dt.replace(tzinfo=timezone.utc)
                source = "INTRADAY_STORE"
        except Exception:
            pass

    if _last_feed_update != datetime.min:
        lfu = _last_feed_update
        if lfu.tzinfo is None:
            lfu = lfu.replace(tzinfo=timezone.utc)
        if snap_dt is None or lfu > snap_dt:
            snap_dt = lfu
            source = "LIVE_TICK"

    if snap_dt is None:
        return {
            "state": "OFFLINE",
            "market_snapshot_health": "UNAVAILABLE",
            "worker_health": "NO_RECENT_RUN",
            "last_update": None,
            "age_seconds": None,
            "source": "NONE",
            "mode": "15m Delayed Public Feed",
            "is_market_open": is_mkt_open,
        }

    age = max((now_utc - snap_dt).total_seconds(), 0.0)

    # Determine snapshot health
    if is_mkt_open:
        if age <= 300:
            snapshot_health = "CURRENT"
            state = "LIVE"
        elif age <= 1800:
            snapshot_health = "DELAYED"
            state = "DELAYED"
        else:
            snapshot_health = "STALE"
            state = "STALE"
    else:
        snapshot_health = "EOD"
        state = "EOD"

    # Determine worker health
    worker_health = "NO_RECENT_RUN"
    if worker_status and isinstance(worker_status, dict):
        w_ts_str = worker_status.get("timestamp")
        w_status_code = str(worker_status.get("status", "")).upper()
        if w_ts_str:
            try:
                w_dt = datetime.fromisoformat(w_ts_str.replace("Z", "+00:00"))
                if w_dt.tzinfo is None:
                    w_dt = w_dt.replace(tzinfo=timezone.utc)
                w_age = max((now_utc - w_dt).total_seconds(), 0.0)
                if w_status_code in ("FAILED", "ERROR"):
                    worker_health = "WORKER_FAILED"
                elif is_mkt_open:
                    if w_age <= 2700:
                        worker_health = "WORKER_OK"
                    elif w_age <= 5400:
                        worker_health = "WORKER_LATE"
                    else:
                        worker_health = "WORKER_STALE"
                else:
                    if w_age <= 86400:
                        worker_health = "WORKER_OK"
                    else:
                        worker_health = "WORKER_STALE"
            except Exception:
                worker_health = "NO_RECENT_RUN"

    return {
        "state": state,
        "market_snapshot_health": snapshot_health,
        "worker_health": worker_health,
        "last_update": snap_dt.isoformat(),
        "age_seconds": round(age, 1),
        "source": source,
        "mode": "15m Delayed Public Feed (NSE / Yahoo)",
        "is_market_open": is_mkt_open,
    }


def is_market_open() -> bool:
    """Return True if current time is within NSE trading hours (09:15 - 15:30 IST, Mon-Fri)."""
    try:
        from zoneinfo import ZoneInfo
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        if now_ist.weekday() >= 5:
            return False
        total_mins = now_ist.hour * 60 + now_ist.minute
        return (9 * 60 + 15) <= total_mins <= (15 * 60 + 30)
    except Exception:
        return False


def _stable_regime(symbol: str, new_regime: MarketRegime) -> MarketRegime:
    """Only switch regime after it's been detected N consecutive times."""
    state = _regime_hysteresis.get(symbol)
    nv = new_regime.value

    if state is None:
        _regime_hysteresis[symbol] = (nv, nv, 1)
        return new_regime

    confirmed, tentative, count = state

    if nv == confirmed:
        # Same as confirmed — reset any tentative
        _regime_hysteresis[symbol] = (confirmed, confirmed, 0)
        return MarketRegime(confirmed)
    elif nv == tentative:
        # Continuing tentative
        count += 1
        if count >= REGIME_HYSTERESIS_MIN:
            _regime_hysteresis[symbol] = (nv, nv, 0)
            return new_regime  # Confirmed switch
        else:
            _regime_hysteresis[symbol] = (confirmed, tentative, count)
            return MarketRegime(confirmed)  # Keep old
    else:
        # New tentative regime
        _regime_hysteresis[symbol] = (confirmed, nv, 1)
        return MarketRegime(confirmed)


def _get_engine() -> PaperTradingEngine:
    """Get or create the paper trading engine (loads real trade history)."""
    global _engine
    if _engine is None:
        _engine = PaperTradingEngine(initial_capital=100_000)
    return _engine


# Startup logic is now handled in the lifespan context manager above.


async def _initial_load():
    """Non-blocking progressive data load — runs as a background task after server starts.

    Phase 1: Load priority symbols (15) — fast, gets dashboard functional
    Phase 2: Load remaining symbols (35) in batches of 10
    Phase 3: Load daily data for swing/breakout scanners
    """
    global _loading_status
    try:
        _loading_status = {
            "phase": "priority",
            "loaded": 0,
            "total": len(_PRIORITY_SYMBOLS),
            "ready": False,
        }
        logger.info("Phase 1: Loading %d priority symbols...", len(_PRIORITY_SYMBOLS))

        sem = asyncio.Semaphore(MAX_NSE_CONCURRENCY)
        # Phase 1: Priority symbols
        tasks = [_fetch_nse_symbol_safe(name, sem) for name in _PRIORITY_SYMBOLS]
        await asyncio.gather(*tasks)
        _loading_status["loaded"] = len(_market_data)
        _loading_status["ready"] = True  # Dashboard is now functional
        logger.info(
            "Phase 1 complete: %d priority symbols loaded. Dashboard ready.",
            len(_market_data),
        )

        # Phase 2: Remaining symbols in batches of 10
        _loading_status["phase"] = "remaining"
        _loading_status["total"] = len(TOP_NSE_SYMBOLS)
        for i in range(0, len(_REMAINING_SYMBOLS), 10):
            batch = _REMAINING_SYMBOLS[i : i + 10]
            logger.info(
                "Phase 2: Loading batch %d-%d (%d symbols)...",
                i,
                i + len(batch),
                len(batch),
            )
            batch_tasks = [_fetch_nse_symbol_safe(name, sem) for name in batch]
            await asyncio.gather(*batch_tasks)
            _loading_status["loaded"] = len(_market_data)
            # Small pause between batches to avoid rate limiting
            await asyncio.sleep(2)

        logger.info("Phase 2 complete: %d total symbols loaded.", len(_market_data))

        # Phase 3: Daily data for scanners (1y history = EMA_200 valid)
        _loading_status["phase"] = "daily"
        try:
            daily_sem = asyncio.Semaphore(3)

            async def _fetch_daily_safe(name: str, sem: asyncio.Semaphore):
                async with sem:
                    try:
                        df = await asyncio.wait_for(
                            asyncio.to_thread(
                                fetch_ohlcv, name, "1y", "1d"
                            ),  # FIX: 1y so EMA_200 is valid
                            timeout=30.0,
                        )
                        if df is not None and not df.empty and len(df) >= 30:
                            df = calculate_indicators(df)
                            _daily_data_cache[name] = df
                            logger.debug(
                                "Phase 3 daily loaded %s: %d bars", name, len(df)
                            )
                    except Exception as e:
                        logger.debug("Daily fetch failed for %s: %s", name, e)

            # Use swing watchlist universe and force-include key user-requested symbols.
            _swing_symbols = _build_swing_symbol_universe(_PRIORITY_SYMBOLS)
            daily_tasks = [_fetch_daily_safe(n, daily_sem) for n in _swing_symbols]
            await asyncio.gather(*daily_tasks)
            logger.info(
                "Phase 3 complete: %d daily symbols loaded for swing scanner (watchlist=%d).",
                len(_daily_data_cache),
                len(_swing_symbols),
            )

            # Precompute swing cache immediately so UI doesn't wait for periodic refresh.
            if _daily_data_cache:
                await _recompute_swing_cache(timeout_sec=90.0)
        except Exception as e:
            logger.error("Daily data fetch failed: %s", e)

        _loading_status["phase"] = "complete"
        _loading_status["loaded"] = len(_market_data)
        global _last_refresh
        _last_refresh = datetime.now()
        logger.info("Initial load complete. Tracking %d symbols.", len(_market_data))

    except Exception as e:
        logger.error(
            "Initial load failed: %s — will retry on next background refresh", e
        )
        _loading_status["phase"] = "error"


async def _fetch_nse_symbol_safe(
    display_name: str,
    sem: asyncio.Semaphore,
    preloaded_df: pd.DataFrame | None = None,
    preloaded_htf_df: pd.DataFrame | None = None,
) -> None:
    """Fetch one symbol with timeout starting only after semaphore acquisition."""
    try:
        # Important: do not include semaphore queue wait in timeout.
        # Under startup burst, queue wait can consume most of a 90s budget,
        # causing false timeouts before a symbol even starts processing.
        async with sem:
            await asyncio.wait_for(
                _fetch_nse_symbol(
                    display_name,
                    preloaded_df=preloaded_df,
                    preloaded_htf_df=preloaded_htf_df,
                ),
                timeout=SYMBOL_FETCH_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        logger.warning(
            "Symbol %s timed out after %.0fs — skipping",
            display_name,
            SYMBOL_FETCH_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.error("Symbol %s failed: %s", display_name, e)


async def _wait_until_ready_or_timeout(max_wait_s: int, *, task_name: str) -> None:
    """Wait for startup readiness with a hard upper bound."""
    waited_s = 0
    while not _loading_status.get("ready") and waited_s < max_wait_s:
        await asyncio.sleep(5)
        waited_s += 5
    if not _loading_status.get("ready"):
        logger.warning(
            "%s starting without startup ready flag after %ss (phase=%s)",
            task_name,
            waited_s,
            _loading_status.get("phase"),
        )


async def _run_swing_outcome_eval_if_due() -> dict | None:
    """Execute swing outcome evaluation if throttle interval has elapsed."""
    global _last_swing_outcome_eval_monotonic

    if not getattr(settings, "SWING_OUTCOME_EVAL_ENABLED", True):
        return None

    interval = max(
        300, int(getattr(settings, "SWING_OUTCOME_EVAL_INTERVAL_SEC", 3600) or 3600)
    )
    now_m = time.monotonic()
    if now_m - _last_swing_outcome_eval_monotonic < interval:
        return None

    try:
        from db.swing_outcome_tracker import run_outcome_evaluation_once

        summary = await asyncio.to_thread(run_outcome_evaluation_once)
        _last_swing_outcome_eval_monotonic = now_m
        return summary if isinstance(summary, dict) else None
    except Exception as exc:
        logger.debug("Swing outcome evaluation skipped: %s", exc)
        return None


async def _background_refresh():
    """Background task: refresh market data periodically.
    If initial load was skipped, immediately triggers a first refresh cycle.
    """
    initial_load_enabled = os.environ.get("INITIAL_LOAD_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    if initial_load_enabled:
        await _wait_until_ready_or_timeout(
            BACKGROUND_READY_WAIT_SECONDS,
            task_name="Background refresh",
        )
    else:
        # On Render free tier with INITIAL_LOAD_ENABLED=false, give DB hydration,
        # model warmup, and health probes 45s to complete and settle before
        # starting the first refresh cycle. Prevents concurrent memory spikes.
        logger.info("INITIAL_LOAD_ENABLED=false: waiting 45s for server warmup before first refresh cycle")
        await asyncio.sleep(45)
        lock = _get_refresh_lock()
        if not lock.locked():
            try:
                await asyncio.wait_for(
                    _refresh_market_data(),
                    timeout=REFRESH_CYCLE_TIMEOUT_SECONDS,
                )
                logger.info("First background refresh complete. %d symbols loaded.", len(_market_data))
            except Exception as exc:
                logger.error("First background refresh failed: %s", exc)

    try:
        while True:
            # Smart cadence: during NSE off-hours or weekends, back off to avoid redundant allocations on Render free tier
            sleep_time = REFRESH_INTERVAL_SECONDS
            try:
                from zoneinfo import ZoneInfo
                _now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
                is_weekend = _now_ist.weekday() >= 5
                is_off_hours = not (9 <= _now_ist.hour < 16) or (_now_ist.hour == 9 and _now_ist.minute < 15) or (_now_ist.hour == 15 and _now_ist.minute > 35)
                if is_weekend or is_off_hours:
                    sleep_time = max(REFRESH_INTERVAL_SECONDS * 3, 300)
            except Exception:
                sleep_time = REFRESH_INTERVAL_SECONDS
            await asyncio.sleep(sleep_time)
            lock = _get_refresh_lock()
            if lock.locked():
                logger.debug("Background refresh skipped — previous refresh still running")
                continue
            try:
                logger.info("Background refresh triggered...")
                await asyncio.wait_for(
                    _refresh_market_data(),
                    timeout=REFRESH_CYCLE_TIMEOUT_SECONDS,
                )
                logger.info(
                    "Background refresh complete. %d symbols loaded.", len(_market_data)
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Background refresh timed out after %ss — skipping this cycle",
                    REFRESH_CYCLE_TIMEOUT_SECONDS,
                )
            except Exception as e:
                logger.error("Background refresh failed: %s", e)
    except asyncio.CancelledError:
        logger.info("Background refresh worker cancelled.")


async def _swing_outcome_background() -> None:
    """Dedicated loop for swing outcome evaluation, independent of refresh path."""
    try:
        await _wait_until_ready_or_timeout(
            BACKGROUND_READY_WAIT_SECONDS,
            task_name="Swing outcome task",
        )

        while True:
            await asyncio.sleep(SWING_OUTCOME_TASK_INTERVAL_SECONDS)
            try:
                summary = await _run_swing_outcome_eval_if_due()
                if summary and not summary.get("skipped"):
                    logger.info("Swing outcome evaluation: %s", summary)
            except Exception as exc:
                logger.error("Swing outcome background task failed: %s", exc)
    except asyncio.CancelledError:
        logger.info("Swing outcome background task cancelled.")


# ── Nightly SwingSpectrum Batch Scan (auto-scheduled) ─────────────────────────

_NIGHTLY_SCAN_HOUR = int(os.environ.get("NIGHTLY_SCAN_HOUR", 21))  # 9 PM IST
_NIGHTLY_SCAN_MINUTE = int(os.environ.get("NIGHTLY_SCAN_MINUTE", 30))  # :30


def _seconds_until_nightly_scan(now: datetime | None = None) -> float:
    """Return seconds until next scheduled nightly scan time (IST)."""
    from datetime import time as dt_time
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
    current = now or datetime.now(tz=_IST)
    target = datetime.combine(
        current.date(),
        dt_time(_NIGHTLY_SCAN_HOUR, _NIGHTLY_SCAN_MINUTE),
        tzinfo=_IST,
    )
    if current >= target:
        target = target + timedelta(days=1)
    return max((target - current).total_seconds(), 1.0)


async def _nightly_swing_scan_background() -> None:
    """
    Scheduled background task: runs SwingSpectrum nightly scan daily.
    - Fetches NIFTY 500 stock universe from NSE
    - Scans all stocks for swing patterns + news sentiment
    - Saves results to DB + evaluates past outcomes
    - Updates the in-memory swing cache for dashboard
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")

    # Wait for server to stabilize first
    await _wait_until_ready_or_timeout(
        BACKGROUND_READY_WAIT_SECONDS,
        task_name="Nightly swing scan",
    )

    while True:
        try:
            delay = _seconds_until_nightly_scan()
            next_run = datetime.now(tz=_IST) + timedelta(seconds=delay)
            logger.info(
                "Nightly SwingSpectrum scan scheduled in %.0f sec (next: %s IST)",
                delay,
                next_run.strftime("%Y-%m-%d %H:%M"),
            )
            await asyncio.sleep(delay)

            logger.info("=== Nightly SwingSpectrum Scan STARTING ===")
            universe = os.environ.get("SWING_UNIVERSE", "nifty500")

            # Run the heavy scan in a thread to avoid blocking the event loop
            from scripts.nightly_swing_scan import run_scan as nightly_run_scan

            top_picks, payload = await asyncio.to_thread(
                nightly_run_scan,
                grade_filter=None,
                top_n=15,
                output_path=None,
                universe=universe,
            )

            logger.info(
                "=== Nightly SwingSpectrum Scan COMPLETE === "
                "setups=%d grade_A=%d grade_B=%d",
                payload.get("setups_found", 0),
                payload.get("grade_summary", {}).get("A", 0),
                payload.get("grade_summary", {}).get("B", 0),
            )

            # Update in-memory swing cache so dashboard shows fresh data
            _swing_cache["setups"] = top_picks
            _swing_cache["total_found"] = payload.get("setups_found", 0)
            _swing_cache["symbols_scanned"] = payload.get("symbols_scanned", 0)
            _swing_cache["scan_timestamp"] = payload.get("scan_timestamp")
            _swing_cache["last_scan_source"] = "nightly_auto"

        except Exception as exc:
            logger.error("Nightly swing scan failed: %s", exc)
            # Don't crash the loop — retry next day
            await asyncio.sleep(60)


def _fire_telegram_signal(display_name: str, df: pd.DataFrame) -> None:
    """
    Fire a Telegram alert if the latest signal is new (changed since last refresh).
    Also records the signal in the trade journal.
    Called from a thread pool — must not use async primitives.
    """
    try:
        last = df.iloc[-1]
        latest_signal = str(last.get("signal", "HOLD"))
        prev = _prev_signal_cache.get(display_name)

        if latest_signal in ("LONG", "SHORT") and latest_signal != prev:
            sl = last.get("stop_loss")
            tp = last.get("target")

            # ── Telegram alert ────────────────────────────────────
            if _tg_signal_alert:
                _tg_signal_alert(
                    symbol=display_name,
                    direction=latest_signal,
                    entry=float(last["close"]),
                    stop_loss=float(sl) if pd.notna(sl) else float(last["close"]),
                    target=float(tp) if pd.notna(tp) else float(last["close"]),
                    confidence=float(
                        last.get("confidence", last.get("ai_confidence", 0))
                    ),
                    trend=str(last.get("htf_trend", "")),
                    rsi=float(last["RSI_14"]) if pd.notna(last.get("RSI_14")) else 0.0,
                    regime=_regime_cache.get(display_name, ""),
                )

            # ── Trade journal recording ───────────────────────────
            try:
                signal_data = {
                    "symbol": display_name,
                    "signal": latest_signal,
                    "timestamp": last["timestamp"].isoformat()
                    if hasattr(last.get("timestamp"), "isoformat")
                    else str(last.get("timestamp", "")),
                    "confidence": float(
                        last.get("confidence", last.get("ai_confidence", 0))
                    ),
                    "regime": _regime_cache.get(display_name, "RANGING"),
                    "quality_score": int(last.get("quality_score", 0)),
                    "quality_grade": str(last.get("quality_grade", "D")),
                }
                data_quality = _data_quality_cache.get(display_name, 100.0)
                get_journal().record_signal(signal_data, data_quality)
                # Try to resolve any pending outcomes while df is available
                get_journal().resolve_pending_outcomes(df, display_name)
            except Exception as jexc:
                logger.debug("Journal record failed for %s: %s", display_name, jexc)

        _prev_signal_cache[display_name] = latest_signal
    except Exception as exc:
        logger.warning("Telegram/journal fire failed for %s: %s", display_name, exc)


async def _fetch_nse_symbol(
    display_name: str,
    preloaded_df: pd.DataFrame | None = None,
    preloaded_htf_df: pd.DataFrame | None = None,
) -> None:
    """Fetch, validate, process, and cache a single NSE symbol."""
    try:
        global _last_feed_update
        if preloaded_df is not None and not preloaded_df.empty:
            df = preloaded_df.copy()
        else:
            df = await asyncio.to_thread(fetch_ohlcv, display_name, "5d", "5m")
        if df is None or df.empty:
            logger.warning("No 5m data for %s — skipping", display_name)
            return

        # ── Data validation (run before indicator calculation) ────────────
        df, report = await asyncio.to_thread(validate_ohlcv, df, display_name, "5m")
        _data_quality_cache[display_name] = report.quality_pct
        if not report.is_safe:
            logger.warning(
                "Skipping %s: data quality too low (%.1f%%)",
                display_name,
                report.quality_pct,
            )
            return

        df = await asyncio.to_thread(calculate_indicators, df)

        # ── Real AI confidence from ensemble predictor ────────────────────
        ai_confidence = await asyncio.to_thread(predict_batch, df)

        if preloaded_htf_df is not None and not preloaded_htf_df.empty:
            htf_df = preloaded_htf_df.copy()
        else:
            htf_df = await asyncio.to_thread(fetch_ohlcv, display_name, "60d", "1d")
        if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:
            htf_df = await asyncio.to_thread(calculate_indicators, htf_df)
            htf_df = await asyncio.to_thread(classify_trend, htf_df)
            df = await asyncio.to_thread(propagate_trend_to_ltf, df, htf_df)
        else:
            htf_df = None
            df = await asyncio.to_thread(classify_trend, df)

        # ── MTF score from resampled timeframes ───────────────────────────
        mtf_data = {"5m": df}
        try:
            _agg = {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
            df_15m = (
                df.set_index("timestamp")
                .resample("15min")
                .agg(_agg)
                .dropna(subset=["close"])
                .reset_index()
            )
            if len(df_15m) >= 30:
                df_15m = await asyncio.to_thread(calculate_indicators, df_15m)
                mtf_data["15m"] = df_15m
        except Exception as e:
            logger.debug("15m resample failed for %s: %s", display_name, e)
        if htf_df is not None and not htf_df.empty:
            mtf_data["1d"] = htf_df
        mtf_score = await asyncio.to_thread(calculate_mtf_score, mtf_data)

        raw_regime = await asyncio.to_thread(detect_regime, df)
        regime = _stable_regime(display_name, raw_regime)
        _regime_cache[display_name] = regime.value

        # ── Sector / Market Correlation: read Nifty 50 trend ──────────────
        index_symbols = {"NIFTY 50", "BANKNIFTY", "SENSEX", "NIFTY IT", "NIFTY BANK"}
        if display_name not in index_symbols:
            market_trend = _regime_cache.get("NIFTY 50")
            nifty_df = _signal_cache.get("NIFTY 50")
            if nifty_df is not None and not nifty_df.empty:
                nifty_last_trend = str(nifty_df.iloc[-1].get("htf_trend", ""))
                if "BULLISH" in nifty_last_trend.upper():
                    market_trend = "BULLISH"
                elif "BEARISH" in nifty_last_trend.upper():
                    market_trend = "BEARISH"
                else:
                    market_trend = None
            else:
                market_trend = None
        else:
            market_trend = None

        # ── Targeted News Sentiment Gate (GDELT / Google RSS) ────────────
        sent_data = None
        try:
            from data.providers.gdelt import get_gdelt_provider
            sent_data = await get_gdelt_provider().get_sentiment(display_name)
        except Exception as sent_err:
            logger.debug("[Service] Sentiment lookup failed for %s: %s", display_name, sent_err)

        df = await asyncio.to_thread(
            generate_signals,
            df,
            ai_confidence=ai_confidence,
            regime=regime,
            symbol=display_name,
            market_trend=market_trend,
            mtf_score=mtf_score,
            news_sentiment=sent_data,
        )
        if len(df) > 300:
            df = df.iloc[-300:].copy()
        _market_data[display_name] = df
        _signal_cache[display_name] = df
        _last_feed_update = datetime.now()

        # Fire Telegram alert + journal for new LONG/SHORT signals (non-blocking)
        await asyncio.to_thread(_fire_telegram_signal, display_name, df)

    except Exception as e:
        logger.error("Failed to fetch %s: %s", display_name, e)


_refresh_task: asyncio.Task | None = None
_refresh_task_lock: asyncio.Lock | None = None


def _get_refresh_task_lock() -> asyncio.Lock:
    global _refresh_task_lock
    if _refresh_task_lock is None:
        _refresh_task_lock = asyncio.Lock()
    return _refresh_task_lock


async def _refresh_market_data():
    """
    Single-flight entrypoint for market data refresh.
    If a refresh cycle is already in-flight, reuses that task to avoid thundering-herd duplicate downloads.
    """
    global _refresh_task
    async with _get_refresh_task_lock():
        if _refresh_task is not None and not _refresh_task.done():
            logger.info("[SingleFlight] Reusing existing in-flight market refresh task")
            task = _refresh_task
        else:
            task = asyncio.create_task(_refresh_market_data_inner())
            _refresh_task = task

    return await asyncio.shield(task)


async def _refresh_market_data_inner():
    """
    Fetch real market data from yFinance/Binance and generate signals.

    Concurrent NSE symbol fetching via semaphore-gated asyncio.gather
    (max 5 concurrent yfinance calls) to keep startup fast even with 50 symbols.
    HTF (1d) trend propagated to 5m data.

    Protected by _refresh_lock so that concurrent callers (background task +
    manual /api/refresh) never produce a partial/interleaved cache snapshot.
    """
    global _last_refresh, _last_feed_update
    async with _get_refresh_lock():
        from utils.profiler import PerfTracker
        refresh_tracker = PerfTracker("market_refresh", slow_threshold_ms=3000.0)
        # ── V2: Fast Vectorized Batch Download via MarketDataRouter ──
        try:
            from data.router import get_default_router
            router = get_default_router()
            t_batch = time.perf_counter()
            dfs_5m = await router.get_batch_ohlcv(TOP_NSE_SYMBOLS, period="5d", interval="5m")
            dfs_1d = await router.get_batch_ohlcv(TOP_NSE_SYMBOLS, period="60d", interval="1d")
            elapsed_batch = time.perf_counter() - t_batch
            refresh_tracker.record("batch_ohlcv", elapsed_batch * 1000.0)
            logger.info(
                "[V2 Router] Batch download completed in %.2fs (5m: %d symbols, 1d: %d symbols)",
                elapsed_batch,
                len(dfs_5m),
                len(dfs_1d),
            )
        except Exception as router_exc:
            logger.warning("[V2 Router] Batch download error, falling back to per-symbol: %s", router_exc)
            dfs_5m = {}
            dfs_1d = {}

        # ── Concurrent NSE symbol processing (with per-symbol timeout) ──
        sem = asyncio.Semaphore(MAX_NSE_CONCURRENCY)
        nse_tasks = [
            _fetch_nse_symbol_safe(
                name,
                sem,
                preloaded_df=dfs_5m.get(name),
                preloaded_htf_df=dfs_1d.get(name),
            )
            for name in TOP_NSE_SYMBOLS
        ]
        await asyncio.gather(*nse_tasks)

        # ── Fetch crypto data concurrently (CCXT with Yahoo fallback) ──
        from agents.execution.market_control import is_crypto_forex_enabled
        if is_crypto_forex_enabled():
            crypto_symbols_to_fetch = settings.CRYPTO_SYMBOLS
        else:
            crypto_symbols_to_fetch = []
            logger.debug("[MARKET_CONTROL] Crypto & Forex refresh skipped under Section 6A policy")

        try:
            from agents.data_ingestion.binance_fetcher import fetch_historical

            async def _process_crypto_symbol(symbol: str) -> None:
                try:
                    df = await asyncio.wait_for(
                        asyncio.to_thread(fetch_historical, symbol, "1h", 7),
                        timeout=12.0,
                    )
                    if df is None or df.empty:
                        return

                    # Data validation
                    df, report = await asyncio.to_thread(
                        validate_ohlcv, df, symbol, "1h"
                    )
                    _data_quality_cache[symbol] = report.quality_pct
                    if not report.is_safe:
                        logger.warning(
                            "Skipping %s: data quality too low (%.1f%%)",
                            symbol,
                            report.quality_pct,
                        )
                        return

                    df = await asyncio.to_thread(calculate_indicators, df)

                    htf_df = await asyncio.wait_for(
                        asyncio.to_thread(fetch_historical, symbol, "1d", 60),
                        timeout=10.0,
                    )
                    if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:
                        htf_df = await asyncio.to_thread(calculate_indicators, htf_df)
                        htf_df = await asyncio.to_thread(classify_trend, htf_df)
                        df = await asyncio.to_thread(propagate_trend_to_ltf, df, htf_df)
                    else:
                        df = await asyncio.to_thread(classify_trend, df)

                    # ── A1: MTF confluence — fetch 4h trend independently.
                    try:
                        mtf_bars = max(
                            60, int(getattr(settings, "CRYPTO_MTF_4H_BARS", 200) or 200)
                        )
                        days_for_4h = max(15, mtf_bars // 6 + 2)  # 6 × 4h bars per day
                        htf4_df = await asyncio.wait_for(
                            asyncio.to_thread(fetch_historical, symbol, "4h", days_for_4h),
                            timeout=10.0,
                        )
                        if (
                            htf4_df is not None
                            and not htf4_df.empty
                            and len(htf4_df) >= 50
                        ):
                            htf4_df = await asyncio.to_thread(calculate_indicators, htf4_df)
                            htf4_df = await asyncio.to_thread(classify_trend, htf4_df)
                            trend_col = htf4_df.get("htf_trend")
                            if trend_col is not None and len(trend_col) > 0:
                                last_trend = (
                                    str(trend_col.iloc[-1] or "").upper() or None
                                )
                                _crypto_htf4_trend[symbol] = last_trend
                            else:
                                _crypto_htf4_trend[symbol] = None
                        else:
                            _crypto_htf4_trend[symbol] = None
                    except Exception as exc:
                        logger.debug("4h MTF fetch failed for %s: %s", symbol, exc)
                        _crypto_htf4_trend[symbol] = None

                    raw_regime = await asyncio.to_thread(detect_regime, df)
                    regime = _stable_regime(symbol, raw_regime)
                    _regime_cache[symbol] = regime.value
                    ai_conf_crypto = await asyncio.to_thread(predict_batch, df)
                    df = await asyncio.to_thread(
                        generate_signals,
                        df,
                        ai_confidence=ai_conf_crypto,
                        regime=regime,
                        symbol=symbol,
                    )
                    _market_data[symbol] = df
                    _signal_cache[symbol] = df
                    _last_feed_update = datetime.now()

                    await asyncio.to_thread(_fire_telegram_signal, symbol, df)

                except Exception as e:
                    logger.error("Failed to fetch %s: %s", symbol, e)

            # Parallelize crypto processing with a fast timeout so crypto never blocks NSE
            await asyncio.gather(
                *[_process_crypto_symbol(s) for s in crypto_symbols_to_fetch],
                return_exceptions=True
            )
        except ImportError:
            logger.warning("CCXT not available for crypto data")

        # ── Phase 6: Refresh futures context (advisory) ──────────
        if crypto_symbols_to_fetch:
            await _refresh_crypto_futures_context()

        # ── Fetch daily data for swing/breakout scanners (Asynchronous Background Task) ──
        # Offload 41-symbol 1y downloads & swing scan so intraday refresh completes in <5s.
        try:
            async def _bg_swing_update():
                try:
                    daily_sem = asyncio.Semaphore(5)
                    async def _fetch_daily(name: str):
                        async with daily_sem:
                            try:
                                df = await asyncio.wait_for(
                                    asyncio.to_thread(fetch_ohlcv, name, "1y", "1d"),
                                    timeout=20.0,
                                )
                                if df is not None and not df.empty and len(df) >= 30:
                                    df = calculate_indicators(df)
                                    _daily_data_cache[name] = df
                            except Exception:
                                pass
                    _sw_list = _build_swing_symbol_universe(TOP_NSE_SYMBOLS)
                    await asyncio.gather(*[_fetch_daily(n) for n in _sw_list], return_exceptions=True)
                    logger.info(
                        "Daily data fetched: %d symbols for swing scanner (watchlist=%d).",
                        len(_daily_data_cache),
                        len(_sw_list),
                    )
                    await _recompute_swing_cache(timeout_sec=90.0)
                except Exception as exc:
                    logger.debug("Background swing update failed: %s", exc)

            if len(_daily_data_cache) == 0 or not _swing_cache.get("setups"):
                asyncio.create_task(_bg_swing_update())
        except Exception as e:
            logger.error("Daily data batch task dispatch failed: %s", e)

        # Precompute dashboard snapshots from latest cache state
        try:
            _refresh_movers_cache()
            _compute_market_mood()
            _compute_overview_intelligence(force_refresh=True)
        except Exception as e:
            logger.debug("Dashboard precompute failed: %s", e)

        # Persist latest signals snapshot to DB for instant cold-boot recovery
        try:
            current_signals = []
            for sym in list(_signal_cache.keys()):
                sig = _get_signal_for_symbol(sym)
                if sig:
                    current_signals.append(sig)
            if current_signals:
                _persisted_signals_snapshot = current_signals
                _persisted_signals_updated_at = datetime.now().isoformat()
                from db.runtime_state import set_runtime_state
                await asyncio.to_thread(
                    set_runtime_state,
                    "latest_signals_snapshot",
                    {
                        "signals": current_signals,
                        "updated_at": _persisted_signals_updated_at,
                    },
                )
                logger.debug("Persisted %d signals to DB runtime snapshot", len(current_signals))
        except Exception as e:
            logger.debug("Persisting signals snapshot failed: %s", e)

        # ── Autonomous Paper Execution & Trade Management ──
        try:
            from agents.execution.autonomous_daemon import get_autonomous_daemon
            daemon = get_autonomous_daemon()
            engine = _get_engine()

            prices = {}
            for sym, d in _market_data.items():
                if isinstance(d, pd.DataFrame) and not d.empty:
                    last_row = d.iloc[-1]
                    prices[sym] = {
                        "high": float(last_row.get("high", last_row["close"])),
                        "low": float(last_row.get("low", last_row["close"])),
                        "close": float(last_row["close"]),
                        "atr": float(last_row.get("ATR_14", 0.0)),
                    }
                elif isinstance(d, dict) and "close" in d:
                    prices[sym] = {
                        "high": float(d.get("high", d["close"])),
                        "low": float(d.get("low", d["close"])),
                        "close": float(d["close"]),
                        "atr": float(d.get("atr", 0.0)),
                    }

            # Ensure every open trade symbol has live prices (priority resolution)
            for t in list(engine.open_trades):
                if t.symbol not in prices:
                    try:
                        if "/" in t.symbol:
                            from agents.data_ingestion.binance_fetcher import fetch_historical
                            cdf = await asyncio.wait_for(
                                asyncio.to_thread(fetch_historical, t.symbol, "1h", 2),
                                timeout=5.0,
                            )
                            if cdf is not None and not cdf.empty:
                                lr = cdf.iloc[-1]
                                prices[t.symbol] = {
                                    "high": float(lr["high"]),
                                    "low": float(lr["low"]),
                                    "close": float(lr["close"]),
                                    "atr": 0.0,
                                }
                        else:
                            from agents.market_intelligence.nse_fetcher import fetch_ohlcv
                            edf = await asyncio.wait_for(
                                asyncio.to_thread(fetch_ohlcv, t.symbol, "5d", "1d"),
                                timeout=5.0,
                            )
                            if edf is not None and not edf.empty:
                                lr = edf.iloc[-1]
                                prices[t.symbol] = {
                                    "high": float(lr["high"]),
                                    "low": float(lr["low"]),
                                    "close": float(lr["close"]),
                                    "atr": 0.0,
                                }
                    except Exception as p_err:
                        logger.debug("Open trade priority fetch failed for %s: %s", t.symbol, p_err)

            atr_lookup = {sym: p.get("atr", 0.0) for sym, p in prices.items()}

            # 1. Manage open positions (SL/TP, Trailing stops, 15:15 IST EOD Square-off)
            await asyncio.to_thread(daemon.manage_open_trades, engine, prices, atr_lookup)

            # 2. Check and auto-execute top qualified setups
            if current_signals:
                await asyncio.to_thread(daemon.evaluate_signals, current_signals, prices, engine)

            # 3. Check for 15:45 IST Daily Self-Learning Review
            await asyncio.to_thread(daemon.check_daily_eod_review)

        except Exception as auto_err:
            logger.debug("[Service] Autonomous daemon cycle skipped: %s", auto_err)

        _last_refresh = datetime.now()
        refresh_tracker.set_meta("nse_loaded", len(TOP_NSE_SYMBOLS))
        refresh_tracker.set_meta("total_symbols", len(_market_data))
        refresh_tracker.finish()
        logger.info("Market data refreshed: %d symbols loaded", len(_market_data))

        # Force garbage collection and Linux malloc_trim to keep Render free tier memory footprint strictly under 350 MB
        _force_free_memory()


# ── API Endpoints ────────────────────────────────────────


def _clean_trend(raw) -> str:
    """Return plain string trend value, stripping any enum class prefix."""
    s = str(raw) if raw is not None else "NEUTRAL"
    return s.split(".")[-1] if "." in s else s


def _cap_confidence(raw) -> float:
    """Cap displayed confidence to a realistic ceiling for UX credibility."""
    try:
        return round(max(0.0, min(float(raw), 85.0)), 0)
    except Exception:
        return 0.0


def _signal_age_minutes(ts) -> float:
    try:
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        now = datetime.now(dt.tzinfo) if getattr(dt, "tzinfo", None) else datetime.now()
        return max((now - dt).total_seconds() / 60.0, 0.0)
    except Exception:
        return 0.0


def _stale_threshold_minutes(symbol: str) -> float:
    # Crypto runs 24/7, so allow up to 60 minutes. NSE intraday signals are stricter.
    return 60.0 if "/" in str(symbol) else 30.0


def _get_signal_for_symbol(symbol: str) -> dict:
    """Extract the latest signal data for a symbol (shared helper)."""
    if symbol not in _signal_cache:
        return None

    df = _signal_cache[symbol]
    last = df.iloc[-1]

    # Always use current-bar values for these live fields
    current_close = round(float(last["close"]), 2)
    first_open_raw = df.iloc[0].get("open", current_close) if "open" in df.columns else current_close
    try:
        first_open = float(first_open_raw) if float(first_open_raw) != 0 else current_close
    except (ValueError, TypeError):
        first_open = current_close
    day_chg_pct = round(((current_close - first_open) / first_open) * 100.0, 2) if first_open else 0.0
    current_rsi = (
        round(float(last["RSI_14"]), 1) if pd.notna(last.get("RSI_14")) else None
    )
    current_trend = _clean_trend(last.get("htf_trend", "NEUTRAL"))
    current_regime = _regime_cache.get(symbol, "RANGING")
    current_ts = last["timestamp"].isoformat()
    current_conf = _cap_confidence(last.get("confidence", last.get("ai_confidence", 0)))
    current_age_min = _signal_age_minutes(last.get("timestamp"))
    stale_threshold_min = _stale_threshold_minutes(symbol)

    # Look for non-HOLD signal within the LAST 100 bars (recent window)
    recent = df.tail(100)
    signals = recent[recent["signal"] != "HOLD"]

    if signals.empty:
        local_trend = "NEUTRAL"
        if pd.notna(last.get("EMA_20")) and pd.notna(last.get("EMA_50")):
            local_trend = "BULLISH" if float(last["EMA_20"]) >= float(last["EMA_50"]) else "BEARISH"
        return {
            "symbol": symbol,
            "signal": "HOLD",
            "direction": "HOLD",
            "signal_status": "NO_SIGNAL",
            "actionable": False,
            "signal_age_minutes": round(current_age_min, 1) if current_age_min is not None else None,
            "entry_distance_pct": 0.0,
            "entry_crossed": False,
            "chase_risk": False,
            "confluence_penalties": [],
            "confluence_score": 0,
            "confluence_factors": [],
            "local_trend": local_trend,
            "current_bar_signal": "HOLD",
            "confidence": current_conf,
            "entry": current_close,
            "close": current_close,
            "change_pct": day_chg_pct,
            "stop_loss": None,
            "target": None,
            "rsi": current_rsi,
            "trend": current_trend,
            "regime": current_regime,
            "timestamp": current_ts,
            "stale": current_age_min > stale_threshold_min,
            "stale_minutes": round(current_age_min, 1),
            "context_score": 0.0,
            "context_adjusted": False,
            "context_warning": None,
            "event_risk_active": False,
            "days_to_next_event": 99,
            "news_sentiment_24hr": 0.0,
            "news_sentiment_7d": 0.0,
            "drift_z_score": 0.0,
            "drift_detected": False,
            "drift_feature": "",
            "drift_adjusted": False,
        }

    last_signal = signals.iloc[-1]
    signal_age_min = _signal_age_minutes(last_signal.get("timestamp"))
    stale = signal_age_min > stale_threshold_min

    # Pull live indicator values from current bar (last row of df)
    def _safe_float(row, col):
        v = row.get(col)
        return round(float(v), 4) if v is not None and pd.notna(v) else None

    def _safe_int(row, col, default=0):
        v = row.get(col)
        return int(v) if v is not None and pd.notna(v) else default

    # Quality info from the signal bar
    quality_score = _safe_int(last_signal, "quality_score", 0)
    quality_grade = str(last_signal.get("quality_grade", "D") or "D")
    quality_breakdown_raw = last_signal.get("quality_breakdown", "{}")
    try:
        quality_breakdown = (
            json.loads(str(quality_breakdown_raw)) if quality_breakdown_raw else {}
        )
    except Exception:
        quality_breakdown = {}
    quality_hard_gate = str(last_signal.get("quality_hard_gate", "") or "")
    signal_conflict = str(last_signal.get("signal_conflict", "") or "")
    conflict_payload = last_signal.get("conflict")
    if not isinstance(conflict_payload, dict):
        conflict_payload = {
            "has_conflict": bool(signal_conflict),
            "severity": "warn" if signal_conflict else "none",
            "reasons": [signal_conflict] if signal_conflict else [],
            "confidence_penalty": 15 if signal_conflict else 0,
        }

    # Determine underlying direction for SKIP signals
    sig_label = str(last_signal["signal"])
    raw_dir = str(last_signal.get("_raw_direction", sig_label))

    raw_close = float(last_signal["close"])
    precision = 6 if raw_close < 0.01 else (4 if raw_close < 1.0 else (3 if raw_close < 10.0 else 2))

    # Entry price = signal bar's close with asset-magnitude-aware precision
    entry_price = round(raw_close, precision)

    # SL/Target from signal bar — validate they make sense relative to entry
    sl_raw = last_signal.get("stop_loss")
    tp_raw = last_signal.get("target")
    sl = round(float(sl_raw), precision) if pd.notna(sl_raw) else None
    tp = round(float(tp_raw), precision) if pd.notna(tp_raw) else None

    # Sanity check: SL must be on the correct side of entry
    effective_dir = sig_label if sig_label in ("LONG", "SHORT") else raw_dir
    if sl is not None and effective_dir == "LONG" and sl >= entry_price:
        # SL above entry for LONG = wrong, recalculate using current ATR
        atr_val = _safe_float(last, f"ATR_{settings.ATR_PERIOD}")
        if atr_val:
            sl = round(entry_price - atr_val * settings.ATR_SL_MULTIPLIER, precision)
            tp = round(
                entry_price
                + atr_val * settings.ATR_SL_MULTIPLIER * settings.MIN_RISK_REWARD,
                precision,
            )
    elif sl is not None and effective_dir == "SHORT" and sl <= entry_price:
        atr_val = _safe_float(last, f"ATR_{settings.ATR_PERIOD}")
        if atr_val:
            sl = round(entry_price + atr_val * settings.ATR_SL_MULTIPLIER, precision)
            tp = round(
                entry_price
                - atr_val * settings.ATR_SL_MULTIPLIER * settings.MIN_RISK_REWARD,
                precision,
            )

    local_trend = "NEUTRAL"
    if pd.notna(last.get("EMA_20")) and pd.notna(last.get("EMA_50")):
        e20 = float(last["EMA_20"])
        e50 = float(last["EMA_50"])
        c = float(last["close"])
        if e20 > e50 and c >= e20:
            local_trend = "BULLISH"
        elif e20 < e50 and c <= e20:
            local_trend = "BEARISH"
        elif e20 > e50:
            local_trend = "BULLISH"
        else:
            local_trend = "BEARISH"
    else:
        local_trend = current_trend

    # Determine trade status relative to current price
    signal_status = "ACTIVE"
    if tp is not None and effective_dir == "LONG" and current_close >= tp:
        signal_status = "TARGET_HIT"
    elif tp is not None and effective_dir == "SHORT" and current_close <= tp:
        signal_status = "TARGET_HIT"
    elif sl is not None and effective_dir == "LONG" and current_close <= sl:
        signal_status = "STOPPED_OUT"
    elif sl is not None and effective_dir == "SHORT" and current_close >= sl:
        signal_status = "STOPPED_OUT"
    elif stale:
        signal_status = "STALE"

    from api.data_honesty import entry_distance_pct, confluence_penalties
    entry_dist = entry_distance_pct(entry_price, current_close)
    is_long_dir = effective_dir in ("LONG", "BUY")
    is_short_dir = effective_dir in ("SHORT", "SELL")
    entry_crossed = (is_long_dir and entry_dist > 0.25) or (is_short_dir and entry_dist < -0.25)
    chase_risk = entry_crossed

    vwap_val = _safe_float(last, "VWAP")
    atr_val = _safe_float(last, f"ATR_{settings.ATR_PERIOD}")
    pens = confluence_penalties(
        signal=effective_dir,
        rsi=current_rsi,
        daily_trend=current_trend,
        close=current_close,
        entry=entry_price,
        vwap=vwap_val,
        atr=atr_val,
    )

    actionable = bool(
        not stale
        and sig_label in ("LONG", "SHORT")
        and signal_status == "ACTIVE"
        and not quality_hard_gate
        and not entry_crossed
    )

    try:
        current_mood_dict = _compute_market_mood()
    except Exception:
        current_mood_dict = {}

    display_conf, display_factors = _confluence_for_setup(
        {
            "signal": sig_label,
            "direction": effective_dir,
            "trend": current_trend,
            "regime": current_regime,
            "context_score": _safe_float(last_signal, "context_score") or 0.0,
            "drift_detected": bool(last_signal.get("drift_detected", False)),
            "event_risk_active": bool(last_signal.get("event_risk_active", False)),
            "conflict": conflict_payload,
            "rsi": current_rsi,
            "close": current_close,
            "entry": entry_price,
            "vwap": vwap_val,
            "atr": atr_val,
        },
        mood=current_mood_dict,
        for_display=True,
    )

    return {
        "symbol": symbol,
        "signal": sig_label,  # LONG / SHORT / SKIP / HOLD
        "direction": effective_dir,  # Underlying direction for SKIPs
        "signal_status": signal_status,
        "actionable": actionable,
        "signal_age_minutes": round(signal_age_min, 1) if signal_age_min is not None else None,
        "entry_distance_pct": entry_dist,
        "entry_crossed": entry_crossed,
        "chase_risk": chase_risk,
        "confluence_penalties": list(pens),
        "confluence_score": display_conf,
        "confluence_factors": display_factors,
        "local_trend": local_trend,
        "current_bar_signal": str(last.get("signal", "HOLD")),
        "confidence": _cap_confidence(
            last_signal.get("confidence", last_signal.get("ai_confidence", 0))
        ),
        "entry": entry_price,
        "close": current_close,  # always current price
        "change_pct": day_chg_pct,
        "stop_loss": sl,
        "target": tp,
        "rsi": current_rsi,  # always current RSI
        "trend": current_trend,  # always current trend
        "regime": current_regime,
        "timestamp": last_signal["timestamp"].isoformat(),
        "sparkline": [round(float(c), 2) for c in df["close"].tail(20)],
        "stale": stale,
        "stale_minutes": round(signal_age_min, 1),
        # Signal quality
        "quality_score": quality_score,
        "quality_grade": quality_grade,
        "quality_breakdown": quality_breakdown,
        "quality_hard_gate": quality_hard_gate,
        "skip_reason": quality_hard_gate if sig_label == "SKIP" else "",
        "signal_conflict": signal_conflict,
        "conflict": conflict_payload,
        # Data quality
        "data_quality": round(_data_quality_cache.get(symbol, 100.0), 1),
        # Live indicator values for signal card display
        "macd_hist": _safe_float(last, "MACD_hist"),
        "ema_20": _safe_float(last, "EMA_20"),
        "ema_50": _safe_float(last, "EMA_50"),
        # Context layer fields
        "context_score": _safe_float(last_signal, "context_score") or 0.0,
        "context_adjusted": bool(last_signal.get("context_adjusted", False)),
        "context_warning": str(last_signal.get("context_warning") or "") or None,
        "event_risk_active": bool(last_signal.get("event_risk_active", False)),
        "days_to_next_event": _safe_int(last_signal, "days_to_next_event", 99),
        "news_sentiment_24hr": _safe_float(last_signal, "news_sentiment_24hr") or 0.0,
        "news_sentiment_7d": _safe_float(last_signal, "news_sentiment_7d") or 0.0,
        # Drift guardrail fields
        "drift_z_score": _safe_float(last_signal, "drift_z_score") or 0.0,
        "drift_detected": bool(last_signal.get("drift_detected", False)),
        "drift_feature": str(last_signal.get("drift_feature") or ""),
        "drift_adjusted": bool(last_signal.get("drift_adjusted", False)),
    }


def _build_market_records(df: pd.DataFrame) -> list[dict]:
    """Convert a market DataFrame to a list of API-friendly record dicts."""
    records = []
    for _, row in df.iterrows():
        records.append(
            {
                "timestamp": row["timestamp"].isoformat(),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
                "volume": round(float(row["volume"]), 0),
                "rsi": round(float(row["RSI_14"]), 1)
                if pd.notna(row.get("RSI_14"))
                else None,
                "ema_20": round(float(row["EMA_20"]), 2)
                if pd.notna(row.get("EMA_20"))
                else None,
                "ema_50": round(float(row["EMA_50"]), 2)
                if pd.notna(row.get("EMA_50"))
                else None,
                "ema_200": round(float(row["EMA_200"]), 2)
                if pd.notna(row.get("EMA_200"))
                else None,
                "vwap": round(float(row["VWAP"]), 2)
                if pd.notna(row.get("VWAP"))
                else None,
                "bb_upper": round(float(row["BB_upper"]), 2)
                if pd.notna(row.get("BB_upper"))
                else None,
                "bb_lower": round(float(row["BB_lower"]), 2)
                if pd.notna(row.get("BB_lower"))
                else None,
                "adx": round(float(row[f"ADX_{settings.ADX_PERIOD}"]), 1)
                if pd.notna(row.get(f"ADX_{settings.ADX_PERIOD}"))
                else None,
                "signal": str(row.get("signal", "HOLD"))
                if "signal" in row.index
                else "HOLD",
            }
        )
    return records


def _compute_gainers_losers_from_cache(mode: str) -> list[dict]:
    """
    Fallback: compute gainers/losers from the in-memory _market_data cache
    when the NSE screener API is unavailable or times out.
    Compares current close vs day's first bar (open) for meaningful daily % change.
    """
    results = []
    for symbol, df in _market_data.items():
        if df is None or len(df) < 5:
            continue
        # Skip indices for screener (only stocks)
        if symbol.startswith(("NIFTY", "BANKNIFTY", "SENSEX")):
            continue
        try:
            close = float(df.iloc[-1]["close"])

            # Find today's open: first bar of the current trading day
            today = (
                df["timestamp"].iloc[-1].date()
                if hasattr(df["timestamp"].iloc[-1], "date")
                else None
            )
            if today:
                today_bars = df[df["timestamp"].dt.date == today]
                if len(today_bars) >= 2:
                    day_open = float(today_bars.iloc[0]["open"])
                else:
                    # Weekend/holiday: use first bar's open from available data
                    day_open = float(df.iloc[0]["open"])
            else:
                day_open = float(df.iloc[0]["open"])

            if day_open == 0:
                continue

            pct_change = ((close - day_open) / day_open) * 100

            # Volume: sum of today's bars (total daily volume so far)
            if today and len(today_bars) > 0:
                total_vol = float(today_bars["volume"].sum())
            else:
                total_vol = float(df["volume"].tail(20).sum())
            vol_cr = round(total_vol * close / 1e7, 1) if total_vol > 0 else 0.1

            # X-Factor: abs(pct_change) normalized later
            results.append(
                {
                    "symbol": symbol,
                    "company": symbol,
                    "sector": _get_sector_for_symbol(symbol),
                    "ltp": round(close, 2),
                    "pct_change": round(pct_change, 2),
                    "volume_cr": max(vol_cr, 0.1),
                    "x_factor": 1.0,  # normalized below
                    "oi_change": None,
                }
            )
        except Exception:
            continue

    if not results:
        return []

    # Normalize X-Factor relative to avg % change
    avg_abs = np.mean([abs(r["pct_change"]) for r in results]) or 1.0
    for r in results:
        r["x_factor"] = round(abs(r["pct_change"]) / avg_abs, 2)

    # Sort: gainers = highest pct_change, losers = lowest pct_change
    if mode == "gainers":
        results.sort(key=lambda x: x["pct_change"], reverse=True)
        results = [r for r in results if r["pct_change"] > 0]
    else:
        results.sort(key=lambda x: x["pct_change"])
        results = [r for r in results if r["pct_change"] < 0]

    return results[:20]


# Sector mapping for cache-based screener/heatmap
_SYMBOL_SECTOR_MAP = {
    "RELIANCE": "Energy",
    "TCS": "IT",
    "HDFCBANK": "Finance",
    "INFY": "IT",
    "ICICIBANK": "Finance",
    "SBIN": "Finance",
    "BHARTIARTL": "Telecom",
    "KOTAKBANK": "Finance",
    "LT": "Infra",
    "ITC": "FMCG",
    "AXISBANK": "Finance",
    "HINDUNILVR": "FMCG",
    "BAJFINANCE": "Finance",
    "MARUTI": "Auto",
    "TITAN": "Consumer",
    "WIPRO": "IT",
    "TATAMOTORS": "Auto",
    "SUNPHARMA": "Pharma",
    "ULTRACEMCO": "Infra",
    "ADANIENT": "Metals",
    "ADANIPORTS": "Infra",
    "ADANIPOWER": "Energy",
    "ADANIGREEN": "Energy",
    "TATASTEEL": "Metals",
    "JSWSTEEL": "Metals",
    "HINDALCO": "Metals",
    "COALINDIA": "Energy",
    "NTPC": "Energy",
    "POWERGRID": "Energy",
    "ONGC": "Energy",
    "BPCL": "Energy",
    "IOC": "Energy",
    "GAIL": "Energy",
    "TECHM": "IT",
    "HCLTECH": "IT",
    "DRREDDY": "Pharma",
    "CIPLA": "Pharma",
    "DIVISLAB": "Pharma",
    "APOLLOHOSP": "Pharma",
    "ASIANPAINT": "Consumer",
    "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG",
    "DABUR": "FMCG",
    "GODREJCP": "FMCG",
    "PIDILITIND": "Consumer",
    "BERGEPAINT": "Consumer",
    "HAVELLS": "Consumer",
    "VOLTAS": "Consumer",
    "SIEMENS": "Infra",
    "ABB": "Infra",
    "BHEL": "Infra",
    "M&M": "Auto",
    "EICHERMOT": "Auto",
    "HEROMOTOCO": "Auto",
    "BAJAJ-AUTO": "Auto",
    "TVSMOTOR": "Auto",
    "INDUSINDBK": "Finance",
    "BANDHANBNK": "Finance",
    "PNB": "Finance",
    "BANKBARODA": "Finance",
    "HAL": "Defence",
    "BEL": "Defence",
    "IRCTC": "Consumer",
    "ZOMATO": "Consumer",
    "TATAPOWER": "Energy",
    "DLF": "Realty",
    "GODREJPROP": "Realty",
    "OBEROIRLTY": "Realty",
    "PRESTIGE": "Realty",
    "TRENT": "Consumer",
    "DMART": "Consumer",
}

_SECTOR_ICONS = {
    "IT": "💻",
    "Finance": "🏦",
    "FMCG": "🛒",
    "Auto": "🚗",
    "Pharma": "💊",
    "Energy": "⚡",
    "Metals": "⚙️",
    "Infra": "🏗️",
    "Telecom": "📡",
    "Consumer": "🛍️",
    "Realty": "🏢",
    "Defence": "🛡️",
}


def _get_sector_for_symbol(symbol: str) -> str:
    """Get sector for a symbol from the mapping."""
    return _SYMBOL_SECTOR_MAP.get(symbol, "Others")


def _compute_sectors_from_cache() -> list[dict]:
    """
    Compute sector performance from _market_data cache.
    Groups symbols by sector, averages their daily % change.
    """
    sector_changes: dict[str, list[float]] = {}

    for symbol, df in _market_data.items():
        if df is None or len(df) < 5:
            continue
        if symbol.startswith(("NIFTY", "BANKNIFTY", "SENSEX")):
            continue

        sector = _get_sector_for_symbol(symbol)
        if sector == "Others":
            continue

        try:
            close = float(df.iloc[-1]["close"])
            today = (
                df["timestamp"].iloc[-1].date()
                if hasattr(df["timestamp"].iloc[-1], "date")
                else None
            )
            if today:
                today_bars = df[df["timestamp"].dt.date == today]
                day_open = (
                    float(today_bars.iloc[0]["open"])
                    if len(today_bars) >= 2
                    else float(df.iloc[0]["open"])
                )
            else:
                day_open = float(df.iloc[0]["open"])

            if day_open > 0:
                pct = ((close - day_open) / day_open) * 100
                sector_changes.setdefault(sector, []).append(pct)
        except Exception:
            continue

    sectors = []
    for name, changes in sector_changes.items():
        if not changes:
            continue
        avg_pct = round(float(np.mean(changes)), 2)
        total = len(changes) * 3  # approximate total stocks in sector
        adv = int(total * max(0.1, (avg_pct + 3.5) / 7.0))
        dec = total - adv

        sectors.append(
            {
                "name": name,
                "pct_change": avg_pct,
                "stocks": total,
                "adv": adv,
                "dec": dec,
                "icons": _SECTOR_ICONS.get(name, "📊"),
                "real_data": True,
            }
        )

    # Sort by absolute change (most active sectors first)
    sectors.sort(key=lambda x: abs(x["pct_change"]), reverse=True)
    return sectors


def _refresh_movers_cache() -> None:
    """Precompute gainers/losers from the in-memory market snapshot."""
    gainers = _compute_gainers_losers_from_cache("gainers")
    losers = _compute_gainers_losers_from_cache("losers")
    _movers_cache["gainers"] = gainers
    _movers_cache["losers"] = losers
    _movers_cache["updated_at"] = datetime.now().isoformat()


def _compute_market_mood() -> dict:
    """Compute market mood score from breadth, signals, regimes, and sectors."""
    gainers = _movers_cache.get("gainers") or []
    losers = _movers_cache.get("losers") or []
    if not gainers and not losers:
        gainers = _compute_gainers_losers_from_cache("gainers")
        losers = _compute_gainers_losers_from_cache("losers")

    total_movers = len(gainers) + len(losers)
    breadth_pct = (
        ((len(gainers) - len(losers)) / total_movers * 100.0) if total_movers else 0.0
    )

    long_count = 0
    short_count = 0
    hold_count = 0
    for symbol in _signal_cache:
        sig = _get_signal_for_symbol(symbol)
        if not sig:
            continue
        sig_label = str(sig.get("signal", "HOLD")).upper()
        if sig_label == "LONG":
            long_count += 1
        elif sig_label == "SHORT":
            short_count += 1
        else:
            hold_count += 1

    signal_total = long_count + short_count + hold_count
    signal_bias_pct = (
        ((long_count - short_count) / signal_total * 100.0) if signal_total else 0.0
    )

    # Breadth split score: % of tracked NSE stocks above 20-DMA proxy (EMA_20 in pipeline).
    index_symbols = {"NIFTY 50", "BANKNIFTY", "SENSEX", "NIFTY IT", "NIFTY BANK"}
    breadth_universe = 0
    breadth_above_20 = 0
    for symbol, df in _market_data.items():
        if "/" in str(symbol) or symbol in index_symbols:
            continue
        if df is None or df.empty:
            continue
        last = df.iloc[-1]
        close_val = last.get("close")
        dma20_val = last.get("EMA_20")
        if (
            close_val is None
            or dma20_val is None
            or pd.isna(close_val)
            or pd.isna(dma20_val)
        ):
            continue
        breadth_universe += 1
        if float(close_val) > float(dma20_val):
            breadth_above_20 += 1

    def _split_label(value: float | None) -> str:
        if value is None:
            return "UNKNOWN"
        if value > 55:
            return "BULLISH"
        if value < 45:
            return "BEARISH"
        return "NEUTRAL"

    if breadth_universe == 0:
        breadth_split_score = None
        breadth_split_label = "UNKNOWN (0 observations)"
        breadth_split_detail = "0/0 stocks above 20 DMA (no observations)"
    else:
        breadth_split_score = round(breadth_above_20 / breadth_universe * 100.0, 1)
        breadth_split_label = _split_label(breadth_split_score)
        breadth_split_detail = f"{breadth_above_20}/{breadth_universe} stocks above 20 DMA"

    buy_sell_total = long_count + short_count
    if buy_sell_total == 0:
        signal_bias_score = None
        signal_bias_label = "UNKNOWN (0 observations)"
        signal_bias_detail = "0 BUY out of 0 directional signals"
    else:
        signal_bias_score = round(long_count / buy_sell_total * 100.0, 1)
        signal_bias_label = _split_label(signal_bias_score)
        signal_bias_detail = f"{long_count} BUY out of {buy_sell_total} directional signals"

    if breadth_split_score is not None and signal_bias_score is not None:
        split_composite_score = round((breadth_split_score * 0.6) + (signal_bias_score * 0.4), 1)
    elif breadth_split_score is not None:
        split_composite_score = breadth_split_score
    elif signal_bias_score is not None:
        split_composite_score = signal_bias_score
    else:
        split_composite_score = None

    regime_counts = {"TRENDING": 0, "RANGING": 0, "HIGH_VOLATILITY": 0}
    for regime in _regime_cache.values():
        key = str(regime).upper()
        if key in regime_counts:
            regime_counts[key] += 1

    regime_total = sum(regime_counts.values())
    regime_bias = 0.0
    if regime_total:
        regime_bias = (
            (
                (regime_counts["TRENDING"] * 1.0)
                + (regime_counts["RANGING"] * 0.0)
                + (regime_counts["HIGH_VOLATILITY"] * -1.0)
            )
            / regime_total
            * 100.0
        )

    sectors = _compute_sectors_from_cache()
    avg_sector_move = (
        float(np.mean([s["pct_change"] for s in sectors])) if sectors else 0.0
    )
    sector_bias = max(min(avg_sector_move * 15.0, 100.0), -100.0)

    has_inputs = (total_movers > 0 or signal_total > 0 or breadth_universe > 0 or regime_total > 0 or len(sectors) > 0)
    if not has_inputs:
        mood = "UNKNOWN"
        score = None
    else:
        score = 50.0
        if total_movers > 0:
            score += breadth_pct * 0.25
        if signal_total > 0:
            score += signal_bias_pct * 0.35
        if regime_total > 0:
            score += regime_bias * 0.25
        if sectors:
            score += sector_bias * 0.15
        score = max(0.0, min(100.0, score))

        if score >= 68:
            mood = "BULLISH"
        elif score >= 52:
            mood = "NEUTRAL"
        elif score >= 40:
            mood = "CAUTIOUS"
        else:
            mood = "BEARISH"

    payload = {
        "mood": mood,
        "score": round(score, 1) if score is not None else 50.0,
        "mood_label": mood,
        "mood_score": round(score, 1) if score is not None else 50.0,
        "breadth": {
            "gainers": len(gainers),
            "losers": len(losers),
            "breadth_pct": round(breadth_pct, 1),
        },
        "signals": {
            "long": long_count,
            "short": short_count,
            "hold": hold_count,
            "bias_pct": round(signal_bias_pct, 1),
        },
        "breadth_signal_split": {
            "breadth": {
                "score": round(breadth_split_score, 1) if breadth_split_score is not None else None,
                "label": breadth_split_label,
                "detail": breadth_split_detail,
            },
            "signal_bias": {
                "score": round(signal_bias_score, 1) if signal_bias_score is not None else None,
                "label": signal_bias_label,
                "detail": signal_bias_detail,
            },
            "composite": {
                "score": round(split_composite_score, 1) if split_composite_score is not None else None,
                "note": "Breadth 60% weight · Signal Bias 40%",
            },
        },
        "regimes": regime_counts,
        "sector_avg_pct": round(avg_sector_move, 2),
        "updated_at": datetime.now().isoformat(),
    }

    _market_mood_cache.update(payload)
    return payload


def _build_ai_market_summary(mood_payload: dict | None = None) -> dict:
    """Generate a concise 2-3 line narrative summary from current market state."""
    mood = mood_payload or _compute_market_mood()
    gainers = list((_movers_cache.get("gainers") or [])[:2])
    losers = list((_movers_cache.get("losers") or [])[:2])

    top_gainer = gainers[0]["symbol"] if gainers else "NA"
    top_gainer_pct = gainers[0].get("pct_change", 0.0) if gainers else 0.0
    top_loser = losers[0]["symbol"] if losers else "NA"
    top_loser_pct = losers[0].get("pct_change", 0.0) if losers else 0.0

    lines = [
        (
            f"Market mood is {mood.get('mood', 'NEUTRAL')} "
            f"with score {mood.get('score', 50):.1f}/100."
        ),
        (
            f"Breadth: {mood.get('breadth', {}).get('gainers', 0)} gainers vs "
            f"{mood.get('breadth', {}).get('losers', 0)} losers; "
            f"signal bias {mood.get('signals', {}).get('bias_pct', 0):+.1f}%."
        ),
        (
            f"Top movers: {top_gainer} ({top_gainer_pct:+.2f}%) and "
            f"{top_loser} ({top_loser_pct:+.2f}%)."
        ),
    ]

    return {
        "lines": lines,
        "summary": " ".join(lines),
        "generated_at": datetime.now().isoformat(),
    }


def _top_quality_factors_from_breakdown(sig: dict, limit: int = 3) -> list[dict]:
    """Top positive contributors from quality_breakdown JSON (Phase 2 explainability)."""
    raw = sig.get("quality_breakdown")
    if isinstance(raw, dict):
        d = raw
    else:
        try:
            d = json.loads(str(raw or "{}"))
        except Exception:
            return []
    items: list[tuple[str, int]] = []
    for k, v in d.items():
        if not isinstance(v, (int, float)):
            continue
        iv = int(v)
        if iv <= 0:
            continue
        label = str(k).replace("_", " ").title()
        items.append((label, iv))
    items.sort(key=lambda x: x[1], reverse=True)
    return [{"factor": a, "points": b} for a, b in items[:limit]]


def _penalty_chips(drift: bool, event: bool, conflict: bool) -> list[dict]:
    out: list[dict] = []
    if drift:
        out.append({"name": "Drift guard", "points": 8.0})
    if event:
        out.append({"name": "Event risk", "points": 10.0})
    if conflict:
        out.append({"name": "Signal conflict", "points": 12.0})
    return out


def _phase2_composite_score(sig: dict, mood: dict) -> tuple[float, dict]:
    """
    Phase 2 ranking: base QS blend + freshness + market bias alignment − penalties.

    Advisory only — not a guarantee of future performance.
    """
    quality_score = int(sig.get("quality_score", 0))
    confidence = float(sig.get("confidence", 0))
    context_score = float(sig.get("context_score") or 0)
    ctx_norm = max(min((context_score + 1) / 2 * 100, 100), 0)
    base = quality_score * 0.50 + min(confidence, 85) * 0.35 + ctx_norm * 0.15

    stale = bool(sig.get("stale"))
    stale_min = float(sig.get("stale_minutes") or 0)
    if stale:
        freshness_mult = 0.5
    else:
        freshness_mult = max(0.55, 1.0 - min(stale_min / 50.0, 0.45))

    blended = base * (0.72 + 0.28 * freshness_mult)

    signal_label = str(sig.get("signal", "HOLD")).upper()
    bias = float((mood.get("signals") or {}).get("bias_pct", 0))
    regime_adj = 0.0
    if signal_label == "LONG":
        if bias > 7.0:
            regime_adj = 4.0
        elif bias < -7.0:
            regime_adj = -4.0
    elif signal_label == "SHORT":
        if bias < -7.0:
            regime_adj = 4.0
        elif bias > 7.0:
            regime_adj = -4.0

    cp = sig.get("conflict")
    has_conflict = (
        bool(cp.get("has_conflict"))
        if isinstance(cp, dict)
        else bool(sig.get("signal_conflict"))
    )
    drift = bool(sig.get("drift_detected"))
    event = bool(sig.get("event_risk_active"))

    penalty = 0.0
    if drift:
        penalty += 8.0
    if event:
        penalty += 10.0
    if has_conflict:
        penalty += 12.0

    composite_v2 = max(0.0, min(100.0, blended + regime_adj - penalty))

    explain: dict = {
        "base_blend": round(blended, 2),
        "freshness_mult": round(freshness_mult, 3),
        "regime_alignment_pts": round(regime_adj, 2),
        "penalty_total": round(penalty, 2),
        "top_contributors": _top_quality_factors_from_breakdown(sig, 3),
        "penalty_detail": _penalty_chips(drift, event, has_conflict),
    }
    return composite_v2, explain


def _calibration_for_setup(
    regime: str,
    quality_score: float,
    confidence: float,
    insights: dict,
) -> dict:
    """
    Advisory calibration from historical paper trades — not predictive of live markets.
    """
    rs = insights.get("regime_stats") or {}
    overall = insights.get("overall") or {}
    overall_wr = overall.get("win_rate")
    total_t = int(overall.get("total_trades", 0))

    rkey = str(regime or "RANGING").upper()
    reg_wr: float | None = None
    reg_n = 0
    for k, v in rs.items():
        if str(k).upper() == rkey and isinstance(v, dict):
            reg_wr = float(v.get("win_rate", 0))
            reg_n = int(v.get("trades", 0))
            break

    band = "Balanced"
    if confidence >= 70 and quality_score >= 72:
        band = "Aggressive"
    elif confidence < 45 or quality_score < 55:
        band = "Conservative"

    hist_note: str | None = None
    if reg_wr is not None and reg_n >= 3:
        hist_note = (
            f"{rkey}: ~{reg_wr:.0f}% WR in last {reg_n} similar-regime trades (paper)"
        )
    elif overall_wr is not None and total_t >= 5:
        hist_note = f"Overall paper WR ~{overall_wr:.0f}% ({total_t} trades)"

    return {
        "confidence_band": band,
        "historical_regime_wr": reg_wr,
        "historical_regime_trades": reg_n,
        "historical_note": hist_note,
    }


def _confluence_for_setup(sig: dict, mood: dict, for_display: bool = False) -> tuple[int, list[str]]:
    """
    Phase 5: independent factor alignment score 0–100 (advisory, not a trade signal).
    When for_display=True, applies technical and execution conflict penalties without altering baseline ranking.
    """
    factors: list[str] = []
    score = 0
    signal_label = str(sig.get("signal", "HOLD")).upper()
    trend = str(sig.get("trend", "NEUTRAL")).upper()
    regime = str(sig.get("regime", "RANGING")).upper()
    bias = float((mood.get("signals") or {}).get("bias_pct", 0))
    ctx = float(sig.get("context_score") or 0)

    if signal_label == "LONG" and "BULL" in trend:
        score += 22
        factors.append("Price trend aligned")
    elif signal_label == "SHORT" and "BEAR" in trend:
        score += 22
        factors.append("Price trend aligned")

    if regime == "TRENDING":
        score += 12
        factors.append("Trending regime")
    elif regime == "RANGING":
        score += 6
        factors.append("Ranging regime")

    if signal_label == "LONG" and bias > 5:
        score += 15
        factors.append("Market bias supportive")
    elif signal_label == "SHORT" and bias < -5:
        score += 15
        factors.append("Market bias supportive")
    elif abs(bias) <= 5:
        score += 5
        factors.append("Neutral market bias")

    if ctx > 0.1:
        score += 10
        factors.append("News context tailwind")
    elif ctx < -0.1:
        score -= 8
        factors.append("News context headwind")

    drift = bool(sig.get("drift_detected"))
    event = bool(sig.get("event_risk_active"))
    cp = sig.get("conflict", {})
    has_conflict = (
        bool(cp.get("has_conflict"))
        if isinstance(cp, dict)
        else bool(sig.get("signal_conflict"))
    )

    if not drift and not event and not has_conflict:
        score += 15
        factors.append("No active guardrail blocks")
    else:
        if drift:
            score -= 12
            factors.append("Drift penalty")
        if event:
            score -= 10
            factors.append("Event risk")
        if has_conflict:
            score -= 12
            factors.append("Signal conflict")

    if for_display:
        from api.data_honesty import confluence_penalties
        sig_dir = str(sig.get("signal") or sig.get("direction") or "HOLD")
        rsi_val = sig.get("rsi")
        daily_trend = sig.get("daily_trend") or sig.get("macro_trend") or sig.get("trend") or ""
        close_val = sig.get("close") or sig.get("current_price") or 0.0
        entry_val = sig.get("entry") or close_val
        vwap_val = sig.get("vwap")
        atr_val = sig.get("atr")
        pens = confluence_penalties(
            signal=sig_dir,
            rsi=rsi_val,
            daily_trend=daily_trend,
            close=close_val,
            entry=entry_val,
            vwap=vwap_val,
            atr=atr_val,
        )
        score += pens.penalty_sum
        for flag in pens:
            factors.append(f"Conflict: {flag}")

    score = max(0, min(100, score))
    return score, factors[:6]


def _compute_delta_since_last_refresh(
    current_signals: dict, current_mood_score: float
) -> dict:
    """
    Phase 3: compare current signal state against last snapshot.
    Returns new LONGs/SHORTs, dropped signals, confidence/QS changes, mood shift.
    """
    global _last_intelligence_snapshot
    prev = _last_intelligence_snapshot.get("signals", {})
    prev_mood = _last_intelligence_snapshot.get("mood_score")

    new_longs: list[str] = []
    new_shorts: list[str] = []
    dropped: list[str] = []
    improved: list[dict] = []
    degraded: list[dict] = []

    for sym, cur in current_signals.items():
        cur_sig = str(cur.get("signal", "HOLD")).upper()
        cur_qs = int(cur.get("quality_score", 0))
        cur_conf = float(cur.get("confidence", 0))
        prev_entry = prev.get(sym, {})
        prev_sig = (
            str(prev_entry.get("signal", "HOLD")).upper() if prev_entry else "HOLD"
        )

        if cur_sig == "LONG" and prev_sig != "LONG":
            new_longs.append(sym)
        elif cur_sig == "SHORT" and prev_sig != "SHORT":
            new_shorts.append(sym)
        elif prev_sig in ("LONG", "SHORT") and cur_sig not in ("LONG", "SHORT"):
            dropped.append(sym)

        if prev_entry:
            prev_qs = int(prev_entry.get("quality_score", 0))
            prev_conf = float(prev_entry.get("confidence", 0))
            qs_delta = cur_qs - prev_qs
            conf_delta = cur_conf - prev_conf
            if qs_delta >= 8:
                improved.append(
                    {
                        "symbol": sym,
                        "qs_delta": qs_delta,
                        "conf_delta": round(conf_delta, 1),
                    }
                )
            elif qs_delta <= -8:
                degraded.append(
                    {
                        "symbol": sym,
                        "qs_delta": qs_delta,
                        "conf_delta": round(conf_delta, 1),
                    }
                )

    improved.sort(key=lambda x: x["qs_delta"], reverse=True)
    degraded.sort(key=lambda x: x["qs_delta"])

    mood_shift: str | None = None
    if prev_mood is not None:
        diff = current_mood_score - prev_mood
        if abs(diff) >= 5:
            mood_shift = f"{'+' if diff > 0 else ''}{diff:.1f} pts vs last refresh"

    return {
        "new_longs": new_longs[:5],
        "new_shorts": new_shorts[:5],
        "dropped": dropped[:5],
        "improved": improved[:3],
        "degraded": degraded[:3],
        "mood_shift": mood_shift,
        "prev_captured_at": _last_intelligence_snapshot.get("captured_at"),
    }


def _sector_rotation_summary() -> list[dict]:
    """
    Phase 3: top 3 strongest and bottom 3 weakest sectors from cache.
    """
    sectors = _compute_sectors_from_cache()
    if not sectors:
        return []
    top3 = sectors[:3]
    bot3 = sectors[-3:] if len(sectors) > 3 else []
    result: list[dict] = []
    for s in top3:
        result.append(
            {
                "name": s["name"],
                "pct_change": s["pct_change"],
                "icon": s.get("icons", ""),
                "direction": "up",
            }
        )
    for s in reversed(bot3):
        result.append(
            {
                "name": s["name"],
                "pct_change": s["pct_change"],
                "icon": s.get("icons", ""),
                "direction": "down",
            }
        )
    return result


def _index_leadership_summary() -> dict:
    """
    Phase 3: lightweight Nifty/BankNifty leadership from in-memory 5m cache.
    """
    result: dict = {}
    for idx_name in ("NIFTY 50", "BANKNIFTY"):
        df = _market_data.get(idx_name)
        if df is None or df.empty:
            continue
        try:
            last = df.iloc[-1]
            close = float(last["close"])
            today = (
                last["timestamp"].date()
                if hasattr(last.get("timestamp"), "date")
                else None
            )
            if today:
                today_bars = df[df["timestamp"].dt.date == today]
                day_open = (
                    float(today_bars.iloc[0]["open"]) if len(today_bars) >= 2 else close
                )
            else:
                day_open = close
            pct = ((close - day_open) / day_open * 100) if day_open > 0 else 0.0
            regime = _regime_cache.get(idx_name, "RANGING")
            trend_raw = str(last.get("htf_trend", "NEUTRAL") or "NEUTRAL").upper()
            trend = (
                "BULLISH"
                if "BULL" in trend_raw
                else "BEARISH"
                if "BEAR" in trend_raw
                else "NEUTRAL"
            )
            result[idx_name] = {
                "close": round(close, 2),
                "pct_change": round(pct, 2),
                "regime": regime,
                "trend": trend,
            }
        except Exception:
            continue
    return result


_cached_overview_intelligence: dict | None = None
_cached_overview_time: float = 0.0
_OVERVIEW_CACHE_TTL_SEC: float = 15.0


def _compute_overview_intelligence(force_refresh: bool = False) -> dict:
    """
    Aggregated decision-cockpit payload for the Overview tab.

    Returns:
      top_setups      — top 5 actionable LONG/SHORT setups ranked by composite score
      avoid_list      — high-quality-but-blocked setups with explicit blocker reasons
      market_guardrails — mood, breadth split, regime summary, feed status
      ranking_meta    — counts: total tracked / actionable / blocked
      system_state    — capital, open trades, circuit breaker, portfolio heat
      learning_overlay — advisory hints from the feedback loop (safe, labelled as advisory)
      updated_at
    """
    global _cached_overview_intelligence, _cached_overview_time
    now_m = time.monotonic()
    if (
        not force_refresh
        and _cached_overview_intelligence is not None
        and (now_m - _cached_overview_time) < _OVERVIEW_CACHE_TTL_SEC
    ):
        return _cached_overview_intelligence

    top_setups: list[dict] = []
    avoid_list: list[dict] = []

    feed_status = _get_feed_status()
    is_market_live = bool(feed_status.get("is_market_open", False)) and str(feed_status.get("state", "")).upper() not in ("EOD", "CLOSED", "WEEKEND")

    mood = _compute_market_mood()
    insights_for_cal: dict = {}
    try:
        from agents.analytics.feedback_loop import get_learning_stats

        _ls = get_learning_stats()
        insights_for_cal = _ls.get("insights", {}) or {}
    except Exception:
        pass

    signals_to_process: list[dict] = []
    for symbol in list(_signal_cache.keys()):
        sig = _get_signal_for_symbol(symbol)
        if sig:
            signals_to_process.append(sig)

    # Cold-boot resilience: if local signal_cache has no items yet, fallback to persisted DB signals
    if not signals_to_process and _persisted_signals_snapshot:
        signals_to_process = list(_persisted_signals_snapshot)

    for sig in signals_to_process:
        symbol = str(sig.get("symbol", ""))
        if not symbol:
            continue

        signal_label = str(sig.get("signal", "HOLD")).upper()
        quality_score = int(sig.get("quality_score") or 0)
        confidence = float(sig.get("confidence") or 0)
        context_score = float(sig.get("context_score") or 0)
        drift_detected = bool(sig.get("drift_detected", False))
        event_risk = bool(sig.get("event_risk_active", False))
        stale = bool(sig.get("stale", False))
        skip_reason = str(sig.get("skip_reason") or "").strip()
        conflict_payload = sig.get("conflict", {})
        has_conflict = bool(
            conflict_payload.get("has_conflict", False)
            if isinstance(conflict_payload, dict)
            else False
        )

        composite, rank_explain = _phase2_composite_score(sig, mood)

        # Build a concise one-line thesis
        trend = str(sig.get("trend", "NEUTRAL")).replace("TrendDirection.", "").upper()
        regime = str(sig.get("regime", "RANGING")).upper()
        rsi = sig.get("rsi")
        thesis_parts: list[str] = []
        if "BULL" in trend:
            thesis_parts.append("Uptrend")
        elif "BEAR" in trend:
            thesis_parts.append("Downtrend")
        if regime == "TRENDING":
            thesis_parts.append("Trending regime")
        elif regime == "HIGH_VOLATILITY":
            thesis_parts.append("High volatility")
        if rsi is not None:
            if float(rsi) < 30:
                thesis_parts.append(f"RSI oversold ({float(rsi):.0f})")
            elif float(rsi) > 70:
                thesis_parts.append(f"RSI overbought ({float(rsi):.0f})")
        if context_score > 0.15:
            thesis_parts.append("Positive news context")
        elif context_score < -0.15:
            thesis_parts.append("Negative news context")
        thesis = ", ".join(thesis_parts) if thesis_parts else "Mixed conditions"

        # R:R label
        rr_label = "—"
        entry_val = sig.get("entry")
        sl_val = sig.get("stop_loss")
        tp_val = sig.get("target")
        if entry_val and sl_val and tp_val:
            risk_dist = abs(float(entry_val) - float(sl_val))
            reward_dist = abs(float(tp_val) - float(entry_val))
            if risk_dist > 0 and reward_dist > 0:
                rr_label = f"1:{reward_dist / risk_dist:.1f}"

        # Distance from current price to the proposed entry — chasing guard
        entry_distance_pct = None
        chase_risk = False
        close_val = sig.get("close")
        try:
            if entry_val is not None and close_val is not None:
                entry_f = float(entry_val)
                close_f = float(close_val)
                if entry_f > 0:
                    entry_distance_pct = round((close_f - entry_f) / entry_f * 100.0, 2)
                    if signal_label == "LONG":
                        chase_risk = entry_distance_pct >= 1.0
                    elif signal_label == "SHORT":
                        chase_risk = entry_distance_pct <= -1.0
        except (TypeError, ValueError):
            entry_distance_pct = None
            chase_risk = False

        cal = _calibration_for_setup(
            regime, float(quality_score), confidence, insights_for_cal
        )
        conf_n, conf_factors = _confluence_for_setup(sig, mood)

        blockers: list[str] = []
        if drift_detected:
            blockers.append("drift_detected")
        if event_risk:
            blockers.append("event_risk")
        if has_conflict:
            blockers.append("signal_conflict")
        # Only treat stale data as a hard blocker if market is actively open/live
        if is_market_live and stale:
            blockers.append("stale_data")

        entry_data = {
            "symbol": symbol,
            "signal": signal_label,
            "quality_score": quality_score,
            "quality_grade": sig.get("quality_grade", "D"),
            "confidence": round(confidence, 1),
            "composite_rank": round(composite, 1),
            "confluence_score": conf_n,
            "confluence_factors": conf_factors,
            "rank_explain": rank_explain,
            "calibration": cal,
            "rr": rr_label,
            "entry_distance_pct": entry_distance_pct,
            "chase_risk": chase_risk,
            "entry": entry_val,
            "stop_loss": sl_val,
            "target": tp_val,
            "close": sig.get("close"),
            "rsi": rsi,
            "regime": regime,
            "trend": trend,
            "context_score": round(context_score, 3),
            "thesis": thesis,
            "stale": stale,
            "drift_detected": drift_detected,
            "event_risk": event_risk,
            "macd_hist": sig.get("macd_hist"),
            "blockers": blockers,
        }

        # Qualifying actionable setups: LONG/SHORT, good quality and confidence, and no active blockers
        if (
            signal_label in ("LONG", "SHORT")
            and quality_score >= 55
            and confidence >= 40
            and not blockers
        ):
            top_setups.append(entry_data)

        # Blocked high-quality setups (either SKIP or blocked LONG/SHORT)
        elif (
            (signal_label in ("LONG", "SHORT") and blockers)
            or (signal_label == "SKIP" and quality_score >= 60)
        ):
            if not blockers:
                blockers.append(skip_reason or "quality_gate")
            entry_data["blockers"] = blockers
            avoid_list.append(entry_data)

    top_setups.sort(key=lambda x: (0 if x.get("stale") else 1, x["composite_rank"]), reverse=True)
    avoid_list.sort(key=lambda x: x["composite_rank"], reverse=True)

    fallback_applied = False
    if not top_setups and avoid_list:
        fallback_candidates = [
            s for s in avoid_list
            if s.get("signal") in ("LONG", "SHORT") and int(s.get("quality_score") or 0) >= 55
        ]
        if fallback_candidates:
            top_setups = fallback_candidates[:5]
            fallback_applied = True

    if not top_setups:
        # Off-hours fallback to latest SwingSpectrum shortlist candidates
        swing_candidates = _swing_cache.get("setups") or []
        for s in swing_candidates[:5]:
            entry_p = float(s.get("entry_price") or s.get("entry") or 0.0)
            sl_p = float(s.get("stop_loss") or 0.0)
            tp_p = float(s.get("target") or 0.0)
            qs = int(s.get("confluence_score") or s.get("score") or 75)
            q_grade = "A" if qs >= 75 else ("B" if qs >= 60 else "C")
            top_setups.append({
                "symbol": str(s.get("symbol", "")),
                "signal": str(s.get("direction") or s.get("signal") or "LONG"),
                "quality_score": qs,
                "quality_grade": q_grade,
                "confidence": float(s.get("confidence") or 70.0),
                "composite_rank": float(qs),
                "entry": entry_p,
                "stop_loss": sl_p,
                "target": tp_p,
                "trend": str(s.get("trend") or "TRENDING"),
                "regime": str(s.get("regime") or "SWING_DAILY"),
                "blockers": [],
                "session_badge": "LAST SESSION SWING",
                "is_off_hours_fallback": True,
            })
        if top_setups:
            fallback_applied = True

    if not top_setups and _persisted_signals_snapshot:
        for s in _persisted_signals_snapshot[:5]:
            sig = s.get("signal")
            if sig in ("LONG", "SHORT", "BUY", "SELL"):
                sig_norm = "LONG" if sig in ("LONG", "BUY") else "SHORT"
                qs = int(s.get("quality_score") or 70)
                q_grade = "A" if qs >= 75 else ("B" if qs >= 60 else "C")
                top_setups.append({
                    "symbol": str(s.get("symbol", "")),
                    "signal": sig_norm,
                    "quality_score": qs,
                    "quality_grade": q_grade,
                    "confidence": float(s.get("confidence") or 65.0),
                    "composite_rank": float(qs),
                    "entry": float(s.get("entry") or s.get("close") or 0.0),
                    "stop_loss": float(s.get("stop_loss") or 0.0),
                    "target": float(s.get("target") or 0.0),
                    "trend": str(s.get("trend") or "NEUTRAL"),
                    "regime": str(s.get("regime") or "LAST_SESSION"),
                    "blockers": [],
                    "session_badge": "LAST SESSION INTRADAY",
                    "is_off_hours_fallback": True,
                })
        if top_setups:
            fallback_applied = True

    # ── Honesty gate: a setup with unusable risk geometry is not a setup ─────
    from api.data_honesty import filter_valid_setups

    top_setups, rejected_setups = filter_valid_setups(top_setups)
    for rejected, reason in rejected_setups:
        rejected["blockers"] = list(rejected.get("blockers") or []) + [reason]
        rejected["invalid_levels"] = True
        avoid_list.append(rejected)
    if rejected_setups:
        avoid_list.sort(key=lambda item: item.get("composite_rank") or 0.0, reverse=True)

    n_top = len(top_setups)
    for idx, t in enumerate(top_setups):
        if n_top <= 1:
            t["setup_percentile"] = 100.0
        else:
            t["setup_percentile"] = round(100.0 * (1.0 - idx / (n_top - 1)), 1)

    # ── Phase 4: opportunity timeline — record first-seen per actionable setup ─
    now_iso = datetime.now().isoformat()
    active_syms = {t["symbol"] for t in top_setups}
    for sym in list(_setup_first_seen.keys()):
        if sym not in active_syms:
            del _setup_first_seen[sym]
    for t in top_setups:
        sym = t["symbol"]
        if sym not in _setup_first_seen:
            _setup_first_seen[sym] = now_iso
        t["first_seen"] = _setup_first_seen[sym]

    # ── Phase 3: freshness age in minutes per setup ──────────────────────────
    now_ts = datetime.now()
    for t in top_setups:
        sym = t["symbol"]
        df = _market_data.get(sym)
        if df is not None and not df.empty:
            try:
                last_ts = df.iloc[-1]["timestamp"]
                if hasattr(last_ts, "to_pydatetime"):
                    last_ts = last_ts.to_pydatetime()
                if last_ts.tzinfo is not None:
                    last_ts = last_ts.replace(tzinfo=None)
                age_min = (now_ts - last_ts).total_seconds() / 60
                t["data_age_min"] = round(age_min, 1)
            except Exception:
                t["data_age_min"] = None
        else:
            t["data_age_min"] = None

    # ── Market guardrails ────────────────────────────────────────────────────
    feed_status = _get_feed_status()

    regime_counts: dict[str, int] = {"TRENDING": 0, "RANGING": 0, "HIGH_VOLATILITY": 0}
    for r in _regime_cache.values():
        k = str(r).upper()
        if k in regime_counts:
            regime_counts[k] += 1
    regime_sum = sum(regime_counts.values())
    total_regimes = regime_sum or 1
    dominant_regime = max(regime_counts, key=lambda k: regime_counts[k]) if regime_sum > 0 else "UNKNOWN"

    # ── System / risk state ──────────────────────────────────────────────────
    engine = _get_engine()
    stats = engine.get_stats()
    risk = engine.get_risk_state()
    open_trades = len(engine.get_open_trades())

    # ── Learning overlay (advisory) ──────────────────────────────────────────
    learning_hint: dict | None = None
    try:
        overall = insights_for_cal.get("overall", {})
        if int(overall.get("total_trades", 0)) >= 10:
            best_signal = insights_for_cal.get("best_signal_type", "—")
            best_regime = insights_for_cal.get("best_regime", "—")
            wr = float(overall.get("win_rate", 0))
            model_acc = float(overall.get("model_accuracy", 0))
            learning_hint = {
                "total_learnt_trades": int(overall["total_trades"]),
                "overall_win_rate": round(wr, 1),
                "model_accuracy": round(model_acc, 1),
                "best_signal_type": best_signal,
                "best_regime": best_regime,
                "advisory": (
                    f"Based on {int(overall['total_trades'])} closed trades: "
                    f"best setup is {best_signal} in {best_regime} regime "
                    f"(win rate {wr:.1f}%)"
                ),
                "phase2_note": (
                    "Phase 2 rank = QS/conf/context blend + freshness + market bias alignment "
                    "minus drift/event/conflict penalties. Historical WR is from paper trades by regime — advisory only."
                ),
            }
    except Exception:
        pass

    br = mood.get("breadth") or {}
    sigs_m = mood.get("signals") or {}
    sec_avg = float(mood.get("sector_avg_pct", 0) or 0)
    why_hint = (
        f"Mood {mood.get('mood', 'NEUTRAL')} ({float(mood.get('score', 50)):.0f}/100). "
        f"Gainers vs losers {br.get('gainers', 0)}:{br.get('losers', 0)}. "
        f"Signal bias {float(sigs_m.get('bias_pct', 0)):+.1f}%. "
        f"Sector avg move {sec_avg:+.2f}%."
    )

    # ── Phase 3: delta, sector rotation, index leadership ───────────────────
    current_mood_score = float(mood.get("score", 50))
    current_signals_snapshot: dict = {}
    for symbol in list(_signal_cache.keys()):
        sig = _get_signal_for_symbol(symbol)
        if sig:
            current_signals_snapshot[symbol] = {
                "signal": str(sig.get("signal", "HOLD")).upper(),
                "quality_score": int(sig.get("quality_score", 0)),
                "confidence": float(sig.get("confidence", 0)),
            }

    delta = _compute_delta_since_last_refresh(
        current_signals_snapshot, current_mood_score
    )
    sector_rotation = _sector_rotation_summary()
    index_leadership = _index_leadership_summary()

    # Portfolio-aware caution flags
    heat_pct = round(float(risk.get("portfolio_heat_pct", 0)) * 100, 1)
    cb_active = bool(risk.get("circuit_breaker_active", False))
    consec_losses = int(risk.get("consecutive_losses", 0))
    portfolio_caution: dict = {
        "heat_warning": heat_pct >= 60,
        "heat_pct": heat_pct,
        "circuit_breaker_active": cb_active,
        "circuit_breaker_reason": str(risk.get("circuit_breaker_reason") or ""),
        "consecutive_losses": consec_losses,
        "caution_level": (
            "HIGH"
            if cb_active or heat_pct >= 80
            else "MEDIUM"
            if heat_pct >= 60 or consec_losses >= 3
            else "LOW"
        ),
        "caution_message": (
            "Circuit breaker active — no new trades recommended."
            if cb_active
            else f"Portfolio heat at {heat_pct:.0f}% — reduce position sizing."
            if heat_pct >= 60
            else f"{consec_losses} consecutive losses — consider smaller sizes."
            if consec_losses >= 3
            else ""
        ),
    }

    # Update snapshot for next delta computation
    _last_intelligence_snapshot["signals"] = current_signals_snapshot
    _last_intelligence_snapshot["mood_score"] = current_mood_score
    _last_intelligence_snapshot["captured_at"] = datetime.now().isoformat()

    updated_at = datetime.now().isoformat()
    truly_actionable = [
        t for t in top_setups
        if not t.get("stale")
        and not t.get("blockers")
        and not t.get("invalid_levels")
        and not t.get("is_off_hours_fallback")
    ]
    actionable_count = len(truly_actionable) if is_market_live else 0

    result = {
        "top_setups": top_setups[:5],
        "avoid_list": avoid_list[:5],
        "ranking_meta": {
            "total_tracked": len(signals_to_process),
            "actionable": actionable_count,
            "blocked_high_quality": len(avoid_list),
            "fallback_applied": fallback_applied,
            "session_state": "LIVE" if is_market_live else "EOD",
            "scoring_version": "phase5_v1",
            "scoring_summary": (
                "Composite blends quality, confidence, and context; freshness decay; "
                "market bias alignment; drift/event/conflict penalties. "
                "Confluence = independent factor alignment (trend, regime, bias, context, guardrails) — advisory."
            ),
        },
        "market_guardrails": {
            "mood": mood.get("mood", "NEUTRAL"),
            "mood_score": round(current_mood_score, 1),
            "breadth_split": mood.get("breadth_signal_split", {}),
            "regime_summary": {
                "dominant": dominant_regime,
                "counts": regime_counts,
                "pct_trending": round(
                    regime_counts["TRENDING"] / total_regimes * 100, 1
                ),
            },
            "feed_status": feed_status,
            "sector_avg_pct": round(sec_avg, 2),
            "why_market_hint": why_hint,
        },
        "system_state": {
            "capital": round(float(stats.get("capital", 100000)), 2),
            "total_pnl": round(float(stats.get("total_pnl", 0)), 2),
            "open_trades": open_trades,
            "circuit_breaker_active": cb_active,
            "circuit_breaker_reason": str(risk.get("circuit_breaker_reason") or ""),
            "portfolio_heat_pct": heat_pct,
            "daily_drawdown_pct": round(float(risk.get("daily_loss_pct", 0)) * 100, 2),
            "consecutive_losses": consec_losses,
            "last_market_refresh": (
                _last_refresh.isoformat() if _last_refresh != datetime.min else None
            ),
        },
        "learning_overlay": learning_hint,
        "delta": delta,
        "sector_rotation": sector_rotation,
        "index_leadership": index_leadership,
        "portfolio_caution": portfolio_caution,
        "updated_at": updated_at,
    }
    _cached_overview_intelligence = result
    _cached_overview_time = time.monotonic()
    return result


# ── Phase 6: Crypto intelligence caches ───────────────────────────────────────
# Keyed futures context snapshot: symbol → {funding_rate, oi, basis, …}
_crypto_futures_cache: dict[str, dict] = {}
_crypto_futures_updated_at: datetime | None = None
# 4h HTF trend label per symbol, used by the MTF confluence gate (A1).
# Values: "BULLISH" / "BEARISH" / "NEUTRAL" / None (= unknown / not fetched).
_crypto_htf4_trend: dict[str, str | None] = {}
# Crypto-specific first-seen tracker for opportunity timeline
_crypto_setup_first_seen: dict[str, str] = {}
# Crypto theme classification
_CRYPTO_THEMES: dict[str, str] = {
    "BTC/USDT": "Store of Value",
    "ETH/USDT": "Smart Contract L1",
    "SOL/USDT": "Smart Contract L1",
    "BNB/USDT": "Exchange Token",
    "XRP/USDT": "Payments",
    "ADA/USDT": "Smart Contract L1",
    "AVAX/USDT": "Smart Contract L1",
    "DOT/USDT": "Interoperability",
    "MATIC/USDT": "Scaling L2",
    "LINK/USDT": "Oracle",
    "UNI/USDT": "DeFi",
    "DOGE/USDT": "Meme",
    "SHIB/USDT": "Meme",
    "LTC/USDT": "Payments",
    "ATOM/USDT": "Interoperability",
}
_BINANCE_FAPI_BASE = "https://fapi.binance.com/fapi/v1"


def _fetch_binance_futures_context(symbols: list[str]) -> dict[str, dict]:
    """
    Phase 6 – Futures-context layer.
    Fetches funding rate + open interest from Binance public futures endpoints.
    Advisory only — never implies broker-grade precision.
    Degrades gracefully: returns {} on any failure.
    """
    import json as _json
    import urllib.request

    result: dict[str, dict] = {}

    # Convert CCXT format BTC/USDT → BTCUSDT for Binance REST
    def _to_binance(sym: str) -> str:
        return sym.replace("/", "")

    funding_data: dict[str, dict] = {}
    oi_data: dict[str, dict] = {}

    # Bulk premium index endpoint (funding, mark price, index price)
    try:
        url = f"{_BINANCE_FAPI_BASE}/premiumIndex"
        with urllib.request.urlopen(url, timeout=6) as resp:
            raw = _json.loads(resp.read().decode())
        if isinstance(raw, list):
            for item in raw:
                sym_b = str(item.get("symbol", ""))
                funding_data[sym_b] = item
    except Exception as exc:
        logger.warning("Binance FAPI premiumIndex failed: %s", exc)

    # Bulk open interest — no key needed, public
    for sym in symbols:
        bsym = _to_binance(sym)
        try:
            url = f"{_BINANCE_FAPI_BASE}/openInterest?symbol={bsym}"
            with urllib.request.urlopen(url, timeout=6) as resp:
                raw = _json.loads(resp.read().decode())
            if isinstance(raw, dict):
                oi_data[bsym] = raw
        except Exception:
            pass  # silently skip; spot signal still works

    for sym in symbols:
        bsym = _to_binance(sym)
        fi = funding_data.get(bsym, {})
        oi = oi_data.get(bsym, {})
        try:
            fr = float(fi.get("lastFundingRate") or 0.0)
        except Exception:
            fr = 0.0
        try:
            oi_val = float(oi.get("openInterest") or 0.0)
        except Exception:
            oi_val = 0.0

        # Basis: mark price - index price (positive = futures premium)
        try:
            mark = float(fi.get("markPrice") or 0.0)
            index_p = float(fi.get("indexPrice") or mark)
            basis_pct = ((mark - index_p) / index_p * 100) if index_p > 0 else 0.0
        except Exception:
            basis_pct = 0.0

        # Advisory funding annotation
        if fr > 0.001:
            fr_label = "HIGH_POSITIVE"  # longs paying — risk of washout
        elif fr < -0.001:
            fr_label = "HIGH_NEGATIVE"  # shorts paying — squeeze potential
        elif abs(fr) < 0.0002:
            fr_label = "NEUTRAL"
        else:
            fr_label = "MODERATE"

        oi_label = "HIGH" if oi_val > 1e9 else "MODERATE" if oi_val > 1e8 else "LOW"

        result[sym] = {
            "symbol": sym,
            "funding_rate": round(fr, 6),
            "funding_rate_pct": round(fr * 100, 4),
            "funding_label": fr_label,
            "open_interest": oi_val,
            "oi_label": oi_label,
            "basis_pct": round(basis_pct, 4),
            "advisory": True,
            "source": "Binance FAPI public",
        }

    return result


def _crypto_futures_modifier(sym: str, signal_label: str) -> tuple[float, str | None]:
    """
    Phase 6 – futures modifier for composite score: ±4 pts advisory adjustment.
    Returns (score_delta, hint_message).
    """
    fc = _crypto_futures_cache.get(sym, {})
    if not fc:
        return 0.0, None
    fr_label = fc.get("funding_label", "NEUTRAL")
    hint: str | None = None
    delta = 0.0
    if signal_label == "LONG" and fr_label == "HIGH_NEGATIVE":
        delta += 3.0
        hint = "Shorts paying — squeeze potential"
    elif signal_label == "LONG" and fr_label == "HIGH_POSITIVE":
        delta -= 3.0
        hint = "Longs crowded — washout risk"
    elif signal_label == "SHORT" and fr_label == "HIGH_POSITIVE":
        delta += 3.0
        hint = "Longs crowded — reversion potential"
    elif signal_label == "SHORT" and fr_label == "HIGH_NEGATIVE":
        delta -= 3.0
        hint = "Shorts crowded — squeeze risk"
    return delta, hint


def _compute_crypto_intelligence() -> dict:
    """
    Phase 6 – aggregated crypto decision-cockpit payload.
    Mirrors the NSE intelligence contract so the frontend can reuse cockpit patterns.
    """
    top_setups: list[dict] = []
    premium_pool: list[dict] = []
    exploratory_pool: list[dict] = []
    avoid_list: list[dict] = []

    mood_score = 50.0
    mood_label = "NEUTRAL"
    btc_pct = 0.0
    eth_pct = 0.0
    gainers_n = 0
    losers_n = 0

    # ── Market mood from live ticker (best-effort) ────────────────────────────
    try:
        import json as _jj
        import urllib.request as _ur

        raw_t = _jj.loads(
            _ur.urlopen("https://api.binance.com/api/v3/ticker/24hr", timeout=6)
            .read()
            .decode()
        )
        if isinstance(raw_t, list):
            usdt = [
                t
                for t in raw_t
                if isinstance(t, dict)
                and str(t.get("symbol", "")).endswith("USDT")
                and float(t.get("quoteVolume") or 0) > 1_000_000
                and not any(
                    k in str(t.get("symbol", ""))
                    for k in ("DOWN", "UP", "BEAR", "BULL")
                )
            ]
            gainers_n = sum(
                1 for t in usdt if float(t.get("priceChangePercent") or 0) > 0
            )
            losers_n = sum(
                1 for t in usdt if float(t.get("priceChangePercent") or 0) < 0
            )
            total = gainers_n + losers_n or 1
            breadth_ratio = (gainers_n - losers_n) / total
            btc_ticker = next((t for t in usdt if t.get("symbol") == "BTCUSDT"), {})
            eth_ticker = next((t for t in usdt if t.get("symbol") == "ETHUSDT"), {})
            btc_pct = float(btc_ticker.get("priceChangePercent") or 0)
            eth_pct = float(eth_ticker.get("priceChangePercent") or 0)
            # Combined mood: weighted BTC + breadth
            raw_mood = btc_pct * 0.5 + breadth_ratio * 50
            mood_score = max(0.0, min(100.0, 50.0 + raw_mood))
            if btc_pct > 3 and breadth_ratio > 0.2:
                mood_label = "BULLISH"
            elif btc_pct < -3 and breadth_ratio < -0.2:
                mood_label = "BEARISH"
            elif btc_pct > 1 or breadth_ratio > 0.1:
                mood_label = "CAUTIOUS_BULLISH"
            elif btc_pct < -1 or breadth_ratio < -0.1:
                mood_label = "CAUTIOUS_BEARISH"
    except Exception as exc:
        logger.warning("Crypto mood fetch failed in intelligence: %s", exc)

    # ── Fear & Greed ─────────────────────────────────────────────────────────
    fg_value: int | None = None
    try:
        import json as _jj2
        import urllib.request as _ur2

        fg_raw = _jj2.loads(
            _ur2.urlopen("https://api.alternative.me/fng/?limit=1", timeout=5)
            .read()
            .decode()
        )
        if isinstance(fg_raw, dict):
            fg_s = (fg_raw.get("data") or [{}])[0].get("value")
            fg_value = int(fg_s) if fg_s is not None else None
    except Exception:
        pass

    # ── Signals from the in-memory cache (aligned with NSE flow) ────────────
    now_iso = datetime.now().isoformat()
    now_ts = datetime.now()
    active_syms: set[str] = set()

    q_floor = max(0, int(getattr(settings, "CRYPTO_QUALITY_FLOOR", 45) or 45))
    c_floor = float(getattr(settings, "CRYPTO_CONF_FLOOR", 35) or 35)
    q_rel = max(0, int(getattr(settings, "CRYPTO_QUALITY_FLOOR_RELAXED", 40) or 40))
    c_rel = float(getattr(settings, "CRYPTO_CONF_FLOOR_RELAXED", 28) or 28)
    if q_rel > q_floor:
        q_rel = q_floor
    if c_rel > c_floor:
        c_rel = c_floor
    sparse_enabled = bool(getattr(settings, "CRYPTO_SPARSE_FILL_ENABLED", True))
    sparse_target = max(1, int(getattr(settings, "CRYPTO_SPARSE_TARGET", 5) or 5))

    for sym_ccxt in list(_signal_cache.keys()):
        if "/" not in sym_ccxt:
            continue  # skip NSE symbols
        sig = _get_signal_for_symbol(sym_ccxt)
        if not sig:
            continue

        signal_label = str(sig.get("signal", "HOLD")).upper()
        quality_score = int(sig.get("quality_score", 0))
        confidence = float(sig.get("confidence", 0))
        context_score = float(sig.get("context_score") or 0)
        drift = bool(sig.get("drift_detected", False))
        event_risk = bool(sig.get("event_risk_active", False))
        stale = bool(sig.get("stale", False))
        skip_reason = str(sig.get("skip_reason") or "").strip()
        cp = sig.get("conflict", {})
        has_conflict = bool(cp.get("has_conflict")) if isinstance(cp, dict) else False

        # Composite: reuse Phase 5 framework
        mood_dict: dict = {
            "mood": mood_label,
            "score": mood_score,
            "signals": {"bias_pct": (btc_pct if signal_label == "LONG" else -btc_pct)},
        }
        composite, rank_explain = _phase2_composite_score(sig, mood_dict)
        conf_n, conf_factors = _confluence_for_setup(sig, mood_dict)

        # Phase 6: futures modifier
        futures_delta, futures_hint = _crypto_futures_modifier(sym_ccxt, signal_label)
        composite = max(0.0, min(100.0, composite + futures_delta))
        if futures_hint:
            rank_explain["futures_hint"] = futures_hint
            rank_explain["futures_delta"] = round(futures_delta, 2)

        trend = str(sig.get("trend", "NEUTRAL")).replace("TrendDirection.", "").upper()
        regime = str(sig.get("regime", "RANGING")).upper()
        rsi = sig.get("rsi")
        thesis_parts: list[str] = []
        if "BULL" in trend:
            thesis_parts.append("Uptrend")
        elif "BEAR" in trend:
            thesis_parts.append("Downtrend")
        if regime == "TRENDING":
            thesis_parts.append("Trending")
        if rsi is not None:
            if float(rsi) < 30:
                thesis_parts.append(f"RSI oversold ({float(rsi):.0f})")
            elif float(rsi) > 70:
                thesis_parts.append(f"RSI overbought ({float(rsi):.0f})")
        if futures_hint:
            thesis_parts.append(futures_hint)
        if context_score > 0.15:
            thesis_parts.append("Positive news")
        elif context_score < -0.15:
            thesis_parts.append("Negative news")
        thesis = ", ".join(thesis_parts) if thesis_parts else "Mixed conditions"

        entry_val = sig.get("entry")
        sl_val = sig.get("stop_loss")
        tp_val = sig.get("target")
        rr_label = "—"
        if entry_val and sl_val and tp_val:
            rd = abs(float(entry_val) - float(sl_val))
            rw = abs(float(tp_val) - float(entry_val))
            if rd > 0 and rw > 0:
                rr_label = f"1:{rw / rd:.1f}"

        # Freshness
        data_age_min: float | None = None
        df = _market_data.get(sym_ccxt)
        if df is not None and not df.empty:
            try:
                last_ts = df.iloc[-1]["timestamp"]
                if hasattr(last_ts, "to_pydatetime"):
                    last_ts = last_ts.to_pydatetime()
                if last_ts.tzinfo is not None:
                    last_ts = last_ts.replace(tzinfo=None)
                data_age_min = round((now_ts - last_ts).total_seconds() / 60, 1)
            except Exception:
                pass

        # Crypto Phase 1 — volume confirmation (A2)
        # Pulls volume_ratio_20 (already populated by calculate_indicators) from
        # the most recent bar. Missing/NaN → ratio=None → gate is skipped.
        volume_ratio: float | None = None
        if df is not None and not df.empty and "volume_ratio_20" in df.columns:
            try:
                raw_vr = df.iloc[-1]["volume_ratio_20"]
                if pd.notna(raw_vr):
                    volume_ratio = float(raw_vr)
            except Exception:
                volume_ratio = None

        # Crypto Phase 1 — funding rate hard gate inputs (A4)
        # Pulls the raw (per-interval) funding rate from the futures cache.
        # A missing entry → funding_rate=None → gate is skipped for that symbol.
        futures_raw = _crypto_futures_cache.get(sym_ccxt, {}) or {}
        funding_rate: float | None = None
        if "funding_rate" in futures_raw:
            try:
                funding_rate = float(futures_raw.get("funding_rate"))
            except (TypeError, ValueError):
                funding_rate = None

        # Crypto Phase 1 — MTF confluence input (A1).
        # Pulls the cached 4h trend label (populated by _refresh_market_data).
        # Missing value → gate skipped for this symbol (same pattern as A2/A4).
        mtf_trend_4h = _crypto_htf4_trend.get(sym_ccxt)
        if isinstance(mtf_trend_4h, str):
            mtf_trend_4h = mtf_trend_4h.upper() or None

        # First-seen for timeline
        if signal_label in ("LONG", "SHORT"):
            active_syms.add(sym_ccxt)
            if sym_ccxt not in _crypto_setup_first_seen:
                _crypto_setup_first_seen[sym_ccxt] = now_iso

        entry_data: dict = {
            "symbol": sym_ccxt,
            "signal": signal_label,
            "quality_score": quality_score,
            "quality_grade": sig.get("quality_grade", "D"),
            "confidence": round(confidence, 1),
            "composite_rank": round(composite, 1),
            "confluence_score": conf_n,
            "confluence_factors": conf_factors,
            "rank_explain": rank_explain,
            "rr": rr_label,
            "entry": entry_val,
            "stop_loss": sl_val,
            "target": tp_val,
            "close": sig.get("close"),
            "rsi": rsi,
            "regime": regime,
            "trend": trend,
            "context_score": round(context_score, 3),
            "thesis": thesis,
            "stale": stale,
            "drift_detected": drift,
            "event_risk": event_risk,
            "data_age_min": data_age_min,
            "theme": _CRYPTO_THEMES.get(sym_ccxt, "Other"),
            "futures_context": _crypto_futures_cache.get(sym_ccxt, {}),
            "first_seen": _crypto_setup_first_seen.get(sym_ccxt),
            "volume_ratio": round(volume_ratio, 2)
            if volume_ratio is not None
            else None,
            "funding_rate": round(funding_rate, 6)
            if funding_rate is not None
            else None,
            "mtf_trend_4h": mtf_trend_4h,
        }

        # Crypto Phase 1 — accuracy gates.
        # Computed as hard gates (move the setup from top_setups → avoid_list
        # with an explicit blocker tag) so the UI can still show it but the
        # user knows why it was blocked. This implements A2 + A4.
        gate_blockers: list[str] = []

        # A2 — volume confirmation gate
        if (
            signal_label in ("LONG", "SHORT")
            and getattr(settings, "CRYPTO_VOL_GATE_ENABLED", True)
            and volume_ratio is not None
        ):
            vr_min = float(getattr(settings, "CRYPTO_VOL_MIN_RATIO", 1.30) or 0.0)
            if vr_min > 0 and volume_ratio < vr_min:
                gate_blockers.append(f"low_volume({volume_ratio:.2f}<{vr_min:.2f})")

        # A4 — funding rate hard gates (asymmetric for LONG vs SHORT)
        if (
            signal_label in ("LONG", "SHORT")
            and getattr(settings, "CRYPTO_FUNDING_GATE_ENABLED", True)
            and funding_rate is not None
        ):
            max_long = float(
                getattr(settings, "CRYPTO_FUNDING_MAX_LONG", 0.0005) or 0.0
            )
            min_short = float(
                getattr(settings, "CRYPTO_FUNDING_MIN_SHORT", -0.0005) or 0.0
            )
            if signal_label == "LONG" and funding_rate > max_long:
                gate_blockers.append(f"crowded_longs(funding={funding_rate:.4%})")
            elif signal_label == "SHORT" and funding_rate < min_short:
                gate_blockers.append(f"crowded_shorts(funding={funding_rate:.4%})")

        # A1 — MTF (4h) confluence gate.
        # Relaxed mode (default): block only when 4h trend is *opposite* to
        #   the signal direction. NEUTRAL 4h passes (avoids over-blocking).
        # Strict mode: require exact 4h match (BULLISH for LONG, BEARISH for
        #   SHORT). NEUTRAL 4h → blocked.
        # Missing 4h trend → gate skipped (graceful degradation).
        if (
            signal_label in ("LONG", "SHORT")
            and getattr(settings, "CRYPTO_MTF_GATE_ENABLED", True)
            and mtf_trend_4h in ("BULLISH", "BEARISH", "NEUTRAL")
        ):
            strict = bool(getattr(settings, "CRYPTO_MTF_STRICT", False))
            if signal_label == "LONG":
                blocked_mtf = mtf_trend_4h == "BEARISH" or (
                    strict and mtf_trend_4h != "BULLISH"
                )
            else:  # SHORT
                blocked_mtf = mtf_trend_4h == "BULLISH" or (
                    strict and mtf_trend_4h != "BEARISH"
                )
            if blocked_mtf:
                gate_blockers.append(f"mtf_conflict(4h={mtf_trend_4h.lower()})")

        meets_strict = quality_score >= q_floor and confidence >= c_floor
        meets_relaxed = (
            sparse_enabled and quality_score >= q_rel and confidence >= c_rel
        )

        if (
            signal_label in ("LONG", "SHORT")
            and not stale
            and (meets_strict or meets_relaxed)
        ):
            tier = "premium" if meets_strict else "exploratory"
            entry_data["tier"] = tier
            blockers: list[str] = []
            if drift:
                blockers.append("drift_detected")
            if event_risk:
                blockers.append("event_risk")
            if has_conflict:
                blockers.append("signal_conflict")
            # Phase-1 gates: any active blocker bumps to avoid_list
            blockers.extend(gate_blockers)
            entry_data["blockers"] = blockers
            if gate_blockers:
                avoid_list.append(entry_data)
            elif tier == "premium":
                premium_pool.append(entry_data)
            else:
                exploratory_pool.append(entry_data)
        elif signal_label == "SKIP" and quality_score >= 55:
            blockers = []
            if skip_reason:
                blockers.append(skip_reason)
            if drift:
                blockers.append("drift_detected")
            if event_risk:
                blockers.append("event_risk")
            if has_conflict:
                blockers.append("signal_conflict")
            if stale:
                blockers.append("stale_data")
            entry_data["blockers"] = blockers or ["quality_gate"]
            avoid_list.append(entry_data)

    # Evict stale first-seen entries
    for s in list(_crypto_setup_first_seen.keys()):
        if s not in active_syms:
            del _crypto_setup_first_seen[s]

    premium_pool.sort(key=lambda x: x["composite_rank"], reverse=True)
    exploratory_pool.sort(key=lambda x: x["composite_rank"], reverse=True)
    avoid_list.sort(key=lambda x: x["composite_rank"], reverse=True)

    top_setups = list(premium_pool)
    sparse_fill_applied = False
    if sparse_enabled and len(top_setups) < sparse_target and exploratory_pool:
        seen_syms = {t["symbol"] for t in top_setups}
        for row in exploratory_pool:
            if len(top_setups) >= sparse_target:
                break
            sym = row["symbol"]
            if sym in seen_syms:
                continue
            top_setups.append(row)
            seen_syms.add(sym)
            sparse_fill_applied = True

    n_top = len(top_setups)
    for idx, t in enumerate(top_setups):
        t["setup_percentile"] = (
            round(100.0 * (1.0 - idx / max(n_top - 1, 1)), 1) if n_top > 1 else 100.0
        )

    # ── Theme leadership ─────────────────────────────────────────────────────
    themes: dict[str, list[float]] = {}
    for sym_ccxt in list(_signal_cache.keys()):
        if "/" not in sym_ccxt:
            continue
        theme = _CRYPTO_THEMES.get(sym_ccxt, "Other")
        sig = _get_signal_for_symbol(sym_ccxt)
        if sig:
            qs = int(sig.get("quality_score") or 0)
            themes.setdefault(theme, []).append(float(qs))
    theme_leadership = [
        {"theme": th, "avg_qs": round(float(np.mean(vals)), 1), "count": len(vals)}
        for th, vals in themes.items()
        if vals
    ]
    theme_leadership.sort(key=lambda x: x["avg_qs"], reverse=True)

    # ── Futures context snapshot (last refresh or live) ───────────────────────
    futures_freshness = (
        _crypto_futures_updated_at.isoformat() if _crypto_futures_updated_at else None
    )
    futures_summary: list[dict] = [
        v
        for v in _crypto_futures_cache.values()
        if v.get("symbol") in [s["symbol"] for s in top_setups[:5]]
    ]

    why_hint = (
        f"BTC {btc_pct:+.2f}%, ETH {eth_pct:+.2f}%. "
        f"Breadth: {gainers_n} gainers vs {losers_n} losers. "
        f"Fear & Greed: {fg_value if fg_value is not None else '—'}. "
        f"Mood: {mood_label} ({mood_score:.0f}/100)."
    )

    return {
        "top_setups": top_setups[:5],
        "avoid_list": avoid_list[:5],
        "market_guardrails": {
            "mood": mood_label,
            "mood_score": round(mood_score, 1),
            "btc_24h_pct": round(btc_pct, 2),
            "eth_24h_pct": round(eth_pct, 2),
            "breadth_gainers": gainers_n,
            "breadth_losers": losers_n,
            "fear_greed": fg_value,
            "why_market_hint": why_hint,
        },
        "futures_context": {
            "per_symbol": futures_summary,
            "updated_at": futures_freshness,
            "source": "Binance FAPI public (advisory)",
        },
        "theme_leadership": theme_leadership[:6],
        "ranking_meta": {
            "total_tracked": sum(1 for s in _signal_cache if "/" in s),
            "actionable": len(top_setups),
            "blocked_high_quality": len(avoid_list),
            "scoring_version": "crypto_phase6_v2",
            "sparse_fill_enabled": sparse_enabled,
            "sparse_fill_applied": sparse_fill_applied,
            "sparse_target": sparse_target,
            "tier_premium_count": sum(
                1 for t in top_setups if t.get("tier") == "premium"
            ),
            "tier_exploratory_count": sum(
                1 for t in top_setups if t.get("tier") == "exploratory"
            ),
            "quality_floor": q_floor,
            "confidence_floor": c_floor,
            "quality_floor_relaxed": q_rel,
            "confidence_floor_relaxed": c_rel,
        },
        "updated_at": now_iso,
    }


async def _refresh_crypto_futures_context() -> None:
    """
    Phase 6 – background refresh of futures context for all CRYPTO_SYMBOLS.
    Called from background task; silently degrades on failure.
    """
    global _crypto_futures_cache, _crypto_futures_updated_at
    try:
        new_ctx = await asyncio.to_thread(
            _fetch_binance_futures_context, settings.CRYPTO_SYMBOLS
        )
        if new_ctx:
            _crypto_futures_cache.update(new_ctx)
            _crypto_futures_updated_at = datetime.now()
    except Exception as exc:
        logger.warning("Crypto futures context refresh failed: %s", exc)


# In-memory job tracker
_retrain_jobs: dict = {}


def _train_xgboost_sync(
    symbols: list[str],
    timeframe: str,
    days: int,
    forward_bars: int,
    min_return_pct: float,
) -> dict:
    """Blocking XGBoost training — runs in a thread executor."""
    import os
    import pickle

    import numpy as np

    import agents.ai_engine.xgboost_model as xm
    from agents.ai_engine.xgboost_model import (
        FEATURE_COLS_XGB,
        SKLEARN_AVAILABLE,
        XGB_AVAILABLE,
        get_xgb_predictor,
        prepare_training_data,
    )
    from agents.data_ingestion.binance_fetcher import fetch_historical
    from agents.feature_engine.indicators import calculate_indicators
    from agents.feature_engine.trend import classify_trend

    if not (XGB_AVAILABLE or SKLEARN_AVAILABLE):
        raise RuntimeError(
            "Neither xgboost nor scikit-learn is installed. Run: pip install xgboost"
        )

    all_X, all_y = [], []
    symbol_stats = {}

    for sym in symbols:
        try:
            df = fetch_historical(sym, timeframe=timeframe, since_days=days)
            if df is None or len(df) < 100:
                symbol_stats[sym] = {"error": "insufficient data"}
                continue
            df = calculate_indicators(df)
            df = classify_trend(df)
            X, y = prepare_training_data(
                df, forward_bars=forward_bars, min_return_pct=min_return_pct
            )
            if len(X) >= 50:
                all_X.append(X)
                all_y.append(y)
                symbol_stats[sym] = {
                    "samples": len(X),
                    "balance": round(float(y.mean()) * 100, 1),
                }
            else:
                symbol_stats[sym] = {"error": f"only {len(X)} samples"}
        except Exception as e:
            symbol_stats[sym] = {"error": str(e)}

    if not all_X:
        raise RuntimeError("No valid training data collected from any symbol")

    X_all = np.vstack(all_X)
    y_all = np.concatenate(all_y)

    # Clean inf/NaN
    X_all = np.where(np.isinf(X_all), np.nan, X_all)
    col_med = np.nanmedian(X_all, axis=0)
    nan_mask = np.isnan(X_all)
    if nan_mask.any():
        X_all[nan_mask] = np.take(col_med, np.where(nan_mask)[1])

    # Train
    if XGB_AVAILABLE:
        import xgboost as xgb_lib

        model = xgb_lib.XGBClassifier(
            n_estimators=300,
            max_depth=5,
            learning_rate=0.03,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_weight=3,
            gamma=0.1,
            eval_metric="logloss",
            random_state=42,
            n_jobs=-1,
            verbosity=0,
        )
    else:
        from sklearn.ensemble import GradientBoostingClassifier

        model = GradientBoostingClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05, random_state=42
        )

    split = int(len(X_all) * 0.8)
    model.fit(X_all[:split], y_all[:split])

    val_acc = float((model.predict(X_all[split:]) == y_all[split:]).mean() * 100)
    fi = dict(zip(FEATURE_COLS_XGB, model.feature_importances_.round(4).tolist()))

    # Update singleton
    xm._predictor = None
    p = get_xgb_predictor()
    p.model = model
    p.is_fitted = True
    p._feature_importance = fi

    os.makedirs("models", exist_ok=True)
    with open("models/xgb_model.pkl", "wb") as f:
        pickle.dump(
            {
                "model": model,
                "feature_importance": fi,
                "feature_cols": FEATURE_COLS_XGB,
                "n_samples": len(X_all),
                "timeframe": timeframe,
                "symbols": symbols,
            },
            f,
        )

    return {
        "val_accuracy": round(val_acc, 2),
        "n_samples": len(X_all),
        "symbol_stats": symbol_stats,
        "top_features": dict(sorted(fi.items(), key=lambda x: -x[1])[:5]),
    }



# ── Shared WebSocket Snapshot Broadcaster (Single-Reader Fan-Out) ──────────────
class SnapshotBroadcaster:
    """
    Singleton broadcaster for real-time WebSocket clients.
    Runs a single background loop that polls the latest market snapshot from
    DB or in-memory cache, serializes the payload ONCE, and fans out to all active clients.
    Prevents N-client polling storm and keeps memory/compute footprint minimal.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._cached_payload: dict[str, Any] | None = None
        self._cached_json: str | None = None
        self._last_snapshot_version: Any = None

    def register(self, ws: WebSocket) -> None:
        self._clients.add(ws)
        logger.info("[Broadcaster] Client registered (%d active)", len(self._clients))

    def unregister(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.info("[Broadcaster] Client unregistered (%d remaining)", len(self._clients))

    async def get_initial_payload(self) -> dict[str, Any]:
        if self._cached_payload:
            return self._cached_payload
        return await self._build_payload()

    async def _build_payload(self) -> dict[str, Any]:
        snapshot = None
        try:
            from db.runtime_state import get_runtime_state
            snapshot = await asyncio.to_thread(get_runtime_state, "latest_market_snapshot")
        except Exception as exc:
            logger.debug("[Broadcaster] DB snapshot read skipped: %s", exc)

        prices: dict[str, Any] = {}
        feed_status = "EOD"
        source = "CACHE"
        updated_at = datetime.now(timezone.utc).isoformat()

        if snapshot and isinstance(snapshot, dict) and "prices" in snapshot:
            prices = snapshot.get("prices", {})
            feed_status = str(snapshot.get("feed_status", "LIVE"))
            source = str(snapshot.get("source", "WORKER_DB"))
            updated_at = str(snapshot.get("timestamp", updated_at))
        else:
            # Fallback to local in-memory _market_data (monolithic / local dev mode)
            for sym, df in _market_data.items():
                if isinstance(df, pd.DataFrame) and not df.empty:
                    last = df.iloc[-1]
                    b_first = float(df.iloc[0]["open"]) if float(df.iloc[0]["open"]) != 0 else float(last["close"])
                    b_chg = round(((float(last["close"]) - b_first) / b_first) * 100.0, 2) if b_first else 0.0
                    prices[sym] = {
                        "close": round(float(last["close"]), 2),
                        "change_pct": b_chg,
                        "high": round(float(last["high"]), 2),
                        "low": round(float(last["low"]), 2),
                        "rsi": round(float(last["RSI_14"]), 1) if pd.notna(last.get("RSI_14")) else None,
                        "signal": str(last.get("signal", "HOLD")),
                        "regime": _regime_cache.get(sym, "RANGING"),
                        "data_status": "LIVE" if "/" in sym else "DELAYED",
                    }
            feed_status = _get_feed_status().get("status", "EOD")
            source = "IN_PROCESS"

        try:
            from agents.execution.market_control import is_crypto_forex_enabled
            if not is_crypto_forex_enabled():
                prices = {k: v for k, v in prices.items() if "/" not in k}
        except Exception:
            pass

        engine = _get_engine()
        stats = engine.get_stats()
        mood = await asyncio.to_thread(_compute_market_mood)

        return {
            "type": "price_update",
            "timestamp": updated_at,
            "source": source,
            "feed_status": feed_status,
            "prices": prices,
            "market_mood": mood,
            "stats": {
                "capital": stats.get("capital", 100000),
                "total_equity": stats.get("total_equity", stats.get("capital", 100000)),
                "initial_capital": stats.get("initial_capital", 100000),
                "unrealized_pnl": stats.get("unrealized_pnl", 0.0),
                "total_pnl": stats.get("total_pnl", 0.0),
                "total_trades": stats.get("total_trades", 0),
                "closed_trades": stats.get("closed_trades", stats.get("total_trades", 0)),
                "winning_trades": stats.get("winning_trades", 0),
                "losing_trades": stats.get("losing_trades", 0),
                "win_rate": stats.get("win_rate"),
                "open_trades": len(engine.get_open_trades()),
            },
        }

    async def broadcast_loop(self) -> None:
        logger.info("[Broadcaster] Shared broadcast loop initialized.")
        while True:
            try:
                await asyncio.sleep(5)
                if not self._clients:
                    continue

                payload = await self._build_payload()
                self._cached_payload = payload
                json_str = json.dumps(payload, ensure_ascii=False)
                self._cached_json = json_str

                # Fan-out to all connected clients concurrently
                dead: set[WebSocket] = set()
                for client in list(self._clients):
                    try:
                        await client.send_text(json_str)
                    except Exception:
                        dead.add(client)
                if dead:
                    self._clients.difference_update(dead)
                    logger.debug("[Broadcaster] Pruned %d dead WebSocket client(s)", len(dead))

            except asyncio.CancelledError:
                logger.info("[Broadcaster] Loop cancelled.")
                break
            except Exception as exc:
                logger.warning("[Broadcaster] Error during broadcast cycle: %s", exc)
                await asyncio.sleep(5)


broadcaster = SnapshotBroadcaster()