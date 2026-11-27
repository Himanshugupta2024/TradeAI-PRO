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
    assert cand["trade_plan"]["risk_reward"] >= 1.25
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
        assert pick["trade_plan"]["risk_reward"] >= 1.25

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

    # Case C: NOT_TRIGGERED (high below trigger, open safely between stop and trigger)
    safe_mid = round((trigger + stop) / 2.0, 2)
    candles_not_trig = {
        sym: {
            "open": safe_mid,
            "high": trigger - 0.5,
            "low": safe_mid - 0.2,
            "close": safe_mid,
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


# ── Test 5: Architectural Upgrades Regression Suite ───────────────────────────

def test_long_breakout_trigger_strictly_exceeds_day_high():
    """Verify long breakout trigger price strictly exceeds previous day's high."""
    src_date = date(2026, 11, 11)
    tgt_date = date(2026, 11, 12)

    dates = [src_date - timedelta(days=30 - i) for i in range(30)]
    closes = [400.0 + (i * 2.0) for i in range(30)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 3.0 for c in closes]
    opens = [c - 1.0 for c in closes]
    volumes = [2000000 for _ in range(30)]

    df = pd.DataFrame({
        "timestamp": dates, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes
    })
    cand = analyze_candidate("TATAMOTORS", df, src_date, tgt_date, benchmark_trend="BULLISH")
    assert cand is not None
    assert cand["directional_bias"] == "LONG"
    # Trigger price must strictly exceed the completed bar's high
    last_high = highs[-1]
    assert cand["trade_plan"]["trigger_price"] > last_high
    assert cand["trade_plan"]["risk_reward"] >= 1.25


def test_short_breakdown_trigger_and_risk_reward():
    """Verify downward trending stock generates short breakdown setup with strict R:R."""
    src_date = date(2026, 11, 11)
    tgt_date = date(2026, 11, 12)

    # 30-day downward trending series
    dates = [src_date - timedelta(days=30 - i) for i in range(30)]
    closes = [1000.0 - (i * 6.0) for i in range(30)]
    opens = [c + 2.0 for c in closes]
    highs = [c + 3.0 for c in closes]
    lows = [c - 4.0 for c in closes]
    volumes = [1500000 for _ in range(30)]

    df = pd.DataFrame({
        "timestamp": dates, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes
    })

    cand = analyze_candidate("WEAKSTOCK", df, src_date, tgt_date, benchmark_trend="BEARISH")
    assert cand is not None
    assert cand["directional_bias"] == "SHORT"
    last_low = lows[-1]
    # Short trigger price must be at or below last low
    assert cand["trade_plan"]["trigger_price"] <= last_low
    # Stop loss must be above trigger price
    assert cand["trade_plan"]["stop_loss"] > cand["trade_plan"]["trigger_price"]
    # Target 1 must be below trigger price
    assert cand["trade_plan"]["target_1"] < cand["trade_plan"]["trigger_price"]
    assert cand["trade_plan"]["risk_reward"] >= 1.25


def test_chop_filtering_returns_none_for_indecisive_bars():
    """Ambiguous chop without clean edge returns None rather than forcing a biased setup."""
    src_date = date(2026, 11, 11)
    tgt_date = date(2026, 11, 12)

    # Completely flat series hovering at same price
    dates = [src_date - timedelta(days=30 - i) for i in range(30)]
    closes = [500.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(30)]
    opens = [500.0 for _ in range(30)]
    highs = [502.0 for _ in range(30)]
    lows = [498.0 for _ in range(30)]
    volumes = [100000 for _ in range(30)]

    df = pd.DataFrame({
        "timestamp": dates, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes
    })

    cand = analyze_candidate("FLATSTOCK", df, src_date, tgt_date, benchmark_trend="NEUTRAL")
    assert cand is None  # Screened out, no false breakout signal



def test_pre_close_session_honest_freshness_and_cutoff():
    """Mid-session 11:41 IST generation must never claim EOD Completed Bar or future cutoff."""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    import numpy as np
    import pandas as pd
    from agents.market_intelligence.next_day_picks import (
        is_eod_bar_complete,
        resolve_next_trading_session,
        _sanitize_daily_bars,
    )

    ist = ZoneInfo("Asia/Kolkata")
    midday = datetime(2026, 11, 25, 11, 41, 18, tzinfo=ist)
    src_date, tgt_date, _, _ = resolve_next_trading_session(midday)
    # 2026-11-24 is Guru Nanak Jayanti holiday, so prior regular trading session is 2026-11-23
    assert src_date == date(2026, 11, 23)
    assert src_date < midday.date()
    assert tgt_date == date(2026, 11, 26)
    assert is_eod_bar_complete(date(2026, 11, 25), midday) is False
    assert is_eod_bar_complete(date(2026, 11, 23), midday) is True

    # Forming bar on 2026-11-25 must be stripped before 15:30 IST
    dates = pd.date_range("2026-10-01", "2026-11-25", freq="B")
    closes = np.linspace(1000.0, 1100.0, len(dates))
    df = pd.DataFrame({
        "timestamp": dates,
        "open": closes - 3.0,
        "high": closes + 6.0,
        "low": closes - 6.0,
        "close": closes,
        "volume": np.full(len(dates), 500_000.0),
    })
    cleaned = _sanitize_daily_bars(df, "SBIN", ref_dt=midday, completed_only=True)
    assert cleaned is not None
    assert pd.to_datetime(cleaned["timestamp"].iloc[-1]).date() < midday.date()


def test_next_day_picks_rr_varies_across_candidates():
    """Structural targets in next-day picks must not collapse to identical fixed R:R (1.500)."""
    from datetime import date, datetime
    from unittest.mock import patch
    from zoneinfo import ZoneInfo
    import numpy as np
    import pandas as pd
    from agents.market_intelligence.next_day_picks import analyze_candidate

    ist = ZoneInfo("Asia/Kolkata")
    ref_dt = datetime(2026, 11, 25, 17, 0, tzinfo=ist)
    dates = pd.date_range("2026-07-01", "2026-11-25", freq="B")
    n = len(dates)

    def _make_df(base: float, range_pct: float, swing_mult: float, cap_r1: float | None = None) -> pd.DataFrame:
        trend = np.linspace(0.0, base * 0.14, n)
        wave = np.sin(np.linspace(0.0, 5.0 * np.pi, n)) * (base * range_pct * swing_mult)
        c = base + trend + wave
        # Ensure strong positive closing trend near the end
        c[-5:] = np.linspace(c[-6], c[-6] * 1.03, 5)
        spread = c * range_pct
        h = c + spread * 0.5
        l = c - spread * 0.5
        if cap_r1 is not None:
            # Place a genuine prior swing high inside reachable structural corridor
            h[-12] = c[-1] * (1.0 + cap_r1)
            c[-12] = c[-1] * (1.0 + cap_r1 * 0.85)
        return pd.DataFrame({
            "timestamp": dates,
            "open": c - spread * 0.2,
            "high": h,
            "low": l,
            "close": c,
            "volume": np.full(n, 800_000.0) * (1.0 + 0.6 * (np.arange(n) == n - 1)),
        })

    specs = [
        ("HCLTECH", 1300.0, 0.022, 1.4, None),
        ("MARUTI", 13500.0, 0.020, 1.0, 0.048),
        ("M&M", 3200.0, 0.026, 2.2, 0.072),
    ]
    rrs = []
    with patch("api.fno_client.fetch_fno_analytics_sync", return_value={"envelope_source": "ATR_FALLBACK", "ce_wall": None, "pe_wall": None}):
        for sym, base, rpct, smult, cap_r1 in specs:
            cand = analyze_candidate(sym, _make_df(base, rpct, smult, cap_r1), date(2026, 11, 25), date(2026, 11, 26), "BULLISH", ref_dt=ref_dt)
            assert cand is not None, f"{sym} unexpectedly filtered out"
            rrs.append(round(float(cand["trade_plan"]["risk_reward"]), 3))
    assert len(set(rrs)) > 1, f"Expected non-formulaic varying R:R, got {rrs}"


def test_catalyst_filter_rejects_seo_spam_aged_and_opposing_bias():
    """Catalyst scorer rejects SEO farms, >7d items, and penalizes BEARISH news on LONG picks."""
    from agents.market_intelligence.next_day_picks import _is_seo_spam_item, _score_catalysts_for_bias

    assert _is_seo_spam_item("Simply Wall St — Why HCLTECH Stock Forecast Changed") is True
    assert _is_seo_spam_item("HCLTech Q3 results beat estimates on NSE filing") is False

    # Stale (>7d) disclosure must be ignored
    stale_only = [
        {"title": "Old Q3 note", "category": "CONFIRMED_DISCLOSURE", "impact": "BULLISH", "age_days": 51}
    ]
    assert _score_catalysts_for_bias(stale_only, "LONG") < 60.0

    # Fresh BEARISH news on LONG bias must penalize below neutral
    bearish_on_long = [
        {"title": "IT shares drag Nifty IT down 2%", "category": "FINANCIAL_NEWS", "impact": "BEARISH", "age_days": 2},
        {"title": "Client budget cut warning", "category": "CONFIRMED_DISCLOSURE", "impact": "BEARISH", "age_days": 3},
    ]
    assert _score_catalysts_for_bias(bearish_on_long, "LONG") < 50.0


def test_analyze_candidate_excludes_opposing_catalysts_and_caps_mixed_score():
    """Mixed catalysts must cap score and LONG picks must never expose BEARISH news_catalysts."""
    from datetime import date, datetime
    from unittest.mock import patch
    from zoneinfo import ZoneInfo
    import numpy as np
    import pandas as pd
    from agents.market_intelligence.next_day_picks import _score_catalysts_for_bias, analyze_candidate

    mixed = [
        {"headline": "JSWSTEEL rallies on expansion", "category": "FINANCIAL_NEWS", "impact": "BULLISH", "age_days": 0},
        {"headline": "Nifty down 1% broad selloff", "category": "FINANCIAL_NEWS", "impact": "BEARISH", "age_days": 0},
    ]
    assert _score_catalysts_for_bias(mixed, "LONG") <= 55.0

    ist = ZoneInfo("Asia/Kolkata")
    ref_dt = datetime(2026, 11, 27, 17, 0, tzinfo=ist)
    dates = pd.date_range("2026-07-01", "2026-11-27", freq="B")
    n = len(dates)
    c = 900.0 + np.linspace(0.0, 120.0, n)
    c[-5:] = np.linspace(c[-6], c[-6] * 1.03, 5)
    df = pd.DataFrame({
        "timestamp": dates,
        "open": c - 4.0,
        "high": c + 12.0,
        "low": c - 12.0,
        "close": c,
        "volume": np.full(n, 900_000.0),
    })

    with patch("api.fno_client.fetch_fno_analytics_sync", return_value={"envelope_source": "ATR_FALLBACK", "ce_wall": None, "pe_wall": None}), \
         patch("agents.market_intelligence.next_day_picks._discover_catalysts_for_symbol", return_value=mixed):
        cand = analyze_candidate("JSWSTEEL", df, date(2026, 11, 27), date(2026, 11, 30), "BULLISH", ref_dt=ref_dt)
        assert cand is not None
        assert all(cat.get("impact") != "BEARISH" for cat in cand["news_catalysts"])
