"""
TradeAI-PRO — Distributed Worker Lock Test Suite.

Verifies:
1. Single-worker acquisition and graceful release.
2. Two-worker contention (Worker 2 is locked out while Worker 1 is healthy).
3. Heartbeat lease extension during active execution.
4. Automatic stale lock reclamation after heartbeat timeout or expiry.
5. Inactive status reflection in get_lock_info.
"""

import uuid
from datetime import datetime, timedelta, timezone
import pytest
from agents.execution.distributed_lock import (
    DistributedLockManager,
    DistributedLock,
)
from db.connection import SessionLocal
from db.models import WorkerDistributedLock


def test_lock_acquire_and_release():
    job_name = f"test_job_{uuid.uuid4().hex[:8]}"
    run_id = "worker-1"
    mgr = DistributedLockManager(default_duration_sec=60, heartbeat_timeout_sec=30)

    # Worker 1 acquires
    assert mgr.acquire(job_name=job_name, run_id=run_id) is True

    # Info inspection
    info = mgr.get_lock_info(job_name)
    assert info is not None
    assert info["job_name"] == job_name
    assert info["run_id"] == run_id
    assert info["is_active"] is True
    assert info["status"] == "RUNNING"

    # Worker 1 releases
    assert mgr.release(job_name=job_name, run_id=run_id, status="COMPLETED") is True

    # Info after release
    info_after = mgr.get_lock_info(job_name)
    assert info_after["is_active"] is False
    assert info_after["status"] == "COMPLETED"


def test_two_worker_contention_and_handover():
    job_name = f"test_contention_{uuid.uuid4().hex[:8]}"
    worker1 = "worker-run-1"
    worker2 = "worker-run-2"
    mgr = DistributedLockManager(default_duration_sec=60, heartbeat_timeout_sec=30)

    # Worker 1 acquires lock
    assert mgr.acquire(job_name=job_name, run_id=worker1) is True

    # Worker 2 attempts concurrent acquisition -> MUST FAIL
    assert mgr.acquire(job_name=job_name, run_id=worker2) is False

    # Worker 1 heartbeats to extend
    assert mgr.heartbeat(job_name=job_name, run_id=worker1, extend_sec=120) is True

    # Worker 2 still blocked
    assert mgr.acquire(job_name=job_name, run_id=worker2) is False

    # Worker 1 completes and releases
    assert mgr.release(job_name=job_name, run_id=worker1, status="COMPLETED") is True

    # Worker 2 can now acquire
    assert mgr.acquire(job_name=job_name, run_id=worker2) is True
    mgr.release(job_name=job_name, run_id=worker2, status="COMPLETED")


def test_stale_lock_recovery_after_timeout():
    job_name = f"test_stale_{uuid.uuid4().hex[:8]}"
    dead_worker = "dead-worker"
    fresh_worker = "fresh-worker"
    mgr = DistributedLockManager(default_duration_sec=60, heartbeat_timeout_sec=10)

    # Dead worker acquires
    assert mgr.acquire(job_name=job_name, run_id=dead_worker) is True

    # Simulate heartbeat staleness by backdating heartbeat_at in database
    stale_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=20)
    with SessionLocal() as session:
        lock = session.query(WorkerDistributedLock).filter_by(job_name=job_name).first()
        lock.heartbeat_at = stale_time
        session.commit()

    # get_lock_info should report EXPIRED and inactive
    info = mgr.get_lock_info(job_name)
    assert info["is_active"] is False
    assert info["status"] == "EXPIRED"

    # Fresh worker can now safely reclaim the abandoned lock
    assert mgr.acquire(job_name=job_name, run_id=fresh_worker) is True
    new_info = mgr.get_lock_info(job_name)
    assert new_info["run_id"] == fresh_worker
    assert new_info["is_active"] is True
    assert new_info["status"] == "RUNNING"

    mgr.release(job_name=job_name, run_id=fresh_worker, status="COMPLETED")


def test_context_manager_lifecycle():
    job_name = f"test_cm_{uuid.uuid4().hex[:8]}"
    mgr = DistributedLockManager(default_duration_sec=60, heartbeat_timeout_sec=30)

    with DistributedLock(job_name=job_name, run_id="cm-run", manager=mgr) as acquired:
        assert acquired is True
        info = mgr.get_lock_info(job_name)
        assert info["is_active"] is True

    # After exit, lock is COMPLETED and not active
    info_exit = mgr.get_lock_info(job_name)
    assert info_exit["is_active"] is False
    assert info_exit["status"] == "COMPLETED"
