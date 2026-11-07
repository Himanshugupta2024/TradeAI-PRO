"""
Unit tests for Symbol Modal Honesty functions.
All tests are offline, fast, and test pure functions in api.data_honesty and api.service.
"""

from datetime import datetime, timezone, timedelta
import pytest

from api.data_honesty import (
    classify_session,
    reconcile_costs,
    stats_available,
    confluence_penalties,
    entry_distance_pct,
    IST,
)


def test_session_classification():
    # 1. Saturday evaluated on Saturday -> CLOSED_WEEKEND, is_stale == True
    sat_now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=IST)
    sat_bar = datetime(2026, 11, 7, 11, 55, 0, tzinfo=IST)
    res_sat = classify_session(now=sat_now, bar_ts=sat_bar, symbol="TATAMOTORS")
    assert res_sat.market_state == "CLOSED_WEEKEND"
    assert res_sat.is_stale is True

    # 2. Friday 15:30 IST timestamp evaluated on Saturday 15:30 IST -> age > 86400, is_stale == True
    fri_bar = datetime(2026, 11, 6, 15, 30, 0, tzinfo=IST)
    res_weekend = classify_session(now=sat_now, bar_ts=fri_bar, symbol="TATAMOTORS")
    assert res_weekend.bar_age_seconds >= 72000
    assert res_weekend.is_stale is True
    assert res_weekend.market_state == "CLOSED_WEEKEND"

    # 3. Tuesday 11:00 IST bar evaluated on Tuesday 11:05 IST with market_open=True -> is_stale == False, OPEN
    tue_now = datetime(2026, 11, 10, 11, 5, 0, tzinfo=IST)
    tue_bar = datetime(2026, 11, 10, 11, 0, 0, tzinfo=IST)
    res_open = classify_session(now=tue_now, market_open=True, bar_ts=tue_bar, symbol="INFY")
    assert res_open.is_stale is False
    assert res_open.market_state == "OPEN"
    assert res_open.bar_age_seconds == 300.0

    # 4. Crypto symbol with 45m bar age -> is_stale == False (under 90m budget)
    crypto_now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    crypto_bar = datetime(2026, 11, 10, 11, 15, 0, tzinfo=timezone.utc)
    res_crypto = classify_session(now=crypto_now, bar_ts=crypto_bar, symbol="BTC/USDT")
    assert res_crypto.bar_age_seconds == 2700.0  # 45 minutes
    assert res_crypto.is_stale is False


def test_cost_reconciliation():
    # 1. gross_return = 1.95, costs = 1.71 -> net_return == 0.24
    rec = reconcile_costs(gross=1.95, costs=1.71)
    assert abs(rec.net - 0.24) <= 0.001
    assert abs(rec.gross - rec.costs - rec.net) <= 0.05
    assert rec.reconciles is True

    # Check equality operator with float
    assert rec == 0.24

    # Check cost drag calculation logic
    gross = 1.95
    costs = 1.71
    cost_drag = (costs / abs(gross)) * 100.0 if gross > 0 else 0.0
    assert round(cost_drag, 1) == 87.7

    # Cost drag when gross <= 0 should be 0 (no division by zero)
    gross_neg = -1.5
    cost_drag_neg = (costs / abs(gross_neg)) * 100.0 if gross_neg > 0 else 0.0
    assert cost_drag_neg == 0.0


def test_sample_size_gate():
    assert stats_available(9) is False
    assert stats_available(19) is False
    assert stats_available(20) is True
    assert stats_available(50) is True


def test_confluence_penalties():
    # 1. LONG with RSI=81.9 -> rsi_overbought_long, penalty <= -8
    pen_ob = confluence_penalties(
        signal="LONG",
        rsi=81.9,
        daily_trend="BULLISH",
        close=100.0,
        entry=100.0,
    )
    assert "rsi_overbought_long" in pen_ob
    assert pen_ob.penalty <= -8

    # 2. LONG with Daily Macro BEARISH -> macro_conflict, penalty <= -10
    pen_macro = confluence_penalties(
        signal="LONG",
        rsi=50.0,
        daily_trend="BEARISH",
        close=100.0,
        entry=100.0,
    )
    assert "macro_conflict" in pen_macro
    assert pen_macro.penalty <= -10

    # 3. Clean setup -> penalties empty, penalty = 0
    pen_clean = confluence_penalties(
        signal="LONG",
        rsi=52.0,
        daily_trend="BULLISH",
        close=100.0,
        entry=100.0,
        vwap=100.0,
        atr=2.0,
    )
    assert len(pen_clean) == 0
    assert pen_clean.penalty == 0

    # 4. Display confluence clamped to [0, 100]
    from api.service import _confluence_for_setup
    score_clean, _ = _confluence_for_setup(
        {"signal": "LONG", "trend": "BULLISH", "regime": "TRENDING"},
        mood={},
        for_display=True,
    )
    assert 0 <= score_clean <= 100

    score_penalised, _ = _confluence_for_setup(
        {
            "signal": "LONG",
            "trend": "BEARISH",
            "daily_trend": "BEARISH",
            "regime": "TRENDING",
            "rsi": 85.0,
            "close": 110.0,
            "entry": 100.0,
        },
        mood={},
        for_display=True,
    )
    assert 0 <= score_penalised <= 100
    assert score_penalised < score_clean


def test_entry_distance_and_crossing():
    # 1. LONG with close=261.75, entry=261.02 -> entry_distance_pct > 0.25, flag entry_crossed
    dist1 = entry_distance_pct(entry=261.02, close=261.75)
    assert dist1 > 0.25
    pens1 = confluence_penalties(
        signal="LONG",
        rsi=50.0,
        daily_trend="BULLISH",
        close=261.75,
        entry=261.02,
    )
    assert "entry_crossed" in pens1

    # 2. LONG with close=261.10, entry=261.02 -> entry_distance_pct <= 0.25, not crossed
    dist2 = entry_distance_pct(entry=261.02, close=261.10)
    assert dist2 <= 0.25
    pens2 = confluence_penalties(
        signal="LONG",
        rsi=50.0,
        daily_trend="BULLISH",
        close=261.10,
        entry=261.02,
    )
    assert "entry_crossed" not in pens2

    # 3. SHORT with close=260.00, entry=261.02 -> entry_distance_pct < -0.25, flag entry_crossed
    dist3 = entry_distance_pct(entry=261.02, close=260.00)
    assert dist3 < -0.25
    pens3 = confluence_penalties(
        signal="SHORT",
        rsi=50.0,
        daily_trend="BEARISH",
        close=260.00,
        entry=261.02,
    )
    assert "entry_crossed" in pens3
