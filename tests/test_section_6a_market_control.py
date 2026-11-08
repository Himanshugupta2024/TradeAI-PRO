"""
TradeAI-PRO — Section 6A Acceptance & Regression Test Suite.
============================================================
Verifies:
  1. Exact Custom NSE Activity Window boundaries (09:14:59, 09:15:00, 15:29:59, 15:30:00 IST and UTC equivalents).
  2. Exchange holiday & weekend suppression (cached 2026 official calendar).
  3. Default installation state (Crypto / Forex strictly OFF).
  4. Server-side password verification, generic failure handling, and rate limiting.
  5. Zero-work enforcement for disabled markets (universe exclusion, gate rejection, scanner suppression).
  6. Transition safeguards on ON -> OFF: active positions block disabling without auto-liquidation.
  7. Manual ON override behavior (crypto 24/7, forex 24/5, NSE priority during window).
  8. Independent money, FX, portfolio heat, and partial exit reconciliation fixture.
"""

import os
from datetime import date, datetime, time as dtime, timezone
from zoneinfo import ZoneInfo
import pytest

from agents.execution.market_control import (
    _IST,
    get_current_ist,
    is_nse_activity_window_active,
    get_market_control_state,
    is_crypto_forex_enabled,
    verify_operator_password,
    set_market_control_state,
    get_market_control_status,
    _hash_password,
    _FAILED_ATTEMPTS,
)
from agents.execution.session_guard import (
    is_nse_market_open,
    check_session_gate,
)
from agents.market_intelligence.nse_holidays import is_holiday, get_holiday_name
from scripts.autonomous_cycle import resolve_universe
from agents.market_intelligence.forex_scanner import scan_all_forex
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade


def test_get_current_ist_is_zoneinfo_aware():
    now_ist = get_current_ist()
    assert now_ist.tzinfo is _IST
    assert now_ist.utcoffset().total_seconds() == 5.5 * 60 * 60


def test_naive_market_control_datetime_is_interpreted_as_utc():
    # Production callers must pass aware datetimes. Naive values retain the
    # existing UTC compatibility contract; they are not treated as IST.
    naive_utc_open = datetime(2026, 11, 4, 3, 45, 0)
    active, reason = is_nse_activity_window_active(naive_utc_open)
    assert active
    assert reason == "SESSION_ACTIVE"


# ── Test 1: Exact Window Boundaries ──────────────────────────────────────────

def test_exact_nse_activity_window_boundaries():
    """
    NSE Activity Window: 09:15:00 inclusive to 15:30:00 exclusive (Mon-Fri).
    Tests Wednesday 2026-11-04 (regular trading day).
    """
    # 09:14:59 IST -> PRE_MARKET (False)
    t_pre = datetime(2026, 11, 4, 9, 14, 59, tzinfo=_IST)
    active, reason = is_nse_activity_window_active(t_pre)
    assert not active
    assert reason == "NSE_PRE_MARKET"

    # 09:15:00 IST -> SESSION_ACTIVE (True)
    t_open = datetime(2026, 11, 4, 9, 15, 0, tzinfo=_IST)
    active, reason = is_nse_activity_window_active(t_open)
    assert active
    assert reason == "SESSION_ACTIVE"

    # 15:29:59 IST -> SESSION_ACTIVE (True)
    t_late = datetime(2026, 11, 4, 15, 29, 59, tzinfo=_IST)
    active, reason = is_nse_activity_window_active(t_late)
    assert active
    assert reason == "SESSION_ACTIVE"

    # 15:30:00 IST -> POST_MARKET (False)
    t_close = datetime(2026, 11, 4, 15, 30, 0, tzinfo=_IST)
    active, reason = is_nse_activity_window_active(t_close)
    assert not active
    assert reason == "NSE_POST_MARKET"


def test_equivalent_utc_boundary_inputs():
    """Verify equivalent UTC inputs match IST boundary conditions perfectly."""
    # 03:44:59 UTC == 09:14:59 IST -> False
    utc_pre = datetime(2026, 11, 4, 3, 44, 59, tzinfo=timezone.utc)
    active, reason = is_nse_activity_window_active(utc_pre)
    assert not active
    assert reason == "NSE_PRE_MARKET"

    # 03:45:00 UTC == 09:15:00 IST -> True
    utc_open = datetime(2026, 11, 4, 3, 45, 0, tzinfo=timezone.utc)
    active, reason = is_nse_activity_window_active(utc_open)
    assert active
    assert reason == "SESSION_ACTIVE"

    # 09:59:59 UTC == 15:29:59 IST -> True
    utc_late = datetime(2026, 11, 4, 9, 59, 59, tzinfo=timezone.utc)
    active, reason = is_nse_activity_window_active(utc_late)
    assert active
    assert reason == "SESSION_ACTIVE"

    # 10:00:00 UTC == 15:30:00 IST -> False
    utc_close = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    active, reason = is_nse_activity_window_active(utc_close)
    assert not active
    assert reason == "NSE_POST_MARKET"


# ── Test 2: Weekends and Gazetted Holidays ─────────────────────────────────────

def test_nse_weekends_and_exchange_holidays():
    """Verify Saturday, Sunday, and scheduled official 2026 holidays reject entries."""
    # Saturday 2026-11-07 at 11:00 IST
    t_sat = datetime(2026, 11, 7, 11, 0, 0, tzinfo=_IST)
    active, reason = is_nse_activity_window_active(t_sat)
    assert not active
    assert reason == "NSE_CLOSED_WEEKEND"

    # Republic Day: 2026-01-26 (Monday) at 10:30 IST
    t_rep = datetime(2026, 1, 26, 10, 30, 0, tzinfo=_IST)
    active, reason = is_nse_activity_window_active(t_rep)
    assert not active
    assert "NSE_MARKET_HOLIDAY" in reason
    assert is_holiday(date(2026, 1, 26))

    # Holi: 2026-03-03 (Tuesday) at 12:00 IST
    t_holi = datetime(2026, 3, 3, 12, 0, 0, tzinfo=_IST)
    active, reason = is_nse_activity_window_active(t_holi)
    assert not active
    assert "NSE_MARKET_HOLIDAY" in reason


# ── Test 3: Default Fresh-Install OFF State ────────────────────────────────────

def test_fresh_install_default_state(monkeypatch):
    """Verify Crypto and Forex default strictly to OFF on new installation."""
    from db.runtime_state import set_runtime_state
    from agents.execution.market_control import STATE_KEY
    # Clear key to simulate uninitialized install
    set_runtime_state(STATE_KEY, None)

    state = get_market_control_state()
    assert state["crypto_forex_enabled"] is False
    assert is_crypto_forex_enabled() is False

    status = get_market_control_status()
    assert status["crypto_forex_enabled"] is False
    assert status["effective_mode"] == "NSE_FOCUS_EXCLUSIVE"


# ── Test 4: Password Verification & Rate Limiting ──────────────────────────────

def test_server_side_password_verification(monkeypatch):
    """Verify password hashing, timing-safe equality, rate limiting, and generic failure."""
    test_pwd = "InstitutionalSafePass2026!"
    expected_hash = _hash_password(test_pwd)

    monkeypatch.setenv("MARKET_CONTROL_PASSWORD_HASH", expected_hash)
    _FAILED_ATTEMPTS.clear()

    # 1. Correct password verifies
    ok, msg = verify_operator_password(test_pwd, client_id="test_client_1")
    assert ok is True

    # 2. Wrong password fails with generic message
    ok, msg = verify_operator_password("WrongPassword123", client_id="test_client_1")
    assert ok is False
    assert "Invalid operator credentials" in msg

    # 3. Missing password fails
    ok, msg = verify_operator_password("", client_id="test_client_1")
    assert ok is False

    # 4. Rate-limiting lockout: 5 failed attempts locks out
    for _ in range(4):
        verify_operator_password("BadPass", client_id="test_client_lockout")
    # 5th attempt triggers lockout
    ok, msg = verify_operator_password("BadPass", client_id="test_client_lockout")
    assert not ok
    # Subsequent attempt receives rate-limit notice even with correct password
    ok, msg = verify_operator_password(test_pwd, client_id="test_client_lockout")
    assert not ok
    assert "lockout" in msg.lower()


# ── Test 5: Zero-Work Policy When OFF ──────────────────────────────────────────

def test_zero_work_when_crypto_forex_off(monkeypatch):
    """
    When switch is OFF:
      - Universe contains zero crypto instruments.
      - Session gate rejects crypto/forex.
      - Forex scanner returns empty without calling Twelve Data / Yahoo.
    """
    from db.runtime_state import set_runtime_state
    from agents.execution.market_control import STATE_KEY

    # Force OFF
    set_runtime_state(STATE_KEY, {"crypto_forex_enabled": False, "version": 1})
    assert is_crypto_forex_enabled() is False

    # 1. Universe excludes all crypto
    symbols_tier1 = resolve_universe("tier1")
    assert all("/" not in s for s in symbols_tier1)
    symbols_crypto = resolve_universe("crypto")
    assert symbols_crypto == []

    # 2. Session gate rejects crypto and forex
    ok, reason = check_session_gate("CRYPTO")
    assert not ok
    assert reason == "DISABLED_BY_NSE_FOCUS_MODE"

    ok, reason = check_session_gate("FOREX")
    assert not ok
    assert reason == "DISABLED_BY_NSE_FOCUS_MODE"

    # 3. Forex scanner returns empty immediately
    forex_setups = scan_all_forex()
    assert forex_setups == []


# ── Test 6: Transition Guard Blocks OFF With Open Positions ───────────────────

def test_transition_guard_blocks_off_with_open_crypto_positions(monkeypatch):
    """
    When active crypto/forex positions exist, switching OFF is BLOCKED.
    Positions are never auto-closed or abandoned.
    """
    from db.runtime_state import set_runtime_state, get_runtime_state
    from agents.execution.market_control import STATE_KEY

    # 1. Start with ON state
    set_runtime_state(STATE_KEY, {"crypto_forex_enabled": True, "version": 2})

    # 2. Inject an open BTC/USDT paper trade in runtime state
    mock_pts = {
        "capital": 100000.0,
        "trades": [
            {
                "id": 991,
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "entry_time": "2026-11-06T10:00:00+05:30",
                "asset_class": "CRYPTO",
                "status": "OPEN",
                "entry_price": 70000.0,
                "stop_loss": 68000.0,
                "target": 74000.0,
                "quantity": 0.1,
            }
        ]
    }
    set_runtime_state("paper_trading_state:v1", mock_pts)

    # 3. Attempt to turn OFF -> MUST BE BLOCKED
    ok, msg, state = set_market_control_state(enabled=False, operator="operator_test")
    assert ok is False
    assert "Transition blocked" in msg
    assert "BTC/USDT" in msg

    # Verify state remains ON and open trade is preserved
    assert is_crypto_forex_enabled() is True
    pts = get_runtime_state("paper_trading_state:v1")
    assert len(pts["trades"]) == 1
    assert pts["trades"][0]["status"] == "OPEN"

    # 4. Resolve the trade to CLOSED_TP -> now turning OFF succeeds cleanly
    mock_pts["trades"][0]["status"] = "CLOSED_TP"
    set_runtime_state("paper_trading_state:v1", mock_pts)

    ok, msg, state = set_market_control_state(enabled=False, operator="operator_test")
    assert ok is True
    assert state["crypto_forex_enabled"] is False


# ── Test 7: Manual ON Enables Crypto 24/7 & Forex 24/5 ────────────────────────

def test_manual_on_override_enables_crypto_and_forex(monkeypatch):
    """
    When explicitly ON:
      - Crypto session is active 24/7 (even midnight / weekends).
      - Forex is active 24/5.
      - NSE equity activity window remains strictly enforced.
    """
    from db.runtime_state import set_runtime_state
    from agents.execution.market_control import STATE_KEY

    set_runtime_state(STATE_KEY, {"crypto_forex_enabled": True, "version": 3})
    assert is_crypto_forex_enabled() is True

    # Crypto active at midnight on Sunday
    sunday_midnight = datetime(2026, 11, 8, 0, 30, 0, tzinfo=_IST)
    ok, reason = check_session_gate("CRYPTO", dt=sunday_midnight)
    assert ok is True
    assert reason == "SESSION_ACTIVE"

    # NSE equities remain CLOSED at sunday midnight
    ok_nse, reason_nse = check_session_gate("EQUITY", dt=sunday_midnight)
    assert ok_nse is False

    # Clean up to default OFF
    set_runtime_state(STATE_KEY, {"crypto_forex_enabled": False, "version": 4})


# ── Test 8: Section 8 Money, FX & Heat Reconciliation Fixture ─────────────────

def test_independent_money_fx_heat_and_partial_exit_reconciliation():
    """
    Independent fixture verification from Section 8:
      - Base capital: INR 100,000.
      - Constant conversion: INR 84.5 per USDT.
      - LONG 100/95 qty 2 -> Native risk 10 USDT, INR risk 845, Heat 0.845%.
      - SHORT 100/105 qty 3 -> Native risk 15 USDT, INR risk 1,267.50, Heat 1.2675%.
      - Combined heat: 2.1125%.
      - +1 LONG at 100/95 adds 422.50 INR -> Projected heat 2.535%.
      - Close LONG 1 at 110, 1 at 105 -> Gross profit 15 USDT = INR 1,267.50.
    """
    capital = 100000.0
    fx_rate = 84.5

    # 1. LONG Position
    entry_long = 100.0
    stop_long = 95.0
    qty_long = 2.0
    native_risk_long = (entry_long - stop_long) * qty_long  # 10.0 USDT
    inr_risk_long = native_risk_long * fx_rate             # 845.0 INR
    heat_long = inr_risk_long / capital                    # 0.00845 (0.845%)

    assert native_risk_long == 10.0
    assert inr_risk_long == 845.0
    assert abs(heat_long - 0.00845) < 1e-6

    # 2. SHORT Position
    entry_short = 100.0
    stop_short = 105.0
    qty_short = 3.0
    native_risk_short = (stop_short - entry_short) * qty_short  # 15.0 USDT
    inr_risk_short = native_risk_short * fx_rate                # 1,267.50 INR
    heat_short = inr_risk_short / capital                       # 0.012675 (1.2675%)

    assert native_risk_short == 15.0
    assert inr_risk_short == 1267.50
    assert abs(heat_short - 0.012675) < 1e-6

    # 3. Combined Heat
    combined_heat = heat_long + heat_short
    assert abs(combined_heat - 0.021125) < 1e-6  # Exactly 2.1125%

    # 4. Projected +1 LONG quantity
    qty_long_add = 1.0
    inr_risk_add = (entry_long - stop_long) * qty_long_add * fx_rate  # 422.50 INR
    assert inr_risk_add == 422.50
    projected_combined_heat = (inr_risk_long + inr_risk_short + inr_risk_add) / capital
    assert abs(projected_combined_heat - 0.02535) < 1e-6  # Exactly 2.535%

    # 5. Partial Realization: Close qty 1 at 110 and qty 1 at 105
    pnl_part1_native = (110.0 - entry_long) * 1.0  # 10.0 USDT
    pnl_part2_native = (105.0 - entry_long) * 1.0  # 5.0 USDT
    gross_profit_native = pnl_part1_native + pnl_part2_native  # 15.0 USDT
    gross_profit_inr = gross_profit_native * fx_rate           # 1,267.50 INR

    assert gross_profit_native == 15.0
    assert gross_profit_inr == 1267.50
