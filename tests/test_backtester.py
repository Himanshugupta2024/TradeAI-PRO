"""
Tests for the Backtesting Engine, Signal Generator, and Walk-Forward Validation.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from agents.backtester.engine import Backtester, BacktestResult
from agents.backtester.walk_forward import generate_windows, run_walk_forward
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.support_resistance import calculate_support_resistance
from agents.feature_engine.trend import classify_trend
from agents.signal_engine.signal_generator import generate_signals, signal_generator_for_backtest
from db.models import TrendDirection


# ── Helpers ──────────────────────────────────────────────

def _make_trending_data(rows: int = 500, trend: str = "up") -> pd.DataFrame:
    """Generate synthetic OHLCV with a clear trend."""
    np.random.seed(42)
    timestamps = pd.date_range("2023-01-01", periods=rows, freq="h")
    slope = 0.1 if trend == "up" else -0.1
    base = 100 + np.cumsum(np.random.randn(rows) * 0.5 + slope)
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": base + np.random.randn(rows) * 0.3,
        "high": base + np.abs(np.random.randn(rows)) * 1.0,
        "low": base - np.abs(np.random.randn(rows)) * 1.0,
        "close": base,
        "volume": np.random.randint(1000, 50000, size=rows).astype(float),
    })


def _make_signaled_df() -> pd.DataFrame:
    """Create a small DataFrame with explicit LONG/SHORT signals for testing."""
    np.random.seed(42)
    n = 50
    timestamps = pd.date_range("2023-06-01", periods=n, freq="h")
    close = 100 + np.cumsum(np.random.randn(n) * 0.3)
    high = close + 1
    low = close - 1

    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": close + 0.1,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.random.randint(1000, 10000, size=n).astype(float),
        "signal": "HOLD",
        "stop_loss": np.nan,
        "target": np.nan,
    })

    # Manually set signals at known rows
    df.loc[5, "signal"] = "LONG"
    df.loc[5, "stop_loss"] = df.loc[5, "close"] - 2.0
    df.loc[5, "target"] = df.loc[5, "close"] + 4.0

    df.loc[25, "signal"] = "LONG"
    df.loc[25, "stop_loss"] = df.loc[25, "close"] - 2.0
    df.loc[25, "target"] = df.loc[25, "close"] + 4.0

    return df


# ── Backtester Tests ─────────────────────────────────────

class TestBacktester:
    def test_no_signals_returns_empty_result(self):
        df = _make_trending_data(100)
        df["signal"] = "HOLD"
        bt = Backtester()
        result = bt.run(df)
        assert result.total_trades == 0

    def test_trades_are_recorded(self):
        df = _make_signaled_df()
        bt = Backtester(slippage_pct=0.0)
        result = bt.run(df)
        assert result.total_trades >= 1
        assert len(result.trades) >= 1

    def test_metrics_computed(self):
        df = _make_signaled_df()
        bt = Backtester(slippage_pct=0.0)
        result = bt.run(df)
        assert isinstance(result.win_rate, float)
        assert isinstance(result.sharpe_ratio, float)
        assert isinstance(result.max_drawdown, float)
        assert isinstance(result.profit_factor, float)

    def test_slippage_reduces_pnl(self):
        df = _make_signaled_df()
        result_no_slip = Backtester(slippage_pct=0.0).run(df)
        result_with_slip = Backtester(slippage_pct=0.1).run(df)

        # Slippage should make PnL worse (or equal)
        if result_no_slip.total_trades > 0 and result_with_slip.total_trades > 0:
            assert result_with_slip.total_pnl <= result_no_slip.total_pnl + 0.01

    def test_consecutive_loss_circuit_breaker(self):
        """After 3 consecutive losses, new signals should be skipped."""
        np.random.seed(99)
        n = 100
        timestamps = pd.date_range("2023-01-01", periods=n, freq="h")
        # Price that always drops — every BUY will hit SL
        close = 100 - np.arange(n) * 0.5
        df = pd.DataFrame({
            "timestamp": timestamps,
            "open": close,
            "high": close + 0.1,
            "low": close - 3.0,  # Low always hits SL
            "close": close,
            "volume": np.ones(n) * 1000,
            "signal": "HOLD",
            "stop_loss": np.nan,
            "target": np.nan,
        })
        # Place many LONG signals
        for i in range(0, 80, 10):
            df.loc[i, "signal"] = "LONG"
            df.loc[i, "stop_loss"] = df.loc[i, "close"] - 2.0
            df.loc[i, "target"] = df.loc[i, "close"] + 5.0

        bt = Backtester(max_consecutive_losses=3, slippage_pct=0.0)
        result = bt.run(df)
        # Should stop trading after 3 consecutive losses
        assert result.total_trades <= 4  # 3 losses + maybe 1 that enters before pause

    def test_empty_df(self):
        bt = Backtester()
        result = bt.run(pd.DataFrame())
        assert result.total_trades == 0

    def test_summary_string(self):
        df = _make_signaled_df()
        bt = Backtester()
        result = bt.run(df)
        s = result.summary()
        assert "Win Rate" in s
        assert "Sharpe Ratio" in s


# ── Walk-Forward Tests ───────────────────────────────────

class TestWalkForward:
    def test_window_generation(self):
        df = _make_trending_data(5000)  # ~208 days of hourly data
        windows = generate_windows(df, train_days=60, test_days=30, step_days=30)
        assert len(windows) >= 2
        for train, test in windows:
            assert not train.empty
            assert not test.empty
            assert train["timestamp"].iloc[-1] <= test["timestamp"].iloc[0]

    def test_empty_df_returns_no_windows(self):
        windows = generate_windows(pd.DataFrame(columns=["timestamp"]))
        assert len(windows) == 0


# ── Signal Generator Tests ───────────────────────────────

class TestSignalGenerator:
    def _enrich(self, df: pd.DataFrame) -> pd.DataFrame:
        """Run full feature pipeline on raw OHLCV."""
        df = calculate_indicators(df)
        df = calculate_support_resistance(df)
        df = classify_trend(df)
        return df

    def test_signal_columns_added(self):
        df = self._enrich(_make_trending_data(300))
        result = generate_signals(df)
        assert "signal" in result.columns
        assert "stop_loss" in result.columns
        assert "target" in result.columns

    def test_buy_signal_has_valid_sl_tp(self):
        df = self._enrich(_make_trending_data(500, trend="up"))
        result = generate_signals(df)
        buys = result[result["signal"] == "LONG"]
        if not buys.empty:
            for _, row in buys.iterrows():
                assert row["stop_loss"] < row["close"], "LONG SL should be below entry"
                assert row["target"] > row["close"], "LONG target should be above entry"
                rr = (row["target"] - row["close"]) / (row["close"] - row["stop_loss"])
                assert rr >= 1.9, f"Risk:Reward {rr:.1f} below 2.0"

    def test_sell_signal_has_valid_sl_tp(self):
        df = self._enrich(_make_trending_data(500, trend="down"))
        result = generate_signals(df)
        sells = result[result["signal"] == "SHORT"]
        if not sells.empty:
            for _, row in sells.iterrows():
                assert row["stop_loss"] > row["close"], "SHORT SL should be above entry"
                assert row["target"] < row["close"], "SHORT target should be below entry"

    def test_hold_is_default(self):
        df = self._enrich(_make_trending_data(300))
        result = generate_signals(df)
        # HOLD + SKIP are both non-actionable; most candles should be non-actionable
        non_action = ((result["signal"] == "HOLD") | (result["signal"] == "SKIP")).sum()
        assert non_action > len(result) * 0.5, "Most candles should be HOLD or SKIP"

    def test_callback_for_walk_forward(self):
        """signal_generator_for_backtest should return a DataFrame with signal columns."""
        df = self._enrich(_make_trending_data(300))
        train = df.iloc[:200]
        test = df.iloc[200:]
        result = signal_generator_for_backtest(train, test)
        assert "signal" in result.columns
        assert len(result) == len(test)
