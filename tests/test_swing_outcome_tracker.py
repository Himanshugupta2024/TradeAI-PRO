"""Unit tests for db.swing_outcome_tracker._resolve_one (no live yFinance)."""

from __future__ import annotations

import pandas as pd

from db.swing_outcome_tracker import _resolve_one


def _daily(ts_start: str, rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    ts = pd.date_range(ts_start, periods=len(rows), freq="D")
    o, h, l, c = zip(*rows)
    return pd.DataFrame(
        {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": [1e6] * len(rows)}
    )


def test_long_stop_hit_first_bar():
    row = {
        "direction": "LONG",
        "entry_zone": 100.0,
        "stop_loss": 99.0,
        "target_1": 101.0,
        "target_2": 105.0,
        "emitted_at": "2025-01-01T10:00:00",
    }
    df = _daily("2025-01-01", [(100, 100.5, 99.5, 99.8), (100, 100.2, 98.5, 99.0)])
    res = _resolve_one(row, df)
    assert res is not None
    status, level, price, _, bars, _pnl = res
    assert status == "SL_HIT"
    assert level == "sl"
    assert bars == 1


def test_long_target1_before_sl():
    row = {
        "direction": "LONG",
        "entry_zone": 100.0,
        "stop_loss": 98.0,
        "target_1": 101.0,
        "target_2": 105.0,
        "emitted_at": "2025-01-05T10:00:00",
    }
    df = _daily(
        "2025-01-05",
        [
            (100, 100.5, 99.5, 100.1),
            (100, 101.5, 99.8, 101.2),
        ],
    )
    res = _resolve_one(row, df)
    assert res is not None
    assert res[0] == "T1_HIT"


def test_expired_after_max_hold(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "SWING_OUTCOME_MAX_HOLD_DAYS", 3)
    row = {
        "direction": "LONG",
        "entry_zone": 100.0,
        "stop_loss": 90.0,
        "target_1": 200.0,
        "target_2": 300.0,
        "emitted_at": "2025-01-01T10:00:00",
    }
    df = _daily("2025-01-02", [(100, 100.2, 99.9, 100.1)] * 5)
    res = _resolve_one(row, df)
    assert res is not None
    assert res[0] == "EXPIRED"


def test_target_hit_before_entry_is_cancelled_missed_entry():
    # Price runs straight to target without dipping to entry zone
    row = {
        "direction": "LONG",
        "entry_zone": 100.0,
        "stop_loss": 95.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "emitted_at": "2025-01-01T10:00:00",
        "status": "OPEN",
    }
    # Bar 1 (2025-01-02): Low is 102.0 (> 100.0 entry), High is 112.0 (>= 110.0 T1)
    df = _daily("2025-01-02", [(103.0, 112.0, 102.0, 111.0)])
    res = _resolve_one(row, df)
    assert res is not None
    status, level, price, _, bars, pnl = res
    assert status == "CANCELLED"
    assert level == "missed_entry"
    assert pnl == 0.0


def test_entry_filled_then_target_hit():
    row = {
        "direction": "LONG",
        "entry_zone": 100.0,
        "stop_loss": 95.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "emitted_at": "2025-01-01T10:00:00",
        "status": "OPEN",
    }
    # Bar 1: Low dips to 99.0 (fills entry at 100.0), High is 104.0 (no target)
    df_day1 = _daily("2025-01-02", [(101.0, 104.0, 99.0, 102.0)])
    res1 = _resolve_one(row, df_day1)
    assert res1 is not None
    assert res1[0] == "FILLED"
    assert res1[1] == "entry"

    # Bar 2: High reaches 111.0 (hits T1)
    df_day2 = _daily(
        "2025-01-02",
        [
            (101.0, 104.0, 99.0, 102.0),
            (102.0, 111.0, 101.0, 110.0),
        ],
    )
    # Simulate row now marked FILLED in DB
    row["status"] = "FILLED"
    res2 = _resolve_one(row, df_day2)
    assert res2 is not None
    assert res2[0] == "T1_HIT"
    assert res2[1] == "t1"
    assert res2[5] == 10.0  # (110 - 100) / 100 * 100


def test_expired_unfilled_returns_cancelled(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "SWING_OUTCOME_MAX_HOLD_DAYS", 3)
    row = {
        "direction": "LONG",
        "entry_zone": 100.0,
        "stop_loss": 90.0,
        "target_1": 200.0,
        "target_2": 300.0,
        "emitted_at": "2025-01-01T10:00:00",
        "status": "OPEN",
    }
    # Price stays between 101 and 105 for 5 days — never fills entry 100
    df = _daily("2025-01-02", [(102.0, 105.0, 101.0, 103.0)] * 5)
    res = _resolve_one(row, df)
    assert res is not None
    assert res[0] == "CANCELLED"
    assert res[1] == "expired_unfilled"
    assert res[5] == 0.0


def test_short_target_hit_before_entry_is_cancelled():
    row = {
        "direction": "SHORT",
        "entry_zone": 100.0,
        "stop_loss": 105.0,
        "target_1": 90.0,
        "target_2": 80.0,
        "emitted_at": "2025-01-01T10:00:00",
        "status": "OPEN",
    }
    # Bar 1: High 98 (never reaches entry 100), Low 89 (<= 90 T1)
    df = _daily("2025-01-02", [(97.0, 98.0, 89.0, 91.0)])
    res = _resolve_one(row, df)
    assert res is not None
    assert res[0] == "CANCELLED"
    assert res[1] == "missed_entry"
    assert res[5] == 0.0
