"""Tests for the operator runtime control layer (kill switch + live knobs).

These are hermetic: the persisted `runtime_state` store is replaced with an
in-memory dict so no database is touched and no cross-test state leaks.
"""

from __future__ import annotations

import pytest

from agents.execution import runtime_controls as rc


@pytest.fixture
def store(monkeypatch):
    """Swap the runtime-state read/write helpers for an in-memory dict."""
    data: dict = {}

    def _read(key):
        return data.get(key), True

    def _write(key, value):
        # Round-trip through a shallow copy so tests cannot mutate stored state
        # by holding onto a reference.
        data[key] = dict(value) if isinstance(value, dict) else value

    monkeypatch.setattr(rc, "_read_state", _read)
    monkeypatch.setattr(rc, "_write_state", _write)
    rc.invalidate_runtime_control_cache()
    yield data
    rc.invalidate_runtime_control_cache()


@pytest.fixture
def broken_store(monkeypatch):
    """Simulate an unreadable state store."""

    def _read(key):
        return None, False

    def _write(key, value):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(rc, "_read_state", _read)
    monkeypatch.setattr(rc, "_write_state", _write)
    rc.invalidate_runtime_control_cache()
    yield
    rc.invalidate_runtime_control_cache()


# ── Knob validation ──────────────────────────────────────────────────────────
def test_validate_knob_rejects_unknown_name():
    value, err = rc.validate_knob("NOT_A_KNOB", 1)
    assert value is None
    assert "Unknown knob" in err


def test_validate_knob_enforces_bounds():
    value, err = rc.validate_knob("SKIP_CONFIDENCE_THRESHOLD", 150)
    assert value is None
    assert "must be between" in err

    value, err = rc.validate_knob("SKIP_CONFIDENCE_THRESHOLD", 35)
    assert err is None
    assert value == 35


def test_validate_knob_coerces_numeric_strings():
    value, err = rc.validate_knob("PAPER_TRADING_MAX_OPEN", "7")
    assert err is None
    assert value == 7
    assert isinstance(value, int)


def test_validate_knob_rejects_non_numeric():
    value, err = rc.validate_knob("DAILY_LOSS_LIMIT_PCT", "abc")
    assert value is None
    assert "must be a float" in err


def test_validate_overrides_rejects_whole_batch_on_one_bad_value():
    clean, errors = rc.validate_overrides(
        {"PAPER_TRADING_MAX_OPEN": 6, "DAILY_LOSS_LIMIT_PCT": 999}
    )
    assert clean == {}
    assert errors


def test_validate_overrides_rejects_empty_payload():
    clean, errors = rc.validate_overrides({})
    assert clean == {}
    assert errors == ["No knob values supplied"]


# ── Effective value precedence ───────────────────────────────────────────────
def test_effective_falls_back_to_settings_default(store):
    # No override present → settings module value wins.
    from config import settings

    assert rc.effective_int("PAPER_TRADING_MAX_OPEN", 5) == int(
        getattr(settings, "PAPER_TRADING_MAX_OPEN", 5)
    )


def test_effective_prefers_override(store):
    ok, message, _state = rc.set_overrides({"PAPER_TRADING_MAX_OPEN": 9})
    assert ok, message
    assert rc.effective_int("PAPER_TRADING_MAX_OPEN", 5) == 9


def test_reset_overrides_restores_defaults(store):
    rc.set_overrides({"PAPER_TRADING_MAX_OPEN": 9})
    assert rc.effective_int("PAPER_TRADING_MAX_OPEN", 5) == 9

    ok, message, _state = rc.reset_overrides()
    assert ok, message
    assert rc.effective_int("PAPER_TRADING_MAX_OPEN", 5) != 9
    assert rc.get_override_state()["values"] == {}


def test_set_overrides_is_idempotent_for_same_value(store):
    rc.set_overrides({"PAPER_TRADING_MAX_OPEN": 7})
    version_after_first = rc.get_override_state()["meta"]["version"]

    ok, message, _state = rc.set_overrides({"PAPER_TRADING_MAX_OPEN": 7})
    assert ok
    assert "No change" in message
    assert rc.get_override_state()["meta"]["version"] == version_after_first


def test_set_overrides_records_audit_entry(store):
    rc.set_overrides({"PAPER_TRADING_MAX_OPEN": 4}, operator="tester", reason="tighten")
    audit = rc.get_override_state()["meta"]["audit_log"]
    assert audit
    last = audit[-1]
    assert last["action"] == "KNOB_OVERRIDE_SET"
    assert last["operator"] == "tester"
    assert last["reason"] == "tighten"
    assert "PAPER_TRADING_MAX_OPEN" in last["changes"]


def test_knob_relation_blocks_risk_above_cap(store):
    ok, message, _state = rc.set_overrides(
        {"RISK_PER_TRADE_PCT": 5.0, "MAX_RISK_PER_TRADE_PCT": 2.0}
    )
    assert not ok
    assert "cannot exceed" in message


def test_live_flag_separates_restart_knobs(store):
    ok, message, _state = rc.set_overrides(
        {"PAPER_TRADING_MAX_OPEN": 6, "RISK_PER_TRADE_PCT": 0.5}
    )
    assert ok
    assert "live now" in message
    assert "after restart" in message


# ── Kill switch ──────────────────────────────────────────────────────────────
def test_kill_switch_defaults_to_not_halted(store):
    assert rc.is_trading_halted() == (False, "")


def test_kill_switch_engage_and_release(store):
    ok, message, state = rc.set_kill_switch(True, operator="tester", reason="drill")
    assert ok, message
    assert state["halted"] is True

    halted, reason = rc.is_trading_halted()
    assert halted is True
    assert "KILL_SWITCH_ENGAGED" in reason
    assert "drill" in reason

    ok, message, state = rc.set_kill_switch(False, operator="tester")
    assert ok, message
    assert state["halted"] is False
    assert rc.is_trading_halted() == (False, "")


def test_kill_switch_toggle_is_idempotent(store):
    rc.set_kill_switch(True)
    version = rc.get_kill_switch_state()["version"]

    ok, message, _state = rc.set_kill_switch(True)
    assert ok
    assert "already" in message
    assert rc.get_kill_switch_state()["version"] == version


def test_kill_switch_writes_audit_trail(store):
    rc.set_kill_switch(True, operator="ops", reason="manual halt")
    audit = rc.get_kill_switch_state()["audit_log"]
    assert audit[-1]["action"] == "KILL_SWITCH_ENGAGE"
    assert audit[-1]["operator"] == "ops"
    assert audit[-1]["reason"] == "manual halt"


# ── Failure policy: fail open, report unreadable ─────────────────────────────
def test_unreadable_store_fails_open(broken_store):
    halted, reason = rc.is_trading_halted()
    assert halted is False
    assert reason == ""


def test_unreadable_store_reports_state_readable_false(broken_store):
    status = rc.get_runtime_controls_status()
    assert status["kill_switch"]["state_readable"] is False
    assert status["override_meta"]["state_readable"] is False


def test_unreadable_store_falls_back_to_configuration(broken_store):
    from config import settings

    assert rc.effective_int("PAPER_TRADING_MAX_OPEN", 5) == int(
        getattr(settings, "PAPER_TRADING_MAX_OPEN", 5)
    )


def test_write_failure_is_reported_not_raised(broken_store):
    ok, message, _state = rc.set_kill_switch(True)
    assert ok is False
    assert "write failure" in message.lower()


# ── Status payload ───────────────────────────────────────────────────────────
def test_status_payload_lists_every_knob(store):
    status = rc.get_runtime_controls_status()
    names = {row["name"] for row in status["knobs"]}
    assert names == set(rc.KNOB_SPECS)
    for row in status["knobs"]:
        assert {"name", "label", "group", "min", "max", "live", "configured", "current"} <= set(row)


def test_snapshot_marks_overridden_knobs(store):
    rc.set_overrides({"PAPER_TRADING_MAX_OPEN": 3})
    row = next(r for r in rc.knob_snapshot() if r["name"] == "PAPER_TRADING_MAX_OPEN")
    assert row["overridden"] is True
    assert row["override"] == 3
    assert row["current"] == 3


# ── Boot-time projection onto settings ───────────────────────────────────────
def test_apply_overrides_to_settings_mutates_module(store, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "PAPER_TRADING_MAX_OPEN", 5, raising=False)
    rc.set_overrides({"PAPER_TRADING_MAX_OPEN": 11})

    applied = rc.apply_overrides_to_settings()

    assert "PAPER_TRADING_MAX_OPEN" in applied
    assert settings.PAPER_TRADING_MAX_OPEN == 11
