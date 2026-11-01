"""
Swing scan persistence layer.

Provides thin, synchronous helpers around the SwingScan / SwingSetupOutcome
tables so the FastAPI service and background workers can save + load
SwingSpectrum scans without coupling to SQLAlchemy detail.

All functions are safe to call from threads spawned via asyncio.to_thread —
they open/close their own SQLAlchemy session.

Public API:
    save_scan(setups, market_context, *, trigger) -> int | None
    load_latest_scan() -> dict | None
    list_recent_scans(limit=30) -> list[dict]
    mark_outcome(outcome_id, *, status, hit_level, hit_price, hit_at, bars_to_hit, pnl_pct)
    fetch_open_outcomes(limit=500) -> list[dict]
    compute_stats(days=30) -> dict
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from db.connection import SessionLocal
from db.models import SwingScan, SwingSetupOutcome

logger = logging.getLogger(__name__)

_DEFAULT_MARKET_CONTEXT_KEYS = (
    "index_symbol",
    "index_trend",
    "index_regime",
    "index_last_close",
)

ACTIVE_STATUSES = ("OPEN", "WATCHLIST", "TRIGGERED", "FILLED")


# ── internal helpers ────────────────────────────────────────────────


def _as_list(setups: Iterable[dict] | None) -> list[dict]:
    if not setups:
        return []
    out: list[dict] = []
    for s in setups:
        if isinstance(s, dict):
            out.append(s)
    return out


def _count_grades(setups: list[dict]) -> tuple[int, int, int]:
    a = b = c = 0
    for s in setups:
        g = str(s.get("grade") or "C").upper()
        if g == "A":
            a += 1
        elif g == "B":
            b += 1
        else:
            c += 1
    return a, b, c


def _session() -> Session:
    return SessionLocal()


def _scan_to_dict(scan: SwingScan) -> dict[str, Any]:
    try:
        setups = json.loads(scan.payload_json or "[]")
    except Exception:
        setups = []
    market_ctx: dict | None
    if scan.market_context_json:
        try:
            market_ctx = json.loads(scan.market_context_json)
        except Exception:
            market_ctx = None
    else:
        market_ctx = None
    return {
        "id": int(scan.id),
        "scan_date": scan.scan_date.isoformat() if scan.scan_date else None,
        "scan_time": scan.scan_time.isoformat() if scan.scan_time else None,
        "trigger": scan.trigger,
        "symbols_scanned": int(scan.symbols_scanned or 0),
        "total_setups": int(scan.total_setups or 0),
        "grade_a_count": int(scan.grade_a_count or 0),
        "grade_b_count": int(scan.grade_b_count or 0),
        "grade_c_count": int(scan.grade_c_count or 0),
        "setups": setups,
        "market_context": market_ctx,
    }


# ── public API ──────────────────────────────────────────────────────


def save_scan(
    setups: Iterable[dict] | None,
    market_context: dict | None = None,
    *,
    symbols_scanned: int | None = None,
    trigger: str = "manual",
) -> int | None:
    """Persist a full scan snapshot + per-setup outcome rows.

    Returns the new SwingScan.id, or None on failure (logged, never raised).

    Duplicate guard: before inserting each SwingSetupOutcome row, we check
    whether an identical (symbol, pattern, direction, entry_zone, scan_date)
    combination already exists for today.  If it does the row is silently
    skipped so that periodic refreshes that call save_scan multiple times per
    day never create 20+ duplicate entries.
    """
    payload = _as_list(setups)
    a, b, c = _count_grades(payload)
    ctx_json: str | None = None
    if market_context:
        try:
            # Keep only the documented keys to avoid storing large unrelated blobs.
            filtered = {k: market_context.get(k) for k in _DEFAULT_MARKET_CONTEXT_KEYS}
            ctx_json = json.dumps(filtered, default=str)
        except Exception:
            ctx_json = None

    today = date.today()

    session = _session()
    try:
        scan = SwingScan(
            scan_date=today,
            scan_time=datetime.now(timezone.utc),
            trigger=trigger,
            symbols_scanned=int(symbols_scanned or 0),
            total_setups=len(payload),
            grade_a_count=a,
            grade_b_count=b,
            grade_c_count=c,
            payload_json=json.dumps(payload, default=str),
            market_context_json=ctx_json,
        )
        session.add(scan)
        session.flush()  # populates scan.id

        # Multi-day deduplication: If a trade on (symbol, pattern, direction) is
        # already ACTIVE (OPEN/WATCHLIST/TRIGGERED/FILLED), skip creating a duplicate outcome row.
        # This keeps scan snapshots intact in SwingScan while ensuring the outcome table
        # tracks exactly 1 trade until closure.
        try:
            active_rows = (
                session.query(
                    SwingSetupOutcome.symbol,
                    SwingSetupOutcome.pattern,
                    SwingSetupOutcome.direction,
                )
                .filter(SwingSetupOutcome.status.in_(ACTIVE_STATUSES))
                .all()
            )
            _active_trades: set[tuple[str, str, str]] = {
                (
                    str(r.symbol or "").upper().removesuffix(".NS").strip(),
                    str(r.pattern or "").strip(),
                    str(r.direction or "LONG").upper().strip(),
                )
                for r in active_rows
            }
        except Exception as exc:
            logger.debug("Could not pre-fetch active outcomes for dupe check: %s", exc)
            _active_trades = set()

        inserted = skipped = 0
        for s in payload:
            try:
                sym = str(s.get("symbol") or "")[:30]
                pattern = str(s.get("pattern") or "")[:80]
                direction = str(s.get("direction") or "LONG")[:10]
                entry_zone = round(float(s.get("entry_zone") or 0.0), 2)

                sym_norm = sym.upper().removesuffix(".NS").strip()
                active_key = (sym_norm, pattern.strip(), direction.upper().strip())
                if active_key in _active_trades:
                    logger.debug(
                        "Duplicate outcome skipped (active trade already open): %s %s %s entry=%.2f",
                        sym,
                        direction,
                        pattern,
                        entry_zone,
                    )
                    skipped += 1
                    continue

                session.add(
                    SwingSetupOutcome(
                        scan_id=scan.id,
                        symbol=sym,
                        pattern=pattern,
                        direction=direction,
                        grade=str(s.get("grade") or "C")[:2],
                        strength=int(s.get("strength") or 0),
                        entry_zone=entry_zone,
                        stop_loss=float(s.get("stop_loss") or 0.0),
                        target_1=float(s.get("target_1") or 0.0),
                        target_2=float(s.get("target_2") or 0.0),
                        risk_reward=float(s.get("risk_reward") or 0.0),
                        timeframe=str(s.get("timeframe") or "")[:20] or None,
                        emitted_at=datetime.now(timezone.utc),
                        status="OPEN",
                    )
                )
                _active_trades.add(active_key)  # guard against dupes within this batch
                inserted += 1
            except Exception as exc:
                logger.debug("Skipping malformed setup row during save: %s", exc)

        if skipped:
            logger.info(
                "save_scan: %d outcome rows inserted, %d duplicates skipped (active trades).",
                inserted,
                skipped,
            )

        session.commit()
        return int(scan.id)
    except Exception as exc:
        logger.exception("save_scan failed: %s", exc)
        try:
            session.rollback()
        except Exception:
            pass
        return None
    finally:
        session.close()


def load_latest_scan() -> dict[str, Any] | None:
    """Return the most recent scan as a dict (or None if no scans saved yet)."""
    session = _session()
    try:
        scan = session.query(SwingScan).order_by(SwingScan.scan_time.desc()).first()
        return _scan_to_dict(scan) if scan else None
    except Exception as exc:
        logger.warning("load_latest_scan failed: %s", exc)
        return None
    finally:
        session.close()


def load_scan_by_id(scan_id: int) -> dict[str, Any] | None:
    """Return a single scan by primary key (full payload), or None."""
    session = _session()
    try:
        scan = session.query(SwingScan).filter(SwingScan.id == int(scan_id)).first()
        return _scan_to_dict(scan) if scan else None
    except Exception as exc:
        logger.warning("load_scan_by_id(%s) failed: %s", scan_id, exc)
        return None
    finally:
        session.close()


def list_recent_scans(limit: int = 30) -> list[dict[str, Any]]:
    """Return up to `limit` most recent scans, without payload (summary only)."""
    session = _session()
    try:
        rows = (
            session.query(SwingScan)
            .order_by(SwingScan.scan_time.desc())
            .limit(max(1, int(limit)))
            .all()
        )
        out: list[dict] = []
        for r in rows:
            out.append(
                {
                    "id": int(r.id),
                    "scan_date": r.scan_date.isoformat() if r.scan_date else None,
                    "scan_time": r.scan_time.isoformat() if r.scan_time else None,
                    "trigger": r.trigger,
                    "symbols_scanned": int(r.symbols_scanned or 0),
                    "total_setups": int(r.total_setups or 0),
                    "grade_a_count": int(r.grade_a_count or 0),
                    "grade_b_count": int(r.grade_b_count or 0),
                    "grade_c_count": int(r.grade_c_count or 0),
                }
            )
        return out
    except Exception as exc:
        logger.warning("list_recent_scans failed: %s", exc)
        return []
    finally:
        session.close()


def fetch_open_outcomes(limit: int = 500) -> list[dict[str, Any]]:
    """Return active (OPEN / WATCHLIST / TRIGGERED / FILLED) setup outcomes for the EOD checker to evaluate."""
    session = _session()
    try:
        rows = (
            session.query(SwingSetupOutcome)
            .filter(SwingSetupOutcome.status.in_(ACTIVE_STATUSES))
            .order_by(SwingSetupOutcome.emitted_at.asc())
            .limit(max(1, int(limit)))
            .all()
        )
        return [
            {
                "id": int(r.id),
                "scan_id": int(r.scan_id),
                "symbol": r.symbol,
                "pattern": r.pattern,
                "direction": r.direction,
                "grade": r.grade,
                "strength": int(r.strength or 0),
                "entry_zone": float(r.entry_zone or 0.0),
                "stop_loss": float(r.stop_loss or 0.0),
                "target_1": float(r.target_1 or 0.0),
                "target_2": float(r.target_2 or 0.0),
                "risk_reward": float(r.risk_reward or 0.0),
                "timeframe": r.timeframe,
                "emitted_at": r.emitted_at.isoformat() if r.emitted_at else None,
                "status": r.status,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("fetch_open_outcomes failed: %s", exc)
        return []
    finally:
        session.close()


def mark_outcome(
    outcome_id: int,
    *,
    status: str,
    hit_level: str | None = None,
    hit_price: float | None = None,
    hit_at: datetime | None = None,
    bars_to_hit: int | None = None,
    pnl_pct: float | None = None,
) -> bool:
    """Update a SwingSetupOutcome row with hit/exit information."""
    session = _session()
    try:
        row = (
            session.query(SwingSetupOutcome)
            .filter(SwingSetupOutcome.id == int(outcome_id))
            .first()
        )
        if row is None:
            return False
        row.status = status[:20]
        row.hit_level = (hit_level or None) and hit_level[:20]
        row.hit_price = float(hit_price) if hit_price is not None else None
        row.hit_at = hit_at
        row.bars_to_hit = int(bars_to_hit) if bars_to_hit is not None else None
        row.pnl_pct = float(pnl_pct) if pnl_pct is not None else None
        session.commit()
        return True
    except Exception as exc:
        logger.warning("mark_outcome(%s) failed: %s", outcome_id, exc)
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def compute_stats(days: int = 30) -> dict[str, Any]:
    """Aggregate outcome stats for the last N days (overall + by grade + by pattern)."""
    session = _session()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        base = session.query(SwingSetupOutcome).filter(
            SwingSetupOutcome.emitted_at >= cutoff
        )

        total = base.count()
        wins = base.filter(SwingSetupOutcome.status.in_(["T1_HIT", "T2_HIT"])).count()
        losses = base.filter(SwingSetupOutcome.status == "SL_HIT").count()
        expired = base.filter(SwingSetupOutcome.status == "EXPIRED").count()
        cancelled = base.filter(
            SwingSetupOutcome.status.in_(["CANCELLED", "MISSED_ENTRY", "EXPIRED_UNFILLED"])
        ).count()
        filled_trades = wins + losses + expired
        closed = filled_trades + cancelled

        def _safe_rate(num: int, denom: int) -> float:
            return round(100.0 * num / denom, 1) if denom > 0 else 0.0

        # Calculate Net Expectancy in R:
        # T1_HIT = +1.0 R; T2_HIT = max(1.5, risk_reward); SL_HIT = -1.0 R; EXPIRED = realized R
        closed_filled_rows = (
            base.filter(
                SwingSetupOutcome.status.in_(["T1_HIT", "T2_HIT", "SL_HIT", "EXPIRED"])
            ).all()
        )
        total_r = 0.0
        for r in closed_filled_rows:
            rr = float(r.risk_reward or 1.5)
            if r.status == "T1_HIT":
                total_r += 1.0
            elif r.status == "T2_HIT":
                total_r += max(1.5, rr)
            elif r.status == "SL_HIT":
                total_r -= 1.0
            elif r.status == "EXPIRED":
                entry_val = float(r.entry_zone or 0.0)
                sl_val = float(r.stop_loss or 0.0)
                risk_pct = abs(entry_val - sl_val) / max(entry_val, 1e-9) * 100.0
                actual_pnl = float(r.pnl_pct or 0.0)
                if risk_pct > 0:
                    total_r += round(actual_pnl / risk_pct, 2)
        expectancy_r = round(total_r / filled_trades, 2) if filled_trades > 0 else 0.0

        by_grade: dict[str, dict] = {}
        for grade in ("A", "B", "C"):
            g_base = base.filter(SwingSetupOutcome.grade == grade)
            g_wins = g_base.filter(
                SwingSetupOutcome.status.in_(["T1_HIT", "T2_HIT"])
            ).count()
            g_losses = g_base.filter(SwingSetupOutcome.status == "SL_HIT").count()
            g_expired = g_base.filter(SwingSetupOutcome.status == "EXPIRED").count()
            g_filled = g_wins + g_losses + g_expired
            g_cancelled = g_base.filter(
                SwingSetupOutcome.status.in_(["CANCELLED", "MISSED_ENTRY", "EXPIRED_UNFILLED"])
            ).count()
            g_closed = g_filled + g_cancelled
            by_grade[grade] = {
                "total": g_base.count(),
                "closed": g_closed,
                "filled_trades": g_filled,
                "cancelled": g_cancelled,
                "wins": g_wins,
                "losses": g_losses,
                "win_rate_pct": _safe_rate(g_wins, g_filled if g_filled > 0 else g_closed),
            }

        pattern_rows = (
            base.with_entities(
                SwingSetupOutcome.pattern,
                func.count(SwingSetupOutcome.id),
            )
            .group_by(SwingSetupOutcome.pattern)
            .all()
        )
        by_pattern: list[dict] = []
        for patt, count in pattern_rows:
            p_base = base.filter(SwingSetupOutcome.pattern == patt)
            p_wins = p_base.filter(
                SwingSetupOutcome.status.in_(["T1_HIT", "T2_HIT"])
            ).count()
            p_losses = p_base.filter(SwingSetupOutcome.status == "SL_HIT").count()
            p_expired = p_base.filter(SwingSetupOutcome.status == "EXPIRED").count()
            p_filled = p_wins + p_losses + p_expired
            p_cancelled = p_base.filter(
                SwingSetupOutcome.status.in_(["CANCELLED", "MISSED_ENTRY", "EXPIRED_UNFILLED"])
            ).count()
            p_closed = p_filled + p_cancelled
            by_pattern.append(
                {
                    "pattern": patt,
                    "total": int(count),
                    "closed": int(p_closed),
                    "filled_trades": int(p_filled),
                    "cancelled": int(p_cancelled),
                    "wins": int(p_wins),
                    "losses": int(p_losses),
                    "win_rate_pct": _safe_rate(p_wins, p_filled if p_filled > 0 else p_closed),
                }
            )
        by_pattern.sort(key=lambda r: (-r["total"], r["pattern"]))

        return {
            "window_days": int(days),
            "total_setups": int(total),
            "closed": int(closed),
            "filled_trades": int(filled_trades),
            "cancelled": int(cancelled),
            "wins": int(wins),
            "losses": int(losses),
            "expired": int(expired),
            "win_rate_pct": _safe_rate(wins, filled_trades if filled_trades > 0 else closed),
            "expectancy_r": expectancy_r,
            "by_grade": by_grade,
            "by_pattern": by_pattern[:20],
        }
    except Exception as exc:
        logger.warning("compute_stats failed: %s", exc)
        return {
            "window_days": int(days),
            "total_setups": 0,
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "expired": 0,
            "win_rate_pct": 0.0,
            "by_grade": {},
            "by_pattern": [],
            "error": str(exc),
        }
    finally:
        session.close()
