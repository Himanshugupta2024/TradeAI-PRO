"""
EOD-style evaluation of OPEN SwingSetupOutcome rows against daily OHLCV.

Uses yFinance-backed `fetch_ohlcv` (5d/1d is too short — use 60d/1d) only for
symbols present in the dashboard NSE map. Unknown symbols are skipped.

Resolution order per bar (conservative): SL before targets for LONG/SHORT.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from config import settings
from db import swing_store

logger = logging.getLogger(__name__)


def _parse_emitted(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _max_hold_days() -> int:
    return max(5, int(getattr(settings, "SWING_OUTCOME_MAX_HOLD_DAYS", 18) or 18))


def _resolve_one(
    row: dict[str, Any],
    daily: pd.DataFrame,
) -> tuple[str, str | None, float | None, datetime | None, int | None, float | None] | None:
    """
    Returns tuple(status, hit_level, hit_price, hit_at, bars_to_hit, pnl_pct)
    or None if cannot evaluate yet (no post-emission bars, or window not elapsed).
    """
    direction = str(row.get("direction") or "LONG").upper()
    entry = float(row.get("entry_zone") or 0.0)
    sl = float(row.get("stop_loss") or 0.0)
    t1 = float(row.get("target_1") or 0.0)
    t2 = float(row.get("target_2") or 0.0)
    emitted = _parse_emitted(row.get("emitted_at"))
    if emitted is None or entry <= 0 or sl <= 0:
        return None

    if daily is None or daily.empty or "timestamp" not in daily.columns:
        return None

    df = daily.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is not None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(None)

    # Bars strictly after the calendar day of emission (avoid same-day lookahead).
    start_d = emitted.date() + timedelta(days=1)
    m = df["timestamp"].dt.date >= start_d
    fwd = df.loc[m].reset_index(drop=True)
    if fwd.empty:
        return None

    max_days = _max_hold_days()
    n = len(fwd)

    is_filled = (str(row.get("status") or "").upper() == "FILLED")
    fill_bar = 0

    for j in range(n):
        bar = fwd.iloc[j]
        ts = bar["timestamp"]
        hi = float(bar["high"])
        lo = float(bar["low"])
        hit_ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else datetime.utcnow()
        bar_n = j + 1

        if not is_filled:
            if direction == "LONG":
                # Target hit before entry touched -> cancelled (missed entry, not a win!)
                if hi >= t1 and lo > entry:
                    return "CANCELLED", "missed_entry", entry, hit_ts, bar_n, 0.0
                if lo <= entry:
                    is_filled = True
                    fill_bar = bar_n
            else:  # SHORT
                # Target hit before entry touched -> cancelled (missed entry, not a win!)
                if t1 > 0 and lo <= t1 and hi < entry:
                    return "CANCELLED", "missed_entry", entry, hit_ts, bar_n, 0.0
                if hi >= entry:
                    is_filled = True
                    fill_bar = bar_n

        # If trade is filled (either on earlier bar or this bar), evaluate exits
        if is_filled:
            if direction == "LONG":
                if lo <= sl:
                    pnl = (sl - entry) / entry * 100.0
                    return "SL_HIT", "sl", sl, hit_ts, bar_n, round(pnl, 2)
                if t2 > entry and hi >= t2:
                    pnl = (t2 - entry) / entry * 100.0
                    return "T2_HIT", "t2", t2, hit_ts, bar_n, round(pnl, 2)
                if t1 > entry and hi >= t1:
                    pnl = (t1 - entry) / entry * 100.0
                    return "T1_HIT", "t1", t1, hit_ts, bar_n, round(pnl, 2)
            else:  # SHORT
                if hi >= sl:
                    pnl = (entry - sl) / entry * 100.0
                    return "SL_HIT", "sl", sl, hit_ts, bar_n, round(pnl, 2)
                if t2 > 0 and t2 < entry and lo <= t2:
                    pnl = (entry - t2) / entry * 100.0
                    return "T2_HIT", "t2", t2, hit_ts, bar_n, round(pnl, 2)
                if t1 > 0 and t1 < entry and lo <= t1:
                    pnl = (entry - t1) / entry * 100.0
                    return "T1_HIT", "t1", t1, hit_ts, bar_n, round(pnl, 2)

    last_ts = fwd.iloc[-1]["timestamp"]
    last_d = last_ts.date() if hasattr(last_ts, "date") else date.today()
    if (last_d - emitted.date()).days >= max_days:
        if is_filled:
            cl = float(fwd.iloc[-1]["close"])
            pnl = (cl - entry) / entry * 100.0 if direction == "LONG" else (entry - cl) / entry * 100.0
            hit_ts = last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else datetime.utcnow()
            return "EXPIRED", "expired", cl, hit_ts, n, round(pnl, 2)
        else:
            return "CANCELLED", "expired_unfilled", entry, hit_ts, n, 0.0

    # If trade became filled during this run but hasn't closed, return FILLED
    if is_filled and str(row.get("status") or "").upper() != "FILLED":
        return "FILLED", "entry", entry, hit_ts, fill_bar, 0.0

    return None


def run_outcome_evaluation_once(
    *,
    max_outcomes: int | None = None,
) -> dict[str, Any]:
    """Evaluate up to N OPEN outcomes; returns counters for logging/metrics."""
    if not getattr(settings, "SWING_OUTCOME_EVAL_ENABLED", True):
        return {"skipped": True, "reason": "disabled"}

    cap = max_outcomes if max_outcomes is not None else int(
        getattr(settings, "SWING_OUTCOME_EVAL_BATCH", 120) or 120
    )
    open_rows = swing_store.fetch_open_outcomes(limit=max(1, cap))
    if not open_rows:
        return {"open": 0, "updated": 0, "skipped_fetch": 0, "errors": 0}

    from agents.market_intelligence.nse_fetcher import NSE_SYMBOLS, fetch_ohlcv

    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for r in open_rows:
        raw = str(r.get("symbol") or "").strip()
        if not raw:
            continue
        # DB may store "RELIANCE" or yFinance-style "RELIANCE.NS" — map keys are display names.
        base = raw.upper().removesuffix(".NS").strip()
        if not base or base not in NSE_SYMBOLS:
            continue
        by_symbol.setdefault(base, []).append(r)

    updated = 0
    errors = 0
    skipped_fetch = 0

    for sym, rows in by_symbol.items():
        try:
            daily = fetch_ohlcv(sym, "90d", "1d")
        except Exception as exc:
            logger.debug("Outcome eval fetch failed %s: %s", sym, exc)
            errors += len(rows)
            continue
        if daily is None or daily.empty:
            skipped_fetch += len(rows)
            continue

        for r in rows:
            try:
                res = _resolve_one(r, daily)
                if res is None:
                    continue
                status, hit_level, hit_price, hit_at, bars_to_hit, pnl_pct = res
                ok = swing_store.mark_outcome(
                    int(r["id"]),
                    status=status,
                    hit_level=hit_level,
                    hit_price=hit_price,
                    hit_at=hit_at,
                    bars_to_hit=bars_to_hit,
                    pnl_pct=pnl_pct,
                )
                if ok:
                    updated += 1
            except Exception as exc:
                logger.debug("Outcome eval row %s failed: %s", r.get("id"), exc)
                errors += 1

    return {
        "open_queried": len(open_rows),
        "symbols": len(by_symbol),
        "updated": updated,
        "skipped_fetch": skipped_fetch,
        "errors": errors,
    }
