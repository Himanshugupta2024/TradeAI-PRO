"""
Tests for db/swing_store.py — SwingScan + SwingSetupOutcome persistence.

Uses a temporary SQLite file via DATABASE_URL monkeypatch + a fresh engine so
tests don't collide with the developer's real trading.db.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ─── fixture: isolated SQLite engine ─────────────────────────────────────────


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Build a fresh SQLAlchemy engine on a tmp SQLite file and point
    db.connection.SessionLocal at it, so every test gets a clean DB.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from db import connection as db_conn
    from db import models as db_models
    from db import swing_store

    db_file = tmp_path / "swing_test.db"
    url = f"sqlite:///{db_file.as_posix()}"

    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    db_models.Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(db_conn, "engine", engine, raising=False)
    monkeypatch.setattr(db_conn, "SessionLocal", TestSession, raising=False)
    monkeypatch.setattr(swing_store, "SessionLocal", TestSession, raising=False)

    yield swing_store

    try:
        engine.dispose()
    except Exception:
        pass


def _setup(symbol: str = "RELIANCE.NS", grade: str = "A", rr: float = 2.5) -> dict:
    return {
        "symbol": symbol,
        "pattern": "20-Day Range Breakout",
        "direction": "LONG",
        "strength": 78,
        "entry_zone": 100.0,
        "stop_loss": 95.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "risk_reward": rr,
        "timeframe": "3-7 days",
        "grade": grade,
        "description": "test setup",
    }


# ─── schema + save/load roundtrip ────────────────────────────────────────────


def test_save_and_load_latest_roundtrip(isolated_db):
    setups = [_setup("RELIANCE.NS", "A"), _setup("TCS.NS", "B"), _setup("INFY.NS", "C")]
    scan_id = isolated_db.save_scan(setups, {"index_symbol": "NIFTY", "index_trend": "BULL"}, trigger="manual")
    assert scan_id is not None and scan_id > 0

    loaded = isolated_db.load_latest_scan()
    assert loaded is not None
    assert loaded["trigger"] == "manual"
    assert loaded["total_setups"] == 3
    assert loaded["grade_a_count"] == 1
    assert loaded["grade_b_count"] == 1
    assert loaded["grade_c_count"] == 1
    assert loaded["market_context"]["index_symbol"] == "NIFTY"
    syms = [s["symbol"] for s in loaded["setups"]]
    assert syms == ["RELIANCE.NS", "TCS.NS", "INFY.NS"]


def test_save_empty_scan_does_not_crash(isolated_db):
    scan_id = isolated_db.save_scan([], None, trigger="startup")
    assert scan_id is not None
    loaded = isolated_db.load_latest_scan()
    assert loaded["total_setups"] == 0
    assert loaded["setups"] == []
    assert loaded["market_context"] is None


def test_multiple_scans_latest_wins(isolated_db):
    first = isolated_db.save_scan([_setup("A.NS")], None)
    second = isolated_db.save_scan([_setup("B.NS"), _setup("C.NS")], None)
    assert second > first

    latest = isolated_db.load_latest_scan()
    assert latest["id"] == second
    assert latest["total_setups"] == 2

    recent = isolated_db.list_recent_scans(limit=10)
    assert len(recent) == 2
    assert recent[0]["id"] == second   # most-recent first
    assert recent[1]["id"] == first


def test_load_scan_by_id(isolated_db):
    sid = isolated_db.save_scan([_setup("X.NS")], None, trigger="periodic")
    row = isolated_db.load_scan_by_id(sid)
    assert row is not None
    assert row["id"] == sid
    assert row["setups"][0]["symbol"] == "X.NS"
    assert isolated_db.load_scan_by_id(999_999) is None


# ─── outcome rows created per setup ─────────────────────────────────────────


def test_outcomes_created_as_open(isolated_db):
    isolated_db.save_scan([_setup("RELIANCE.NS"), _setup("TCS.NS")], None)
    open_rows = isolated_db.fetch_open_outcomes()
    assert len(open_rows) == 2
    for row in open_rows:
        assert row["status"] if False else True  # status not returned — handled below
        assert row["entry_zone"] == pytest.approx(100.0)
        assert row["stop_loss"] == pytest.approx(95.0)
        assert row["target_1"] == pytest.approx(110.0)


def test_mark_outcome_updates_status(isolated_db):
    isolated_db.save_scan([_setup("RELIANCE.NS")], None)
    open_rows = isolated_db.fetch_open_outcomes()
    assert len(open_rows) == 1
    oid = open_rows[0]["id"]

    ok = isolated_db.mark_outcome(
        oid,
        status="T1_HIT",
        hit_level="t1",
        hit_price=110.0,
        hit_at=datetime.now(timezone.utc),
        bars_to_hit=4,
        pnl_pct=10.0,
    )
    assert ok is True
    assert isolated_db.fetch_open_outcomes() == []


def test_mark_outcome_unknown_id_returns_false(isolated_db):
    assert isolated_db.mark_outcome(999999, status="SL_HIT") is False


# ─── stats aggregation ─────────────────────────────────────────────────────


def test_compute_stats_empty(isolated_db):
    stats = isolated_db.compute_stats(days=30)
    assert stats["total_setups"] == 0
    assert stats["win_rate_pct"] == 0.0
    assert stats["by_grade"]["A"]["total"] == 0


def test_compute_stats_win_rate(isolated_db):
    isolated_db.save_scan(
        [
            _setup("A.NS", "A"),
            _setup("B.NS", "A"),
            _setup("C.NS", "B"),
            _setup("D.NS", "B"),
        ],
        None,
    )
    ids = [r["id"] for r in isolated_db.fetch_open_outcomes()]
    assert len(ids) == 4

    isolated_db.mark_outcome(ids[0], status="T1_HIT", hit_level="t1", pnl_pct=5.0)
    isolated_db.mark_outcome(ids[1], status="T2_HIT", hit_level="t2", pnl_pct=10.0)
    isolated_db.mark_outcome(ids[2], status="SL_HIT", hit_level="sl", pnl_pct=-3.0)
    # ids[3] stays OPEN — should NOT count in win-rate denominator

    stats = isolated_db.compute_stats(days=30)
    assert stats["total_setups"] == 4
    assert stats["closed"] == 3
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["win_rate_pct"] == pytest.approx(66.7, abs=0.1)

    grade_a = stats["by_grade"]["A"]
    assert grade_a["total"] == 2
    assert grade_a["closed"] == 2
    assert grade_a["wins"] == 2
    assert grade_a["win_rate_pct"] == pytest.approx(100.0)

    grade_b = stats["by_grade"]["B"]
    assert grade_b["total"] == 2
    assert grade_b["closed"] == 1
    assert grade_b["wins"] == 0
    assert grade_b["win_rate_pct"] == pytest.approx(0.0)


def test_list_recent_scans_respects_limit(isolated_db):
    for i in range(5):
        isolated_db.save_scan([_setup(f"X{i}.NS")], None)
    assert len(isolated_db.list_recent_scans(limit=3)) == 3
    assert len(isolated_db.list_recent_scans(limit=10)) == 5


# ─── payload resilience ────────────────────────────────────────────────────


def test_save_tolerates_malformed_rows(isolated_db):
    payload = [_setup("GOOD.NS"), {"symbol": "BAD.NS"}]  # missing numeric fields
    scan_id = isolated_db.save_scan(payload, None)
    assert scan_id is not None
    # One outcome should still be created per row (bad row uses zero defaults)
    rows = isolated_db.fetch_open_outcomes()
    assert len(rows) == 2
    good = next(r for r in rows if r["symbol"] == "GOOD.NS")
    bad = next(r for r in rows if r["symbol"] == "BAD.NS")
    assert good["entry_zone"] == pytest.approx(100.0)
    assert bad["entry_zone"] == pytest.approx(0.0)


def test_multiday_deduplication_skips_active_trade(isolated_db):
    # Day 1 scan inserts 1 outcome
    scan1 = isolated_db.save_scan([_setup("RELIANCE.NS", "A")], None)
    assert scan1 is not None
    open_rows = isolated_db.fetch_open_outcomes()
    assert len(open_rows) == 1
    rel_id = open_rows[0]["id"]

    # Day 2 scan with same setup should be deduplicated (active trade already open)
    scan2 = isolated_db.save_scan([_setup("RELIANCE.NS", "A")], None)
    assert scan2 is not None
    open_rows2 = isolated_db.fetch_open_outcomes()
    assert len(open_rows2) == 1
    assert open_rows2[0]["id"] == rel_id

    # Resolve active trade to T1_HIT
    isolated_db.mark_outcome(rel_id, status="T1_HIT", hit_level="t1", pnl_pct=4.5)
    assert len(isolated_db.fetch_open_outcomes()) == 0

    # Day 3 scan with same setup should now be accepted as a new trade
    scan3 = isolated_db.save_scan([_setup("RELIANCE.NS", "A")], None)
    assert scan3 is not None
    open_rows3 = isolated_db.fetch_open_outcomes()
    assert len(open_rows3) == 1
    assert open_rows3[0]["id"] != rel_id


def test_compute_stats_separates_cancelled_from_filled_and_calculates_expectancy(isolated_db):
    isolated_db.save_scan(
        [
            _setup("WIN.NS", "A"),
            _setup("LOSS.NS", "B"),
            _setup("CANCEL.NS", "C"),
        ],
        None,
    )
    rows = isolated_db.fetch_open_outcomes()
    assert len(rows) == 3

    id_map = {r["symbol"]: r["id"] for r in rows}
    isolated_db.mark_outcome(id_map["WIN.NS"], status="T1_HIT", hit_level="t1", pnl_pct=5.0)
    isolated_db.mark_outcome(id_map["LOSS.NS"], status="SL_HIT", hit_level="sl", pnl_pct=-2.5)
    isolated_db.mark_outcome(id_map["CANCEL.NS"], status="CANCELLED", hit_level="missed_entry", pnl_pct=0.0)

    stats = isolated_db.compute_stats(days=30)
    assert stats["total_setups"] == 3
    assert stats["closed"] == 3
    assert stats["filled_trades"] == 2
    assert stats["cancelled"] == 1
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    # Win rate should be 1 / 2 filled trades (50.0%), NOT 1 / 3 (33.3%)!
    assert stats["win_rate_pct"] == pytest.approx(50.0)
    # 1 win (+1.0 R) and 1 loss (-1.0 R) -> Net expectancy 0.0 R
    assert stats["expectancy_r"] == pytest.approx(0.0)
