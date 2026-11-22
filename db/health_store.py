"""
TradeAI Pro — Health & Telemetry Probe Store.
=============================================
Persists periodic health probe records to database (SQLite / PostgreSQL)
to provide zero-cost observability: tracking uptime, latency, memory RSS,
and feed lag over time without external APM services.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, select, delete
from sqlalchemy.orm import Session

from db.connection import SessionLocal, init_db
from db.models import HealthProbeRecord

logger = logging.getLogger(__name__)

_schema_initialized = False


def _ensure_schema() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    try:
        init_db()
        _schema_initialized = True
    except Exception as exc:
        logger.warning("Database schema check warning in health_store: %s", exc)


def record_health_probe(
    box: str = "box_a",
    status: str = "HEALTHY",
    latency_ms: Optional[float] = None,
    memory_rss_mb: Optional[float] = None,
    uptime_seconds: Optional[float] = None,
    feed_lag_seconds: Optional[float] = None,
    sidecar_active: bool = False,
    build_sha: Optional[str] = None,
    config_hash: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Append a health probe record into the database.
    Safe, fail-tolerant, returns True if recorded, False on error.
    """
    _ensure_schema()
    try:
        session: Session = SessionLocal()
        try:
            details_str = json.dumps(details) if details else None
            record = HealthProbeRecord(
                timestamp=datetime.now(timezone.utc),
                box=box[:32],
                status=status[:32].upper(),
                latency_ms=round(float(latency_ms), 2) if latency_ms is not None else None,
                memory_rss_mb=round(float(memory_rss_mb), 2) if memory_rss_mb is not None else None,
                uptime_seconds=round(float(uptime_seconds), 1) if uptime_seconds is not None else None,
                feed_lag_seconds=round(float(feed_lag_seconds), 1) if feed_lag_seconds is not None else None,
                sidecar_active=1 if sidecar_active else 0,
                build_sha=(build_sha or "")[:16] or None,
                config_hash=(config_hash or "")[:16] or None,
                details_json=details_str,
            )
            session.add(record)
            session.commit()
            return True
        finally:
            session.close()
    except Exception as exc:
        logger.warning("Failed to record health probe for %s: %s", box, exc)
        return False


def get_recent_probes(
    box: Optional[str] = None,
    limit: int = 50,
    hours: int = 24,
) -> List[Dict[str, Any]]:
    """Retrieve recent probe history ordered by timestamp descending."""
    _ensure_schema()
    try:
        session: Session = SessionLocal()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            query = select(HealthProbeRecord).where(HealthProbeRecord.timestamp >= cutoff)
            if box:
                query = query.where(HealthProbeRecord.box == box)
            query = query.order_by(desc(HealthProbeRecord.timestamp)).limit(limit)

            rows = session.execute(query).scalars().all()
            results = []
            for r in rows:
                details = None
                if r.details_json:
                    try:
                        details = json.loads(r.details_json)
                    except Exception:
                        pass
                results.append({
                    "id": r.id,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                    "box": r.box,
                    "status": r.status,
                    "latency_ms": r.latency_ms,
                    "memory_rss_mb": r.memory_rss_mb,
                    "uptime_seconds": r.uptime_seconds,
                    "feed_lag_seconds": r.feed_lag_seconds,
                    "sidecar_active": bool(r.sidecar_active),
                    "build_sha": r.build_sha,
                    "config_hash": r.config_hash,
                    "details": details,
                })
            return results
        finally:
            session.close()
    except Exception as exc:
        logger.warning("Failed to query recent health probes: %s", exc)
        return []


def get_health_summary(hours: int = 24) -> Dict[str, Any]:
    """
    Compute aggregate observability telemetry over the specified window (default 24h).
    Returns uptime percentage, peak RSS memory, average latency, and max feed lag.
    """
    _ensure_schema()
    default_summary = {
        "window_hours": hours,
        "total_probes": 0,
        "uptime_pct": 100.0,
        "peak_rss_mb": 0.0,
        "avg_latency_ms": 0.0,
        "max_feed_lag_seconds": 0.0,
        "sidecar_uptime_pct": 0.0,
        "latest_build_sha": None,
        "latest_config_hash": None,
    }
    try:
        session: Session = SessionLocal()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            probes = (
                session.execute(
                    select(HealthProbeRecord)
                    .where(HealthProbeRecord.timestamp >= cutoff)
                    .order_by(desc(HealthProbeRecord.timestamp))
                )
                .scalars()
                .all()
            )
            if not probes:
                return default_summary

            total = len(probes)
            healthy_count = sum(1 for p in probes if p.status in ("HEALTHY", "ALIVE", "OK"))
            sidecar_count = sum(1 for p in probes if p.sidecar_active == 1)

            rss_values = [p.memory_rss_mb for p in probes if p.memory_rss_mb is not None]
            lat_values = [p.latency_ms for p in probes if p.latency_ms is not None]
            lag_values = [p.feed_lag_seconds for p in probes if p.feed_lag_seconds is not None]

            latest = probes[0]
            return {
                "window_hours": hours,
                "total_probes": total,
                "uptime_pct": round((healthy_count / total) * 100.0, 2) if total > 0 else 100.0,
                "peak_rss_mb": round(max(rss_values), 2) if rss_values else 0.0,
                "avg_latency_ms": round(sum(lat_values) / len(lat_values), 2) if lat_values else 0.0,
                "max_feed_lag_seconds": round(max(lag_values), 1) if lag_values else 0.0,
                "sidecar_uptime_pct": round((sidecar_count / total) * 100.0, 2) if total > 0 else 0.0,
                "latest_build_sha": latest.build_sha,
                "latest_config_hash": latest.config_hash,
            }
        finally:
            session.close()
    except Exception as exc:
        logger.warning("Failed to compute health summary: %s", exc)
        return default_summary


def prune_old_probes(retention_days: int = 7) -> int:
    """Prune probes older than retention_days to keep database storage bounded."""
    _ensure_schema()
    try:
        session: Session = SessionLocal()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
            stmt = delete(HealthProbeRecord).where(HealthProbeRecord.timestamp < cutoff)
            res = session.execute(stmt)
            session.commit()
            return res.rowcount or 0
        finally:
            session.close()
    except Exception as exc:
        logger.warning("Failed to prune old health probes: %s", exc)
        return 0
