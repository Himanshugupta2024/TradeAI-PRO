"""Reliable market-hours dispatcher for the autonomous paper cycle.

Why this exists
---------------
The cycle used to be scheduled only by GitHub Actions ``schedule`` cron. On a
low-traffic public repository GitHub routinely delays or drops those runs (we
observed ~4 runs/day instead of the ~20 the cron asked for, and zero runs for
the first four hours of a live session). When that happens the whole product
looks broken: quotes freeze, P&L goes stale, signal lists empty out.

This module runs inside the always-on API process and dispatches the GitHub
Actions workflow on a fixed cadence while the NSE session is live. It only ever
makes one small outbound HTTP POST per interval — the heavy cycle still runs on
a GitHub runner, never inside the 512 MB Render process.

If no ``GITHUB_TOKEN`` is configured the scheduler is inert and reports
``no_github_token`` instead of silently doing nothing.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Optional

from config.logger import get_logger

log = get_logger(__name__)

# Tick cadence for the supervisor loop (seconds). The dispatch interval itself
# is CYCLE_INTERVAL_SEC; the loop just needs to wake often enough to honour it.
SUPERVISOR_TICK_SEC = 60
CYCLE_INTERVAL_SEC = 20 * 60
STATE_KEY = "cycle_scheduler:v1"

_REPO_DEFAULT = "Himanshugupta2024/TradeAI-PRO"
_WORKFLOW_FILE = "autonomous_cycle.yml"


def _github_token() -> str:
    return (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()


def _github_repo() -> str:
    return (os.getenv("GITHUB_REPOSITORY") or _REPO_DEFAULT).strip()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def evaluate_tick(
    *,
    now: datetime,
    market_open: bool,
    token_present: bool,
    last_dispatch_at: Any = None,
    enabled: bool = True,
    interval_sec: int = CYCLE_INTERVAL_SEC,
) -> dict:
    """Decide whether to dispatch a cycle right now.

    Pure function so the cadence rules stay unit-testable without network,
    database, or clock dependencies.
    """
    if not enabled:
        return {"action": "skip", "reason": "scheduler_disabled"}
    if not market_open:
        return {"action": "skip", "reason": "market_closed"}
    if not token_present:
        return {
            "action": "skip",
            "reason": "no_github_token",
            "detail": "Set GITHUB_TOKEN on the API service so cycles dispatch to GitHub Actions.",
        }

    last = _parse_iso(last_dispatch_at)
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_sec = (now - last).total_seconds()
        if age_sec < interval_sec:
            return {
                "action": "skip",
                "reason": "cooldown",
                "next_dispatch_in_sec": int(interval_sec - max(age_sec, 0)),
            }

    return {"action": "dispatch", "reason": "market_open_interval_elapsed"}


LOCAL_CYCLE_REFUSED_MESSAGE = (
    "Autonomous cycle cannot run inside the API process (it OOM-kills the "
    "512 MB web service). Set GITHUB_TOKEN so the cycle dispatches to GitHub "
    "Actions, or set ALLOW_LOCAL_AUTONOMOUS_CYCLE=true to force it."
)


def local_cycle_allowed(env: Optional[dict] = None) -> tuple[bool, str]:
    """May the API process execute a cycle inline instead of dispatching it?

    In lightweight (free-tier) mode the answer is no unless an operator opts in:
    the cycle imports the ML stack and the kernel OOM-kills the web service,
    taking every open panel down with it. Returns (allowed, refusal_message).
    """
    source = env if env is not None else os.environ

    def _flag(name: str, default: str = "false") -> bool:
        return str(source.get(name, default)).strip().lower() in ("1", "true", "yes", "on")

    lightweight = _flag("LIGHTWEIGHT_API_MODE") or not _flag("BACKGROUND_REFRESH_ENABLED", "true")
    if lightweight and not _flag("ALLOW_LOCAL_AUTONOMOUS_CYCLE"):
        return False, LOCAL_CYCLE_REFUSED_MESSAGE
    return True, ""


async def dispatch_cycle(*, universe: str = "tier1") -> dict:
    """POST a workflow_dispatch to the autonomous cycle workflow."""
    token = _github_token()
    repo = _github_repo()
    if not token:
        return {"ok": False, "error": "no_github_token"}

    import httpx

    url = f"https://api.github.com/repos/{repo}/actions/workflows/{_WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "TradeAI-PRO-cycle-scheduler",
    }
    body = {
        "ref": "main",
        "inputs": {"universe": universe, "force_scan": "false", "dry_run": "false"},
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(url, headers=headers, json=body)
        if res.status_code in (201, 204):
            return {"ok": True, "status": res.status_code, "repo": repo}
        return {
            "ok": False,
            "status": res.status_code,
            "error": (res.text or "")[:300],
        }
    except Exception as exc:  # network/HTTP failure must never kill the loop
        return {"ok": False, "error": str(exc)}


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
        log.debug("[cycle-scheduler] state persist skipped: %s", exc)


async def run_scheduler_loop() -> None:
    """Supervise dispatch cadence for as long as the API process lives."""
    # Let startup hydration settle before touching the network.
    await asyncio.sleep(30)
    while True:
        try:
            from agents.execution.session_guard import is_nse_market_open
            from zoneinfo import ZoneInfo

            ist = ZoneInfo("Asia/Kolkata")
            now = datetime.now(timezone.utc)
            market_open, _reason = is_nse_market_open(datetime.now(ist))
            state = await asyncio.to_thread(_read_state)
            token_present = bool(_github_token())

            decision = evaluate_tick(
                now=now,
                market_open=market_open,
                token_present=token_present,
                last_dispatch_at=state.get("last_dispatch_at"),
                enabled=os.getenv("CYCLE_SCHEDULER_ENABLED", "true").strip().lower()
                in ("1", "true", "yes", "on"),
            )

            state.update(
                {
                    "last_check_at": now.isoformat(),
                    "market_open": market_open,
                    "token_present": token_present,
                    "last_decision": decision,
                }
            )

            if decision["action"] == "dispatch":
                result = await dispatch_cycle()
                state["last_dispatch_result"] = result
                if result.get("ok"):
                    state["last_dispatch_at"] = now.isoformat()
                    state["dispatches_total"] = int(state.get("dispatches_total") or 0) + 1
                    log.info("[cycle-scheduler] dispatched autonomous cycle on %s", _github_repo())
                else:
                    state["last_dispatch_error"] = result.get("error") or result.get("status")
                    log.warning("[cycle-scheduler] dispatch failed: %s", state["last_dispatch_error"])

            await asyncio.to_thread(_write_state, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[cycle-scheduler] supervisor tick failed: %s", exc)

        await asyncio.sleep(SUPERVISOR_TICK_SEC)


async def get_scheduler_state() -> dict:
    """Read-only snapshot for status endpoints and the dashboard."""
    state = await asyncio.to_thread(_read_state)
    state["token_present"] = bool(_github_token())
    state["repo"] = _github_repo()
    state["interval_sec"] = CYCLE_INTERVAL_SEC
    return state
