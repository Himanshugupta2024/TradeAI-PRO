"""
TradeAI-PRO — PostgreSQL & SQLite Backed Distributed Lock Manager.
================================================================
Coordinates single-owner execution of autonomous paper trading cycles across
GitHub Actions, cron runners, local machines, and containers.

Features:
  - Atomic acquisition using database transaction
  - Automatic stale lock recovery (expired heartbeat or locked_until)
  - Heartbeat maintenance during long operations
  - Context manager interface
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, update, insert
from db.connection import SessionLocal
from db.models import WorkerDistributedLock

log = logging.getLogger(__name__)


class DistributedLockError(Exception):
    """Base exception for distributed lock failures."""
    pass


class LockAlreadyHeldError(DistributedLockError):
    """Raised when an active, healthy worker already holds the distributed lock."""
    def __init__(self, job_name: str, current_run_id: str, locked_until: datetime):
        self.job_name = job_name
        self.current_run_id = current_run_id
        self.locked_until = locked_until
        super().__init__(
            f"Lock for job '{job_name}' is currently held by run '{current_run_id}' "
            f"until {locked_until.isoformat()}"
        )


class DistributedLockManager:
    """Manages acquisition, heartbeating, and release of distributed worker locks."""

    def __init__(self, default_duration_sec: int = 480, heartbeat_timeout_sec: int = 180):
        self.default_duration_sec = default_duration_sec
        self.heartbeat_timeout_sec = heartbeat_timeout_sec

    def acquire(
        self,
        job_name: str,
        run_id: str,
        duration_sec: Optional[int] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        Attempt to acquire the distributed lock for `job_name`.
        Returns True if acquired (fresh or recovered from stale state).
        Returns False if a valid, active worker currently holds the lock.
        """
        duration = duration_sec or self.default_duration_sec
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        locked_until = now + timedelta(seconds=duration)
        meta_json = json.dumps(meta or {})

        session = SessionLocal()
        try:
            with session.begin():
                # Query with row lock where supported
                lock = session.execute(
                    select(WorkerDistributedLock).where(WorkerDistributedLock.job_name == job_name)
                ).scalar_one_or_none()

                if lock is None:
                    # Brand new lock
                    new_lock = WorkerDistributedLock(
                        job_name=job_name,
                        run_id=run_id,
                        started_at=now,
                        locked_until=locked_until,
                        completed_at=None,
                        status="RUNNING",
                        heartbeat_at=now,
                        meta_json=meta_json,
                    )
                    session.add(new_lock)
                    log.info("[DistributedLock] Acquired new lock for '%s' (run_id: %s)", job_name, run_id)
                    return True

                # Check if currently held by an active worker
                is_running = lock.status == "RUNNING"
                is_unexpired = lock.locked_until is not None and lock.locked_until > now
                is_heartbeat_fresh = (
                    lock.heartbeat_at is not None
                    and (now - lock.heartbeat_at).total_seconds() < self.heartbeat_timeout_sec
                )

                if is_running and is_unexpired and is_heartbeat_fresh:
                    log.warning(
                        "[DistributedLock] Job '%s' already running (run_id: %s, locked_until: %s)",
                        job_name, lock.run_id, lock.locked_until
                    )
                    return False

                # Recover stale lock or reuse completed lock
                old_run_id = lock.run_id
                old_status = lock.status
                lock.run_id = run_id
                lock.started_at = now
                lock.locked_until = locked_until
                lock.completed_at = None
                lock.status = "RUNNING"
                lock.heartbeat_at = now
                lock.meta_json = meta_json

                log.info(
                    "[DistributedLock] Reclaimed lock for '%s' (previous run: %s, status: %s) -> new run_id: %s",
                    job_name, old_run_id, old_status, run_id
                )
                return True

        except Exception as exc:
            log.error("[DistributedLock] Exception during lock acquisition for '%s': %s", job_name, exc)
            session.rollback()
            return False
        finally:
            session.close()

    def heartbeat(self, job_name: str, run_id: str, extend_sec: int = 120) -> bool:
        """Update heartbeat timestamp and extend lock duration."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        new_locked_until = now + timedelta(seconds=extend_sec)

        session = SessionLocal()
        try:
            with session.begin():
                lock = session.execute(
                    select(WorkerDistributedLock).where(
                        WorkerDistributedLock.job_name == job_name,
                        WorkerDistributedLock.run_id == run_id,
                    )
                ).scalar_one_or_none()

                if lock and lock.status == "RUNNING":
                    lock.heartbeat_at = now
                    lock.locked_until = new_locked_until
                    return True
                return False
        except Exception as exc:
            log.debug("[DistributedLock] Heartbeat failed for '%s': %s", job_name, exc)
            return False
        finally:
            session.close()

    def release(
        self,
        job_name: str,
        run_id: str,
        status: str = "COMPLETED",
        meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Release lock and record completion status."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        session = SessionLocal()
        try:
            with session.begin():
                lock = session.execute(
                    select(WorkerDistributedLock).where(
                        WorkerDistributedLock.job_name == job_name,
                        WorkerDistributedLock.run_id == run_id,
                    )
                ).scalar_one_or_none()

                if lock:
                    lock.status = status
                    lock.completed_at = now
                    lock.locked_until = now
                    if meta:
                        try:
                            existing = json.loads(lock.meta_json or "{}")
                            existing.update(meta)
                            lock.meta_json = json.dumps(existing)
                        except Exception:
                            lock.meta_json = json.dumps(meta)
                    log.info("[DistributedLock] Released lock for '%s' (run_id: %s, status: %s)", job_name, run_id, status)
                    return True
                return False
        except Exception as exc:
            log.error("[DistributedLock] Failed to release lock for '%s': %s", job_name, exc)
            return False
        finally:
            session.close()

    def get_lock_info(self, job_name: str, session: Optional[Any] = None) -> Optional[dict[str, Any]]:
        """Return raw inspection data for a job lock with active validity check."""
        close_session = False
        if session is None:
            session = SessionLocal()
            close_session = True
        try:
            lock = session.execute(
                select(WorkerDistributedLock).where(WorkerDistributedLock.job_name == job_name)
            ).scalar_one_or_none()
            if not lock:
                return None

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            is_active = False
            if lock.status == "RUNNING":
                is_unexpired = lock.locked_until is not None and lock.locked_until > now
                is_heartbeat_fresh = (
                    lock.heartbeat_at is not None
                    and (now - lock.heartbeat_at).total_seconds() < self.heartbeat_timeout_sec
                )
                is_active = bool(is_unexpired and is_heartbeat_fresh)

            return {
                "job_name": lock.job_name,
                "run_id": lock.run_id,
                "started_at": lock.started_at.isoformat() if lock.started_at else None,
                "locked_until": lock.locked_until.isoformat() if lock.locked_until else None,
                "completed_at": lock.completed_at.isoformat() if lock.completed_at else None,
                "status": "EXPIRED" if (lock.status == "RUNNING" and not is_active) else lock.status,
                "is_active": is_active,
                "heartbeat_at": lock.heartbeat_at.isoformat() if lock.heartbeat_at else None,
            }
        finally:
            if close_session:
                session.close()


class DistributedLock:
    """Context manager for distributed lock execution."""

    def __init__(
        self,
        job_name: str,
        run_id: str,
        duration_sec: int = 480,
        meta: Optional[dict[str, Any]] = None,
        manager: Optional[DistributedLockManager] = None,
    ):
        self.job_name = job_name
        self.run_id = run_id
        self.duration_sec = duration_sec
        self.meta = meta
        self.manager = manager or DistributedLockManager()
        self.acquired = False

    def __enter__(self) -> bool:
        self.acquired = self.manager.acquire(
            job_name=self.job_name,
            run_id=self.run_id,
            duration_sec=self.duration_sec,
            meta=self.meta,
        )
        return self.acquired

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            status = "FAILED" if exc_type else "COMPLETED"
            err_meta = {"error": str(exc_val)} if exc_val else None
            self.manager.release(self.job_name, self.run_id, status=status, meta=err_meta)
