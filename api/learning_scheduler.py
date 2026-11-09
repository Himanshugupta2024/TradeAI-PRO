"""Run the EOD self-learning review from the always-on API process.

Why this exists
---------------
The daily review (Bayesian weight adaptation) used to be reachable only from
inside ``scripts/autonomous_cycle.py``, which runs on a GitHub Actions schedule.
When GitHub drops those runs the cycle never executes, so the review never runs
either — even though closed trades are already sitting in the database. On
2026-11-09 the feed reported ``last_eod_review: null`` all day: the learning
loop had nothing to do with the market, it was simply never triggered.

This supervisor calls the same daemon method on a timer, so self-learning keeps
working independently of the scanner. It is deliberately cheap: a time-window
check every few minutes and one review per day, guarded by a database marker so
a restart cannot double-adapt the weights.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from config.logger import get_logger

log = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

TICK_SEC = 300
STATE_KEY = "eod_review:last_run:v1"

# Same window the daemon enforces internally (15:45-16:30 IST, weekdays).
WINDOW_START_MIN = 15 * 60 + 45
WINDOW_END_MIN = 16 * 60 + 30


def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def should_run_review(now_ist: datetime, last_review_date: Optional[str]) -> tuple[bool, str]:
    """Decide whether the EOD review should run right now.

    Pure function so the window/day rules stay testable without a database or
    clock dependency.
    """
    if now_ist.weekday() >= 5:
        return False, "weekend"
    minutes = now_ist.hour * 60 + now_ist.minute
    if minutes < WINDOW_START_MIN:
        return False, "before_window"
    if minutes > WINDOW_END_MIN:
        return False, "after_window"
    today = now_ist.date().isoformat()
    if last_review_date == today:
        return False, "already_ran_today"
    return True, "in_window"


def _read_state() -> dict:
    try:
        from db.runtime_state import get_runtime_state

        state = get_runtime_state(STATE_KEY)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        from db.runtime_state import set_runtime_state

        set_runtime_state(STATE_KEY, state)
    except Exception as exc:
        log.debug("[eod-review] state persist skipped: %s", exc)


def _run_review() -> Optional[dict]:
    """Execute the daemon review synchronously (call via asyncio.to_thread)."""
    from agents.execution.autonomous_daemon import get_autonomous_daemon

    return get_autonomous_daemon().check_daily_eod_review()


async def run_review_scheduler_loop() -> None:
    """Check the review window every few minutes for the life of the process."""
    await asyncio.sleep(45)  # let startup hydration settle
    while True:
        try:
            if not _flag("EOD_REVIEW_SCHEDULER_ENABLED", True):
                await asyncio.sleep(TICK_SEC)
                continue

            now_ist = datetime.now(_IST)
            state = await asyncio.to_thread(_read_state)
            should_run, reason = should_run_review(now_ist, state.get("last_review_date"))

            if should_run:
                review: Any = await asyncio.to_thread(_run_review)
                if isinstance(review, dict) and review:
                    state.update(
                        {
                            "last_review_date": now_ist.date().isoformat(),
                            "last_review_at": datetime.now(timezone.utc).isoformat(),
                            "last_review": {
                                "trades": review.get("today_trade_count"),
                                "win_rate": review.get("today_win_rate"),
                                "pnl_pct": review.get("today_total_pnl_pct"),
                                "best_signal_type": review.get("best_signal_type"),
                            },
                        }
                    )
                    await asyncio.to_thread(_write_state, state)
                    log.info(
                        "[eod-review] self-learning review completed: trades=%s win_rate=%s",
                        review.get("today_trade_count"),
                        review.get("today_win_rate"),
                    )
                else:
                    state["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
                    state["last_attempt_reason"] = "no_review_returned"
                    await asyncio.to_thread(_write_state, state)
            # Outside the window there is nothing to persist: a write every tick
            # would just churn the runtime_state table.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[eod-review] supervisor tick failed: %s", exc)

        await asyncio.sleep(TICK_SEC)
