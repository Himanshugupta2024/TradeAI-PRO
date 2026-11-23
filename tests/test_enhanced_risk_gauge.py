"""Unit tests for enhanced Risk Gauge, Dual VIX, Market Breadth, and Dynamic Sizing."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
import pytest

from agents.macro.risk_gauge import RiskGauge, RiskIndicators
from agents.execution.position_sizing import regime_adjusted_risk_pct, compute_risk_pct
from agents.intraday_engine.risk_manager import calculate_position_size, can_take_trade
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade


def test_risk_indicators_dataclass_enhanced_fields():
    """Verify RiskIndicators dataclass handles new India VIX & Breadth fields."""
    now = datetime.now(timezone.utc)
    ind = RiskIndicators(
        vix=18.5,
        vix_change_pct=-5.0,
        btc_trend="RISING",
        nifty_trend="RISING",
        usdjpy_level=159.5,
        usdjpy_trend="FLAT",
        gold_trend="FLAT",
        risk_regime="RISK_ON",
        timestamp=now,
        india_vix=14.2,
        india_vix_change_pct=-8.0,
        india_vix_trend="FALLING",
        ad_ratio=2.2,
        above_ema20_pct=70.0,
        breadth_regime="STRONG_BREADTH",
    )
    d = ind.to_dict()
    assert d["india_vix"] == 14.2
    assert d["ad_ratio"] == 2.2
    assert d["breadth_regime"] == "STRONG_BREADTH"

    # Test deserialization
    deser = RiskIndicators.from_dict(d)
    assert deser.india_vix == 14.2
    assert deser.ad_ratio == 2.2
    assert deser.risk_regime == "RISK_ON"


def test_risk_gauge_dual_vix_and_breadth_scoring(monkeypatch):
    """Verify get_risk_score evaluates India VIX and Market Breadth."""
    gauge = RiskGauge()
    now = datetime.now(timezone.utc)

    # Bullish scenario: Low India VIX (12.0), Strong Breadth (2.5), Low US VIX (14.0)
    bullish_indicators = RiskIndicators(
        vix=14.0,  # +100
        vix_change_pct=-10.0,
        btc_trend="RISING",  # +100
        nifty_trend="RISING",  # +100
        usdjpy_level=155.0,
        usdjpy_trend="RISING",  # +100 (weak yen / carry on = risk-on)
        gold_trend="FALLING",  # +100 (inverted)
        risk_regime="RISK_ON",
        timestamp=now,
        india_vix=12.0,  # +100 (< 13.0)
        ad_ratio=2.5,  # +100 (>= 2.0)
    )

    monkeypatch.setattr(gauge, "fetch_risk_indicators", lambda: bullish_indicators)
    score_bullish = gauge.get_risk_score()
    assert score_bullish == 100.0

    # Bearish scenario: High India VIX (25.0), Poor Breadth (0.4), High US VIX (32.0)
    bearish_indicators = RiskIndicators(
        vix=32.0,  # -100
        vix_change_pct=15.0,
        btc_trend="FALLING",  # -100
        nifty_trend="FALLING",  # -100
        usdjpy_level=162.0,
        usdjpy_trend="FALLING",  # -100 (yen surge / carry unwind = risk-off)
        gold_trend="RISING",  # -100 (inverted)
        risk_regime="RISK_OFF",
        timestamp=now,
        india_vix=25.0,  # -100 (> 22.0)
        ad_ratio=0.4,  # -100 (<= 0.5)
    )

    monkeypatch.setattr(gauge, "fetch_risk_indicators", lambda: bearish_indicators)
    score_bearish = gauge.get_risk_score()
    assert score_bearish == -100.0


def test_risk_gauge_vetoes_and_liquidation_conditioning(monkeypatch):
    """Verify market shock trips vetoes, conditions gold to liquidation (-40), and forces STAND_DOWN."""
    gauge = RiskGauge()
    now = datetime.now(timezone.utc)

    # Exact tape readings from user screenshot:
    # VIX=35.89 (-100), VIX_CHANGE=105.2%, INDIA_VIX=17.32 (+4), AD=0.25 (-100),
    # BTC=FALLING (-100), NIFTY=FLAT (0), USDJPY=FLAT (0), GOLD=FALLING (liquidation -> -40)
    tape_indicators = RiskIndicators(
        vix=35.89,
        vix_change_pct=105.20,
        btc_trend="FALLING",
        nifty_trend="FLAT",
        usdjpy_level=158.56,
        usdjpy_trend="FLAT",
        gold_trend="FALLING",
        risk_regime="RISK_OFF",  # Will be evaluated by safety gates
        timestamp=now,
        india_vix=17.32,
        ad_ratio=0.25,
        above_ema20_pct=60.0,
    )

    monkeypatch.setattr(gauge, "fetch_risk_indicators", lambda: tape_indicators)
    score = gauge.get_risk_score()

    # NSE weighted model with de-saturated breadth scaling: -48.3
    assert score == -48.3

    # Evaluate safety gates
    regime, vetoes, posture, integrity = gauge._evaluate_safety_gates(
        vix=tape_indicators.vix,
        vix_change=tape_indicators.vix_change_pct,
        btc_trend=tape_indicators.btc_trend,
        nifty_trend=tape_indicators.nifty_trend,
        usdjpy_trend=tape_indicators.usdjpy_trend,
        gold_trend=tape_indicators.gold_trend,
        india_vix=tape_indicators.india_vix,
        ad_ratio=tape_indicators.ad_ratio,
    )

    assert regime == "RISK_OFF"
    assert posture == "STAND_DOWN"
    assert "VOL_SHOCK" in vetoes
    assert "BREADTH_FLOOR" in vetoes
    assert "US_VIX_STRESS" in vetoes
    assert integrity == "UNCONFIRMED_VOL_SPIKE"


def test_vix_staleness_guards_and_spike_expiry():
    """Verify stale US VIX (>15m) cannot veto, and unconfirmed spike expires after 30m."""
    from datetime import timedelta
    gauge = RiskGauge()
    now = datetime.now(timezone.utc)

    # 1. Stale US VIX test: fetch time is 20 minutes ago
    vix_stale_time = now - timedelta(minutes=20)
    regime, vetoes, posture, integrity = gauge._evaluate_safety_gates(
        vix=35.89,  # High VIX would normally veto
        vix_change=105.2,  # Shock would normally veto
        btc_trend="RISING",
        nifty_trend="RISING",
        usdjpy_trend="FLAT",
        gold_trend="FLAT",
        india_vix=15.0,  # Normal India VIX
        ad_ratio=1.5,   # Healthy breadth
        vix_fetched_at=vix_stale_time,
        now=now,
    )

    # Stale US VIX must NOT veto and posture must NOT be STAND_DOWN!
    assert "US_VIX_STRESS" not in vetoes
    assert "VOL_SHOCK" not in vetoes
    assert integrity == "VIX_SOURCE_STALE"
    assert posture != "STAND_DOWN"

    # 2. Unconfirmed vol spike expiration: spike flag from 35 minutes ago
    spike_time = now - timedelta(minutes=35)
    regime2, vetoes2, posture2, integrity2 = gauge._evaluate_safety_gates(
        vix=20.0,
        vix_change=125.0,  # Massive spike
        btc_trend="RISING",
        nifty_trend="RISING",
        usdjpy_trend="FLAT",
        gold_trend="FLAT",
        india_vix=15.0,
        ad_ratio=1.5,
        spike_flag_time=spike_time,
        now=now,
    )
    assert integrity2 == "VOL_SPIKE_EXPIRED"
    assert "VOL_SHOCK" not in vetoes2


def test_regime_adjusted_risk_pct():
    """Verify position sizing scales down risk in RISK_OFF and up in RISK_ON."""
    base_risk = 1.0  # 1%

    risk_on = regime_adjusted_risk_pct(base_risk, regime="RISK_ON")
    neutral = regime_adjusted_risk_pct(base_risk, regime="NEUTRAL")
    risk_off = regime_adjusted_risk_pct(base_risk, regime="RISK_OFF")

    assert risk_on == 1.25
    assert neutral == 0.65
    assert risk_off == 0.35
    assert risk_off < neutral < risk_on


def test_intraday_calculate_position_size_regime_scaling():
    """Verify intraday position sizing reduces share quantity in RISK_OFF."""
    capital = 100000.0
    entry = 1000.0
    atr = 15.0

    qty_on = calculate_position_size(
        capital=capital, entry=entry, atr=atr, risk_pct=0.01, risk_regime="RISK_ON", max_single_trade_pct=0.50
    )
    qty_neutral = calculate_position_size(
        capital=capital, entry=entry, atr=atr, risk_pct=0.01, risk_regime="NEUTRAL", max_single_trade_pct=0.50
    )
    qty_off = calculate_position_size(
        capital=capital, entry=entry, atr=atr, risk_pct=0.01, risk_regime="RISK_OFF", max_single_trade_pct=0.50
    )

    assert qty_on > qty_neutral > qty_off
    # RISK_OFF quantity should be approximately 35% of RISK_ON
    assert qty_off <= int(qty_on * 0.45)


def test_can_take_trade_sector_concentration():
    """Verify can_take_trade blocks 3rd trade in same sector."""
    state_ok = {
        "open_trades": 2,
        "daily_pnl_pct": 0.0,
        "sector_positions": {"BANKING": 1, "IT": 1},
    }
    ok1, reason1 = can_take_trade(state_ok, symbol="SBIN", sector="BANKING")
    assert ok1 is True
    assert reason1 == "OK"

    state_full = {
        "open_trades": 2,
        "daily_pnl_pct": 0.0,
        "sector_positions": {"BANKING": 2, "IT": 0},
    }
    ok2, reason2 = can_take_trade(state_full, symbol="KOTAKBANK", sector="BANKING")
    assert ok2 is False
    assert "Sector concentration limit reached" in reason2


def test_paper_trading_sector_concentration_guardrail(tmp_path):
    """Verify paper trading engine blocks 3rd concurrent position in same sector."""
    engine = PaperTradingEngine(
        account_id="test_sector",
        initial_capital=100000.0,
        log_file=str(tmp_path / "trades.json"),
        state_key=f"test_sector_{uuid.uuid4().hex[:8]}",
    )

    # 1st Banking trade: HDFCBANK
    t1 = engine.execute(
        symbol="HDFCBANK",
        direction="LONG",
        entry=1000.0,
        sl=980.0,
        tp=1040.0,
        atr=10.0,
    )
    assert t1 is not None

    # 2nd Banking trade: ICICIBANK
    t2 = engine.execute(
        symbol="ICICIBANK",
        direction="LONG",
        entry=800.0,
        sl=784.0,
        tp=832.0,
        atr=8.0,
    )
    assert t2 is not None

    # 3rd Banking trade: SBIN (should be blocked by sector concentration guardrail)
    t3 = engine.execute(
        symbol="SBIN",
        direction="LONG",
        entry=600.0,
        sl=588.0,
        tp=624.0,
        atr=6.0,
    )
    assert t3 is None
    assert "Sector concentration limit reached for BANKING" in engine.last_rejection_reason

    # Trade in a different sector (e.g. IT - TCS) should still be allowed
    t_it = engine.execute(
        symbol="TCS",
        direction="LONG",
        entry=3000.0,
        sl=2940.0,
        tp=3120.0,
        atr=30.0,
    )
    assert t_it is not None


def test_score_arithmetic_reconciles_with_adjustments(tmp_path):
    """Verify that published indicators detail sum + adjustments strictly equals score (< 0.05)."""
    gauge = RiskGauge(cache_file=str(tmp_path / "risk_cache.json"))

    # Test 1: Full indicators with cautions
    ind = RiskIndicators(
        vix=26.5,  # > 25 -> caution
        vix_change_pct=7.2,
        vix_1d_change_pct=-4.3,
        btc_trend="FLAT",
        nifty_trend="FLAT",
        usdjpy_level=154.2,
        usdjpy_trend="FLAT",
        gold_trend="FLAT",
        risk_regime="NEUTRAL",
        india_vix=18.5,  # > 18 -> caution
        india_vix_change_pct=15.1,
        ad_ratio=0.65,  # < 0.75 -> caution
        above_ema20_pct=35.0,  # < 40 -> caution
        posture="OPEN_WITH_CAUTION",
        cautions=["INDIA_VIX_ELEVATED", "US_VIX_ELEVATED", "BREADTH_WEAK", "MOMENTUM_DIVERGENCE"],
    )

    breakdown = gauge.get_risk_breakdown(ind)
    score = breakdown["score"]
    published_sum = breakdown["published_sum"]
    adjustments = breakdown["adjustments"]
    weight_sum = breakdown["weight_sum"]

    assert weight_sum > 0
    adj_pts = sum(a.get("points", 0.0) for a in adjustments)
    # The user audit invariant: abs(published_sum + adjustments − score) < 0.05
    assert abs((published_sum + adj_pts) - score) < 0.05
    assert breakdown["score_reconciles"] is True

    # Test 2: Sub-100 weight sum (e.g. 96%)
    # Partial indicators (omitting USDJPY trend and Gold trend)
    ind_partial = RiskIndicators(
        vix=14.9,
        vix_change_pct=5.0,
        vix_1d_change_pct=-2.0,
        btc_trend="FLAT",
        nifty_trend="RISING",
        usdjpy_level=None,
        usdjpy_trend=None,
        gold_trend=None,
        risk_regime="RISK_ON",
        india_vix=14.2,
        india_vix_change_pct=-1.5,
        ad_ratio=1.4,
        above_ema20_pct=60.0,
        posture="OPEN",
        cautions=[],
    )
    bd_partial = gauge.get_risk_breakdown(ind_partial)
    score_p = bd_partial["score"]
    pub_p = bd_partial["published_sum"]
    adj_p = sum(a.get("points", 0.0) for a in bd_partial["adjustments"])
    assert abs((pub_p + adj_p) - score_p) < 0.05
    assert bd_partial["score_reconciles"] is True
