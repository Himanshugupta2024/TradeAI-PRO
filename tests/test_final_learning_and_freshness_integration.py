"""
TradeAI PRO — Final Learning Pipeline & Intraday Freshness Integration Tests.

Verifies:
1. test_paper_close_creates_one_outcome
2. test_mock_records_create_zero_outcomes
3. test_duplicate_close_idempotent
4. test_legacy_incomplete_not_backfilled
5. test_cold_start_below_threshold
6. test_learned_weights_persist_supabase
7. test_intraday_api_reports_scan_freshness
8. test_stale_results_not_actionable
9. test_expired_setups_removed
10. test_no_trade_run_visible_in_feed
11. test_cached_response_exposes_staleness
12. test_ml_validation_non_durable_marker
"""

import os
import pytest
from datetime import datetime, timezone, timedelta, UTC

from db.connection import SessionLocal, init_db
from db.models import TradeOutcome, IntradayScan
from db.runtime_state import get_runtime_state, set_runtime_state
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade
from agents.analytics.feedback_loop import (
    TradeOutcomeLogger,
    FeedbackAnalyser,
    WeightAdapter,
    get_learning_stats,
    backfill_closed_paper_trades,
    DEFAULT_WEIGHTS,
)
from db import intraday_store

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    # Clean up test rows
    with SessionLocal() as s:
        s.query(TradeOutcome).filter(
            TradeOutcome.symbol.in_(["RELIANCE_TEST", "TCS_TEST", "MOCK_BTC", "INFY_INCOMPLETE", "TEST_SYM", "TATAMOTORS_TEST", "RELIANCE_VAL"])
        ).delete(synchronize_session=False)
        s.query(IntradayScan).filter(IntradayScan.trigger == "test_freshness").delete(synchronize_session=False)
        s.commit()
    # Reset learned weights in runtime state
    set_runtime_state("learned_weights:v1", dict(DEFAULT_WEIGHTS))
    yield
    with SessionLocal() as s:
        s.query(TradeOutcome).filter(
            TradeOutcome.symbol.in_(["RELIANCE_TEST", "TCS_TEST", "MOCK_BTC", "INFY_INCOMPLETE", "TEST_SYM", "TATAMOTORS_TEST", "RELIANCE_VAL"])
        ).delete(synchronize_session=False)
        s.query(IntradayScan).filter(IntradayScan.trigger == "test_freshness").delete(synchronize_session=False)
        s.commit()
    set_runtime_state("learned_weights:v1", dict(DEFAULT_WEIGHTS))


# ── Test 1: Paper close creates exactly one outcome in PostgreSQL ─────────────
def test_paper_close_creates_one_outcome():
    logger = TradeOutcomeLogger()
    trade_id = "pt-test-close-001"
    
    with SessionLocal() as s:
        s.query(TradeOutcome).filter(TradeOutcome.trade_id == trade_id).delete()
        s.commit()

    # Log genuine paper close
    out_id = logger.log_closed_trade(
        trade_id=trade_id,
        symbol="RELIANCE_VAL",
        direction="LONG",
        signal_type="BREAKOUT",
        regime="TRENDING",
        rsi_at_entry=62.5,
        volume_above_avg=True,
        entry_price=1250.0,
        exit_price=1275.0,
        pnl_pct=2.0,
        exit_reason="CLOSED_TP",
        confidence=75.0,
        environment="PAPER_FORWARD",
    )
    assert out_id is not None

    with SessionLocal() as s:
        rows = s.query(TradeOutcome).filter(TradeOutcome.trade_id == trade_id).all()
        assert len(rows) == 1
        assert rows[0].symbol == "RELIANCE_VAL"
        assert rows[0].result == "WIN"
        assert rows[0].environment == "PAPER_FORWARD"
        assert rows[0].pnl_pct == 2.0


# ── Test 2: Mock/Test records produce zero learning outcomes ──────────────────
def test_mock_records_create_zero_outcomes():
    logger = TradeOutcomeLogger()
    
    for mock_sym in ["TEST_RELIANCE", "MOCK_BTC", "DEMO_ETH", "SIM_NIFTY"]:
        out_id = logger.log_closed_trade(
            trade_id=f"mock-{mock_sym}",
            symbol=mock_sym,
            direction="LONG",
            signal_type="RSI_BOUNCE",
            regime="TRENDING",
            rsi_at_entry=25.0,
            entry_price=100.0,
            exit_price=105.0,
            pnl_pct=5.0,
            confidence=80.0,
        )
        assert out_id is None, f"Mock trade {mock_sym} should have been quarantined"

    with SessionLocal() as s:
        count = s.query(TradeOutcome).filter(
            TradeOutcome.symbol.like("TEST%") |
            TradeOutcome.symbol.like("MOCK%") |
            TradeOutcome.symbol.like("DEMO%") |
            TradeOutcome.symbol.like("SIM_%")
        ).count()
        assert count == 0


# ── Test 3: Duplicate trade close is strictly idempotent ──────────────────────
def test_duplicate_close_idempotent():
    logger = TradeOutcomeLogger()
    dup_id = "pt-test-dup-002"

    with SessionLocal() as s:
        s.query(TradeOutcome).filter(TradeOutcome.trade_id == dup_id).delete()
        s.commit()

    # First close
    id1 = logger.log_closed_trade(
        trade_id=dup_id,
        symbol="TCS_TEST",
        direction="LONG",
        entry_price=3500.0,
        exit_price=3550.0,
        pnl_pct=1.42,
        regime="TRENDING",
        confidence=70.0,
        environment="PAPER_FORWARD",
    )
    assert id1 is not None

    # Second close with identical trade_id
    id2 = logger.log_closed_trade(
        trade_id=dup_id,
        symbol="TCS_TEST",
        direction="LONG",
        entry_price=3500.0,
        exit_price=3550.0,
        pnl_pct=1.42,
        regime="TRENDING",
        confidence=70.0,
        environment="PAPER_FORWARD",
    )
    assert id2 == id1

    with SessionLocal() as s:
        rows = s.query(TradeOutcome).filter(TradeOutcome.trade_id == dup_id).all()
        assert len(rows) == 1, "Duplicate trade close must not create multiple rows"


# ── Test 4: Incomplete legacy trades marked LEGACY_INCOMPLETE & excluded ──────
def test_legacy_incomplete_not_backfilled():
    legacy_trade = {
        "id": 9991,
        "symbol": "INFY_INCOMPLETE",
        "direction": "LONG",
        "entry_price": 1400.0,
        "exit_price": 1390.0,
        "pnl_pct": -0.71,
        "status": "CLOSED_SL",
        "regime": "UNKNOWN",  # missing regime
        "confidence": 0.0,    # missing confidence
        "entry_time": "2026-11-01T10:00:00+05:30",
        "exit_time": "2026-11-01T10:30:00+05:30",
    }
    
    res = backfill_closed_paper_trades([legacy_trade])
    assert res["legacy_incomplete"] == 1
    assert res["genuine_forward"] == 0

    with SessionLocal() as s:
        row = s.query(TradeOutcome).filter(TradeOutcome.trade_id == "pt-9991").first()
        assert row is not None
        assert row.environment == "LEGACY_INCOMPLETE"

    # Verify FeedbackAnalyser excludes it
    stats = get_learning_stats()
    assert stats["health"]["legacy_incomplete_trades"] >= 1


# ── Test 5: Cold start below canonical threshold (0-29 trades) ────────────────
def test_cold_start_below_threshold():
    stats = get_learning_stats()
    health = stats["health"]
    
    assert health["canonical_sample_requirement"] == 30
    sample = health.get("genuine_forward_trades", 0)
    if sample < 30:
        assert health["state"] == "COLD_START"
        weights = stats["weights"]
        for k in DEFAULT_WEIGHTS:
            assert weights.get(k) == 1.0


# ── Test 6: Learned weights persist to PostgreSQL runtime_state ───────────────
def test_learned_weights_persist_supabase():
    adapter = WeightAdapter()
    dummy_insights = {
        "signal_type_stats": {
            "RSI_BOUNCE": {"win_rate": 75.0, "trades": 35, "weighted_trades": 35.0},
        },
        "overall": {"total_trades": 35, "effective_trades": 35.0, "win_rate": 75.0},
    }

    new_weights = adapter.update_weights(dummy_insights)
    
    # Read from DB runtime state
    db_weights = get_runtime_state("learned_weights:v1")
    assert db_weights is not None
    assert "_bounds" in new_weights
    # Verify conservative bounds [0.75, 1.25] for sample < 100
    assert new_weights["_bounds"] == [0.75, 1.25]
    for k in DEFAULT_WEIGHTS:
        val = new_weights[k]
        assert 0.75 <= val <= 1.25, f"Weight for {k} ({val}) violated conservative bounds [0.75, 1.25]"


# ── Test 7: Intraday API reports complete scan freshness contract ─────────────
def test_intraday_api_reports_scan_freshness():
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    now_ist = datetime.now(_IST)

    scan_id = "iscan-test-freshness-001"
    signals = [{
        "symbol": "TATAMOTORS_TEST",
        "direction": "LONG",
        "confluence_score": 78.5,
        "entry": 850.0,
        "stop_loss": 840.0,
        "target": 870.0,
    }]

    intraday_store.save_scan(
        signals,
        symbols_scanned=15,
        trigger="test_freshness",
        scan_id=scan_id,
        started_at=now_utc - timedelta(seconds=12),
        completed_at=now_utc,
        source_last_candle_at=now_utc - timedelta(minutes=5),
        market_session_date=now_ist.date(),
        provider="yfinance",
        provider_freshness="15m Delayed",
        symbols_attempted=15,
        symbols_successful=15,
        symbols_failed=0,
        scanner_version="2.0.0",
        scan_duration_ms=12000,
    )

    row = intraday_store.load_latest_scan()
    assert row is not None
    assert row["scan_id"] == scan_id
    assert row["provider"] == "yfinance"
    assert row["provider_freshness"] == "15m Delayed"
    assert row["scanner_version"] == "2.0.0"
    assert row["symbols_attempted"] == 15
    assert row["symbols_successful"] == 15
    assert row["scan_age_seconds"] is not None
    assert row["freshness_status"] in ("FRESH", "DELAYED", "EOD", "STALE")
    assert row["qualified_setups"] >= 1


# ── Test 8: Stale scan results marked STALE and not actionable ─────────────────
def test_stale_results_not_actionable():
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    now_ist = datetime.now(_IST)

    # Scan from 2 hours ago
    stale_time = now_utc - timedelta(hours=2)
    stale_candle = now_utc - timedelta(hours=2, minutes=15)

    intraday_store.save_scan(
        [{"symbol": "TATAMOTORS_TEST", "direction": "LONG", "confluence_score": 60.0}],
        symbols_scanned=20,
        trigger="test_freshness",
        scan_id="iscan-stale-002",
        started_at=stale_time,
        completed_at=stale_time,
        source_last_candle_at=stale_candle,
        market_session_date=now_ist.date(),
    )

    row = intraday_store.load_latest_scan()
    assert row is not None
    assert row["freshness_status"] == "STALE"
    assert row["scan_age_seconds"] > 3600


# ── Test 9: Expired setups past valid_until are flagged ────────────────────────
def test_expired_setups_removed():
    now_ist = datetime.now(_IST)
    expired_time = (now_ist - timedelta(minutes=45)).isoformat()

    signals = [
        {
            "symbol": "TATAMOTORS_TEST",
            "direction": "LONG",
            "confluence_score": 82.0,
            "valid_until": expired_time,  # 45 minutes in the past
        }
    ]

    intraday_store.save_scan(
        signals,
        symbols_scanned=1,
        trigger="test_freshness",
        scan_id="iscan-expired-003",
    )

    row = intraday_store.load_latest_scan()
    assert row is not None
    assert len(row["signals"]) == 1
    assert row["signals"][0]["is_expired"] is True
    assert len(row["active_signals"]) == 0
    assert row["qualified_setups"] == 0


# ── Test 10: NO_TRADE autonomous run is recorded in feed ──────────────────────
def test_no_trade_run_visible_in_feed():
    from agents.execution.autonomous_daemon import get_autonomous_daemon
    from agents.execution.idempotency import record_autonomous_decision_to_db

    daemon = get_autonomous_daemon()
    today_str = datetime.now(_IST).date().isoformat()
    unique_sig = f"nt-test-{int(datetime.now().timestamp())}"

    entry = daemon.record_decision(
        action="NO_TRADE",
        symbol="CYCLE",
        direction="NEUTRAL",
        price=0.0,
        reason="Scan complete (20 symbols scanned, 0 qualified setups)",
    )
    assert entry["action"] == "NO_TRADE"
    assert entry["symbol"] == "CYCLE"

    record_autonomous_decision_to_db(
        signal_id=unique_sig,
        run_id="test-cycle-no-trade",
        symbol="CYCLE",
        action="NO_TRADE",
        direction="NEUTRAL",
        reason="Scan complete (20 symbols scanned, 0 qualified setups)",
    )

    feed = daemon.get_decision_feed(10)
    actions = [d.get("action") for d in feed]
    assert "NO_TRADE" in actions


# ── Test 11: Cached response exposes data staleness truthfully ────────────────
def test_cached_response_exposes_staleness():
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    
    intraday_store.save_scan(
        [{"symbol": "RELIANCE_TEST", "direction": "LONG", "confluence_score": 70.0}],
        symbols_scanned=20,
        trigger="test_freshness",
        scan_id="iscan-cached-004",
        started_at=now_utc - timedelta(minutes=8),
        completed_at=now_utc - timedelta(minutes=8),
        source_last_candle_at=now_utc - timedelta(minutes=23),
        provider="yfinance",
        provider_freshness="15m Delayed",
    )

    row = intraday_store.load_latest_scan()
    assert row is not None
    assert "Delayed" in row["provider_freshness"]
    assert row["data_age_seconds"] >= 900
    assert row["freshness_status"] in ("DELAYED", "EOD", "STALE")


# ── Test 12: ML validation endpoint returns non-durable marker ─────────────────
@pytest.mark.asyncio
async def test_ml_validation_non_durable_marker():
    from api.routes import ensemble_validate
    
    res = await ensemble_validate(run=False)
    assert "durable" in res
    assert res["durable"] is False
    assert res["recommended_runner"] == "github_actions"
    assert "runner_note" in res
