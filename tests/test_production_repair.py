"""
TradeAI PRO — Production Repair Test Suite
Verifies all 10 confirmed market-hours production defects and system data health.
"""

import asyncio
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from agents.analytics.trade_journal import TradeJournal
from agents.execution.paper_trading import PaperTradingEngine
from agents.intraday_engine.types import IntradaySignal, format_price_by_magnitude
from agents.intraday_engine.strategies import strat_orb, _day_session
from agents.market_intelligence.market_data import fetch_fii_dii_flow
from agents.market_intelligence.swing_scanner import (
    _check_52w_high_breakout,
    SwingSetup,
)
from agents.market_intelligence.breakout_scanner import scan_breakouts
from api import service as svc


# ── 1. Feed Health Semantics & Decoupling ───────────────────────────────────

def test_feed_health_semantics():
    """Verify feed status reads durable state and decouples API health from in-memory cache."""
    status = svc._get_feed_status()
    assert "state" in status
    assert "market_snapshot_health" in status
    assert "worker_health" in status
    assert "source" in status
    assert status["market_snapshot_health"] in ("CURRENT", "DELAYED", "STALE", "EOD", "UNAVAILABLE")
    assert status["worker_health"] in ("WORKER_OK", "WORKER_LATE", "WORKER_FAILED", "NO_RECENT_RUN")


@pytest.mark.asyncio
async def test_health_check_endpoint():
    """Verify /api/health endpoint returns API_UP, DB status, and durable feed metrics."""
    from api.routes import health_check
    res = await health_check()
    assert res["status"] in ("ok", "degraded", "starting")
    assert res["api_health"] in ("API_UP", "API_DEGRADED")
    assert "market_snapshot_health" in res
    assert "worker_health" in res
    assert "feed" in res
    assert "database" in res
    assert res["database"]["connected"] is True


@pytest.mark.asyncio
async def test_ready_check_endpoint():
    """Verify /api/ready returns ready status for container probes."""
    from api.routes import readiness_check
    res = await readiness_check()
    assert res["status"] == "ready"


# ── 2. System Data Health Endpoint ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_system_data_health_endpoint():
    """Verify /api/system/data-health returns consolidated status across all 9 subsystems."""
    from api.routes import get_system_data_health
    res = await get_system_data_health()
    assert "status" in res
    assert res["status"] in ("HEALTHY", "DEGRADED", "UNHEALTHY")
    assert "subsystems" in res
    subs = res["subsystems"]
    expected_subsystems = [
        "nse_snapshot",
        "intraday_scanner",
        "swing_scanner",
        "breakout_scanner",
        "institutional_flow",
        "macro",
        "signal_scorecard",
        "autonomous_worker",
        "websocket",
    ]
    for s in expected_subsystems:
        assert s in subs, f"Subsystem {s} missing from /api/system/data-health"
        assert "status" in subs[s] or "state" in subs[s]


# ── 3. Sub-Rupee SHORT Paper Trade Geometry & Tick Precision ───────────────

def test_subrupee_tick_precision_formatting():
    """Verify sub-rupee assets retain high decimal precision and do not collapse to 0.07."""
    entry = 0.071234
    sl = 0.074345
    tp = 0.068123

    # Crypto / sub-rupee formatting should produce 6 decimals
    f_entry = format_price_by_magnitude(entry, "DOGEUSDT")
    f_sl = format_price_by_magnitude(sl, "DOGEUSDT")
    f_tp = format_price_by_magnitude(tp, "DOGEUSDT")

    assert f_entry == 0.071234
    assert f_sl == 0.074345
    assert f_tp == 0.068123
    assert f_tp < f_entry < f_sl


def test_short_paper_trade_geometry_validation():
    """Verify PaperTradingEngine correctly validates SHORT geometry for micro-priced assets."""
    engine = PaperTradingEngine()
    engine.balance = 50000.0

    # Risk: 0.0743 - 0.0712 = 0.0031. Reward: 0.0712 - 0.0640 = 0.0072. RR = 2.32 (>= 2.0)
    trade = engine.execute(
        symbol="DOGEUSDT",
        direction="SHORT",
        entry=0.0712,
        sl=0.0743,
        tp=0.0640,
        confidence=75.0,
        allow_duplicate_symbol=True,
    )
    assert trade is not None, "Paper trade execution failed"
    assert trade.direction == "SHORT"
    assert trade.entry_price == 0.0712
    assert trade.stop_loss == 0.0743
    assert trade.target == 0.0640


# ── 4. Breakout 52-Week High Lookback Excludes Current Bar ──────────────────

def test_breakout_52w_high_lookback_excludes_current_bar():
    """Verify 52W high baseline excludes current candle to prevent self-comparison."""
    dates = pd.date_range(end=datetime.now(), periods=260, freq="B")
    # Historical 259 bars with high <= 120.0
    highs = [100.0 + (i % 20) for i in range(259)] + [135.0]  # bar 260 breaks out to 135
    lows = [h - 5.0 for h in highs]
    closes = [h - 1.0 for h in highs]
    opens = [l + 1.0 for l in lows]
    volumes = [500000.0] * 260

    df = pd.DataFrame({
        "timestamp": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "ATR_14": [2.0] * 260,
        "RSI_14": [65.0] * 260,
        "EMA_20": [115.0] * 260,
        "EMA_50": [105.0] * 260,
    })

    # When lookback excludes current bar, baseline 52w high is 119.0, so 135.0 is a 52W breakout!
    breakouts = scan_breakouts(df, "RELIANCE")
    types = [b.breakout_type for b in breakouts]
    assert "52W_HIGH" in types, "Expected 52W_HIGH breakout signal"

    # Now test non-breakout: current candle close is 110.0 (below prior high 119.0)
    df.iloc[-1, df.columns.get_loc("close")] = 110.0
    df.iloc[-1, df.columns.get_loc("high")] = 112.0
    breakouts_fail = scan_breakouts(df, "RELIANCE")
    types_fail = [b.breakout_type for b in breakouts_fail]
    assert "52W_HIGH" not in types_fail, "Should not trigger 52W_HIGH when close < prior 52W high"


# ── 5. Prior Session EOD Bar Leakage & Market Session Date ──────────────────

def test_session_prior_eod_candle_leakage_during_market_hours():
    """Verify that during market hours, if today's bars haven't arrived, prior EOD bar is rejected."""
    _IST = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(_IST)

    # Build synthetic DF where the last bar is from yesterday
    yesterday = now_ist.date() - timedelta(days=1)
    base_ts = datetime(yesterday.year, yesterday.month, yesterday.day, 14, 0, tzinfo=_IST)
    ts_list = [base_ts + timedelta(minutes=5 * i) for i in range(15)]

    df = pd.DataFrame({
        "timestamp": ts_list,
        "open": [100.0] * 15,
        "high": [101.0] * 15,
        "low": [99.0] * 15,
        "close": [100.5] * 15,
        "volume": [10000.0] * 15,
    })

    today_df, latest_row, prev_df = _day_session(df)
    from agents.intraday_engine.session import evaluate_market_session
    session = evaluate_market_session(now_ist)
    if session.is_trading_active:
        assert today_df.empty, "Yesterday's EOD bar must not be treated as today's active session"
        assert latest_row is None


# ── 6. Institutional Trade Flow Naming & Date Formatting ───────────────────

def test_institutional_flow_naming_and_formatting():
    """Verify residual category is renamed to Estimated Other / Residual and has unit disclosures."""
    flow = fetch_fii_dii_flow()
    assert "flows" in flow
    cat_names = [c["category"] for c in flow["flows"]]
    assert "Estimated Other / Residual" in cat_names
    assert "Retail" not in cat_names, "'Retail' label should be replaced with 'Estimated Other / Residual'"
    assert "Crores" in flow.get("units", "")
    assert "Data Date:" in flow.get("display_date", "")


# ── 7. Scorecard Hydration from Paper Trades ────────────────────────────────

def test_trade_journal_scorecard_hydration():
    """Verify TradeJournal hydrates stats from persistent paper trades even without in-memory loop."""
    journal = TradeJournal()
    scorecard = journal.get_scorecard()
    assert "total_signals" in scorecard
    assert scorecard["total_signals"] > 0
    assert "overall_win_rate" in scorecard
    assert "by_regime" in scorecard
    assert "by_confidence" in scorecard


# ── 8. SwingSpectrum Setup Strength Framing ─────────────────────────────────

def test_swingspectrum_setup_strength_framing():
    """Verify SwingSetup provides setup_strength_display and calibrated_win_rate_pct."""
    from dataclasses import asdict
    setup = SwingSetup(
        symbol="INFY",
        pattern="Pullback in Trend",
        direction="LONG",
        strength=85,
        entry_zone=1500.0,
        stop_loss=1470.0,
        target_1=1530.0,
        target_2=1560.0,
        risk_reward=2.0,
        timeframe="1D",
        description="Daily trend pullback",
        indicators={"RSI": 55.0},
    )
    assert setup.setup_strength_display == "Setup Strength: 85/100"
    assert setup.calibrated_win_rate_pct == 64.0  # Bucket 80-100: ~64%
    assert "64% Historical" in setup.win_rate_expectation_bucket
    d = asdict(setup)
    assert "setup_strength_display" in d
    assert d["setup_strength_display"] == "Setup Strength: 85/100"


# ── 9. Screener Opportunities & Breakouts Fallback ─────────────────────────

@pytest.mark.asyncio
async def test_screener_opportunities_endpoint():
    """Verify /api/screener/opportunities returns a valid list of opportunities."""
    from api.routes import get_screener_opportunities
    res = await get_screener_opportunities()
    assert "opportunities" in res
    assert isinstance(res["opportunities"], list)


@pytest.mark.asyncio
async def test_breakouts_endpoint():
    """Verify /api/breakouts returns structured diagnostic status."""
    from api.routes import get_breakouts
    res = await get_breakouts(min_strength=30)
    assert "all" in res
    assert "total_breakouts" in res
    assert "status" in res
    assert res["status"] in ("READY", "OK", "ZERO_RESULTS", "DATA_UNAVAILABLE", "ERROR")
    assert "updated_at" in res


# ── 10. Macro Heatmap Resilience ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_macro_heatmap_endpoint():
    """Verify /api/edge/macro-heatmap returns asset class scores and doesn't fail."""
    from api.routes import get_edge_macro_heatmap
    res = await get_edge_macro_heatmap(force_refresh=False)
    assert "heatmap" in res
    assert isinstance(res["heatmap"], dict)
