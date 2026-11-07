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


# ── Symbol Modal Honesty Helpers ─────────────────────────────────────────────

class SessionClassification(dict):
    """Result of session classification with dict and property access."""
    @property
    def market_state(self) -> str:
        return self["market_state"]

    @property
    def is_stale(self) -> bool:
        return self["is_stale"]

    @property
    def bar_age_seconds(self) -> float:
        return self["bar_age_seconds"]

    @property
    def as_of(self) -> str:
        return self["as_of"]


def classify_session(
    now: Any = None,
    market_open: Optional[bool] = None,
    bar_ts: Any = None,
    symbol: str = "",
) -> SessionClassification:
    """
    Classify market session state and evaluate bar freshness.
    Market states: OPEN | PRE_OPEN | CLOSED | CLOSED_WEEKEND | EOD
    """
    now_dt = _as_datetime(now) or datetime.now(timezone.utc)
    now_ist = now_dt.astimezone(IST)

    bar_dt = _as_datetime(bar_ts)
    if bar_dt is None:
        bar_dt = now_dt
    bar_ist = bar_dt.astimezone(IST)

    # Calculate bar age relative to now
    bar_age_seconds = max((now_dt - bar_dt).total_seconds(), 0.0)

    # Determine instrument freshness threshold from valuation constants
    try:
        from api.valuation import _stale_threshold_seconds
        threshold = _stale_threshold_seconds(symbol, now=now_dt)
    except Exception:
        threshold = 90.0 * 60 if "/" in str(symbol) else (30.0 * 60 if market_open else 18.0 * 3600)

    # Market state determination
    is_weekend = (now_ist.weekday() >= 5) or (bar_ist.weekday() >= 5 and market_open is not True)
    if is_weekend:
        market_state = "CLOSED_WEEKEND"
        is_stale = True
    elif market_open is True:
        market_state = "OPEN"
        is_stale = bool(bar_age_seconds > threshold)
    elif market_open is False:
        market_state = "CLOSED"
        is_stale = bool(bar_age_seconds > threshold or bar_age_seconds > 30 * 60)
    else:
        # Evaluate by IST clock
        mins = now_ist.hour * 60 + now_ist.minute
        if 9 * 60 <= mins < 9 * 60 + 15:
            market_state = "PRE_OPEN"
            is_stale = bool(bar_age_seconds > threshold)
        elif 9 * 60 + 15 <= mins <= 15 * 60 + 30:
            market_state = "OPEN"
            is_stale = bool(bar_age_seconds > threshold)
        elif mins > 15 * 60 + 30:
            market_state = "EOD"
            is_stale = bool(bar_age_seconds > threshold)
        else:
            market_state = "CLOSED"
            is_stale = bool(bar_age_seconds > threshold)

    return SessionClassification({
        "as_of": bar_dt.isoformat(),
        "bar_age_seconds": round(bar_age_seconds, 1),
        "market_state": market_state,
        "is_stale": bool(is_stale),
    })


class CostReconciliation(dict):
    """Reconciled backtest cost metrics with comparison operators."""
    def __init__(self, gross: float, costs: float, net: Optional[float] = None):
        gross_f = round(float(gross), 4)
        costs_f = round(float(costs), 4)
        if net is None:
            net_f = round(gross_f - costs_f, 4)
            reconciles = True
        else:
            net_f = round(float(net), 4)
            reconciles = abs(gross_f - costs_f - net_f) <= 0.05

        super().__init__({
            "gross": gross_f,
            "costs": costs_f,
            "net": net_f,
            "reconciles": reconciles,
        })
        self.gross = gross_f
        self.costs = costs_f
        self.net = net_f
        self.reconciles = reconciles

    def __eq__(self, other):
        if isinstance(other, (int, float)):
            return abs(self.net - other) <= 0.05
        if isinstance(other, dict):
            return super().__eq__(other)
        return False


def reconcile_costs(gross: float, costs: float, net: Optional[float] = None) -> CostReconciliation:
    """
    Ensure gross - costs == net on the exact same capital basis.
    Acceptance: abs(gross - costs - net) <= 0.05.
    """
    return CostReconciliation(gross, costs, net)


def stats_available(trade_count: int, min_trades: int = 20) -> bool:
    """True only when trade sample size >= min_trades (default 20)."""
    return bool(trade_count >= min_trades)


def entry_distance_pct(entry: float, close: float) -> float:
    """
    Signed percentage distance between proposed limit entry and current close.
    Positive value means current price is above proposed entry.
    """
    e = float(entry)
    c = float(close)
    if e <= 0:
        return 0.0
    return round(((c - e) / e) * 100.0, 2)


class ConfluencePenaltyList(list):
    """List of penalty flags with total score penalty attribute."""
    def __init__(self, flags: list[str], penalty: int = 0):
        super().__init__(flags)
        self.penalty = penalty
        self.penalty_sum = penalty
        self.score_penalty = penalty


def confluence_penalties(
    signal: str,
    rsi: Optional[float],
    daily_trend: str,
    close: float,
    entry: float,
    vwap: Optional[float] = None,
    atr: Optional[float] = None,
) -> ConfluencePenaltyList:
    """
    Detect explicit technical and execution conflicts:
    - LONG and RSI >= 70 -> penalty -8, 'rsi_overbought_long'
    - SHORT and RSI <= 30 -> penalty -8, 'rsi_oversold_short'
    - Signal direction opposite to daily trend -> penalty -10, 'macro_conflict'
    - Price > 2x ATR from VWAP in trade direction -> penalty -5, 'vwap_stretched'
    - Limit entry crossed by >0.25% -> penalty -5, 'entry_crossed'
    """
    flags: list[str] = []
    penalty_sum = 0
    sig_upper = str(signal or "").upper()
    is_long = sig_upper in LONG_ALIASES
    is_short = sig_upper in SHORT_ALIASES

    # 1. RSI Overbought while LONG
    if is_long and rsi is not None and float(rsi) >= 70.0:
        flags.append("rsi_overbought_long")
        penalty_sum -= 8

    # 2. RSI Oversold while SHORT
    if is_short and rsi is not None and float(rsi) <= 30.0:
        flags.append("rsi_oversold_short")
        penalty_sum -= 8

    # 3. Macro conflict (opposite to daily trend)
    dt_upper = str(daily_trend or "").upper()
    if (is_long and "BEAR" in dt_upper) or (is_short and "BULL" in dt_upper):
        flags.append("macro_conflict")
        penalty_sum -= 10

    # 4. VWAP stretched (>2x ATR in trade direction)
    if vwap is not None and atr is not None:
        try:
            vwap_f = float(vwap)
            atr_f = float(atr)
            close_f = float(close)
            if atr_f > 0:
                if is_long and (close_f - vwap_f) > 2.0 * atr_f:
                    flags.append("vwap_stretched")
                    penalty_sum -= 5
                elif is_short and (vwap_f - close_f) > 2.0 * atr_f:
                    flags.append("vwap_stretched")
                    penalty_sum -= 5
        except (ValueError, TypeError):
            pass

    # 5. Limit entry already crossed by >0.25%
    try:
        entry_f = float(entry)
        close_f = float(close)
        if entry_f > 0:
            dist_pct = ((close_f - entry_f) / entry_f) * 100.0
            if (is_long and dist_pct > 0.25) or (is_short and dist_pct < -0.25):
                flags.append("entry_crossed")
                penalty_sum -= 5
    except (ValueError, TypeError):
        pass

    return ConfluencePenaltyList(flags, penalty_sum)
