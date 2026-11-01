"""SQLite persistence for IntradayEngine scan snapshots."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from db.connection import SessionLocal
from db.models import IntradayScan

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    # Keep DB values as naive UTC timestamps while avoiding deprecated utcnow().
    return datetime.now(UTC).replace(tzinfo=None)


def _session() -> Session:
    return SessionLocal()


def save_scan(
    signals: Iterable[dict] | None,
    *,
    symbols_scanned: int,
    trigger: str = "manual",
) -> int | None:
    rows = [s for s in (signals or []) if isinstance(s, dict)]
    session = _session()
    try:
        scan = IntradayScan(
            scan_time=_utcnow_naive(),
            trigger=str(trigger or "manual")[:24],
            symbols_scanned=int(symbols_scanned or 0),
            signal_count=len(rows),
            payload_json=json.dumps(rows, default=str),
        )
        session.add(scan)
        session.commit()
        return int(scan.id)
    except Exception as exc:
        logger.warning("intraday save_scan failed: %s", exc)
        try:
            session.rollback()
        except Exception:
            pass
        return None
    finally:
        session.close()


def load_latest_scan() -> dict[str, Any] | None:
    session = _session()
    try:
        row = session.query(IntradayScan).order_by(IntradayScan.scan_time.desc()).first()
        if row is None:
            return None
        try:
            setups = json.loads(row.payload_json or "[]")
        except Exception:
            setups = []
        return {
            "id": int(row.id),
            "scan_time": row.scan_time.isoformat() if row.scan_time else None,
            "trigger": row.trigger,
            "symbols_scanned": int(row.symbols_scanned or 0),
            "signal_count": int(row.signal_count or 0),
            "signals": setups if isinstance(setups, list) else [],
        }
    except Exception as exc:
        logger.warning("intraday load_latest_scan failed: %s", exc)
        return None
    finally:
        session.close()


def list_recent_scans(limit: int = 20) -> list[dict[str, Any]]:
    session = _session()
    try:
        rows = (
            session.query(IntradayScan)
            .order_by(IntradayScan.scan_time.desc())
            .limit(max(1, int(limit)))
            .all()
        )
        return [
            {
                "id": int(r.id),
                "scan_time": r.scan_time.isoformat() if r.scan_time else None,
                "trigger": r.trigger,
                "symbols_scanned": int(r.symbols_scanned or 0),
                "signal_count": int(r.signal_count or 0),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("intraday list_recent_scans failed: %s", exc)
        return []
    finally:
        session.close()


def compute_stats(days: int = 14) -> dict[str, Any]:
    """Lightweight scan-frequency stats (paper engine — no fill tracking yet)."""
    from datetime import timedelta

    session = _session()
    try:
        cutoff = _utcnow_naive() - timedelta(days=max(1, int(days)))
        q = session.query(IntradayScan).filter(IntradayScan.scan_time >= cutoff)
        n = q.count()
        last = (
            session.query(IntradayScan)
            .order_by(IntradayScan.scan_time.desc())
            .first()
        )
        return {
            "window_days": int(days),
            "scan_count": int(n),
            "last_scan_time": last.scan_time.isoformat() if last and last.scan_time else None,
            "last_signal_count": int(last.signal_count or 0) if last else 0,
        }
    except Exception as exc:
        return {"window_days": int(days), "scan_count": 0, "error": str(exc)}
    finally:
        session.close()
