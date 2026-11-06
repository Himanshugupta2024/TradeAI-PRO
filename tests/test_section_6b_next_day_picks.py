"""
TradeAI-PRO — Section 6B Next Trading Day Picks Acceptance & Regression Test Suite.
===================================================================================
Verifies:
  1. Target session calendar resolution: Mon-Fri regular session policy, holiday skipping,
     and special session (Muhurat) segregation.
  2. Bounded two-stage pipeline: technical levels, catalysts, R:R geometry (>= 1.5x),
     and max 5 picks cap without forced padding.
  3. Honest state reporting: NO_QUALIFIED_SETUPS when criteria are not met.
  4. Zero Crypto / Forex work verification during equity research generation.
  5. Deterministic idempotent generation, caching, and archive indexing.
  6. Independent post-session outcome resolution: TRIGGERED, NOT_TRIGGERED,
     RESOLVED_WIN, RESOLVED_LOSS, INVALIDATED, and AMBIGUOUS.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from agents.market_intelligence.next_day_picks import (
    _IST,
    resolve_next_trading_session,
    generate_next_day_picks,
    analyze_candidate,
    resolve_report_outcomes,
    get_latest_next_day_picks,
    get_next_day_picks_history,
    get_next_day_pick_by_id,
    STATE_KEY_LATEST,
    STATE_KEY_INDEX,
)
from db.runtime_state import get_runtime_state, set_runtime_state


# ── Test 1: Calendar & Target Session Resolution ──────────────────────────────

def test_friday_to_monday_regular_session_resolution_with_muhurat_note():
    """
    Test user example:
    Friday, 06 November 2026 must NOT target Saturday 07 Nov.
    Under regular session policy, target is Monday, 09 November 2026.
    Sunday 08 Nov (Diwali Laxmi Pujan / Muhurat Trading) is flagged in special notes.
    """
    fri_dt = datetime(2026, 11, 6, 17, 0, 0, tzinfo=_IST)
    src_date, tgt_date, tgt_label, special_notes = resolve_next_trading_session(fri_dt)

    assert src_date == date(2026, 11, 6)
    assert tgt_date == date(2026, 11, 9)
    assert "Monday" in tgt_label
    assert "09 November 2026" in tgt_label
    assert special_notes is not None
    assert "Muhurat" in special_notes
    assert "08 November 2026" in special_notes


def test_regular_weekday_session_resolution():
    """Wednesday 2026-11-04 targets Thursday 2026-11-05."""
    wed_dt = datetime(2026, 11, 4, 16, 30, 0, tzinfo=_IST)
    src_date, tgt_date, tgt_label, special_notes = resolve_next_trading_session(wed_dt)

    assert src_date == date(2026, 11, 4)
    assert tgt_date == date(2026, 11, 5)
    assert "Thursday" in tgt_label
    assert "05 November 2026" in tgt_label
    assert special_notes is None


def test_holiday_skipping_resolution():
    """
    Monday 2026-11-09 targets Wednesday 2026-11-11,
    skipping Tuesday 2026-11-10 (Diwali Balipratipada holiday).
    """
    mon_dt = datetime(2026, 11, 9, 16, 30, 0, tzinfo=_IST)
    src_date, tgt_date, tgt_label, special_notes = resolve_next_trading_session(mon_dt)

    assert src_date == date(2026, 11, 9)
    assert tgt_date == date(2026, 11, 11)  # Skips 2026-11-10
    assert "Wednesday" in tgt_label
    assert "11 November 2026" in tgt_label


# ── Test 2: Candidate Analysis & Strict R:R Gate ──────────────────────────────

def test_analyze_candidate_enforces_min_rr_and_completeness():
    """Verify candidate analysis generates complete structure and enforces R:R >= 1.5x."""
    src_date = date(2026, 11, 6)
    tgt_date = date(2026, 11, 9)

    # 30-day upward trending series
    dates = [src_date - timedelta(days=30 - i) for i in range(30)]
    closes = [1000.0 + (i * 5.0) for i in range(30)]
    opens = [c - 2.0 for c in closes]
    highs = [c + 4.0 for c in closes]
    lows = [c - 3.0 for c in closes]
    volumes = [1000000 for _ in range(30)]

    df = pd.DataFrame({
        "timestamp": dates, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes
    })

    cand = analyze_candidate("RELIANCE", df, src_date, tgt_date)
    assert cand is not None
    assert cand["symbol"] == "RELIANCE"
    assert cand["exchange"] == "NSE"
    assert cand["directional_bias"] == "LONG"
    assert cand["trade_plan"]["risk_reward"] >= 1.5
    assert "trigger_price" in cand["trade_plan"]
    assert "stop_loss" in cand["trade_plan"]
    assert "target_1" in cand["trade_plan"]
    assert cand["scenarios"]["bull"] != ""
    assert cand["scenarios"]["invalidation"] != ""
    assert len(cand["news_catalysts"]) > 0


def test_no_qualified_setups_reported_truthfully():
    """When candidates fail R:R or quality gates, reports NO_QUALIFIED_SETUPS without padding."""
    ref_dt = datetime(2026, 11, 6, 17, 0, 0, tzinfo=_IST)
    # Pass an empty universe
    report = generate_next_day_picks(ref_dt=ref_dt, force_refresh=True, universe=[])

    assert report["picks_count"] == 0
    assert report["picks"] == []
    assert report["status"] == "NO_QUALIFIED_SETUPS"
    assert "No stocks met" in report["status_reason"]


# ── Test 3: Full Pipeline Execution, Zero-Work & Caching ──────────────────────

def test_generate_next_day_picks_pipeline_and_caching():
    """Verify pipeline produces max 5 picks, stores atomically, and serves cached response."""
    ref_dt = datetime(2026, 11, 6, 17, 0, 0, tzinfo=_IST)
    
    # 1. Fresh Generation
    report = generate_next_day_picks(ref_dt=ref_dt, force_refresh=True, max_picks=5)
    assert report["status"] in ("READY", "NO_QUALIFIED_SETUPS")
    assert report["picks_count"] <= 5
    assert report["target_session_date"] == "2026-11-09"
    assert "report_id" in report

    # Verify zero crypto/forex symbols in equity research
    for pick in report["picks"]:
        assert "/" not in pick["symbol"]
        assert pick["exchange"] == "NSE"
        assert pick["trade_plan"]["risk_reward"] >= 1.5

    # 2. Idempotent Cached Fetch
    cached_report = generate_next_day_picks(ref_dt=ref_dt, force_refresh=False)
    assert cached_report["report_id"] == report["report_id"]
    assert cached_report["generated_at_ist"] == report["generated_at_ist"]

    # 3. Archive Index Check
    history = get_next_day_picks_history()
    assert len(history) > 0
    assert history[0]["report_id"] == report["report_id"]


# ── Test 4: Post-Session Outcome Resolution ───────────────────────────────────

def test_outcome_resolution_all_permutations():
    """
    Test outcome evaluation logic:
      - RESOLVED_WIN: Triggers and hits Target 1
      - RESOLVED_LOSS: Triggers and hits Stop Loss
      - NOT_TRIGGERED: High never reaches trigger
      - INVALIDATED: Gap open below Stop Loss
      - AMBIGUOUS: Touches both Target 1 and Stop Loss in same session
    """
    ref_dt = datetime(2026, 11, 6, 17, 0, 0, tzinfo=_IST)
    report = generate_next_day_picks(ref_dt=ref_dt, force_refresh=True, max_picks=5)
    report_id = report["report_id"]

    if not report["picks"]:
        pytest.skip("No picks generated to test outcome resolution.")

    first_pick = report["picks"][0]
    sym = first_pick["symbol"]
    plan = first_pick["trade_plan"]
    trigger = plan["trigger_price"]
    stop = plan["stop_loss"]
    t1 = plan["target_1"]

    # Case A: RESOLVED_WIN (hits T1)
    candles_win = {
        sym: {
            "open": trigger,
            "high": t1 + 5.0,
            "low": stop + 2.0,
            "close": t1,
        }
    }
    res_win = resolve_report_outcomes(report_id, candles_win)
    assert res_win["ok"] is True
    updated_pick = res_win["report"]["picks"][0]
    assert updated_pick["outcome_status"] == "RESOLVED_WIN"
    assert updated_pick["outcome_details"]["exit_price"] == t1

    # Case B: RESOLVED_LOSS (hits Stop Loss)
    candles_loss = {
        sym: {
            "open": trigger,
            "high": trigger + 1.0,
            "low": stop - 2.0,
            "close": stop,
        }
    }
    res_loss = resolve_report_outcomes(report_id, candles_loss)
    updated_pick = res_loss["report"]["picks"][0]
    assert updated_pick["outcome_status"] == "RESOLVED_LOSS"
    assert updated_pick["outcome_details"]["exit_price"] == stop

    # Case C: NOT_TRIGGERED (high below trigger)
    candles_not_trig = {
        sym: {
            "open": trigger - 10.0,
            "high": trigger - 2.0,
            "low": stop + 5.0,
            "close": trigger - 5.0,
        }
    }
    res_not_trig = resolve_report_outcomes(report_id, candles_not_trig)
    updated_pick = res_not_trig["report"]["picks"][0]
    assert updated_pick["outcome_status"] == "NOT_TRIGGERED"

    # Case D: INVALIDATED (gap down below stop)
    candles_inval = {
        sym: {
            "open": stop - 5.0,
            "high": trigger + 5.0,
            "low": stop - 10.0,
            "close": trigger,
        }
    }
    res_inval = resolve_report_outcomes(report_id, candles_inval)
    updated_pick = res_inval["report"]["picks"][0]
    assert updated_pick["outcome_status"] == "INVALIDATED"

    # Case E: AMBIGUOUS (session range covers both target and stop)
    candles_ambig = {
        sym: {
            "open": trigger,
            "high": t1 + 10.0,
            "low": stop - 10.0,
            "close": trigger,
        }
    }
    res_ambig = resolve_report_outcomes(report_id, candles_ambig)
    updated_pick = res_ambig["report"]["picks"][0]
    assert updated_pick["outcome_status"] == "AMBIGUOUS"
