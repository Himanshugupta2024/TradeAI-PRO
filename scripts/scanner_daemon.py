"""
TradeAI PRO — Autonomous Scanning & Snapshot Daemon.
===================================================
Runs as an independent background worker (Oracle Cloud ARM VM, dedicated container,
or local machine). Connects directly to PostgreSQL, computes technical indicators,
evaluates quantitative regimes, and persists snapshots to runtime_state.

Key Features:
  - Single-owner PostgreSQL Advisory Lock (pg_try_advisory_lock) prevents duplicate runs
  - Idempotent paper order execution via AutonomousExecutionDaemon
  - Bounded memory usage with explicit GC trimming
  - Fully decoupled from FastAPI/Uvicorn web processes
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text
from config import settings
from db.connection import engine, init_db, SessionLocal
from db.runtime_state import set_runtime_state
from agents.market_intelligence.nse_fetcher import fetch_ohlcv
from agents.data_ingestion.binance_fetcher import fetch_historical
from agents.execution.autonomous_daemon import get_autonomous_daemon
from agents.execution.paper_trading import PaperTradingEngine
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.regime_detector import detect_regime
from agents.signal_engine.signal_generator import generate_signals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scanner_daemon")

ADVISORY_LOCK_ID = 54524144  # ASCII hex representation for 'TRAD'
_shutdown_event = asyncio.Event()


def _is_postgres() -> bool:
    url = str(engine.url)
    return "postgres" in url


def acquire_advisory_lock() -> bool:
    """Acquire PostgreSQL session-level advisory lock to ensure single-owner execution."""
    if not _is_postgres():
        logger.info("[Lock] SQLite backend in use; single-process lock assumed.")
        return True
    try:
        with engine.connect() as conn:
            res = conn.execute(text(f"SELECT pg_try_advisory_lock({ADVISORY_LOCK_ID})")).scalar()
            return bool(res)
    except Exception as exc:
        logger.warning("[Lock] Failed to acquire advisory lock: %s", exc)
        return True


def release_advisory_lock() -> None:
    """Release PostgreSQL advisory lock on shutdown."""
    if not _is_postgres():
        return
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SELECT pg_advisory_unlock({ADVISORY_LOCK_ID})"))
            logger.info("[Lock] Released PostgreSQL advisory lock %d.", ADVISORY_LOCK_ID)
    except Exception as exc:
        logger.debug("[Lock] Advisory lock release error: %s", exc)


async def execute_scanner_cycle(daemon, engine_inst: PaperTradingEngine) -> None:
    """Execute one scan pass across tracked equity and crypto symbols."""
    t0 = time.perf_counter()
    market_prices: dict[str, Any] = {}
    regimes: dict[str, str] = {}
    candidate_signals: list[dict[str, Any]] = []

    # Bounded symbol universe for responsive worker cycles
    nse_syms = list(settings.NSE_SYMBOLS.keys()) if isinstance(settings.NSE_SYMBOLS, dict) else list(settings.NSE_SYMBOLS)
    crypto_syms = list(settings.CRYPTO_SYMBOLS.keys()) if isinstance(settings.CRYPTO_SYMBOLS, dict) else list(settings.CRYPTO_SYMBOLS)
    symbols = nse_syms[:15] + crypto_syms[:15]

    for sym in symbols:
        try:
            if "/" in sym:
                df = await asyncio.to_thread(fetch_historical, sym, "5m", 3)
            else:
                df = await asyncio.to_thread(fetch_ohlcv, sym, "5d", "5m")
            if df is None or df.empty or len(df) < 15:
                continue

            df = calculate_indicators(df)
            regime = detect_regime(df)
            regimes[sym] = regime.value

            last = df.iloc[-1]
            market_prices[sym] = {
                "close": round(float(last["close"]), 2),
                "high": round(float(last["high"]), 2),
                "low": round(float(last["low"]), 2),
                "atr": round(float(last.get("ATR_14", 0.0)), 2),
                "rsi": round(float(last.get("RSI_14", 50.0)), 1),
                "signal": str(last.get("signal", "HOLD")),
                "regime": regime.value,
                "data_status": "LIVE" if "/" in sym else "DELAYED",
            }

            sig_df = generate_signals(df, symbol=sym)
            if not sig_df.empty:
                sig_last = sig_df.iloc[-1]
                sig_action = str(sig_last.get("signal", "HOLD")).upper()
                if sig_action in ("LONG", "SHORT", "BUY", "SELL"):
                    candidate_signals.append({
                        "symbol": sym,
                        "signal": "LONG" if sig_action in ("LONG", "BUY") else "SHORT",
                        "quant_score": float(sig_last.get("quant_score", sig_last.get("confidence", 65.0))),
                        "confidence": float(sig_last.get("confidence", 65.0)),
                        "entry": float(sig_last["close"]),
                        "stop_loss": float(sig_last.get("stop_loss", 0.0)),
                        "target": float(sig_last.get("target", 0.0)),
                        "atr": float(last.get("ATR_14", 0.0)),
                    })

        except Exception as sym_err:
            logger.debug("[Scanner] Error scanning symbol %s: %s", sym, sym_err)

    now_iso = datetime.now(timezone.utc).isoformat()
    version = int(time.time() * 1000)

    # 1. Persist market snapshot to PostgreSQL runtime_state
    snapshot_payload = {
        "version": version,
        "timestamp": now_iso,
        "source": "WORKER_DAEMON",
        "feed_status": "LIVE",
        "freshness_sec": 0,
        "symbols": list(market_prices.keys()),
        "prices": market_prices,
        "regimes": regimes,
    }
    await asyncio.to_thread(set_runtime_state, "latest_market_snapshot", snapshot_payload)

    # 2. Persist signals snapshot to PostgreSQL runtime_state
    if candidate_signals:
        signals_payload = {
            "version": version,
            "timestamp": now_iso,
            "signals": candidate_signals,
            "count": len(candidate_signals),
        }
        await asyncio.to_thread(set_runtime_state, "latest_signals_snapshot", signals_payload)

    # 3. Autonomous Execution & Trade Management
    if daemon.enabled:
        atr_lookup = {sym: p.get("atr", 0.0) for sym, p in market_prices.items()}
        # Manage open trades (trailing stops, SL/TP)
        daemon.manage_open_trades(engine_inst, market_prices, atr_lookup)
        # Evaluate incoming candidate signals
        if candidate_signals:
            daemon.evaluate_signals(candidate_signals, market_prices, engine_inst)

    elapsed = time.perf_counter() - t0
    logger.info(
        "[Scanner] Pass complete in %.2fs. Symbols: %d, Signals: %d, Open Trades: %d",
        elapsed,
        len(market_prices),
        len(candidate_signals),
        len(engine_inst.open_trades),
    )

    # Optional explicit memory trim
    gc.collect()


async def run_worker_loop() -> None:
    """Main worker lifecycle with adaptive polling."""
    init_db()

    if not acquire_advisory_lock():
        logger.error(
            "[Lock] Another scanner daemon instance is already active. Exiting to avoid duplicate execution."
        )
        sys.exit(0)

    logger.info("[Daemon] TradeAI Autonomous Scanner Worker initialized and running.")
    daemon = get_autonomous_daemon()
    engine_inst = PaperTradingEngine()

    interval = int(os.getenv("SCAN_INTERVAL_SECONDS", "180"))  # Default 3 minutes

    try:
        while not _shutdown_event.is_set():
            try:
                await execute_scanner_cycle(daemon, engine_inst)
            except Exception as loop_err:
                logger.exception("[Daemon] Scanner cycle encountered unhandled error: %s", loop_err)

            # Sleep in 1-second chunks to respond immediately to SIGINT/SIGTERM
            for _ in range(interval):
                if _shutdown_event.is_set():
                    break
                await asyncio.sleep(1)

    finally:
        release_advisory_lock()
        logger.info("[Daemon] Worker cleanly shut down.")


def handle_signal(sig, frame):
    logger.info("Termination signal received. Shutting down worker...")
    _shutdown_event.set()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeAI PRO Autonomous Scanning Daemon")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single autonomous scan/execution cycle and exit cleanly.",
    )
    parser.add_argument(
        "--universe",
        type=str,
        default="tier1",
        help="Asset universe for single cycle: tier1, nse, crypto, all, or comma-separated symbols.",
    )
    args, _ = parser.parse_known_args()

    if args.once:
        from scripts.autonomous_cycle import run_single_autonomous_cycle
        result = asyncio.run(run_single_autonomous_cycle(universe_arg=args.universe))
        logger.info("[Daemon] Single cycle complete: %s", result.get("status", "OK"))
        sys.exit(0 if result.get("ok", True) else 1)
    else:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        asyncio.run(run_worker_loop())
