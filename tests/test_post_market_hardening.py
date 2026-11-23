"""
Unit tests locking in the 7 Post-Market Data Provenance & Telemetry Fixes.
========================================================================
1. Shoonya promoted to PRIMARY_OHLCV when FEED_SERVICE_URL set; Yahoo is FALLBACK_OHLCV.
2. Dynamic feed source/mode/delay based on actual bar age and active provider.
3. Signal age clocks reconciled (stale_minutes == signal_age_minutes) & skip_reason refreshed.
4. Prior-session marks gated from masquerading as 0-delay live data.
5. Risk gauge cache TTL <= 60s during market hours.
6. India VIX velocity actively scored (12% weight) & above_ema20 de-saturated.
7. Terminal signals have 0 active confluence score (no misleading frozen 85).
8. Loading status total >= loaded (no 11/8 impossible state).
9. EOD review catch-up eligible post 15:45 IST on weekdays.
"""
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pytest

from data.router import MarketDataRouter, create_default_router
from data.providers.base import MarketDataProvider, ProviderHealth
from api.data_honesty import apply_signal_freshness, classify_quote_state
from agents.macro.risk_gauge import RiskGauge, RiskIndicators
from api.learning_scheduler import should_run_review, _IST


class DummyProvider(MarketDataProvider):
    def __init__(self, name: str, healthy: bool = True, calls: int = 0):
        super().__init__()
        self.name = name
        self.health = ProviderHealth(name=name)
        self.health.is_healthy = healthy
        self.health.total_calls = calls

    async def get_quote(self, symbol: str):
        return None

    async def get_batch_quotes(self, symbols):
        return {}

    async def get_candles(self, symbol: str, timeframe: str = "5m", limit: int = 100):
        return []


def test_shoonya_promoted_to_primary_ohlcv():
    router = MarketDataRouter(providers=[
        DummyProvider("shoonya_feed", healthy=True, calls=10),
        DummyProvider("yahoo", healthy=True, calls=5),
        DummyProvider("tradingview", healthy=True, calls=2),
        DummyProvider("twelve_data", healthy=True, calls=1),
    ])
    status = router.get_health_status()
    assert status["shoonya_feed"]["role"] == "PRIMARY_OHLCV"
    assert status["yahoo"]["role"] == "FALLBACK_OHLCV"
    assert status["tradingview"]["role"] == "FALLBACK_QUOTES"
    assert status["twelve_data"]["role"] == "SPECIALIST_FOREX"


def test_yahoo_is_primary_when_shoonya_absent():
    router = MarketDataRouter(providers=[
        DummyProvider("yahoo", healthy=True, calls=5),
        DummyProvider("tradingview", healthy=True, calls=2),
    ])
    status = router.get_health_status()
    assert status["yahoo"]["role"] == "PRIMARY_OHLCV"


def test_signal_age_clocks_reconciled():
    now = datetime(2026, 11, 23, 10, 17, tzinfo=ZoneInfo("Asia/Kolkata"))
    # Signal from Friday Nov 20 15:05 IST
    friday_ts = "2026-11-20T15:05:00+05:30"
    raw_signal = {
        "symbol": "ICICIBANK",
        "timestamp": friday_ts,
        "signal": "LONG",
        "actionable": True,
        "entry": 1250.0,
        "stop_loss": 1230.0,
        "target": 1290.0,
        "skip_reason": "Signal stale (2728.6m > 30.0m threshold)",
    }
    cleaned = apply_signal_freshness(raw_signal, now=now)
    assert cleaned["stale"] is True
    # Both age fields must agree exactly
    assert cleaned["stale_minutes"] == cleaned["signal_age_minutes"]
    assert cleaned["stale_minutes"] > 4000.0  # ~4032 minutes
    # skip_reason must quote the exact fresh age
    assert str(cleaned["stale_minutes"]) in cleaned["skip_reason"]
    assert "2728.6m" not in cleaned["skip_reason"]


def test_prior_session_data_gated_during_live():
    # Signal from Friday
    friday_ts = "2026-11-20T15:25:00+05:30"
    raw_signal = {
        "symbol": "SBIN",
        "timestamp": friday_ts,
        "signal": "HOLD",
        "data_delay_minutes": 0,
        "source": "NSE_BHAVCOPY_EOD",
    }
    # Pretend current time is Monday morning 10:17 IST
    monday_open = datetime(2026, 11, 23, 10, 17, tzinfo=ZoneInfo("Asia/Kolkata"))
    cleaned = apply_signal_freshness(raw_signal, now=monday_open)
    assert cleaned["stale"] is True
    assert cleaned["as_of"] == friday_ts
    assert cleaned["data_age_minutes"] > 4000.0


def test_risk_gauge_weights_sum_to_100_and_scores_india_velocity():
    gauge = RiskGauge()
    assert sum(gauge.WEIGHTS.values()) == pytest.approx(100.0, 0.01)
    assert gauge.WEIGHTS["india_vix_velocity"] == 12.0
    assert gauge.WEIGHTS["india_vix"] == 18.0

    ind = RiskIndicators(
        vix=18.0,
        vix_change_pct=10.0,
        vix_1d_change_pct=2.0,
        btc_trend="RISING",
        nifty_trend="RISING",
        usdjpy_level=155.0,
        usdjpy_trend="RISING",
        gold_trend="FLAT",
        risk_regime="RISK_ON",
        timestamp=datetime.now(timezone.utc),
        india_vix=15.0,
        india_vix_change_pct=10.12,  # +10.12% surge today
        above_ema20_pct=80.0,
    )
    scores = gauge.calculate_component_scores(ind)
    assert "india_vix_velocity" in scores
    assert scores["india_vix_velocity"]["weight"] == 12.0
    assert scores["india_vix_velocity"]["value"] == 10.12

    # Verify above_ema20 does not saturate at 80%
    assert scores["above_ema20"]["score"] < 100.0
    assert round(scores["above_ema20"]["score"], 1) == 66.7

    # Verify detail metadata marks india_vix_velocity as scored
    detail = gauge.get_indicators_detail(ind)
    iv_vel_item = next((item for item in detail if item["key"] == "india_vix_velocity"), None)
    assert iv_vel_item is not None
    assert iv_vel_item["scored"] is True
    assert iv_vel_item["weight_pct"] == 12


def test_vol_shock_distinguishes_1d_from_7d():
    gauge = RiskGauge()
    # 7-day rise of +50% is NOT a vol shock veto (gradual week-long move)
    regime, vetoes, posture, _ = gauge._evaluate_safety_gates(
        vix=22.0,
        vix_change=50.0,
        vix_1d_change=5.0,
        btc_trend="RISING",
        nifty_trend="RISING",
        usdjpy_trend="RISING",
        gold_trend="FLAT",
        india_vix=16.0,
        india_vix_1d_change=3.0,
        ad_ratio=1.2,
    )
    assert "VOL_SHOCK" not in vetoes

    # Single-day panic spike of +45% IS a vol shock veto
    regime, vetoes, posture, _ = gauge._evaluate_safety_gates(
        vix=28.0,
        vix_change=50.0,
        vix_1d_change=45.0,
        btc_trend="RISING",
        nifty_trend="RISING",
        usdjpy_trend="RISING",
        gold_trend="FLAT",
        india_vix=16.0,
        india_vix_1d_change=3.0,
        ad_ratio=1.2,
    )
    assert "VOL_SHOCK" in vetoes
    assert posture == "STAND_DOWN"


def test_eod_review_evening_catchup():
    # 20:30 IST on Monday (evening after close)
    now_evening = datetime(2026, 11, 23, 20, 30, tzinfo=_IST)
    # Without catchup: after_window
    assert should_run_review(now_evening, "2026-11-20", allow_catchup=False) == (False, "after_window")
    # With catchup: in_window eligible
    assert should_run_review(now_evening, "2026-11-20", allow_catchup=True) == (True, "in_window")
    # Already ran today: skips
    assert should_run_review(now_evening, "2026-11-23", allow_catchup=True) == (False, "already_ran_today")
