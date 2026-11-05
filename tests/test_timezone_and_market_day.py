"""
Tests for Timezone Handling, Impossible Market Session Rejection,
and Full Market-Day Lifecycle Operations.
"""

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from agents.execution.session_guard import (
    _IST,
    get_current_ist_time,
    is_nse_market_open,
    check_session_gate,
    evaluate_data_freshness,
)


def test_utc_to_ist_mathematical_invariants():
    """Verify standard NSE operating windows in both UTC and IST."""
    # Market Open: 09:15 IST == 03:45 UTC
    ist_open = datetime(2026, 11, 4, 9, 15, 0, tzinfo=_IST)
    utc_open = ist_open.astimezone(timezone.utc)
    assert utc_open.hour == 3
    assert utc_open.minute == 45
    assert utc_open.second == 0

    # Market Close: 15:30 IST == 10:00 UTC
    ist_close = datetime(2026, 11, 4, 15, 30, 0, tzinfo=_IST)
    utc_close = ist_close.astimezone(timezone.utc)
    assert utc_close.hour == 10
    assert utc_close.minute == 0
    assert utc_close.second == 0

    # EOD Review: 15:45 IST == 10:15 UTC
    ist_eod = datetime(2026, 11, 4, 15, 45, 0, tzinfo=_IST)
    utc_eod = ist_eod.astimezone(timezone.utc)
    assert utc_eod.hour == 10
    assert utc_eod.minute == 15


def test_nse_equity_naive_timestamp_localized_to_ist():
    """
    NSE fetcher returns naive timestamps in IST (e.g. 10:00).
    Verify that evaluate_data_freshness interprets naive NSE bars as IST,
    avoiding the +5.5 hour skew bug.
    """
    now_ist = datetime.now(_IST)
    # Naive timestamp representing 5 minutes ago in IST
    naive_candle_time = (now_ist - timedelta(minutes=5)).replace(tzinfo=None)

    df = pd.DataFrame({
        "timestamp": [naive_candle_time],
        "close": [2500.0],
        "high": [2510.0],
        "low": [2490.0],
    })

    # When evaluated for NSE symbol, it must treat naive as IST
    status, age_sec = evaluate_data_freshness(df, "RELIANCE")
    # If it wrongly treated it as UTC, age_sec would be skewed by 19800s (5.5 hrs)
    assert age_sec < 600, f"Expected age < 600s, got {age_sec}s (skew detected!)"


def test_impossible_nse_session_times_rejected():
    """
    Verify that candle timestamps occurring outside valid NSE cash session
    (09:15 - 15:30 IST) are rejected as IMPOSSIBLE_SESSION_TIME or EOD.
    """
    today_ist = get_current_ist_time().date()

    # Case 1: Midnight candle (00:00 IST) -> Treated as daily/EOD bar
    df_midnight = pd.DataFrame({
        "timestamp": [datetime.combine(today_ist, datetime.min.time(), tzinfo=_IST)],
        "close": [100.0],
    })
    status, _ = evaluate_data_freshness(df_midnight, "INFY")
    assert status == "EOD"

    # Case 2: 04:30 IST (Pre-dawn impossible candle) -> IMPOSSIBLE_SESSION_TIME
    time_0430 = datetime(today_ist.year, today_ist.month, today_ist.day, 4, 30, 0, tzinfo=_IST)
    df_0430 = pd.DataFrame({"timestamp": [time_0430], "close": [100.0]})
    status, _ = evaluate_data_freshness(df_0430, "INFY")
    assert status == "IMPOSSIBLE_SESSION_TIME"

    # Case 3: 18:00 IST (Late evening impossible candle) -> IMPOSSIBLE_SESSION_TIME
    time_1800 = datetime(today_ist.year, today_ist.month, today_ist.day, 18, 0, 0, tzinfo=_IST)
    df_1800 = pd.DataFrame({"timestamp": [time_1800], "close": [100.0]})
    status, _ = evaluate_data_freshness(df_1800, "INFY")
    assert status == "IMPOSSIBLE_SESSION_TIME"

    # Case 4: Weekend candle (Saturday 11:00 IST) -> IMPOSSIBLE_SESSION_TIME
    saturday_time = datetime(2026, 11, 7, 11, 0, 0, tzinfo=_IST)
    df_weekend = pd.DataFrame({"timestamp": [saturday_time], "close": [100.0]})
    status, _ = evaluate_data_freshness(df_weekend, "INFY")
    assert status == "IMPOSSIBLE_SESSION_TIME"


def test_session_gate_boundary_conditions():
    """Verify precise NSE open and close minute boundaries."""
    # 09:14:59 IST -> Closed
    t_pre = datetime(2026, 11, 4, 9, 14, 59, tzinfo=_IST)
    is_open, reason = is_nse_market_open(t_pre)
    assert not is_open
    assert reason == "NSE_PRE_MARKET"

    # 09:15:00 IST -> Open
    t_open = datetime(2026, 11, 4, 9, 15, 0, tzinfo=_IST)
    is_open, reason = is_nse_market_open(t_open)
    assert is_open
    assert reason == "SESSION_ACTIVE"

    # 15:30:00 IST -> Open (closing boundary)
    t_close = datetime(2026, 11, 4, 15, 30, 0, tzinfo=_IST)
    is_open, reason = is_nse_market_open(t_close)
    assert is_open
    assert reason == "SESSION_ACTIVE"

    # 15:30:01 IST -> Closed
    t_post = datetime(2026, 11, 4, 15, 30, 1, tzinfo=_IST)
    is_open, reason = is_nse_market_open(t_post)
    assert not is_open
    assert reason == "NSE_POST_MARKET"


def test_eod_squareoff_equities_closed_crypto_exempt():
    """
    Verify that check_eod_squareoff at 15:15 IST squares off all open intraday equities
    while exempting crypto pairs.
    """
    from agents.execution.paper_trading import PaperTradingEngine
    import tempfile
    import os
    import uuid

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = f.name

    try:
        engine = PaperTradingEngine(log_file=tmp_path, initial_capital=100000.0, state_key=f"test_{uuid.uuid4().hex[:6]}")

        # Execute 1 equity and 1 crypto position
        t_equity = engine.execute(
            symbol="TCS",
            direction="LONG",
            entry=3500.0,
            sl=3450.0,
            tp=3600.0,
            signal_type="MOMENTUM_BREAKOUT",
            confidence=75.0,
        )
        assert t_equity is not None

        t_crypto = engine.execute(
            symbol="BTC/USDT",
            direction="LONG",
            entry=68000.0,
            sl=66000.0,
            tp=72000.0,
            signal_type="MOMENTUM_BREAKOUT",
            confidence=80.0,
        )
        assert t_crypto is not None
        assert len(engine.open_trades) == 2

        # 1. At 14:00 IST (Active session) -> No square-off
        t_1400 = datetime(2026, 11, 4, 14, 0, 0, tzinfo=_IST)
        closed_1400 = engine.check_eod_squareoff(prices={"TCS": {"close": 3520.0}}, dt=t_1400)
        assert len(closed_1400) == 0
        assert len(engine.open_trades) == 2

        # 2. At 15:15 IST (EOD threshold) -> TCS closed, BTC/USDT stays open
        t_1515 = datetime(2026, 11, 4, 15, 15, 0, tzinfo=_IST)
        closed_1515 = engine.check_eod_squareoff(prices={"TCS": {"close": 3540.0}}, dt=t_1515)
        assert len(closed_1515) == 1
        assert closed_1515[0].symbol == "TCS"
        assert closed_1515[0].status == "CLOSED_EOD_SQUAREOFF"
        assert closed_1515[0].exit_price == 3540.0

        # Remaining open trade is BTC/USDT
        assert len(engine.open_trades) == 1
        assert engine.open_trades[0].symbol == "BTC/USDT"

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_full_market_day_simulation_cycle():
    """
    Simulate key steps of an entire NSE market day lifecycle:
    09:15 Market Open -> Mid-day trade entry -> Risk gating -> 15:15 EOD square-off -> 15:45 Review.
    """
    from agents.execution.paper_trading import PaperTradingEngine
    from agents.execution.autonomous_daemon import AutonomousExecutionDaemon
    import tempfile
    import os
    import uuid

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = f.name

    try:
        engine = PaperTradingEngine(log_file=tmp_path, initial_capital=100000.0, state_key=f"test_{uuid.uuid4().hex[:6]}")
        daemon = AutonomousExecutionDaemon()
        daemon.enabled = True
        daemon.max_positions = 5

        # 09:15 IST: Open cycle with 2 candidate signals
        signals_0915 = [
            {
                "symbol": "RELIANCE",
                "direction": "LONG",
                "entry": 2500.0,
                "sl": 2470.0,
                "tp": 2560.0,
                "qs_score": 78.0,
                "ai_confidence": 72.0,
                "strategy": "MOMENTUM_BREAKOUT",
                "regime": "TRENDING_BULLISH",
                "rsi": 58.0,
                "atr": 25.0,
                "volume_above_avg": True,
            },
            {
                "symbol": "INFY",
                "direction": "LONG",
                "entry": 1600.0,
                "sl": 1580.0,
                "tp": 1640.0,
                "qs_score": 62.0,
                "ai_confidence": 60.0,
                "strategy": "MOMENTUM_BREAKOUT",
                "regime": "TRENDING_BULLISH",
                "rsi": 55.0,
                "atr": 15.0,
                "volume_above_avg": True,
            },
            # Sub-threshold signal: QS < 50
            {
                "symbol": "WIPRO",
                "direction": "LONG",
                "entry": 450.0,
                "sl": 440.0,
                "tp": 460.0,
                "qs_score": 42.0,
                "ai_confidence": 35.0,
                "strategy": "MOMENTUM_BREAKOUT",
                "regime": "RANGING",
                "rsi": 49.0,
                "atr": 5.0,
                "volume_above_avg": False,
            },
        ]

        executed = daemon.evaluate_signals(signals_0915, {}, engine)
        # RELIANCE and INFY should be executed; WIPRO rejected by risk gate (QS < 50, AI < 40)
        assert len(executed) == 2
        symbols_entered = {t.symbol for t in executed}
        assert symbols_entered == {"RELIANCE", "INFY"}
        assert len(engine.open_trades) == 2

        # Verify duplicate signal rejection on subsequent cycle (10:00 IST)
        # Trying to re-evaluate RELIANCE while open should be ignored
        executed_dup = daemon.evaluate_signals(signals_0915[:1], {}, engine)
        assert len(executed_dup) == 0
        assert len(engine.open_trades) == 2

        # 12:30 IST: RELIANCE hits target (TP = 2560.0)
        prices_1230 = {
            "RELIANCE": {"high": 2565.0, "low": 2490.0, "close": 2562.0},
            "INFY": {"high": 1610.0, "low": 1595.0, "close": 1605.0},
        }
        closed_trades = engine.update_prices(prices_1230)
        assert len(closed_trades) == 1
        assert closed_trades[0].symbol == "RELIANCE"
        assert closed_trades[0].status == "CLOSED_TP"
        assert closed_trades[0].pnl > 0

        # INFY remains open
        assert len(engine.open_trades) == 1
        assert engine.open_trades[0].symbol == "INFY"

        # 15:15 IST: EOD square-off closes remaining open equity (INFY)
        t_1515 = datetime(2026, 11, 4, 15, 15, 0, tzinfo=_IST)
        eod_closed = engine.check_eod_squareoff(prices={"INFY": {"close": 1615.0}}, dt=t_1515)
        assert len(eod_closed) == 1
        assert eod_closed[0].symbol == "INFY"
        assert eod_closed[0].status == "CLOSED_EOD_SQUAREOFF"
        assert len(engine.open_trades) == 0

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def test_utc_datetime_normalized_to_ist_session():
    """
    Assert that UTC datetime input is normalized to Asia/Kolkata:
    1. 07:23 AM UTC on Wednesday is 12:53 PM IST -> SESSION_ACTIVE (not NSE_PRE_MARKET).
    2. 14:00 PM UTC on Wednesday is 19:30 PM IST -> NSE_POST_MARKET (not NSE_PRE_MARKET).
    """
    from datetime import timezone
    # 07:23 UTC = 12:53 IST (Wednesday)
    t_midday_utc = datetime(2026, 11, 4, 7, 23, 0, tzinfo=timezone.utc)
    is_open, reason = is_nse_market_open(t_midday_utc)
    assert is_open is True
    assert reason == "SESSION_ACTIVE"

    # 14:00 UTC = 19:30 IST (7:30 PM IST, Wednesday)
    t_evening_utc = datetime(2026, 11, 4, 14, 0, 0, tzinfo=timezone.utc)
    is_open, reason = is_nse_market_open(t_evening_utc)
    assert is_open is False
    assert reason == "NSE_POST_MARKET"



def test_session_state_resolution_evening_post_market_and_midnight_pre_market():
    """
    Assert that:
    1. 19:23:12 IST (evening, Thursday) produces (False, 'NSE_POST_MARKET').
    2. 00:53:12 IST (midnight, Thursday) produces (False, 'NSE_PRE_MARKET').
    3. 19:23:12 UTC on Wednesday is 00:53:12 IST on Thursday -> (False, 'NSE_PRE_MARKET').
    4. Next session descriptors distinguish 'today at 09:15 IST' from 'tomorrow at 09:15 IST'
       and 'Monday at 09:15 IST', preventing any misleading 09:15 references in evening sessions.
    """
    from datetime import timezone
    from agents.execution.session_guard import get_next_nse_session

    # 1. 19:23:12 IST Thursday -> POST_MARKET
    t_evening_ist = datetime(2026, 11, 5, 19, 23, 12, tzinfo=_IST)
    is_open, reason = is_nse_market_open(t_evening_ist)
    assert is_open is False
    assert reason == "NSE_POST_MARKET"
    _, next_desc = get_next_nse_session(t_evening_ist)
    assert "tomorrow (2026-11-06) at 09:15 IST" in next_desc

    # 2. 00:53:12 IST Thursday -> PRE_MARKET
    t_midnight_ist = datetime(2026, 11, 5, 0, 53, 12, tzinfo=_IST)
    is_open, reason = is_nse_market_open(t_midnight_ist)
    assert is_open is False
    assert reason == "NSE_PRE_MARKET"
    _, next_desc = get_next_nse_session(t_midnight_ist)
    assert "today (2026-11-05) at 09:15 IST" in next_desc

    # 3. 19:23:12 UTC on Nov 4 = 00:53:12 IST on Nov 5 -> PRE_MARKET
    t_utc = datetime(2026, 11, 4, 19, 23, 12, tzinfo=timezone.utc)
    is_open, reason = is_nse_market_open(t_utc)
    assert is_open is False
    assert reason == "NSE_PRE_MARKET"
    _, next_desc = get_next_nse_session(t_utc)
    assert "today (2026-11-05) at 09:15 IST" in next_desc

    # 4. Friday evening -> next session is Monday
    t_friday_evening = datetime(2026, 11, 6, 18, 0, 0, tzinfo=_IST)
    _, next_desc_fri = get_next_nse_session(t_friday_evening)
    assert "Monday (2026-11-09) at 09:15 IST" in next_desc_fri
