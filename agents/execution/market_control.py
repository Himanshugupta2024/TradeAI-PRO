"""
TradeAI-PRO — Section 6A Market Control & NSE Focus Mode Engine.
================================================================
Implements:
  1. Strict Custom NSE Activity Window (09:15:00 inclusive to 15:30:00 exclusive IST, Mon-Fri, non-holiday).
  2. Single Protected Crypto / Forex Switch (defaults to OFF, server-side password verification on ON).
  3. Authoritative persistence in Supabase PostgreSQL (market_control_state:v1) with audit logs.
  4. Transition safeguards on ON -> OFF: blocks disabling while open positions require monitoring.
  5. Zero-work policy: Gates background tasks, providers, scanners, inference, and WebSocket feeds when OFF.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import date, datetime, time as dtime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from agents.market_intelligence.nse_holidays import is_holiday, get_holiday_name

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# Exact NSE Activity Window specification:
# 09:15:00 inclusive to 15:30:00 exclusive
_NSE_WINDOW_START = dtime(9, 15, 0)
_NSE_WINDOW_END = dtime(15, 30, 0)

STATE_KEY = "market_control_state:v1"

# Rate limiting for password verification: max 5 failed attempts per 300 seconds
_FAILED_ATTEMPTS: Dict[str, List[float]] = {}
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_WINDOW_SEC = 300.0


def get_current_ist() -> datetime:
    """Return timezone-aware datetime in Asia/Kolkata."""
    return datetime.now(_IST)


def is_nse_activity_window_active(dt: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Inexpensive backend eligibility function for NSE equity activity window.
    Strictly enforces:
      - Asia/Kolkata timezone.
      - 09:15:00 inclusive to 15:30:00 exclusive.
      - Monday through Friday (weekday < 5).
      - Intersected with cached official NSE holiday calendar.
    Returns:
      (True, "SESSION_ACTIVE") or (False, reason)
    """
    now = dt or get_current_ist()
    if now.tzinfo is not None:
        now_ist = now.astimezone(_IST)
    else:
        # Guardrail 3: Naive datetimes (e.g. from Render UTC container) must be assumed UTC first, then converted to IST
        now_ist = now.replace(tzinfo=timezone.utc).astimezone(_IST)

    current_date = now_ist.date()

    # 0. Special Scheduled Session Check (e.g. Diwali Muhurat Trading)
    from agents.market_intelligence.nse_holidays import get_special_trading_session
    special = get_special_trading_session(current_date)
    if special:
        start_time, end_time, session_name = special
        now_time = now_ist.time()
        now_time_sec = dtime(now_time.hour, now_time.minute, now_time.second)
        if start_time <= now_time_sec < end_time:
            return True, f"SESSION_ACTIVE:{session_name}"
        if now_time_sec < start_time:
            return False, "NSE_PRE_MARKET"
        if now_time_sec >= end_time:
            return False, "NSE_POST_MARKET"

    # 1. Weekend Check (Monday=0 ... Sunday=6)
    if now_ist.weekday() >= 5:
        return False, "NSE_CLOSED_WEEKEND"

    # 2. Holiday Check
    if is_holiday(current_date):
        h_name = get_holiday_name(current_date)
        return False, f"NSE_MARKET_HOLIDAY:{h_name}" if h_name else "NSE_MARKET_HOLIDAY"

    # 3. Exact Time Window: 09:15:00 <= time < 15:30:00
    now_time = now_ist.time()
    # Normalize comparison ignoring microseconds for exact second boundaries
    now_time_sec = dtime(now_time.hour, now_time.minute, now_time.second)

    if now_time_sec < _NSE_WINDOW_START:
        return False, "NSE_PRE_MARKET"
    if now_time_sec >= _NSE_WINDOW_END:
        return False, "NSE_POST_MARKET"

    return True, "SESSION_ACTIVE"


def _clean_rate_limit(client_id: str) -> None:
    now = time.time()
    attempts = _FAILED_ATTEMPTS.get(client_id, [])
    valid_attempts = [t for t in attempts if now - t < _LOCKOUT_WINDOW_SEC]
    if valid_attempts:
        _FAILED_ATTEMPTS[client_id] = valid_attempts
    else:
        _FAILED_ATTEMPTS.pop(client_id, None)


def _check_rate_limit(client_id: str) -> bool:
    """Return True if client is within rate limits; False if locked out."""
    _clean_rate_limit(client_id)
    return len(_FAILED_ATTEMPTS.get(client_id, [])) < _MAX_FAILED_ATTEMPTS


def _record_failed_attempt(client_id: str) -> None:
    _clean_rate_limit(client_id)
    _FAILED_ATTEMPTS.setdefault(client_id, []).append(time.time())


def _hash_password(pwd: str, salt: str = "tradeai_salt_2026") -> str:
    """Compute SHA-256 HMAC for password verification."""
    return hmac.new(salt.encode("utf-8"), pwd.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_operator_password(password: str, client_id: str = "global") -> Tuple[bool, str]:
    """
    Verify operator password against server-side secret.
    Accepts password and verifies against:
      - MARKET_CONTROL_PASSWORD_HASH env var (HMAC-SHA256 hex or raw SHA-256 hex)
      - ADMIN_API_KEY env var (if configured as operator credential)
    Uses constant-time comparison to prevent timing side-channel attacks.
    """
    if not _check_rate_limit(client_id):
        logger.warning("Operator password verification locked out due to rate limit for: %s", client_id)
        return False, "Too many failed attempts. Temporary lockout active for 5 minutes."

    if not password or not isinstance(password, str):
        _record_failed_attempt(client_id)
        return False, "Invalid operator credentials."

    configured_hash = os.getenv("MARKET_CONTROL_PASSWORD_HASH", "").strip()
    admin_key = os.getenv("ADMIN_API_KEY", "").strip()

    if not configured_hash and not admin_key:
        _record_failed_attempt(client_id)
        logger.error("Market Control operator credentials not configured; refusing all operator logins")
        return False, "Operator credentials are not configured on this server."

    pwd_bytes = password.encode("utf-8")
    verified = False

    # Check 1: MARKET_CONTROL_PASSWORD_HASH (HMAC-SHA256 with salt)
    if configured_hash:
        candidate_hmac = _hash_password(password)
        candidate_sha256 = hashlib.sha256(pwd_bytes).hexdigest()
        if hmac.compare_digest(candidate_hmac, configured_hash) or hmac.compare_digest(candidate_sha256, configured_hash):
            verified = True

    # Check 2: ADMIN_API_KEY direct equality if configured
    if not verified and admin_key:
        if hmac.compare_digest(password, admin_key):
            verified = True

    if verified:
        _FAILED_ATTEMPTS.pop(client_id, None)
        return True, "Authorization successful."
    else:
        _record_failed_attempt(client_id)
        return False, "Invalid operator credentials."


def get_market_control_state() -> Dict[str, Any]:
    """
    Fetch the persistent market control state from authoritative database.
    Defaults to OFF on a new installation or database read error.
    """
    try:
        from db.runtime_state import get_runtime_state
        state = get_runtime_state(STATE_KEY)
        if isinstance(state, dict):
            return {
                "crypto_forex_enabled": bool(state.get("crypto_forex_enabled", False)),
                "version": int(state.get("version", 1)),
                "updated_at": str(state.get("updated_at", "")),
                "updated_by": str(state.get("updated_by", "system")),
                "audit_log": state.get("audit_log", [])[-20:],  # keep last 20
            }
    except Exception as exc:
        logger.error("Failed to load market control state from DB: %s (defaulting to OFF)", exc)

    # Safe default: OFF on new install or store error
    return {
        "crypto_forex_enabled": False,
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "system_default",
        "audit_log": [
            {
                "action": "DEFAULT_INIT",
                "state": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reason": "Default installation state: NSE Focus Mode active, Crypto/Forex OFF",
            }
        ],
    }


def is_crypto_schedule_active(dt: Optional[datetime] = None) -> bool:
    """
    Return True if current time is in the off-hours crypto window:
      - Weekdays: after 18:00 IST (6:00 PM) in evening through 08:45 AM IST next morning.
      - Weekends: Saturday and Sunday all day.
    """
    now = dt or get_current_ist()
    if now.tzinfo is not None:
        now_ist = now.astimezone(_IST)
    else:
        now_ist = now.replace(tzinfo=timezone.utc).astimezone(_IST)

    # Weekends (Saturday=5, Sunday=6)
    if now_ist.weekday() >= 5:
        return True

    # Weekdays: >= 18:00 (6:00 PM) or < 08:45 AM
    hour = now_ist.hour
    minute = now_ist.minute
    if hour >= 18:
        return True
    if hour < 8 or (hour == 8 and minute < 45):
        return True

    return False


def is_crypto_research_allowed() -> bool:
    """
    Return True if Crypto research & dashboard telemetry should be served.
    Allowed if explicitly enabled by operator, OR if in off-hours crypto schedule (after 6 PM IST / weekends).
    """
    return is_crypto_forex_enabled() or is_crypto_schedule_active()


def is_crypto_forex_enabled() -> bool:
    """Return True if Crypto/Forex processing is explicitly enabled by operator override."""
    return get_market_control_state().get("crypto_forex_enabled", False)


def check_active_crypto_forex_positions() -> List[Dict[str, Any]]:
    """
    Inspect open positions to identify any active Crypto or Forex trades requiring ongoing risk management.
    """
    active = []
    try:
        from db.runtime_state import get_runtime_state
        pts = get_runtime_state("paper_trading_state:v1")
        if isinstance(pts, dict):
            trades = pts.get("trades", [])
            for t in trades:
                if t.get("status") == "OPEN":
                    sym = str(t.get("symbol", ""))
                    asset_class = str(t.get("asset_class", "")).upper()
                    if "/" in sym or asset_class in ("CRYPTO", "CRYPTOCURRENCY", "FOREX", "FX"):
                        active.append(t)
    except Exception as exc:
        logger.warning("Could not inspect open crypto/forex positions from DB: %s", exc)
    return active


def set_market_control_state(
    enabled: bool,
    password: Optional[str] = None,
    client_id: str = "global",
    operator: str = "operator",
    reason: str = "",
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Update the persistent market control state with strict transition safeguards:
      - Turning ON requires valid operator password verification.
      - Turning OFF checks for open positions; blocks if active crypto/forex positions require management.
      - Persists state with version increment and non-secret audit log.
    Returns:
      (success: bool, message: str, state: dict)
    """
    current_state = get_market_control_state()
    current_enabled = current_state.get("crypto_forex_enabled", False)

    if current_enabled == enabled:
        status_text = "ON" if enabled else "OFF"
        return True, f"Crypto/Forex is already {status_text}.", current_state

    # 1. Verification for OFF -> ON
    if enabled:
        if not password:
            return False, "Password required to enable Crypto / Forex processing.", current_state
        ok, msg = verify_operator_password(password, client_id=client_id)
        if not ok:
            return False, msg, current_state

    # 2. Transition Guard for ON -> OFF
    if not enabled:
        open_managed = check_active_crypto_forex_positions()
        if open_managed:
            symbols = [p.get("symbol") for p in open_managed]
            msg = (
                f"Transition blocked: {len(open_managed)} open position(s) ({', '.join(symbols)}) "
                "require active risk monitoring. Close all open Crypto/Forex positions before switching OFF."
            )
            logger.warning(msg)
            return False, msg, current_state

    # 3. Apply atomic persistence
    new_version = current_state.get("version", 1) + 1
    now_iso = datetime.now(timezone.utc).isoformat()
    action_name = "OPERATOR_ENABLE" if enabled else "OPERATOR_DISABLE"
    default_reason = "Operator enabled Crypto/Forex" if enabled else "Operator returned to strict NSE Focus Mode"

    audit_entry = {
        "action": action_name,
        "state": enabled,
        "timestamp": now_iso,
        "operator": operator,
        "reason": reason or default_reason,
    }

    audit_log = current_state.get("audit_log", [])
    audit_log.append(audit_entry)

    updated_payload = {
        "crypto_forex_enabled": enabled,
        "version": new_version,
        "updated_at": now_iso,
        "updated_by": operator,
        "audit_log": audit_log[-20:],
    }

    try:
        from db.runtime_state import set_runtime_state
        set_runtime_state(STATE_KEY, updated_payload)
        logger.info("Market control state updated: crypto_forex_enabled=%s (v%d)", enabled, new_version)
        status_text = "ON (Override Active)" if enabled else "OFF (Strict NSE Focus Mode)"
        return True, f"Crypto/Forex successfully switched {status_text}.", updated_payload
    except Exception as exc:
        logger.error("Failed to persist updated market control state: %s", exc)
        return False, f"Database write failure: {exc}", current_state


def get_market_control_status() -> Dict[str, Any]:
    """Return consolidated telemetry for frontend cockpit, session banner, and worker gates."""
    state = get_market_control_state()
    cf_enabled = state.get("crypto_forex_enabled", False)
    nse_active, nse_reason = is_nse_activity_window_active()
    open_cf = check_active_crypto_forex_positions()

    if not cf_enabled:
        effective_mode = "NSE_FOCUS_EXCLUSIVE"
        mode_label = "NSE Focus Mode (Crypto & Forex Disabled)"
        badge_color = "blue"
    else:
        if nse_active:
            effective_mode = "NSE_PRIORITY_MULTI_ASSET"
            mode_label = "Multi-Asset Active (NSE Priority Window)"
            badge_color = "amber"
        else:
            effective_mode = "CRYPTO_FOREX_OFF_HOURS"
            mode_label = "Crypto/Forex Override (NSE Closed)"
            badge_color = "purple"

    return {
        "nse_focus_mode": True,
        "nse_window_active": nse_active,
        "nse_session_status": nse_reason,
        "crypto_forex_enabled": cf_enabled,
        "effective_mode": effective_mode,
        "mode_label": mode_label,
        "badge_color": badge_color,
        "open_crypto_forex_positions_count": len(open_cf),
        "open_crypto_forex_positions": [p.get("symbol") for p in open_cf],
        "state_version": state.get("version", 1),
        "last_updated": state.get("updated_at", ""),
        "last_updated_by": state.get("updated_by", ""),
        "recent_audit": state.get("audit_log", [])[-5:],
    }
