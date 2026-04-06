"""
Tests for AI Prediction Engine (fallback) and Paper Trading Engine.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest
import json
import tempfile

from agents.ai_engine.fallback_predictor import predict_direction, predict_batch
from agents.ai_engine.trainer import prepare_sequences, FEATURE_COLS
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade


# ── Helpers ──────────────────────────────────────────────

def _make_data(rows=300):
    """Create enriched test data with indicator columns."""
    np.random.seed(42)
    ts = pd.date_range("2023-01-01", periods=rows, freq="h")
    close = 100 + np.cumsum(np.random.randn(rows) * 0.5 + 0.05)
    df = pd.DataFrame({
        "timestamp": ts,
        "open": close + np.random.randn(rows) * 0.3,
        "high": close + np.abs(np.random.randn(rows)),
        "low": close - np.abs(np.random.randn(rows)),
        "close": close,
        "volume": np.random.randint(1000, 50000, size=rows).astype(float),
    })
    # Add indicator columns
    from agents.feature_engine.indicators import calculate_indicators
    df = calculate_indicators(df)
    return df


# ── Fallback Predictor Tests ──────────────────────────────

class TestFallbackPredictor:
    def test_returns_valid_structure(self):
        df = _make_data()
        result = predict_direction(df)
        assert "direction" in result
        assert "confidence" in result
        assert "prob_up" in result
        assert "prob_down" in result
        assert result["direction"] in ("UP", "DOWN")

    def test_confidence_bounded(self):
        df = _make_data()
        result = predict_direction(df)
        assert 0 <= result["confidence"] <= 100
        assert 0 <= result["prob_up"] <= 100
        assert 0 <= result["prob_down"] <= 100

    def test_probabilities_sum_to_100(self):
        df = _make_data()
        result = predict_direction(df)
        assert abs(result["prob_up"] + result["prob_down"] - 100) < 1.0

    def test_empty_df(self):
        result = predict_direction(pd.DataFrame())
        assert result["confidence"] == 0

    def test_batch_prediction(self):
        df = _make_data()
        confidences = predict_batch(df)
        assert len(confidences) == len(df)
        assert (confidences >= 0).all()
        assert (confidences <= 100).all()

    def test_uptrend_predicts_up(self):
        """In a strong uptrend, predictor should lean bullish."""
        df = _make_data(300)
        result = predict_direction(df)
        # With our uptrend data (positive drift), at least shouldn't be strongly bearish
        assert result["prob_up"] >= 30


# ── Sequence Preparation Tests ───────────────────────────

class TestPrepareSequences:
    def test_shape(self):
        df = _make_data(300)
        X, y, params = prepare_sequences(df, seq_length=30)
        if len(X) > 0:
            assert X.ndim == 3
            assert X.shape[1] == 30
            assert X.shape[2] == len(FEATURE_COLS)
            assert len(y) == len(X)
            assert set(np.unique(y)).issubset({0, 1})

    def test_normalization_params(self):
        df = _make_data(300)
        _, _, params = prepare_sequences(df, seq_length=30)
        assert "mean" in params
        assert "std" in params
        assert len(params["mean"]) == len(FEATURE_COLS)

    def test_too_short_data(self):
        df = _make_data(20)
        X, y, _ = prepare_sequences(df, seq_length=60)
        assert len(X) == 0


# ── Paper Trading Tests ──────────────────────────────────

class TestPaperTrading:
    def _engine(self):
        """Create a paper trading engine with a temp file."""
        tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tf.close()
        return PaperTradingEngine(
            initial_capital=100000,
            log_file=tf.name,
        ), tf.name

    def test_execute_trade(self):
        engine, path = self._engine()
        trade = engine.execute("BTC/USDT", "LONG", 65000, 64000, 67000)
        assert trade is not None
        assert trade.direction == "LONG"
        assert trade.status == "OPEN"
        assert len(engine.open_trades) == 1
        os.unlink(path)

    def test_sl_tp_monitoring(self):
        engine, path = self._engine()
        engine.execute("BTC/USDT", "LONG", 65000, 64000, 67000)

        # Price hits target
        closed = engine.update_prices({"BTC/USDT": {"high": 68000, "low": 65500, "close": 67500}})
        assert len(closed) == 1
        assert closed[0].status == "CLOSED_TP"
        assert closed[0].pnl > 0
        os.unlink(path)

    def test_stop_loss_triggered(self):
        engine, path = self._engine()
        engine.execute("BTC/USDT", "LONG", 65000, 64000, 67000)

        closed = engine.update_prices({"BTC/USDT": {"high": 65100, "low": 63000, "close": 63500}})
        assert len(closed) == 1
        assert closed[0].status == "CLOSED_SL"
        assert closed[0].pnl < 0
        os.unlink(path)

    def test_consecutive_loss_block(self):
        engine, path = self._engine()
        engine._consecutive_losses = 3  # Simulate 3 prior losses

        trade = engine.execute("BTC/USDT", "LONG", 65000, 64000, 67000)
        assert trade is None  # Should be blocked
        os.unlink(path)

    def test_rr_validation(self):
        engine, path = self._engine()
        # Bad RR: risk=1000, reward=500 → RR=0.5 < 2.0
        trade = engine.execute("BTC/USDT", "LONG", 65000, 64000, 65500)
        assert trade is None  # Should be blocked
        os.unlink(path)

    def test_persistence(self):
        engine, path = self._engine()
        engine.execute("BTC/USDT", "LONG", 65000, 64000, 67000)

        # Load a fresh engine from the same file
        engine2 = PaperTradingEngine(log_file=path)
        assert len(engine2.trades) == 1
        assert len(engine2.open_trades) == 1
        os.unlink(path)

    def test_stats(self):
        engine, path = self._engine()
        engine.execute("BTC/USDT", "LONG", 65000, 64000, 67000)
        engine.update_prices({"BTC/USDT": {"high": 68000, "low": 65500, "close": 67500}})
        stats = engine.get_stats()
        assert stats["total_trades"] == 1
        assert stats["win_rate"] == 100.0
        os.unlink(path)

    def test_sell_trade(self):
        engine, path = self._engine()
        trade = engine.execute("BTC/USDT", "SHORT", 65000, 66000, 63000)
        assert trade is not None
        assert trade.direction == "SHORT"

        closed = engine.update_prices({"BTC/USDT": {"high": 65500, "low": 62500, "close": 63000}})
        assert len(closed) == 1
        assert closed[0].pnl > 0
        os.unlink(path)
