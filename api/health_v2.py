"""
TradeAI-PRO — Health v2 Engine (3-Tier Composite Health Model)
==============================================================
Evaluates system health across 3 independent architectural layers:
  Tier 1: Infrastructure (Database connectivity + API runtime)
  Tier 2: Autonomous Worker State (Cycle recency vs market session)
  Tier 3: Market Data Freshness (Feed age vs trading hours)

Prevents false "Healthy" reporting when market feed or worker cycles are stale.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from agents.execution.market_control import is_nse_activity_window_active

logger = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")


def get_last_completed_session_date(ref_ist: datetime) -> Any:
    """Find the date of the most recent completed NSE trading session."""
    from datetime import date, time as dt_time, timedelta
    try:
        from agents.market_intelligence.nse_holidays import is_holiday
    except Exception:
        is_holiday = lambda d: False  # type: ignore

    d = ref_ist.date()
    # If today is a weekday and not a holiday, and current time is >= 15:30 IST, today completed.
    if ref_ist.weekday() < 5 and not is_holiday(d) and ref_ist.time() >= dt_time(15, 30):
        return d

    # Otherwise step back 1 day at a time to find the last completed session
    cur = d - timedelta(days=1)
    while cur.weekday() >= 5 or is_holiday(cur):
        cur -= timedelta(days=1)
    return cur


def format_human_data_age(
    snap_dt: Optional[datetime],
    age_seconds: Optional[float],
    is_nse_open: bool,
    now_ist: Optional[datetime] = None,
    data_age_source: str = "LAST_BAR",
) -> str:
    """Format an honest, user-facing label for data freshness and exchange delay."""
    if snap_dt is None or age_seconds is None:
        return "Data: Unavailable"

    if now_ist is None:
        now_ist = datetime.now(_IST)

    snap_ist = snap_dt.astimezone(_IST) if snap_dt.tzinfo else snap_dt.replace(tzinfo=_IST)

    if not is_nse_open:
        # Market Closed / Weekend
        bar_str = snap_ist.strftime("%d %b %H:%M")
        if age_seconds < 86400:
            h = int(age_seconds // 3600)
            m = int((age_seconds % 3600) // 60)
            age_str = f"{h}h {m}m old"
        else:
            d = int(age_seconds // 86400)
            h = int((age_seconds % 86400) // 3600)
            age_str = f"{d}d {h}h old"

        suffix = " · bar time unknown" if data_age_source == "CYCLE_FALLBACK" else ""
        return f"EOD Snapshot · {bar_str} (Market Closed) · {age_str}{suffix}"

    # Market is active (09:15 - 15:30 IST)
    mins_ago = int(age_seconds // 60)
    if mins_ago <= 1:
        age_str = "just now"
    elif mins_ago < 60:
        age_str = f"{mins_ago}m ago"
    else:
        hours_ago = int(mins_ago // 60)
        age_str = f"{hours_ago}h ago"

    if age_seconds <= 1800:
        return f"15m Delayed · Updated {age_str}"
    else:
        return f"⚠️ Stale Feed · Updated {age_str}"


def evaluate_health_v2(
    db_ok: bool,
    feed_status: Dict[str, Any],
    worker_status: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Evaluate 3-tier composite health.
    Returns:
      {
        "status": "HEALTHY" | "STALE_DATA" | "WORKER_DOWN" | "DEGRADED",
        "healthy": bool,
        "health_tier": {"infra": ..., "worker": ..., "freshness": ...},
        "health_reasons": {"infra": ..., "worker": ..., "freshness": ...},
        "overall_reason": str,
        "data_age_seconds": float | None,
        "data_age_human": str,
      }
    """
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_ist = now_utc.astimezone(_IST)

    is_nse_open, nse_reason = is_nse_activity_window_active(now_ist)

    # ── Tier 1: Infrastructure ────────────────────────────────────────────────
    infra_tier = "UP" if db_ok else "DOWN"
    infra_healthy = db_ok
    infra_reason = "Database connected" if db_ok else "Database connectivity failure"

    # ── Tier 2: Worker State ──────────────────────────────────────────────────
    worker_health_code = str(feed_status.get("worker_health", "NO_RECENT_RUN")).upper()
    w_age: Optional[float] = None
    if worker_status and isinstance(worker_status, dict) and worker_status.get("timestamp"):
        try:
            w_ts = worker_status["timestamp"]
            w_dt = datetime.fromisoformat(w_ts.replace("Z", "+00:00"))
            if w_dt.tzinfo is None:
                w_dt = w_dt.replace(tzinfo=timezone.utc)
            w_age = max((now_utc - w_dt).total_seconds(), 0.0)
        except Exception:
            pass

    if not is_nse_open:
        # Off-hours: Worker is permitted to be in standby
        if worker_health_code in ("WORKER_OK", "NO_RECENT_RUN") or (w_age is not None and w_age <= 86400 * 3):
            worker_tier = "STANDBY_OFF_HOURS"
            worker_healthy = True
            worker_reason = f"Standby nominal ({nse_reason})"
        elif worker_health_code == "WORKER_FAILED":
            worker_tier = "WORKER_FAILED"
            worker_healthy = False
            worker_reason = "Last worker run encountered an execution error"
        else:
            worker_tier = "STANDBY_OFF_HOURS"
            worker_healthy = True
            worker_reason = f"Standby ({nse_reason})"
    else:
        # Active market hours (09:15 - 15:30 IST)
        if worker_health_code == "WORKER_OK":
            worker_tier = "WORKER_OK"
            worker_healthy = True
            worker_reason = "Worker active (< 30m)"
        elif worker_health_code == "WORKER_LATE":
            worker_tier = "WORKER_LATE"
            worker_healthy = False
            worker_reason = "Worker cycle delayed (> 45m)"
        elif worker_health_code == "WORKER_FAILED":
            worker_tier = "WORKER_FAILED"
            worker_healthy = False
            worker_reason = "Worker execution failure reported"
        else:
            worker_tier = "WORKER_STALE"
            worker_healthy = False
            worker_reason = "Worker cycle has not executed during active market session"

    # ── Tier 3: Data Freshness ────────────────────────────────────────────────
    last_bar_at_val = feed_status.get("last_bar_at")
    bar_age_val = feed_status.get("bar_age_seconds")

    snap_dt: Optional[datetime] = None
    age_seconds: Optional[float] = None
    data_age_source = "CYCLE_FALLBACK"

    if last_bar_at_val or bar_age_val is not None:
        if last_bar_at_val:
            try:
                snap_dt = datetime.fromisoformat(str(last_bar_at_val).replace("Z", "+00:00"))
                if snap_dt.tzinfo is None:
                    snap_dt = snap_dt.replace(tzinfo=_IST)
            except Exception:
                snap_dt = None
        if bar_age_val is not None:
            try:
                age_seconds = float(bar_age_val)
            except Exception:
                age_seconds = None
        elif snap_dt is not None:
            age_seconds = max((now_utc - snap_dt).total_seconds(), 0.0)

        if snap_dt is not None or age_seconds is not None:
            data_age_source = "LAST_BAR"

    if snap_dt is None or age_seconds is None:
        data_age_source = "CYCLE_FALLBACK"
        last_update_str = feed_status.get("last_update")
        if last_update_str:
            try:
                snap_dt = datetime.fromisoformat(str(last_update_str).replace("Z", "+00:00"))
                if snap_dt.tzinfo is None:
                    snap_dt = snap_dt.replace(tzinfo=timezone.utc)
            except Exception:
                snap_dt = None
        cycle_age = feed_status.get("age_seconds")
        if cycle_age is not None:
            try:
                age_seconds = float(cycle_age)
            except Exception:
                age_seconds = None
        elif snap_dt is not None:
            age_seconds = max((now_utc - snap_dt).total_seconds(), 0.0)

    if age_seconds is None or snap_dt is None:
        freshness_tier = "UNAVAILABLE"
        freshness_healthy = False
        freshness_reason = "No market data snapshot available"
    elif not is_nse_open:
        # Off-hours: EOD snapshot is valid ONLY if bar date matches or is not older than the last completed session
        bar_ist = snap_dt.astimezone(_IST) if snap_dt.tzinfo else snap_dt.replace(tzinfo=_IST)
        last_session_date = get_last_completed_session_date(now_ist)
        if bar_ist.date() < last_session_date:
            freshness_tier = "STALE_DATA"
            freshness_healthy = False
            freshness_reason = (
                f"Market data bar ({bar_ist.strftime('%d %b')}) is older than "
                f"last completed session ({last_session_date.strftime('%d %b')})"
            )
        else:
            freshness_tier = "EOD_VALID"
            freshness_healthy = True
            hours_ago = int(age_seconds // 3600)
            freshness_reason = f"EOD Snapshot valid ({hours_ago}h ago, market closed)"
    else:
        # Market open: Expecting ~15m delay (tolerance up to 30 mins)
        if age_seconds <= 1800:
            freshness_tier = "DATA_FRESH"
            freshness_healthy = True
            freshness_reason = f"Feed fresh (~15m delayed, updated {int(age_seconds // 60)}m ago)"
        else:
            freshness_tier = "STALE_DATA"
            freshness_healthy = False
            freshness_reason = f"Market feed is lagging ({int(age_seconds // 60)}m old > 30m limit)"

    # ── Consolidated Status & Reason ──────────────────────────────────────────
    if not infra_healthy:
        overall_status = "DEGRADED"
        overall_reason = infra_reason
    elif not freshness_healthy:
        overall_status = "STALE_DATA"
        overall_reason = freshness_reason
    elif is_nse_open and not worker_healthy:
        overall_status = "WORKER_DOWN"
        overall_reason = worker_reason
    else:
        overall_status = "HEALTHY"
        overall_reason = "All system tiers nominal"

    data_age_human = format_human_data_age(
        snap_dt, age_seconds, is_nse_open, now_ist, data_age_source=data_age_source
    )

    return {
        "status": overall_status,
        "healthy": (overall_status == "HEALTHY"),
        "health_tier": {
            "infra": infra_tier,
            "worker": worker_tier,
            "freshness": freshness_tier,
        },
        "health_reasons": {
            "infra": infra_reason,
            "worker": worker_reason,
            "freshness": freshness_reason,
        },
        "overall_reason": overall_reason,
        "data_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "data_age_human": data_age_human,
        "data_age_source": data_age_source,
        "last_bar_at": feed_status.get("last_bar_at") or (snap_dt.isoformat() if data_age_source == "LAST_BAR" and snap_dt else None),
        "bar_age_seconds": feed_status.get("bar_age_seconds") or (round(age_seconds, 1) if data_age_source == "LAST_BAR" and age_seconds is not None else None),
        "is_market_open": is_nse_open,
        "market_session_status": nse_reason,
    }
