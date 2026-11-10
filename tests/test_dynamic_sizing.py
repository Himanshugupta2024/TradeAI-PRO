"""Dynamic position sizing: regime × ATR vol-targeting × fractional Kelly.

Covers the pure planner in ``agents.execution.position_sizing`` and the engine
wiring that actually applies it (``risk_pct_applied`` on the opened trade).
"""

import os
import sys
import tempfile
import uuid
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agents.execution.paper_trading import PaperTrade, PaperTradingEngine
from agents.execution.position_sizing import (
    compute_risk_pct,
    edge_stats_from_pnls,
    regime_adjusted_risk_pct,
)


# ── edge_stats_from_pnls ──────────────────────────────────────────────────


class TestEdgeStats:
    def test_returns_none_below_min_sample(self):
        pnls = [100.0, -50.0] * 10  # 20 decided trades
        assert edge_stats_from_pnls(pnls, lookback=60, min_sample=30) is None

    def test_returns_none_when_one_side_missing(self):
        assert edge_stats_from_pnls([100.0] * 40, min_sample=30) is None
        assert edge_stats_from_pnls([-100.0] * 40, min_sample=30) is None

    def test_ignores_breakeven_and_non_numeric(self):
        pnls = [0.0] * 20 + ["nope", None] + [100.0] * 15 + [-50.0] * 15
        stats = edge_stats_from_pnls(pnls, lookback=60, min_sample=30)
        assert stats is not None
        assert stats["sample"] == 30

    def test_math_is_correct(self):
        pnls = [100.0] * 20 + [-50.0] * 20
        stats = edge_stats_from_pnls(pnls, lookback=60, min_sample=30)
        assert stats["sample"] == 40
        assert stats["win_rate"] == pytest.approx(0.5)
        assert stats["avg_win"] == pytest.approx(100.0)
        assert stats["avg_loss"] == pytest.approx(50.0)
        assert stats["payoff_ratio"] == pytest.approx(2.0)
        assert stats["expectancy"] == pytest.approx(25.0)
        assert stats["profit_factor"] == pytest.approx(2.0)

    def test_lookback_keeps_only_recent_trades(self):
        # Recent 40 are balanced; the two ancient outliers must be ignored.
        pnls = [1000.0, -1000.0] + [100.0] * 20 + [-50.0] * 20
        stats = edge_stats_from_pnls(pnls, lookback=40, min_sample=30)
        assert stats["sample"] == 40
        assert stats["win_rate"] == pytest.approx(0.5)
        assert stats["avg_win"] == pytest.approx(100.0)


# ── compute_risk_pct ──────────────────────────────────────────────────────


class TestComputeRiskPct:
    def test_base_only(self):
        plan = compute_risk_pct(1.25)
        assert plan["risk_pct"] == pytest.approx(1.25)
        assert plan["binding"] == "base"
        assert plan["kelly_cap_pct"] is None

    def test_regime_high_volatility_halves(self):
        plan = compute_risk_pct(1.25, regime="HIGH_VOLATILITY")
        assert plan["risk_pct"] == pytest.approx(0.625)
        assert plan["binding"] == "regime"

    def test_regime_accepts_lowercase_string(self):
        assert regime_adjusted_risk_pct(2.0, "high_volatility") == pytest.approx(1.0)
        assert regime_adjusted_risk_pct(2.0, "TRENDING") == pytest.approx(2.5)

    def test_atr_doubling_halves_risk(self):
        plan = compute_risk_pct(2.0, current_atr=20.0, historical_atr=10.0)
        assert plan["vol_pct"] == pytest.approx(1.0)
        assert plan["risk_pct"] == pytest.approx(1.0)
        assert plan["binding"] == "volatility"

    def test_atr_ratio_is_clamped(self):
        # 10x expansion must not shrink risk below the 3.0x cap.
        plan = compute_risk_pct(3.0, current_atr=100.0, historical_atr=10.0)
        assert plan["risk_pct"] == pytest.approx(1.0)

    def test_kelly_cap_floors_instead_of_zeroing(self):
        plan = compute_risk_pct(
            1.25,
            kelly_fraction=0.0,
            kelly_fractionality=0.5,
            kelly_floor_ratio=0.25,
        )
        assert plan["kelly_cap_pct"] == pytest.approx(0.3125)
        assert plan["risk_pct"] == pytest.approx(0.3125)
        assert plan["binding"] == "kelly"

    def test_kelly_never_raises_risk(self):
        # A 45% cap is far above the 1.25% base — risk must stay at base.
        plan = compute_risk_pct(1.25, kelly_fraction=0.9, kelly_fractionality=0.5)
        assert plan["risk_pct"] == pytest.approx(1.25)
        assert plan["binding"] == "base"

    def test_kelly_none_disables_cap(self):
        plan = compute_risk_pct(1.25, kelly_fraction=None)
        assert plan["kelly_cap_pct"] is None
        assert plan["risk_pct"] == pytest.approx(1.25)

    def test_hard_ceiling_applies_after_scaling(self):
        plan = compute_risk_pct(2.0, regime="TRENDING", max_risk_pct=2.0)
        assert plan["regime_pct"] == pytest.approx(2.5)
        assert plan["risk_pct"] == pytest.approx(2.0)
        assert plan["binding"] == "max_cap"

    def test_invalid_base_is_rejected(self):
        for base in (0.0, -1.0, float("nan")):
            plan = compute_risk_pct(base)
            assert plan["risk_pct"] == 0.0
            assert plan["binding"] == "invalid_base"


# ── Engine wiring ─────────────────────────────────────────────────────────


def _engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    engine = PaperTradingEngine(
        initial_capital=100_000.0,
        log_file=tmp.name,
        state_key=f"test_dynamic_sizing_{uuid.uuid4().hex[:8]}",
    )
    return engine, tmp.name


def _closed_trade(idx: int, pnl: float) -> PaperTrade:
    return PaperTrade(
        id=10_000 + idx,
        symbol="TCS",
        direction="LONG",
        entry_price=3500.0,
        stop_loss=3450.0,
        target=3600.0,
        entry_time="2026-11-01T09:30:00+05:30",
        exit_time="2026-11-01T15:00:00+05:30",
        status="CLOSED_TP",
        quantity=0.0,
        total_pnl=pnl,
    )


class TestEngineSizing:
    def test_opens_at_base_risk_without_history(self):
        engine, path = _engine()
        try:
            trade = engine.execute(
                symbol="RELIANCE",
                direction="LONG",
                entry=2500.0,
                sl=2470.0,
                tp=2600.0,
                signal_type="MOMENTUM_BREAKOUT",
                regime="UNKNOWN",
                confidence=70.0,
            )
            assert trade is not None
            assert trade.risk_pct_applied == pytest.approx(1.25)
        finally:
            os.unlink(path)

    def test_kelly_guard_shrinks_risk_on_thin_edge(self):
        engine, path = _engine()
        try:
            # 20 wins × ₹100 and 10 losses × ₹200 → f* = 0. Half-Kelly floor applies.
            engine.trades.extend(_closed_trade(i, 100.0) for i in range(20))
            engine.trades.extend(_closed_trade(20 + i, -200.0) for i in range(10))

            trade = engine.execute(
                symbol="RELIANCE",
                direction="LONG",
                entry=2500.0,
                sl=2470.0,
                tp=2600.0,
                signal_type="MOMENTUM_BREAKOUT",
                regime="UNKNOWN",
                confidence=70.0,
            )
            assert trade is not None
            assert trade.risk_pct_applied == pytest.approx(0.3125)
        finally:
            os.unlink(path)

    def test_atr_expansion_halves_risk(self):
        engine, path = _engine()
        try:
            engine._atr_history["RELIANCE"] = deque([10.0] * 12, maxlen=60)

            trade = engine.execute(
                symbol="RELIANCE",
                direction="LONG",
                entry=2500.0,
                sl=2470.0,
                tp=2600.0,
                atr=20.0,
                signal_type="MOMENTUM_BREAKOUT",
                regime="UNKNOWN",
                confidence=70.0,
            )
            assert trade is not None
            assert trade.risk_pct_applied == pytest.approx(0.625)
        finally:
            os.unlink(path)

    def test_regime_and_atr_compose(self):
        engine, path = _engine()
        try:
            engine._atr_history["RELIANCE"] = deque([10.0] * 12, maxlen=60)

            trade = engine.execute(
                symbol="RELIANCE",
                direction="LONG",
                entry=2500.0,
                sl=2470.0,
                tp=2600.0,
                atr=20.0,
                signal_type="MOMENTUM_BREAKOUT",
                regime="HIGH_VOLATILITY",
                confidence=70.0,
            )
            assert trade is not None
            # 1.25 × 0.5 (regime) ÷ 2.0 (ATR) = 0.3125
            assert trade.risk_pct_applied == pytest.approx(0.3125)
        finally:
            os.unlink(path)

    def test_update_prices_records_atr_for_symbols_without_trades(self):
        engine, path = _engine()
        try:
            engine.update_prices(
                {"TCS": {"high": 101.0, "low": 99.0, "close": 100.0}},
                atr_values={"TCS": 3.5},
            )
            assert list(engine._atr_history["TCS"]) == pytest.approx([3.5])
        finally:
            os.unlink(path)

    def test_update_prices_dedupes_repeated_bar_atr(self):
        engine, path = _engine()
        try:
            price = {"high": 101.0, "low": 99.0, "close": 100.0, "timestamp": "2026-11-10T09:15:00+05:30"}
            for _ in range(5):
                engine.update_prices({"TCS": price}, atr_values={"TCS": 3.5})
            assert list(engine._atr_history["TCS"]) == pytest.approx([3.5])
        finally:
            os.unlink(path)

    def test_record_atr_ignores_invalid_values(self):
        engine, path = _engine()
        try:
            engine._record_atr("TCS", 0.0)
            engine._record_atr("TCS", -2.0)
            engine._record_atr("TCS", float("nan"))
            assert "TCS" not in engine._atr_history
        finally:
            os.unlink(path)

    def test_rolling_atr_mean_needs_min_samples(self):
        engine, path = _engine()
        try:
            engine._atr_history["TCS"] = deque([5.0] * 5, maxlen=60)
            assert engine._rolling_atr_mean("TCS") == 0.0
        finally:
            os.unlink(path)
