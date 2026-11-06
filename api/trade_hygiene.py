"""
Paper-trade hygiene: zombie detection and bulk-close selection.

A "zombie" is an open paper position that should already have been closed by
the engine's own rules — an intraday equity trade that survived past its
session, or a swing/crypto trade open far beyond its intended horizon. These
positions usually mean the monitoring cycle did not run; they should be
visible and closable, not silently marked to market forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

IST = timezone(timedelta(hours=5, minutes=30))

SWING_MAX_AGE_DAYS = 15
CRYPTO_MAX_AGE_DAYS = 7


def _get(trade: Any, key: str, default: Any = None) -> Any:
    if isinstance(trade, dict):
        return trade.get(key, default)
    return getattr(trade, key, default)


def _parse_ts(value: Any):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value


def _is_crypto(symbol: str) -> bool:
    upper = str(symbol or "").upper()
    return "/" in upper or upper.endswith("USDT")


def _is_swing(trade: Any) -> bool:
    return "SWING" in str(_get(trade, "signal_type", "") or "").upper()


def is_zombie(trade: Any, now: datetime | None = None) -> tuple[bool, str]:
    """Return (is_zombie, reason)."""
    reference = (now or datetime.now(timezone.utc)).astimezone(IST)
    stamp = _parse_ts(_get(trade, "entry_time") or _get(trade, "timestamp"))
    if stamp is None:
        return False, ""

    entered = stamp.astimezone(IST)
    age_hours = (reference - entered).total_seconds() / 3600.0
    symbol = str(_get(trade, "symbol", "") or "")

    if _is_crypto(symbol):
        if age_hours > CRYPTO_MAX_AGE_DAYS * 24:
            return True, "crypto_older_than_7d"
        return False, ""

    if _is_swing(trade):
        if age_hours > SWING_MAX_AGE_DAYS * 24:
            return True, "swing_older_than_15d"
        return False, ""

    # Intraday equity must be squared off the same session.
    if entered.date() < reference.date():
        return True, "intraday_missed_eod"
    return False, ""


def select_bulk_targets(
    trades: Iterable[Any],
    mode: str = "intraday",
    now: datetime | None = None,
    ids: Iterable[Any] | None = None,
) -> list[Any]:
    """Select open trades for a bulk close.

    Modes: ``intraday`` (non-swing equity), ``zombie``, ``all``, ``ids``.
    """
    mode = str(mode or "intraday").lower()
    id_set = {str(x) for x in ids} if ids else set()
    targets: list[Any] = []

    for trade in trades or []:
        symbol = str(_get(trade, "symbol", "") or "")

        if mode == "ids":
            if str(_get(trade, "id", "")) in id_set:
                targets.append(trade)
            continue

        if mode == "all":
            targets.append(trade)
            continue

        if mode == "zombie":
            zombie, _ = is_zombie(trade, now=now)
            if zombie:
                targets.append(trade)
            continue

        # Default: intraday equity only — crypto and swing positions are held.
        if _is_crypto(symbol) or _is_swing(trade):
            continue
        targets.append(trade)

    return targets
