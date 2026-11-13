"""
Intraday scan persistence store with full Freshness Contract support.
Provides:
- save_scan() with freshness metadata
- load_latest_scan() with computed freshness status, ages, and stale-signal filtering
- list_recent_scans()
- compute_stats()
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timezone, timedelta, date
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from db.connection import SessionLocal
from db.models import IntradayScan

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _now_ist() -> datetime:
    return datetime.now(_IST)


def _session() -> Session:
    return SessionLocal()


def save_scan(
    signals: Iterable[dict] | None,
    *,
    symbols_scanned: int = 0,
    trigger: str = "manual",
    scan_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    source_last_candle_at: Optional[datetime] = None,
    market_session_date: Optional[date] = None,
    provider: str = "yfinance",
    provider_freshness: str = "15m Delayed",
    symbols_attempted: Optional[int] = None,
    symbols_successful: Optional[int] = None,
    symbols_failed: int = 0,
    scanner_version: str = "2.0.0",
    scan_duration_ms: int = 0,
    meta: Optional[dict[str, Any]] = None,
) -> int | None:
    """
    Save an intraday scan snapshot with complete Scan Freshness Contract.
    """
    now = _utcnow_naive()
    now_ist = _now_ist()
    started_at = started_at or now
    completed_at = completed_at or now
    market_session_date = market_session_date or now_ist.date()

    if not scan_id:
        scan_id = f"iscan-{now_ist.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    # Infer source_last_candle_at from signals if not explicitly provided
    if source_last_candle_at is None:
        candle_times = []
        for s in (signals or []):
            if isinstance(s, dict):
                ct = s.get("source_candle_at") or s.get("candle_time") or s.get("source_candle_time") or s.get("timestamp")
                if ct:
                    try:
                        ct_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                        if ct_dt.tzinfo is not None:
                            ct_dt = ct_dt.astimezone(timezone.utc).replace(tzinfo=None)
                        candle_times.append(ct_dt)
                    except Exception:
                        pass
        if candle_times:
            source_last_candle_at = max(candle_times)

    # Enrich signals with generated_at, valid_until, source_candle_at
    rows = []
    qualified_count = 0
    validity_window_minutes = 30

    for s in (signals or []):
        if not isinstance(s, dict):
            continue
        sig = dict(s)
        gen_time = sig.get("generated_at") or now_ist.isoformat()
        sig["generated_at"] = gen_time
        if "valid_until" not in sig:
            valid_until_dt = now_ist + timedelta(minutes=validity_window_minutes)
            sig["valid_until"] = valid_until_dt.isoformat()
        if "source_candle_at" not in sig:
            sig["source_candle_at"] = sig.get("candle_time") or (
                source_last_candle_at.isoformat() if source_last_candle_at else gen_time
            )

        # Mark qualified if score/confidence passes threshold
        score = float(sig.get("confluence_score", sig.get("score", sig.get("quant_score", 0.0))))
        if score >= 50.0:
            qualified_count += 1
        rows.append(sig)

    attempted = symbols_attempted if symbols_attempted is not None else int(symbols_scanned or len(rows))
    successful = symbols_successful if symbols_successful is not None else (attempted - symbols_failed)

    meta_payload = {
        "scan_id": scan_id,
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "source_last_candle_at": source_last_candle_at.isoformat() if source_last_candle_at else None,
        "market_session_date": market_session_date.isoformat() if market_session_date else None,
        "provider": provider,
        "provider_freshness": provider_freshness,
        "symbols_attempted": attempted,
        "symbols_successful": successful,
        "symbols_failed": symbols_failed,
        "qualified_setups": qualified_count,
        "scanner_version": scanner_version,
        "scan_duration_ms": scan_duration_ms,
    }
    if meta:
        meta_payload.update(meta)

    session = _session()
    try:
        scan = IntradayScan(
            scan_id=scan_id,
            scan_time=completed_at,
            started_at=started_at,
            completed_at=completed_at,
            source_last_candle_at=source_last_candle_at,
            market_session_date=market_session_date,
            provider=provider,
            provider_freshness=provider_freshness,
            symbols_attempted=attempted,
            symbols_successful=successful,
            symbols_failed=symbols_failed,
            scanner_version=scanner_version,
            scan_duration_ms=scan_duration_ms,
            trigger=str(trigger or "manual")[:24],
            symbols_scanned=int(symbols_scanned or attempted),
            signal_count=len(rows),
            payload_json=json.dumps(rows, default=str),
            meta_json=json.dumps(meta_payload, default=str),
        )
        session.add(scan)
        session.commit()
        logger.info(
            "✅ [IntradayStore] Saved scan '%s' (id=%d): %d signals (%d qualified) in %dms",
            scan_id, scan.id, len(rows), qualified_count, scan_duration_ms
        )
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
    """
    Retrieve latest intraday scan with computed freshness metrics.
    """
    session = _session()
    try:
        row = session.query(IntradayScan).order_by(IntradayScan.scan_time.desc()).first()
        if row is None:
            return None

        try:
            setups = json.loads(row.payload_json or "[]")
        except Exception:
            setups = []

        now_utc = _utcnow_naive()
        now_ist = _now_ist()
        today_ist_str = now_ist.date().isoformat()

        # Extract or fallback metadata
        meta = {}
        if row.meta_json:
            try:
                meta = json.loads(row.meta_json)
            except Exception:
                meta = {}

        scan_time_dt = row.scan_time or row.completed_at
        source_candle_dt = row.source_last_candle_at

        # Calculate ages
        scan_age_seconds = None
        if scan_time_dt:
            scan_age_seconds = max(0, int((now_utc - scan_time_dt).total_seconds()))

        data_age_seconds = None
        if source_candle_dt:
            data_age_seconds = max(0, int((now_utc - source_candle_dt).total_seconds()))
        elif scan_age_seconds is not None:
            # Fallback estimation for 15m delayed data
            data_age_seconds = scan_age_seconds + 900

        # Determine freshness status
        is_today = False
        if row.market_session_date:
            is_today = (row.market_session_date.isoformat() == today_ist_str)
        elif scan_time_dt:
            is_today = (scan_time_dt.date() == now_utc.date())

        if not is_today:
            freshness_status = "EOD"
        elif scan_age_seconds is None or scan_age_seconds > 3600 or (data_age_seconds and data_age_seconds > 3600):
            freshness_status = "STALE"
        elif scan_age_seconds <= 1200:
            freshness_status = "DELAYED" if "Delayed" in str(row.provider_freshness or "") else "FRESH"
        else:
            freshness_status = "DELAYED"

        # Mark expired signals past valid_until
        active_setups = []
        for s in setups:
            valid_until_str = s.get("valid_until")
            is_expired = False
            if valid_until_str:
                try:
                    vu_dt = datetime.fromisoformat(valid_until_str)
                    if vu_dt.tzinfo is None:
                        vu_dt = vu_dt.replace(tzinfo=_IST)
                    if now_ist > vu_dt:
                        is_expired = True
                except Exception:
                    pass
            if not is_today:
                is_expired = True
            s["is_expired"] = is_expired
            if not is_expired:
                active_setups.append(s)

        qualified_count = sum(
            1 for s in setups
            if float(s.get("confluence_score", s.get("score", s.get("quant_score", 0)))) >= 50.0
            and not s.get("is_expired")
        )

        return {
            "id": int(row.id),
            "scan_id": row.scan_id or meta.get("scan_id") or f"iscan-{row.id}",
            "scan_time": row.scan_time.isoformat() if row.scan_time else None,
            "last_scan_at": (row.scan_time or row.completed_at).isoformat() if (row.scan_time or row.completed_at) else None,
            "started_at": row.started_at.isoformat() if row.started_at else meta.get("started_at"),
            "completed_at": row.completed_at.isoformat() if row.completed_at else meta.get("completed_at"),
            "source_candle_at": source_candle_dt.isoformat() if source_candle_dt else meta.get("source_last_candle_at"),
            "market_session_date": row.market_session_date.isoformat() if row.market_session_date else meta.get("market_session_date"),
            "provider": row.provider or meta.get("provider", "yfinance"),
            "provider_freshness": row.provider_freshness or meta.get("provider_freshness", "15m Delayed"),
            "symbols_attempted": row.symbols_attempted or meta.get("symbols_attempted", row.symbols_scanned),
            "symbols_successful": row.symbols_successful or meta.get("symbols_successful", row.symbols_scanned),
            "symbols_failed": row.symbols_failed or meta.get("symbols_failed", 0),
            "scanner_version": row.scanner_version or meta.get("scanner_version", "2.0.0"),
            "scan_duration_ms": row.scan_duration_ms or meta.get("scan_duration_ms", 0),
            "trigger": row.trigger,
            "symbols_scanned": int(row.symbols_scanned or 0),
            "signal_count": int(row.signal_count or 0),
            "qualified_setups": qualified_count,
            "scan_age_seconds": scan_age_seconds,
            "data_age_seconds": data_age_seconds,
            "freshness_status": freshness_status,
            "signals": setups if isinstance(setups, list) else [],
            "active_signals": active_setups,
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
                "scan_id": r.scan_id,
                "scan_time": r.scan_time.isoformat() if r.scan_time else None,
                "trigger": r.trigger,
                "symbols_scanned": int(r.symbols_scanned or 0),
                "signal_count": int(r.signal_count or 0),
                "provider": r.provider,
                "provider_freshness": r.provider_freshness,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("intraday list_recent_scans failed: %s", exc)
        return []
    finally:
        session.close()


def compute_stats(days: int = 14) -> dict[str, Any]:
    """Lightweight scan-frequency stats."""
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
