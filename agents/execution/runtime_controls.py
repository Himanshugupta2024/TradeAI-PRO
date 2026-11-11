"""
TradeAI-PRO — Operator Runtime Controls: Kill Switch + Live Knob Overrides.
===========================================================================
Two operator controls that do not require a redeploy:

  1. **Kill switch** — a single persisted flag that blocks *new* entries from
     the paper engine and the autonomous daemon. Open positions are left
     untouched so the operator can still flatten/manage them deliberately.

  2. **Knob overrides** — a whitelist of risk/signal settings whose values can
     be changed at runtime. Each knob declares explicit bounds and a `live`
     flag:

       - `live=True`  : every call site reads through `effective()`, so a new
                        value takes effect on the next decision (no restart).
       - `live=False` : the override is persisted and applied to the in-memory
                        `settings` module during the next process boot
                        (`apply_overrides_to_settings()`), so it becomes
                        effective only after a restart.

     The admin UI surfaces that distinction, so an operator never sees a
     "saved" knob and assumes it is already governing decisions.

Storage
-------
Both controls persist in the authoritative `runtime_state` table:

  - `trading_kill_switch:v1`
  - `runtime_knob_overrides:v1`

Writes are versioned and carry a bounded audit log, mirroring the Section 6A
`market_control` module. Reads go through a short-TTL in-process cache so the
hot decision paths do not hit the database on every call; writes invalidate
the cache immediately.

Failure policy
--------------
This module is an *operator tool*, not a safety interlock. The genuine
fail-closed interlocks already exist (circuit breaker, daily loss limit,
consecutive-loss pause) and stay untouched.

If the state store cannot be read, this module fails **open** — trading is
`not halted`, overrides fall back to `settings` — and reports
`state_readable: False` in the status payload so the operator sees a broken
control rather than a silent halt. A transient database error must never look
like a deliberate engine stop.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KILL_SWITCH_KEY = "trading_kill_switch:v1"
OVERRIDES_KEY = "runtime_knob_overrides:v1"

_MAX_AUDIT_ENTRIES = 50
_CACHE_TTL_SEC = 5.0

_MISSING = object()


# ─────────────────────────────────────────────────────────────────────────────
# Knob whitelist
# ─────────────────────────────────────────────────────────────────────────────
# `live=True` means the call site resolves through `effective()` per decision.
# Anything else only takes effect after a process restart.
KNOB_SPECS: Dict[str, Dict[str, Any]] = {
    # ── Position count ──────────────────────────────────────────────────────
    "PAPER_TRADING_MAX_OPEN": {
        "type": "int",
        "min": 1,
        "max": 50,
        "live": True,
        "group": "positions",
        "label": "Paper Max Open Positions",
        "help": "Hard cap on simultaneous open paper trades. Enforced per entry.",
    },
    "AUTONOMOUS_MAX_POSITIONS": {
        "type": "int",
        "min": 1,
        "max": 50,
        "live": True,
        "group": "positions",
        "label": "Autonomous Max Positions",
        "help": "Concurrent position cap for the autonomous daemon cycles.",
    },
    "INTRADAY_MAX_OPEN_TRADES": {
        "type": "int",
        "min": 1,
        "max": 50,
        "live": True,
        "group": "positions",
        "label": "Intraday Max Open Trades",
        "help": "Intraday risk-manager position cap used by signal sizing.",
    },
    # ── Risk gates ──────────────────────────────────────────────────────────
    "DAILY_LOSS_LIMIT_PCT": {
        "type": "float",
        "min": 0.1,
        "max": 20.0,
        "live": True,
        "group": "risk",
        "label": "Daily Loss Limit (%)",
        "help": "New entries blocked once the day's loss exceeds this % of capital.",
    },
    "MAX_CONSECUTIVE_LOSSES": {
        "type": "int",
        "min": 1,
        "max": 20,
        "live": True,
        "group": "risk",
        "label": "Max Consecutive Losses",
        "help": "Losses in a row before the engine pauses entries for the day.",
    },
    "RISK_PER_TRADE_PCT": {
        "type": "float",
        "min": 0.01,
        "max": 10.0,
        "live": False,
        "group": "risk",
        "label": "Risk Per Trade (%)",
        "help": "Base sizing risk. Applied on next restart.",
    },
    "MAX_RISK_PER_TRADE_PCT": {
        "type": "float",
        "min": 0.01,
        "max": 10.0,
        "live": False,
        "group": "risk",
        "label": "Max Risk Per Trade Cap (%)",
        "help": "Hard ceiling for dynamic sizing. Applied on next restart.",
    },
    "INTRADAY_MAX_DAILY_LOSS_PCT": {
        "type": "float",
        "min": 0.001,
        "max": 0.5,
        "live": False,
        "group": "risk",
        "label": "Intraday Daily Loss Fraction",
        "help": "Fractional form (0.02 = 2%) used by the intraday risk manager. Applied on next restart.",
    },
    # ── Signal gates ────────────────────────────────────────────────────────
    "SKIP_CONFIDENCE_THRESHOLD": {
        "type": "int",
        "min": 0,
        "max": 100,
        "live": True,
        "group": "signal",
        "label": "Skip Confidence Threshold",
        "help": "Signals below this AI confidence are graded SKIP. Live hard gate.",
    },
    "AI_CONFIDENCE_THRESHOLD": {
        "type": "int",
        "min": 0,
        "max": 100,
        "live": True,
        "group": "signal",
        "label": "AI Confidence Threshold",
        "help": "Fallback confidence floor for regimes without a per-regime override. Regime table values (TRENDING/RANGING/HIGH_VOLATILITY) still win.",
    },
    "AUTONOMOUS_MIN_QS_SCORE": {
        "type": "float",
        "min": 0.0,
        "max": 100.0,
        "live": True,
        "group": "signal",
        "label": "Autonomous Min Quality Score",
        "help": "Minimum quality score before the daemon considers an entry.",
    },
    "AUTONOMOUS_MIN_AI_CONFIDENCE": {
        "type": "float",
        "min": 0.0,
        "max": 100.0,
        "live": True,
        "group": "signal",
        "label": "Autonomous Min AI Confidence",
        "help": "Minimum AI confidence before the daemon considers an entry.",
    },
}

# Pairs where one value must not exceed the other.
_KNOB_RELATIONS: List[Tuple[str, str, str]] = [
    ("RISK_PER_TRADE_PCT", "MAX_RISK_PER_TRADE_PCT", "Risk per trade cannot exceed the max risk cap"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Cache plumbing
# ─────────────────────────────────────────────────────────────────────────────
_lock = threading.RLock()
_kill_cache: Dict[str, Any] = {"loaded": False, "ts": 0.0, "state": None, "readable": True}
_override_cache: Dict[str, Any] = {"loaded": False, "ts": 0.0, "values": {}, "meta": {}, "readable": True}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def invalidate_runtime_control_cache() -> None:
    """Drop all cached control state. Called after every successful write."""
    with _lock:
        _kill_cache.update({"loaded": False, "ts": 0.0, "state": None, "readable": True})
        _override_cache.update({"loaded": False, "ts": 0.0, "values": {}, "meta": {}, "readable": True})


def _cache_fresh(entry: Dict[str, Any]) -> bool:
    return bool(entry.get("loaded")) and (time.time() - float(entry.get("ts") or 0.0)) < _CACHE_TTL_SEC


def _read_state(key: str) -> Tuple[Any, bool]:
    """Read a runtime-state key. Returns (value, readable)."""
    try:
        from db.runtime_state import get_runtime_state

        return get_runtime_state(key), True
    except Exception as exc:
        logger.warning("Runtime control state unreadable for '%s': %s", key, exc)
        return None, False


def _write_state(key: str, value: Any) -> None:
    from db.runtime_state import set_runtime_state

    set_runtime_state(key, value)


# ─────────────────────────────────────────────────────────────────────────────
# Kill switch
# ─────────────────────────────────────────────────────────────────────────────
def _default_kill_state() -> Dict[str, Any]:
    return {
        "halted": False,
        "version": 1,
        "updated_at": "",
        "updated_by": "system_default",
        "reason": "",
        "audit_log": [],
    }


def get_kill_switch_state(force_refresh: bool = False) -> Dict[str, Any]:
    """Return the persisted kill-switch state (never raises)."""
    with _lock:
        if not force_refresh and _cache_fresh(_kill_cache):
            return dict(_kill_cache.get("state") or _default_kill_state())

    raw, readable = _read_state(KILL_SWITCH_KEY)
    state = _default_kill_state()
    if isinstance(raw, dict):
        state.update(
            {
                "halted": bool(raw.get("halted", False)),
                "version": int(raw.get("version", 1) or 1),
                "updated_at": str(raw.get("updated_at", "") or ""),
                "updated_by": str(raw.get("updated_by", "system_default") or "system_default"),
                "reason": str(raw.get("reason", "") or ""),
                "audit_log": list(raw.get("audit_log", []) or [])[-_MAX_AUDIT_ENTRIES:],
            }
        )

    with _lock:
        _kill_cache.update(
            {"loaded": True, "ts": time.time(), "state": state, "readable": readable}
        )
    return dict(state)


def is_trading_halted() -> Tuple[bool, str]:
    """
    Entry gate for new trades.

    Returns `(halted, reason)`. `reason` is a short machine-friendly token that
    callers append to their rejection message, e.g. `KILL_SWITCH_ENGAGED`.
    """
    state = get_kill_switch_state()
    if not bool(state.get("halted")):
        return False, ""
    reason = str(state.get("reason") or "").strip()
    detail = f"KILL_SWITCH_ENGAGED ({reason})" if reason else "KILL_SWITCH_ENGAGED"
    return True, detail


def set_kill_switch(
    halted: bool,
    operator: str = "operator",
    reason: str = "",
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Engage or release the kill switch.

    Returns `(success, message, state)`. Idempotent: re-applying the current
    value succeeds without incrementing the version.
    """
    current = get_kill_switch_state(force_refresh=True)
    if bool(current.get("halted")) == bool(halted):
        label = "ENGAGED" if halted else "RELEASED"
        return True, f"Kill switch is already {label}.", current

    now_iso = _now_iso()
    audit = list(current.get("audit_log") or [])
    audit.append(
        {
            "action": "KILL_SWITCH_ENGAGE" if halted else "KILL_SWITCH_RELEASE",
            "halted": bool(halted),
            "timestamp": now_iso,
            "operator": operator,
            "reason": reason or ("Operator halted new entries" if halted else "Operator resumed new entries"),
        }
    )

    payload = {
        "halted": bool(halted),
        "version": int(current.get("version", 1) or 1) + 1,
        "updated_at": now_iso,
        "updated_by": operator,
        "reason": reason or "",
        "audit_log": audit[-_MAX_AUDIT_ENTRIES:],
    }

    try:
        _write_state(KILL_SWITCH_KEY, payload)
    except Exception as exc:
        logger.error("Failed to persist kill switch state: %s", exc)
        return False, f"Database write failure: {exc}", current

    invalidate_runtime_control_cache()
    label = "ENGAGED — new entries blocked" if halted else "RELEASED — new entries allowed"
    logger.warning("Kill switch %s by %s (%s)", label, operator, reason or "no reason")
    return True, f"Kill switch {label}.", payload


# ─────────────────────────────────────────────────────────────────────────────
# Knob overrides
# ─────────────────────────────────────────────────────────────────────────────
def _default_override_state() -> Dict[str, Any]:
    return {
        "values": {},
        "version": 1,
        "updated_at": "",
        "updated_by": "system_default",
        "audit_log": [],
    }


def get_override_state(force_refresh: bool = False) -> Dict[str, Any]:
    """Return the persisted override state (never raises)."""
    with _lock:
        if not force_refresh and _cache_fresh(_override_cache):
            return {
                "values": dict(_override_cache.get("values") or {}),
                "meta": dict(_override_cache.get("meta") or {}),
                "readable": bool(_override_cache.get("readable", True)),
            }

    raw, readable = _read_state(OVERRIDES_KEY)
    state = _default_override_state()
    if isinstance(raw, dict):
        raw_values = raw.get("values")
        if isinstance(raw_values, dict):
            # Only whitelisted names survive a read, so a stale or hostile
            # payload can never introduce an unknown knob.
            state["values"] = {k: v for k, v in raw_values.items() if k in KNOB_SPECS}
        state["version"] = int(raw.get("version", 1) or 1)
        state["updated_at"] = str(raw.get("updated_at", "") or "")
        state["updated_by"] = str(raw.get("updated_by", "system_default") or "system_default")
        state["audit_log"] = list(raw.get("audit_log", []) or [])[-_MAX_AUDIT_ENTRIES:]

    with _lock:
        _override_cache.update(
            {
                "loaded": True,
                "ts": time.time(),
                "values": state["values"],
                "meta": state,
                "readable": readable,
            }
        )
    return {"values": dict(state["values"]), "meta": state, "readable": readable}


def _settings_default(name: str) -> Any:
    """Resolve the configured value: settings module, then env var, else None."""
    try:
        from config import settings

        value = getattr(settings, name, _MISSING)
        if value is not _MISSING:
            return value
    except Exception as exc:  # pragma: no cover - settings import is stable
        logger.debug("settings lookup failed for %s: %s", name, exc)

    env_value = os.getenv(name)
    if env_value is not None:
        return env_value
    return None


def _coerce(name: str, raw: Any) -> Any:
    """Coerce `raw` to the knob's declared type. Raises ValueError on bad input."""
    spec = KNOB_SPECS[name]
    if raw is None or isinstance(raw, bool):
        raise ValueError(f"{name} requires a numeric value")
    try:
        if spec["type"] == "int":
            if isinstance(raw, str) and not raw.strip():
                raise ValueError("empty")
            return int(float(raw))
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a {spec['type']}") from exc


def validate_knob(name: str, raw: Any) -> Tuple[Any, Optional[str]]:
    """Validate a single knob value. Returns `(coerced_value, error_or_None)`."""
    if name not in KNOB_SPECS:
        return None, f"Unknown knob '{name}'"
    spec = KNOB_SPECS[name]
    try:
        value = _coerce(name, raw)
    except ValueError as exc:
        return None, str(exc)

    if spec["type"] == "int":
        value = int(value)
    if value < spec["min"] or value > spec["max"]:
        return None, f"{name} must be between {spec['min']} and {spec['max']}"
    return value, None


def validate_overrides(values: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Validate a batch of overrides against the whitelist, bounds, and relations.

    Returns `(clean_values, errors)`. The batch is rejected as a whole when any
    error is present — a partially applied risk configuration is worse than a
    rejected one.
    """
    if not isinstance(values, dict) or not values:
        return {}, ["No knob values supplied"]

    clean: Dict[str, Any] = {}
    errors: List[str] = []
    for name, raw in values.items():
        coerced, err = validate_knob(str(name), raw)
        if err:
            errors.append(err)
        else:
            clean[str(name)] = coerced

    if errors:
        return {}, errors

    merged = dict(get_override_state().get("values") or {})
    merged.update(clean)

    for low_name, high_name, message in _KNOB_RELATIONS:
        low = merged.get(low_name, _settings_default(low_name))
        high = merged.get(high_name, _settings_default(high_name))
        try:
            if low is not None and high is not None and float(low) > float(high):
                errors.append(f"{message} ({low_name}={low} > {high_name}={high})")
        except (TypeError, ValueError):
            continue

    if errors:
        return {}, errors
    return clean, []


def set_overrides(
    values: Dict[str, Any],
    operator: str = "operator",
    reason: str = "",
) -> Tuple[bool, str, Dict[str, Any]]:
    """Persist a validated batch of knob overrides."""
    clean, errors = validate_overrides(values)
    if errors:
        return False, "; ".join(errors), {"values": get_override_state().get("values") or {}}

    current = get_override_state(force_refresh=True)
    merged = dict(current.get("values") or {})
    changed = {k: v for k, v in clean.items() if merged.get(k) != v}
    if not changed:
        return True, "No change — supplied values already active.", {"values": merged}

    merged.update(changed)
    now_iso = _now_iso()
    meta = current.get("meta") or {}
    audit = list(meta.get("audit_log") or [])
    audit.append(
        {
            "action": "KNOB_OVERRIDE_SET",
            "timestamp": now_iso,
            "operator": operator,
            "reason": reason or "Operator updated runtime knobs",
            "changes": {k: {"from": current["values"].get(k), "to": v} for k, v in changed.items()},
        }
    )

    payload = {
        "values": merged,
        "version": int(meta.get("version", 1) or 1) + 1,
        "updated_at": now_iso,
        "updated_by": operator,
        "audit_log": audit[-_MAX_AUDIT_ENTRIES:],
    }

    try:
        _write_state(OVERRIDES_KEY, payload)
    except Exception as exc:
        logger.error("Failed to persist knob overrides: %s", exc)
        return False, f"Database write failure: {exc}", {"values": current.get("values") or {}}

    invalidate_runtime_control_cache()
    live_names = sorted(k for k in changed if KNOB_SPECS[k].get("live"))
    deferred_names = sorted(k for k in changed if not KNOB_SPECS[k].get("live"))
    parts = []
    if live_names:
        parts.append(f"live now: {', '.join(live_names)}")
    if deferred_names:
        parts.append(f"after restart: {', '.join(deferred_names)}")
    message = "Overrides saved (" + "; ".join(parts) + ")." if parts else "Overrides saved."
    logger.info("Runtime knob overrides updated by %s: %s", operator, changed)
    return True, message, {"values": merged}


def reset_overrides(operator: str = "operator", reason: str = "") -> Tuple[bool, str, Dict[str, Any]]:
    """Clear every override, returning all knobs to their configured defaults."""
    current = get_override_state(force_refresh=True)
    if not current.get("values"):
        return True, "No overrides active.", {"values": {}}

    now_iso = _now_iso()
    meta = current.get("meta") or {}
    audit = list(meta.get("audit_log") or [])
    audit.append(
        {
            "action": "KNOB_OVERRIDE_RESET",
            "timestamp": now_iso,
            "operator": operator,
            "reason": reason or "Operator reset runtime knobs to defaults",
            "cleared": sorted(current["values"].keys()),
        }
    )

    payload = {
        "values": {},
        "version": int(meta.get("version", 1) or 1) + 1,
        "updated_at": now_iso,
        "updated_by": operator,
        "audit_log": audit[-_MAX_AUDIT_ENTRIES:],
    }

    try:
        _write_state(OVERRIDES_KEY, payload)
    except Exception as exc:
        logger.error("Failed to reset knob overrides: %s", exc)
        return False, f"Database write failure: {exc}", {"values": current.get("values") or {}}

    invalidate_runtime_control_cache()
    logger.info("Runtime knob overrides reset by %s", operator)
    return True, "All overrides cleared — configured defaults restored.", {"values": {}}


# ─────────────────────────────────────────────────────────────────────────────
# Effective value resolution (hot path)
# ─────────────────────────────────────────────────────────────────────────────
def effective(name: str, default: Any = None) -> Any:
    """
    Resolve the effective value for a knob: override → settings/env → default.

    Never raises; an unreadable store simply falls back to configuration.
    """
    overrides = get_override_state().get("values") or {}
    if name in overrides:
        try:
            return _coerce(name, overrides[name])
        except ValueError:
            logger.warning("Ignoring invalid stored override for %s: %r", name, overrides[name])

    configured = _settings_default(name)
    if configured is None:
        return default
    try:
        spec = KNOB_SPECS.get(name)
        if spec is None:
            return configured
        return _coerce(name, configured)
    except ValueError:
        logger.warning("Configured value for %s is not numeric: %r", name, configured)
        return default


def effective_int(name: str, default: int) -> int:
    try:
        return int(effective(name, default))
    except (TypeError, ValueError):
        return default


def effective_float(name: str, default: float) -> float:
    try:
        return float(effective(name, default))
    except (TypeError, ValueError):
        return default


def apply_overrides_to_settings() -> List[str]:
    """
    Project persisted overrides onto the in-memory `settings` module.

    Called once during application startup so that call sites which read
    `settings.X` directly (rather than through `effective()`) also honour the
    operator's values on the boot after they were saved.

    Returns the list of settings names that were applied.
    """
    applied: List[str] = []
    try:
        from config import settings
    except Exception as exc:  # pragma: no cover
        logger.warning("Cannot apply runtime overrides — settings import failed: %s", exc)
        return applied

    values = get_override_state(force_refresh=True).get("values") or {}
    for name, raw in values.items():
        spec = KNOB_SPECS.get(name)
        if spec is None:
            continue
        try:
            coerced = _coerce(name, raw)
        except ValueError:
            continue
        try:
            current = getattr(settings, name, _MISSING)
            setattr(settings, name, coerced)
            applied.append(name)
            if current is not _MISSING and current != coerced:
                logger.info("Runtime override applied at boot: %s %r -> %r", name, current, coerced)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to apply override for %s: %s", name, exc)

    if applied:
        logger.info("Runtime overrides projected onto settings: %s", ", ".join(sorted(applied)))
    return applied


# ─────────────────────────────────────────────────────────────────────────────
# Status payload for the admin console
# ─────────────────────────────────────────────────────────────────────────────
def knob_snapshot() -> List[Dict[str, Any]]:
    """Build the operator-facing view of every controllable knob."""
    override_state = get_override_state()
    overrides = override_state.get("values") or {}
    rows: List[Dict[str, Any]] = []

    for name, spec in KNOB_SPECS.items():
        configured_raw = _settings_default(name)
        try:
            configured = _coerce(name, configured_raw) if configured_raw is not None else None
        except ValueError:
            configured = None
        has_override = name in overrides
        try:
            current = _coerce(name, overrides[name]) if has_override else configured
        except ValueError:
            current = configured

        rows.append(
            {
                "name": name,
                "label": spec["label"],
                "help": spec["help"],
                "group": spec["group"],
                "type": spec["type"],
                "min": spec["min"],
                "max": spec["max"],
                "live": bool(spec["live"]),
                "configured": configured,
                "current": current,
                "override": overrides.get(name) if has_override else None,
                "overridden": has_override,
            }
        )

    return rows


def get_runtime_controls_status() -> Dict[str, Any]:
    """Consolidated payload for `GET /api/admin/runtime-controls`."""
    kill_state = get_kill_switch_state()
    override_state = get_override_state()
    meta = override_state.get("meta") or {}

    return {
        "kill_switch": {
            "halted": bool(kill_state.get("halted")),
            "reason": kill_state.get("reason", ""),
            "version": int(kill_state.get("version", 1) or 1),
            "updated_at": kill_state.get("updated_at", ""),
            "updated_by": kill_state.get("updated_by", ""),
            "recent_audit": list(kill_state.get("audit_log") or [])[-5:],
            "state_readable": bool(_kill_cache.get("readable", True)),
        },
        "knobs": knob_snapshot(),
        "override_meta": {
            "count": len(override_state.get("values") or {}),
            "version": int(meta.get("version", 1) or 1),
            "updated_at": meta.get("updated_at", ""),
            "updated_by": meta.get("updated_by", ""),
            "recent_audit": list(meta.get("audit_log") or [])[-5:],
            "state_readable": bool(override_state.get("readable", True)),
        },
    }
