"""Unit tests for intraday risk helpers."""

from __future__ import annotations

from datetime import datetime

import pytest

from agents.intraday_engine import risk_manager


def test_calculate_sl_tp_long_short():
    sl_l, tp_l = risk_manager.calculate_sl_tp(entry=100.0, atr=2.0, direction="LONG")
    sl_s, tp_s = risk_manager.calculate_sl_tp(entry=100.0, atr=2.0, direction="SHORT")

    assert sl_l < 100.0 < tp_l
    assert tp_s < 100.0 < sl_s


def test_calculate_position_size_respects_single_trade_cap():
    # With 100k capital and 20% single-trade cap, max notional is 20k.
    # At entry 100, quantity cannot exceed 200 shares.
    qty = risk_manager.calculate_position_size(
        capital=100000.0,
        entry=100.0,
        atr=2.0,
        risk_pct=0.01,
        sl_atr_multiplier=1.5,
        max_single_trade_pct=0.20,
    )
    assert qty == 200


def test_can_take_trade_limits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(risk_manager.settings, "INTRADAY_MAX_OPEN_TRADES", 4, raising=False)
    monkeypatch.setattr(risk_manager.settings, "INTRADAY_MAX_DAILY_LOSS_PCT", 0.02, raising=False)

    ok1, reason1 = risk_manager.can_take_trade({"open_trades": 4, "daily_pnl_pct": 0.0})
    assert ok1 is False
    assert "Max open trades" in reason1

    ok2, reason2 = risk_manager.can_take_trade({"open_trades": 1, "daily_pnl_pct": -0.03})
    assert ok2 is False
    assert "Daily loss limit" in reason2

    ok3, reason3 = risk_manager.can_take_trade({"open_trades": 1, "daily_pnl_pct": -0.005})
    assert ok3 is True
    assert reason3 == "OK"


def test_should_square_off_now(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(risk_manager.settings, "INTRADAY_SQUARE_OFF_HOUR_IST", 15, raising=False)
    monkeypatch.setattr(risk_manager.settings, "INTRADAY_SQUARE_OFF_MINUTE_IST", 15, raising=False)

    before = datetime(2026, 4, 1, 15, 10, 0)
    after = datetime(2026, 4, 1, 15, 16, 0)

    assert risk_manager.should_square_off_now(before) is False
    assert risk_manager.should_square_off_now(after) is True
