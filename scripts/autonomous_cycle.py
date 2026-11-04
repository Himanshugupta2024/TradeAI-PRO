"""
TradeAI PRO — Production-Grade Zero-Click Autonomous Paper Worker.
==================================================================
Runs a single, deterministic, idempotent cycle of market intelligence,
signal generation, risk-gated paper execution, position management, and
autonomous decision feed updates.

Designed for:
  - GitHub Actions cron runs (every 15-30m during market hours)
  - Supabase Database Webhook / pg_cron triggers
  - Manual execution via CLI or API dispatch
  - Zero-click paper execution (NO real-money broker integration)

Usage:
  python scripts/autonomous_cycle.py [--universe tier1|crypto|nse|all|SYM1,SYM2] [--force-scan] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from config import settings
from db.connection import init_db, SessionLocal
from db.runtime_state import get_runtime_state, set_runtime_state
from agents.execution.distributed_lock import DistributedLock, DistributedLockManager
from agents.execution.idempotency import (
    generate_signal_id,
    is_signal_already_executed,
    record_autonomous_decision_to_db,
)
from agents.execution.session_guard import (
    is_nse_market_open,
    check_session_gate,
    evaluate_data_freshness,
    get_current_ist_time,
)
from agents.execution.autonomous_daemon import get_autonomous_daemon
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade
from agents.market_intelligence.nse_fetcher import fetch_ohlcv
from agents.data_ingestion.binance_fetcher import fetch_historical
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.regime_detector import detect_regime
from agents.signal_engine.signal_generator import generate_signals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("autonomous_cycle")

# Default Tier-1 liquid universes
TIER1_NSE = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK", "LT",
    "AXISBANK", "TATAMOTORS", "MARUTI", "SUNPHARMA", "TITAN",
]
TIER1_CRYPTO = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TradeAI PRO Autonomous Paper Worker (Single Cycle)")
    parser.add_argument(
        "--universe",
        type=str,
        default="tier1",
        help="Asset universe: tier1, nse, crypto, all, or comma-separated symbols (e.g. RELIANCE,BTC/USDT)",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Unique identifier for this run. If omitted, an ID is automatically generated.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Run exactly one cycle and terminate (default: True).",
    )
    parser.add_argument(
        "--force-scan",
        action="store_true",
        default=False,
        help="Bypass market session hours check (useful for testing or crypto-only runs).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Calculate indicators and signals without executing paper orders.",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=20,
        help="Maximum number of symbols to evaluate per cycle.",
    )
    return parser.parse_args()


def resolve_universe(universe_arg: str, max_symbols: int = 20) -> list[str]:
    u = universe_arg.strip().lower()
    if u == "tier1":
        symbols = TIER1_NSE + TIER1_CRYPTO
    elif u == "nse":
        symbols = list(TIER1_NSE)
    elif u == "crypto":
        symbols = list(TIER1_CRYPTO)
    elif u == "all":
        nse_all = list(settings.NSE_SYMBOLS.keys()) if isinstance(settings.NSE_SYMBOLS, dict) else list(settings.NSE_SYMBOLS)
        crypto_all = list(settings.CRYPTO_SYMBOLS.keys()) if isinstance(settings.CRYPTO_SYMBOLS, dict) else list(settings.CRYPTO_SYMBOLS)
        symbols = nse_all[:15] + crypto_all[:5]
    else:
        symbols = [s.strip().upper() for s in universe_arg.split(",") if s.strip()]

    return symbols[:max_symbols]


async def run_single_autonomous_cycle(
    universe_arg: str = "tier1",
    run_id: Optional[str] = None,
    force_scan: bool = False,
    dry_run: bool = False,
    max_symbols: int = 20,
) -> dict[str, Any]:
    """
    Execute one complete autonomous paper trading worker cycle.
    Returns summary dictionary of actions taken.
    """
    t0 = time.perf_counter()
    now_ist = get_current_ist_time()
    today_str = now_ist.date().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    if not run_id:
        run_id = f"cycle-{now_ist.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    logger.info("================================================================================")
    logger.info("🚀 [AutonomousWorker] Starting Cycle: %s (IST: %s)", run_id, now_ist.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("   Options: universe=%s, force_scan=%s, dry_run=%s, max_symbols=%d", universe_arg, force_scan, dry_run, max_symbols)
    logger.info("================================================================================")

    # 1. Initialize Database
    try:
        init_db()
    except Exception as exc:
        logger.error("[AutonomousWorker] Database initialization failed: %s", exc)

    # 2. Acquire Distributed Lock
    lock_mgr = DistributedLockManager(default_duration_sec=480, heartbeat_timeout_sec=180)
    lock_acquired = lock_mgr.acquire(
        job_name="autonomous_paper_worker",
        run_id=run_id,
        duration_sec=480,
        meta={"source": "autonomous_cycle.py", "start_time": now_iso},
    )

    if not lock_acquired:
        logger.warning("⚠️ [AutonomousWorker] SKIP_ALREADY_RUNNING: Another worker holds the active lock. Exiting.")
        return {
            "ok": False,
            "status": "SKIP_ALREADY_RUNNING",
            "run_id": run_id,
            "reason": "Distributed lock held by another active worker",
        }

    summary: dict[str, Any] = {
        "ok": True,
        "run_id": run_id,
        "start_time": now_iso,
        "symbols_scanned": 0,
        "signals_generated": 0,
        "trades_executed": 0,
        "trades_closed": 0,
        "decisions_recorded": 0,
        "errors": [],
    }

    try:
        # 3. Market Session Awareness
        is_nse_open, nse_reason = is_nse_market_open(now_ist)
        logger.info("[Session] Market Gate Evaluation -> NSE Open: %s (%s)", is_nse_open, nse_reason)

        raw_symbols = resolve_universe(universe_arg, max_symbols=max_symbols)
        active_symbols = []

        for sym in raw_symbols:
            is_crypto = "/" in sym
            if is_crypto:
                active_symbols.append(sym)
            else:
                if is_nse_open or force_scan:
                    active_symbols.append(sym)
                else:
                    logger.debug("[Session] Skipping %s: NSE Cash market closed (%s)", sym, nse_reason)

        if not active_symbols:
            logger.info("ℹ️ [Session] No eligible symbols for active market hours. Recording MARKET_CLOSED.")
            record_autonomous_decision_to_db(
                signal_id=f"mc-{today_str}-{int(time.time())}",
                run_id=run_id,
                symbol="NSE",
                action="MARKET_CLOSED",
                direction="NEUTRAL",
                reason=f"NSE cash market closed ({nse_reason}). Next session: 09:15 IST.",
            )
            summary["status"] = "MARKET_CLOSED"
            summary["reason"] = nse_reason
            return summary

        logger.info("[Universe] Active symbols to scan (%d): %s", len(active_symbols), active_symbols)

        # 4. Engine & Daemon Initialization
        daemon = get_autonomous_daemon()
        engine_inst = PaperTradingEngine()

        market_prices: dict[str, Any] = {}
        regimes: dict[str, str] = {}
        atr_lookup: dict[str, float] = {}
        candidate_signals: list[dict[str, Any]] = []

        # 5. Data Ingestion, Freshness Verification & Feature Computation
        for sym in active_symbols:
            try:
                # Maintain lock heartbeat
                lock_mgr.heartbeat("autonomous_paper_worker", run_id)

                is_crypto = "/" in sym
                if is_crypto:
                    df = await asyncio.to_thread(fetch_historical, sym, "5m", 3)
                else:
                    df = await asyncio.to_thread(fetch_ohlcv, sym, "5d", "5m")

                if df is None or df.empty or len(df) < 15:
                    logger.debug("[Fetch] Insufficient data for %s", sym)
                    continue

                # Freshness Check
                freshness, age_sec = evaluate_data_freshness(df, sym)
                if freshness == "STALE":
                    logger.warning("[Freshness] Symbol %s data is STALE (age: %.1fm). Skipping entry.", sym, age_sec / 60.0)
                    record_autonomous_decision_to_db(
                        signal_id=f"stale-{sym}-{today_str}-{int(time.time())}",
                        run_id=run_id,
                        symbol=sym,
                        action="SKIP_STALE_DATA",
                        direction="HOLD",
                        source_freshness="STALE",
                        reason=f"Market data age is {age_sec/60.0:.1f}m, exceeding 60m threshold",
                    )
                    summary["decisions_recorded"] += 1
                    continue
                elif freshness == "EOD" and not is_crypto:
                    logger.debug("[Freshness] Symbol %s returned prior EOD bar during intraday. Skipping entry.", sym)
                    continue

                summary["symbols_scanned"] += 1

                # Feature indicators & quantitative regime
                df = calculate_indicators(df)
                regime = detect_regime(df)
                regimes[sym] = regime.value

                last = df.iloc[-1]
                close_price = float(last["close"])
                atr_val = float(last.get("ATR_14", 0.0))
                rsi_val = float(last.get("RSI_14", 50.0))

                market_prices[sym] = {
                    "close": round(close_price, 2),
                    "high": round(float(last["high"]), 2),
                    "low": round(float(last["low"]), 2),
                    "atr": round(atr_val, 2),
                    "rsi": round(rsi_val, 1),
                    "regime": regime.value,
                    "data_status": freshness,
                    "freshness_sec": int(age_sec),
                }
                atr_lookup[sym] = atr_val

                # Signal Generation
                sig_df = generate_signals(df, symbol=sym)
                if sig_df is not None and not sig_df.empty:
                    sig_last = sig_df.iloc[-1]
                    sig_action = str(sig_last.get("signal", "HOLD")).upper()
                    if sig_action in ("LONG", "SHORT", "BUY", "SELL"):
                        norm_dir = "LONG" if sig_action in ("LONG", "BUY") else "SHORT"
                        quant_score = float(sig_last.get("quant_score", sig_last.get("confidence", 65.0)))
                        ai_conf = float(sig_last.get("confidence", 65.0))
                        entry_pr = float(sig_last.get("close", close_price))
                        sl_pr = float(sig_last.get("stop_loss", 0.0))
                        tp_pr = float(sig_last.get("target", 0.0))
                        strategy_name = str(sig_last.get("signal_type", "AUTONOMOUS_V1"))

                        # Compute Risk-Reward
                        risk = abs(entry_pr - sl_pr)
                        reward = abs(tp_pr - entry_pr)
                        rr = reward / risk if risk > 0 else 0.0

                        # Candle time
                        c_time = last.get("timestamp", now_iso)

                        candidate_signals.append({
                            "symbol": sym,
                            "signal": norm_dir,
                            "direction": norm_dir,
                            "quant_score": quant_score,
                            "confidence": ai_conf,
                            "entry": entry_pr,
                            "stop_loss": sl_pr,
                            "target": tp_pr,
                            "risk_reward": rr,
                            "atr": atr_val,
                            "rsi": rsi_val,
                            "regime": regime.value,
                            "strategy": strategy_name,
                            "candle_time": c_time,
                            "data_status": freshness,
                        })
                        summary["signals_generated"] += 1

            except Exception as sym_err:
                logger.debug("[Scan] Error scanning %s: %s", sym, sym_err)
                summary["errors"].append({"symbol": sym, "error": str(sym_err)})

        # 6. Manage Open Trades (Trailing Stops, +1R breakeven, +1.5R partial profit, 15:15 IST EOD)
        if market_prices:
            closed_trades = daemon.manage_open_trades(engine_inst, market_prices, atr_lookup)
            summary["trades_closed"] = len(closed_trades)

        # 7. Evaluate Signals with Deterministic Idempotency & Risk Gates
        if candidate_signals and daemon.enabled and not dry_run:
            open_count = len(engine_inst.open_trades)
            remaining_slots = max(0, daemon.max_positions - open_count)
            logger.info("[Risk] Open positions: %d/%d (Slots remaining: %d)", open_count, daemon.max_positions, remaining_slots)

            if remaining_slots > 0:
                # Filter candidates by quality and AI confidence
                qualified: list[dict[str, Any]] = []
                for cand in candidate_signals:
                    sym = cand["symbol"]
                    c_time = cand["candle_time"]
                    strat = cand["strategy"]
                    direction = cand["direction"]

                    # Generate deterministic signal ID
                    sig_id = generate_signal_id(sym, strat, direction, c_time, today_str)
                    cand["signal_id"] = sig_id

                    # Check Idempotency: Already executed in this session?
                    if is_signal_already_executed(sig_id):
                        logger.info("🔁 [Idempotency] SKIP_DUPLICATE_SIGNAL: %s already executed.", sig_id)
                        record_autonomous_decision_to_db(
                            signal_id=f"dup-{sig_id}",
                            run_id=run_id,
                            symbol=sym,
                            action="SKIP_DUPLICATE_SIGNAL",
                            direction=direction,
                            entry_price=cand["entry"],
                            reason=f"Identical signal event {sig_id} was already executed in this session",
                        )
                        summary["decisions_recorded"] += 1
                        continue

                    # Minimum R:R check (>= 1.5)
                    if cand["risk_reward"] < 1.5:
                        logger.debug("[Risk] %s R:R (%.2f) below 1.5 threshold. Skipping.", sym, cand["risk_reward"])
                        continue

                    # Geometry check
                    if direction == "LONG" and not (cand["stop_loss"] < cand["entry"] < cand["target"]):
                        continue
                    if direction == "SHORT" and not (cand["target"] < cand["entry"] < cand["stop_loss"]):
                        continue

                    qualified.append(cand)

                # Rank qualified candidates: QS score desc, then confidence desc
                qualified.sort(key=lambda c: (c["quant_score"], c["confidence"]), reverse=True)
                selected = qualified[:remaining_slots]

                for cand in selected:
                    try:
                        trade = engine_inst.execute(
                            symbol=cand["symbol"],
                            direction=cand["direction"],
                            entry=cand["entry"],
                            sl=cand["stop_loss"],
                            tp=cand["target"],
                            atr=cand["atr"],
                            signal_type=cand["strategy"],
                            regime=cand["regime"],
                            rsi_at_entry=cand["rsi"],
                            confidence=cand["confidence"],
                            min_rr=1.5,
                        )
                        if trade:
                            summary["trades_executed"] += 1
                            # Record autonomous decision in PostgreSQL & runtime_state
                            record_autonomous_decision_to_db(
                                signal_id=cand["signal_id"],
                                run_id=run_id,
                                symbol=cand["symbol"],
                                action="AUTO_ENTER",
                                direction=cand["direction"],
                                entry_price=cand["entry"],
                                stop_loss=cand["stop_loss"],
                                target_price=cand["target"],
                                quality_score=cand["quant_score"],
                                confluence_score=cand["confidence"],
                                risk_reward=cand["risk_reward"],
                                market_regime=cand["regime"],
                                source_freshness=cand["data_status"],
                                paper_trade_id=trade.id,
                                reason=f"Top Setup (QS: {cand['quant_score']:.1f}, AI: {cand['confidence']:.1f}%, R:R {cand['risk_reward']:.2f}) passed all risk gates",
                            )
                            summary["decisions_recorded"] += 1
                            logger.info(
                                "🚀 [Autonomous] AUTO_ENTER: %s %s #%d @ %.2f (QS: %.1f, AI: %.1f%%, R:R: %.2f)",
                                cand["direction"], cand["symbol"], trade.id, cand["entry"],
                                cand["quant_score"], cand["confidence"], cand["risk_reward"],
                            )
                    except Exception as exec_err:
                        logger.error("[Autonomous] Execution failed for %s: %s", cand["symbol"], exec_err)

        # 8. Check Daily EOD Review (15:45-16:30 IST weekdays)
        daemon.check_daily_eod_review()

        # 9. If no trades executed and no signals matched, record NO_TRADE summary
        if summary["trades_executed"] == 0 and summary["decisions_recorded"] == 0:
            no_trade_msg = f"Scan complete ({summary['symbols_scanned']} symbols scanned, {summary['signals_generated']} raw signals, 0 qualified entries)"
            record_autonomous_decision_to_db(
                signal_id=f"nt-{today_str}-{int(time.time())}",
                run_id=run_id,
                symbol="CYCLE",
                action="NO_TRADE",
                direction="NEUTRAL",
                reason=no_trade_msg,
            )
            daemon.record_decision(
                action="NO_TRADE",
                symbol="CYCLE",
                direction="NEUTRAL",
                price=0.0,
                reason=no_trade_msg,
            )
            summary["decisions_recorded"] += 1

        # 10. Persist Market Snapshots & IntradayScan to database
        elapsed = time.perf_counter() - t0
        summary["duration_sec"] = round(elapsed, 2)
        summary["open_positions"] = len(engine_inst.open_trades)

        version = int(time.time() * 1000)
        snapshot_payload = {
            "version": version,
            "timestamp": now_iso,
            "source": "AUTONOMOUS_WORKER_CYCLE",
            "feed_status": "LIVE",
            "symbols": list(market_prices.keys()),
            "prices": market_prices,
            "regimes": regimes,
        }
        await asyncio.to_thread(set_runtime_state, "latest_market_snapshot", snapshot_payload)

        # 10b. Persist IntradayScan with Scan Freshness Contract for dashboard
        try:
            from db import intraday_store
            scan_duration_ms = int(elapsed * 1000)
            await asyncio.to_thread(
                intraday_store.save_scan,
                candidate_signals,
                symbols_scanned=summary["symbols_scanned"],
                trigger="autonomous_cycle",
                scan_id=f"iscan-{run_id}",
                started_at=datetime.fromisoformat(now_iso.replace("Z", "+00:00")),
                completed_at=datetime.now(timezone.utc),
                market_session_date=now_ist.date(),
                provider="yfinance" if any("/" not in s for s in active_symbols) else "binance",
                provider_freshness="15m Delayed",
                symbols_attempted=len(active_symbols),
                symbols_successful=summary["symbols_scanned"],
                symbols_failed=len(summary["errors"]),
                scanner_version="2.0.0",
                scan_duration_ms=scan_duration_ms,
            )
            logger.info("💾 [AutonomousWorker] Persisted IntradayScan to database (%d candidate signals)", len(candidate_signals))
        except Exception as scan_err:
            logger.warning("[AutonomousWorker] Could not persist IntradayScan: %s", scan_err)

        # Persist latest worker cycle status for watchdog
        status_payload = {
            "run_id": run_id,
            "timestamp": now_iso,
            "status": "COMPLETED",
            "symbols_scanned": summary["symbols_scanned"],
            "signals_generated": summary["signals_generated"],
            "trades_executed": summary["trades_executed"],
            "trades_closed": summary["trades_closed"],
            "open_positions": summary["open_positions"],
            "duration_sec": summary["duration_sec"],
            "errors_count": len(summary["errors"]),
        }
        await asyncio.to_thread(set_runtime_state, "autonomous_worker:latest_status", status_payload)

        logger.info(
            "✅ [AutonomousWorker] Cycle '%s' completed in %.2fs. Scanned: %d, Signals: %d, Executed: %d, Open: %d",
            run_id, elapsed, summary["symbols_scanned"], summary["signals_generated"],
            summary["trades_executed"], summary["open_positions"],
        )

        return summary

    finally:
        # 11. Release Distributed Lock
        lock_mgr.release(
            job_name="autonomous_paper_worker",
            run_id=run_id,
            status="COMPLETED",
            meta={"duration_sec": round(time.perf_counter() - t0, 2)},
        )
        gc.collect()


def main():
    args = parse_args()
    result = asyncio.run(
        run_single_autonomous_cycle(
            universe_arg=args.universe,
            run_id=args.run_id,
            force_scan=args.force_scan,
            dry_run=args.dry_run,
            max_symbols=args.max_symbols,
        )
    )
    print("\n--- AUTONOMOUS CYCLE SUMMARY ---")
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok", True) else 1)


if __name__ == "__main__":
    main()
