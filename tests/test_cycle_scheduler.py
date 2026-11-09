"""Tests for the market-hours cycle dispatcher (no network, no clock dependency)."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.cycle_scheduler import (  # noqa: E402
    CYCLE_INTERVAL_SEC,
    dispatch_cycle,
    evaluate_tick,
)

NOW = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)  # 09:30 IST, session live


def _tick(**kwargs):
    base = dict(now=NOW, market_open=True, token_present=True, last_dispatch_at=None)
    base.update(kwargs)
    return evaluate_tick(**base)


def test_dispatches_when_market_open_and_never_dispatched():
    decision = _tick()
    assert decision["action"] == "dispatch"


def test_skips_outside_market_hours():
    decision = _tick(market_open=False, last_dispatch_at=None)
    assert decision["action"] == "skip"
    assert decision["reason"] == "market_closed"


def test_skips_without_github_token_and_explains_why():
    decision = _tick(token_present=False)
    assert decision["action"] == "skip"
    assert decision["reason"] == "no_github_token"
    assert "GITHUB_TOKEN" in decision["detail"]


def test_respects_cooldown_window():
    last = (NOW - timedelta(minutes=5)).isoformat()
    decision = _tick(last_dispatch_at=last)
    assert decision["action"] == "skip"
    assert decision["reason"] == "cooldown"
    assert 0 < decision["next_dispatch_in_sec"] <= CYCLE_INTERVAL_SEC


def test_dispatches_once_cooldown_elapsed():
    last = (NOW - timedelta(seconds=CYCLE_INTERVAL_SEC + 1)).isoformat()
    assert _tick(last_dispatch_at=last)["action"] == "dispatch"


def test_tolerates_garbage_last_dispatch_value():
    assert _tick(last_dispatch_at="not-a-timestamp")["action"] == "dispatch"


def test_can_be_disabled_by_operator():
    decision = _tick(enabled=False)
    assert decision["action"] == "skip"
    assert decision["reason"] == "scheduler_disabled"


async def test_dispatch_without_token_is_a_noop(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    result = await dispatch_cycle()
    assert result["ok"] is False
    assert result["error"] == "no_github_token"
