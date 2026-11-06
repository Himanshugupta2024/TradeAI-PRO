"""
Data-honesty helpers.

Pure functions used by the API layer so that a response never presents stale or
invalid data as if it were live and tradeable. Kept dependency-free so the
behaviour can be unit-tested without FastAPI, SQLAlchemy or a database.

Rules encoded here:

* A signal is stale when its own timestamp is older than the instrument's
  freshness budget (30 min for NSE, 60 min for crypto pairs). Age is computed
  against the timestamp, not against when the cache entry was created.
* A setup needs three distinct, positive levels in the correct order for its
  direction. ``entry == stop_loss == target`` is not a setup.
* A paper trade whose quote is an entry fallback is *unpriced*; its P&L is a
  placeholder, not a mark.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

IST = timezone(timedelta(hours=5, minutes=30))

NSE_STALE_MINUTES = 30.0
CRYPTO_STALE_MINUTES = 60.0

LONG_ALIASES = {"LONG", "BUY"}
SHORT_ALIASES = {"SHORT", "SELL"}


def _as_datetime(value: Any) -> Optional[datetime]:
    """Parse timestamps defensively. Naive values are assumed to be IST."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value


def signal_age_minutes(timestamp: Any, now: Any = None) -> Optional[float]:
    """Minutes between ``timestamp`` and ``now`` (default: current UTC)."""
    stamp = _as_datetime(timestamp)
    if stamp is None:
        return None
    reference = _as_datetime(now) or datetime.now(timezone.utc)
    return max((reference - stamp).total_seconds() / 60.0, 0.0)


def stale_threshold_minutes(symbol: str) -> float:
    return CRYPTO_STALE_MINUTES if "/" in str(symbol) else NSE_STALE_MINUTES


def apply_signal_freshness(signal: dict, now: Any = None) -> dict:
    """Return a copy of ``signal`` with honest freshness fields."""
    out = dict(signal)
    age = signal_age_minutes(out.get("timestamp"), now=now)
    threshold = stale_threshold_minutes(str(out.get("symbol", "")))

    if age is None:
        out["stale"] = True
        out["stale_minutes"] = None
        out["freshness"] = "unknown"
    else:
        out["stale"] = bool(age > threshold)
        out["stale_minutes"] = round(age, 1)
        out["freshness"] = "stale" if out["stale"] else "fresh"
    return out


def _positive_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def validate_setup_levels(
    entry: Any,
    stop_loss: Any,
    target: Any,
    direction: str = "LONG",
) -> tuple[bool, str]:
    """Check that a setup has usable risk geometry."""
    entry_n = _positive_number(entry)
    stop_n = _positive_number(stop_loss)
    target_n = _positive_number(target)
    if entry_n is None or stop_n is None or target_n is None:
        return False, "missing_or_invalid_levels"

    if len({round(entry_n, 6), round(stop_n, 6), round(target_n, 6)}) < 3:
        return False, "degenerate_levels"

    side = str(direction).upper()
    if side in LONG_ALIASES:
        if not (stop_n < entry_n < target_n):
            return False, "inconsistent_levels"
    elif side in SHORT_ALIASES:
        if not (target_n < entry_n < stop_n):
            return False, "inconsistent_levels"
    else:
        return False, "unknown_direction"

    risk = abs(entry_n - stop_n)
    reward = abs(target_n - entry_n)
    if risk <= 0 or reward <= 0:
        return False, "invalid_rr"
    return True, ""


def filter_valid_setups(setups: Iterable[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Split setups into (valid, [(rejected, reason), ...])."""
    valid: list[dict] = []
    rejected: list[tuple[dict, str]] = []
    for setup in setups:
        if not isinstance(setup, dict):
            continue
        ok, reason = validate_setup_levels(
            setup.get("entry"),
            setup.get("stop_loss"),
            setup.get("target"),
            setup.get("signal", "LONG"),
        )
        if ok:
            valid.append(setup)
        else:
            rejected.append((setup, reason))
    return valid, rejected


def mark_trade_pricing(trade: dict) -> dict:
    """Tag a paper trade as priced or unpriced without changing its numbers."""
    out = dict(trade)
    fallback = bool(out.get("is_entry_fallback")) or str(
        out.get("quote_source", "")
    ).upper() == "ENTRY_FALLBACK"
    out["priced"] = not fallback
    out["pnl_status"] = "UNPRICED" if fallback else "MARKED"
    return out


def disabled_scan_payload(
    kind: str,
    reason: str,
    extra: Optional[dict] = None,
) -> dict:
    """Explicit response for a scan that is disabled or has no data."""
    payload = {
        "status": "disabled",
        "reason": reason,
        "scan_kind": kind,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if kind == "morning_scan":
        payload.update({"scan_time": None, "count": 0, "results": []})
    elif kind == "screener_opportunities":
        payload.update({"opportunities": []})
    if extra:
        payload.update(extra)
    return payload


def empty_scan_payload(kind: str, reason: str, extra: Optional[dict] = None) -> dict:
    """Scan ran but produced nothing — distinct from disabled."""
    payload = disabled_scan_payload(kind, reason, extra)
    payload["status"] = "empty"
    return payload
