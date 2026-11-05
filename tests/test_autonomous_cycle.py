"""
Tests for TradeAI PRO Autonomous Paper Worker, Distributed Lock, Idempotency & Session Guards.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from agents.execution.distributed_lock import DistributedLockManager, DistributedLock
from agents.execution.idempotency import (
    generate_signal_id,
    is_signal_already_executed,
    record_autonomous_decision_to_db,
)
from agents.execution.session_guard import (
    is_nse_market_open,
    check_session_gate,
    evaluate_data_freshness,
)
from db.connection import SessionLocal
from db.models import WorkerDistributedLock, AutonomousDecisionRecord
from db.runtime_state import get_runtime_state


_IST = ZoneInfo("Asia/Kolkata")


# ── Distributed Lock Tests ───────────────────────────────────────────────────

def test_distributed_lock_acquisition_and_release():
    mgr = DistributedLockManager()
    job_name = "test_job_1"
    run_1 = "test_run_1"

    # Clean existing
    session = SessionLocal()
    session.query(WorkerDistributedLock).filter_by(job_name=job_name).delete()
    session.commit()
    session.close()

    # 1. Acquire
    acq1 = mgr.acquire(job_name, run_1, duration_sec=60)
    assert acq1 is True

    # 2. Re-acquire with active lock should fail
    run_2 = "test_run_2"
    acq2 = mgr.acquire(job_name, run_2, duration_sec=60)
    assert acq2 is False

    # 3. Release run_1
    rel = mgr.release(job_name, run_1, status="COMPLETED")
    assert rel is True

    # 4. Now run_2 can acquire
    acq3 = mgr.acquire(job_name, run_2, duration_sec=60)
    assert acq3 is True

    # Cleanup
    mgr.release(job_name, run_2, status="COMPLETED")


def test_distributed_lock_stale_recovery():
    mgr = DistributedLockManager(default_duration_sec=10, heartbeat_timeout_sec=5)
    job_name = "test_stale_job"
    run_old = "test_run_stale"

    # Clean existing
    session = SessionLocal()
    session.query(WorkerDistributedLock).filter_by(job_name=job_name).delete()
    session.commit()

    # Insert a stale lock (expired locked_until and stale heartbeat)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_time = now - timedelta(seconds=200)
    stale_lock = WorkerDistributedLock(
        job_name=job_name,
        run_id=run_old,
        started_at=stale_time,
        locked_until=stale_time + timedelta(seconds=10),
        status="RUNNING",
        heartbeat_at=stale_time,
    )
    session.add(stale_lock)
    session.commit()
    session.close()

    # Attempt acquire with new run_id
    run_new = "test_run_new"
    acq = mgr.acquire(job_name, run_new, duration_sec=60)
    assert acq is True

    info = mgr.get_lock_info(job_name)
    assert info is not None
    assert info["run_id"] == run_new
    assert info["status"] == "RUNNING"

    mgr.release(job_name, run_new, status="COMPLETED")


def test_distributed_lock_context_manager():
    job_name = "test_ctx_job"
    run_id = "test_run_ctx"

    with DistributedLock(job_name, run_id, duration_sec=60) as acquired:
        assert acquired is True
        mgr = DistributedLockManager()
        info = mgr.get_lock_info(job_name)
        assert info["status"] == "RUNNING"

    # After exit, status should be COMPLETED
    mgr = DistributedLockManager()
    info = mgr.get_lock_info(job_name)
    assert info["status"] == "COMPLETED"


# ── Idempotency & Signal ID Tests ─────────────────────────────────────────────

def test_deterministic_signal_id_generation():
    dt = datetime(2026, 11, 4, 10, 15, 0, tzinfo=timezone.utc)
    id1 = generate_signal_id("RELIANCE", "MOMENTUM_BREAKOUT", "LONG", dt, "2026-11-04")
    id2 = generate_signal_id("RELIANCE", "MOMENTUM_BREAKOUT", "LONG", dt, "2026-11-04")
    assert id1 == id2
    assert id1.startswith("sig-RELIANCE-2026-11-04-")

    # Different strategy or direction produces different ID
    id3 = generate_signal_id("RELIANCE", "MEAN_REVERSION", "LONG", dt, "2026-11-04")
    assert id1 != id3


def test_idempotency_duplicate_signal_check():
    sig_id = f"test-sig-{int(datetime.now(timezone.utc).timestamp() * 1000)}"

    # Before execution
    assert is_signal_already_executed(sig_id) is False

    # Record AUTO_ENTER
    record_autonomous_decision_to_db(
        signal_id=sig_id,
        run_id="test_run_idemp",
        symbol="BTC/USDT",
        action="AUTO_ENTER",
        direction="LONG",
        entry_price=68000.0,
        stop_loss=66500.0,
        target_price=71000.0,
        quality_score=78.5,
        confluence_score=82.0,
        risk_reward=2.0,
        market_regime="BULLISH_TREND",
        source_freshness="LIVE",
        reason="Unit test verification",
    )

    # After execution
    assert is_signal_already_executed(sig_id) is True


# ── Session Guard Tests ───────────────────────────────────────────────────────

def test_nse_market_open_time_windows():
    # Tuesday at 10:30 IST -> OPEN
    tuesday_open = datetime(2026, 11, 3, 10, 30, 0, tzinfo=_IST)
    is_open, reason = is_nse_market_open(tuesday_open)
    assert is_open is True
    assert reason == "SESSION_ACTIVE"

    # Tuesday at 08:30 IST -> PRE_MARKET
    tuesday_pre = datetime(2026, 11, 3, 8, 30, 0, tzinfo=_IST)
    is_open, reason = is_nse_market_open(tuesday_pre)
    assert is_open is False
    assert reason == "NSE_PRE_MARKET"

    # Tuesday at 16:00 IST -> POST_MARKET
    tuesday_post = datetime(2026, 11, 3, 16, 0, 0, tzinfo=_IST)
    is_open, reason = is_nse_market_open(tuesday_post)
    assert is_open is False
    assert reason == "NSE_POST_MARKET"

    # Sunday at 11:00 IST -> WEEKEND
    sunday = datetime(2026, 11, 1, 11, 0, 0, tzinfo=_IST)
    is_open, reason = is_nse_market_open(sunday)
    assert is_open is False
    assert reason == "NSE_CLOSED_WEEKEND"


def test_crypto_session_gate_always_active():
    sunday = datetime(2026, 11, 1, 11, 0, 0, tzinfo=_IST)
    is_open, reason = check_session_gate("CRYPTO", sunday)
    assert is_open is True
    assert reason == "SESSION_ACTIVE"


# ── Data Freshness Evaluation Tests ───────────────────────────────────────────

def test_data_freshness_evaluation():
    now_utc = datetime.now(timezone.utc)

    # 1. LIVE bar (5 minutes old)
    df_live = pd.DataFrame({
        "timestamp": [now_utc - timedelta(minutes=5)],
        "close": [100.0],
        "high": [101.0],
        "low": [99.0],
    })
    freshness, age = evaluate_data_freshness(df_live, "BTC/USDT")
    assert freshness == "LIVE"
    assert age < 600

    # 2. DELAYED bar (30 minutes old)
    df_delayed = pd.DataFrame({
        "timestamp": [now_utc - timedelta(minutes=30)],
        "close": [100.0],
    })
    freshness, age = evaluate_data_freshness(df_delayed, "BTC/USDT")
    assert freshness == "DELAYED"
    assert 900 < age <= 3600

    # 3. STALE bar (120 minutes old)
    df_stale = pd.DataFrame({
        "timestamp": [now_utc - timedelta(minutes=120)],
        "close": [100.0],
    })
    freshness, age = evaluate_data_freshness(df_stale, "BTC/USDT")
    assert freshness == "STALE"
    assert age > 3600

    # 4. Empty dataframe -> UNAVAILABLE
    freshness, _ = evaluate_data_freshness(pd.DataFrame(), "BTC/USDT")
    assert freshness == "UNAVAILABLE"


def test_resolve_universe_crypto_prioritization_under_any_input(monkeypatch):
    """
    Assert that when NSE is closed, 24/7 crypto symbols are prioritized to the front,
    even if a user passes a custom comma-separated list with dozens of leading NSE symbols,
    ensuring crypto is never truncated by max_symbols.
    """
    from scripts.autonomous_cycle import resolve_universe
    import agents.execution.session_guard as sg

    # Mock NSE as closed (e.g. evening or weekend)
    monkeypatch.setattr(sg, "is_nse_market_open", lambda dt=None: (False, "NSE_POST_MARKET"))

    # Case 1: Custom comma-separated list where BTC/USDT is at the end of 25 NSE symbols
    custom_input = "RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK,SBIN,BHARTIARTL,ITC,KOTAKBANK,LT,AXISBANK,MARUTI,SUNPHARMA,TITAN,ULTRACEMCO,WIPRO,BAJFINANCE,NESTLEIND,TATACONSUM,JSWSTEEL,TATASTEEL,ADANIENT,NTPC,POWERGRID,ONGC,BTC/USDT,ETH/USDT"
    res = resolve_universe(custom_input, max_symbols=5)
    # Crypto must be in the top 5!
    assert "BTC/USDT" in res
    assert "ETH/USDT" in res
    assert res[0] == "BTC/USDT"
    assert res[1] == "ETH/USDT"

    # Case 2: Tier1 universe when NSE closed
    res_tier1 = resolve_universe("tier1", max_symbols=5)
    assert res_tier1[0] == "BTC/USDT"
    assert res_tier1[1] == "ETH/USDT"
    assert res_tier1[2] == "SOL/USDT"
