"""
Accuracy Guard Tests.

Regression suite that validates the key consistency fixes applied to the
prediction pipeline. Failures here indicate a regression in accuracy-critical
behaviour.

Guards:
  1. Signal vocabulary parity — generator, backtester, runner all use LONG/SHORT
  2. Session timezone correctness — NSE (IST) vs crypto (UTC)
  3. Training label alignment — 1R:2R outcome labels flow end-to-end
  4. Regime ATR percentile — correct parameter used (not bb_width_percentile_high)
  5. DataManager merge mode — gap-fill does not overwrite full history
  6. Daily report IST — daily report check uses IST not local time
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest
import asyncio
import inspect

from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.support_resistance import calculate_support_resistance
from agents.feature_engine.trend import classify_trend


# ── Shared helpers ───────────────────────────────────────────────────────────

def _make_ohlcv(rows: int = 300, seed: int = 42, trend: float = 0.05) -> pd.DataFrame:
    np.random.seed(seed)
    timestamps = pd.date_range("2023-01-01", periods=rows, freq="h")
    close = 100 + np.cumsum(np.random.randn(rows) * 0.5 + trend)
    return pd.DataFrame({
        "timestamp": timestamps,
        "open":   close + np.random.randn(rows) * 0.2,
        "high":   close + np.abs(np.random.randn(rows)) * 1.0,
        "low":    close - np.abs(np.random.randn(rows)) * 1.0,
        "close":  close,
        "volume": np.random.randint(5000, 50000, size=rows).astype(float),
    })


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    df = calculate_indicators(df)
    df = calculate_support_resistance(df)
    df = classify_trend(df)
    return df


# ── Guard 1: Signal vocabulary parity ───────────────────────────────────────

class TestSignalVocabularyParity:
    """LONG/SHORT must be the canonical signals throughout the stack."""

    def test_generator_emits_long_short(self):
        from agents.signal_engine.signal_generator import generate_signals
        df = _enrich(_make_ohlcv(400))
        result = generate_signals(df)
        valid_signals = {"LONG", "SHORT", "SKIP", "HOLD"}
        assert set(result["signal"].unique()).issubset(valid_signals), (
            f"Unexpected signal values: {result['signal'].unique()}"
        )
        # Critically, BUY/SELL must NOT appear
        assert "BUY" not in result["signal"].values
        assert "SELL" not in result["signal"].values

    def test_backtester_accepts_long_short(self):
        from agents.backtester.engine import Backtester
        n = 60
        timestamps = pd.date_range("2023-01-01", periods=n, freq="h")
        np.random.seed(7)
        close = 100 + np.cumsum(np.random.randn(n) * 0.3)
        df = pd.DataFrame({
            "timestamp": timestamps,
            "open": close, "high": close + 1,
            "low": close - 1, "close": close,
            "volume": np.ones(n) * 1000,
            "signal": "HOLD", "stop_loss": np.nan, "target": np.nan,
        })
        df.loc[5, "signal"] = "LONG"
        df.loc[5, "stop_loss"] = df.loc[5, "close"] - 2.0
        df.loc[5, "target"]   = df.loc[5, "close"] + 4.0

        result = Backtester(slippage_pct=0.0).run(df)
        assert result.total_trades >= 1, "Backtester should enter LONG trades"

    def test_backtester_does_not_accept_buy(self):
        from agents.backtester.engine import Backtester
        n = 30
        timestamps = pd.date_range("2023-01-01", periods=n, freq="h")
        close = np.ones(n) * 100
        df = pd.DataFrame({
            "timestamp": timestamps,
            "open": close, "high": close + 1,
            "low": close - 1, "close": close,
            "volume": np.ones(n) * 1000,
            "signal": "HOLD", "stop_loss": np.nan, "target": np.nan,
        })
        # Place a legacy BUY signal — should be ignored
        df.loc[5, "signal"] = "BUY"
        df.loc[5, "stop_loss"] = 98.0
        df.loc[5, "target"]   = 104.0
        result = Backtester(slippage_pct=0.0).run(df)
        assert result.total_trades == 0, "Backtester must not execute legacy BUY signals"

    def test_trade_result_direction_uses_long_short(self):
        from agents.backtester.engine import TradeResult
        sig = inspect.signature(TradeResult)
        # Verify the docstring / annotation still says LONG/SHORT not BUY/SELL
        src = inspect.getsource(TradeResult)
        assert "LONG" in src
        assert "BUY" not in src or "Legacy" in src or "BUY" not in src.split("direction")[1].split("\n")[0]


# ── Guard 2: Session timezone correctness ────────────────────────────────────

class TestSessionTimezones:
    def test_nse_session_uses_ist(self):
        from agents.feature_engine.session_features import get_session_quality
        # 10:00 AM IST — should be high quality (in 9:15–11:30 slot)
        ts_ist = pd.Timestamp("2024-03-04 10:00:00+05:30")
        q = get_session_quality(ts_ist, "NIFTY 50")
        assert q == 1.0, f"NSE 10am IST should be quality 1.0, got {q}"

    def test_nse_session_outside_hours_is_zero(self):
        from agents.feature_engine.session_features import get_session_quality
        # 8:00 AM IST — pre-market
        ts_ist = pd.Timestamp("2024-03-04 08:00:00+05:30")
        q = get_session_quality(ts_ist, "RELIANCE")
        assert q == 0.0, f"NSE pre-market should be quality 0.0, got {q}"

    def test_crypto_session_uses_utc(self):
        from agents.feature_engine.session_features import get_session_quality
        # 09:00 UTC — EU open window (8–12 UTC), quality 1.0
        ts_utc = pd.Timestamp("2024-03-04 09:00:00+00:00")
        q = get_session_quality(ts_utc, "BTC/USDT")
        assert q == 1.0, f"Crypto EU-open UTC should be quality 1.0, got {q}"

    def test_nse_utc_timestamp_converted_to_ist(self):
        from agents.feature_engine.session_features import get_session_quality
        # 04:45 UTC = 10:15 IST — NSE high-quality slot
        ts_utc = pd.Timestamp("2024-03-04 04:45:00+00:00")
        q = get_session_quality(ts_utc, "TCS")
        assert q == 1.0, f"04:45 UTC (=10:15 IST) NSE should be 1.0, got {q}"

    def test_nse_naive_timestamp_treated_as_ist(self):
        from agents.feature_engine.session_features import get_session_quality
        # Naive 10:00 — should be treated as IST for NSE symbols
        ts_naive = pd.Timestamp("2024-03-04 10:00:00")
        q = get_session_quality(ts_naive, "HDFCBANK")
        assert q == 1.0, f"Naive 10:00 for NSE should be treated as IST, got {q}"


# ── Guard 3: Training label alignment ────────────────────────────────────────

class TestTrainingLabelAlignment:
    def _get_signals_df(self) -> pd.DataFrame:
        """Return enriched df with signals populated."""
        from agents.signal_engine.signal_generator import generate_signals
        df = _enrich(_make_ohlcv(500, trend=0.1))
        return generate_signals(df)

    def test_outcome_labels_return_correct_shape(self):
        from agents.feature_engine.label_generator import label_1r2r_outcome
        df = self._get_signals_df()
        labels = label_1r2r_outcome(df)
        assert len(labels) == len(df)
        assert labels.dtype == "int8"
        assert set(labels.unique()).issubset({-1, 0, 1})

    def test_prepare_sequences_outcome_mode(self):
        from agents.ai_engine.trainer import prepare_sequences
        df = _enrich(_make_ohlcv(500, trend=0.1))
        from agents.signal_engine.signal_generator import generate_signals
        df = generate_signals(df)
        X, y, meta = prepare_sequences(df, seq_length=30, use_outcome_labels=True)
        assert meta["label_mode"] == "outcome_1r2r"
        if len(X) > 0:
            assert set(np.unique(y)).issubset({0, 1}), "y should be binary 0/1"

    def test_prepare_sequences_direction_mode(self):
        from agents.ai_engine.trainer import prepare_sequences
        df = _enrich(_make_ohlcv(400, trend=0.1))
        X, y, meta = prepare_sequences(df, seq_length=30, use_outcome_labels=False)
        assert meta["label_mode"] == "next_candle_direction"
        if len(X) > 0:
            assert set(np.unique(y)).issubset({0, 1})

    def test_outcome_labels_aligned_with_scorecard_logic(self):
        from agents.feature_engine.label_generator import label_1r2r_outcome, compute_label_stats
        df = self._get_signals_df()
        labels = label_1r2r_outcome(df)
        stats = compute_label_stats(labels)
        # Stats should be well-formed
        assert "win_rate" in stats
        assert 0.0 <= stats["win_rate"] <= 1.0


# ── Guard 4: Regime ATR percentile fix ──────────────────────────────────────

class TestRegimeATRPercentileFix:
    """Verify that detect_regime uses the correct percentile param for ATR."""

    def test_atr_and_bb_use_separate_percentiles(self):
        from agents.feature_engine.regime_detector import detect_regime
        import inspect
        src = inspect.getsource(detect_regime)
        # Check the fix: atr_pct should use atr_percentile_high, not bb_width_percentile_high
        lines = [l.strip() for l in src.split("\n") if "atr_pct" in l and "percentile" in l]
        assert lines, "Expected a line computing atr_pct with percentile"
        atr_line = lines[0]
        assert "atr_percentile_high" in atr_line, (
            f"atr_pct must use 'atr_percentile_high', found: {atr_line}"
        )

    def test_high_volatility_detected_with_correct_percentile(self):
        """With correct ATR percentile, extreme volatility should classify as HIGH_VOLATILITY."""
        from agents.feature_engine.regime_detector import detect_regime, MarketRegime
        np.random.seed(99)
        rows = 200
        ts = pd.date_range("2023-01-01", periods=rows, freq="h")
        close = 100 + np.cumsum(np.random.randn(rows) * 5.0)  # Very high vol
        df = pd.DataFrame({
            "timestamp": ts, "open": close,
            "high": close + np.abs(np.random.randn(rows)) * 8.0,
            "low":  close - np.abs(np.random.randn(rows)) * 8.0,
            "close": close,
            "volume": np.random.randint(1000, 50000, rows).astype(float),
        })
        df = calculate_indicators(df)
        regime = detect_regime(df)
        assert regime in (MarketRegime.HIGH_VOLATILITY, MarketRegime.TRENDING), (
            f"High-noise data should classify as HIGH_VOLATILITY or TRENDING, got {regime}"
        )


# ── Guard 5: DataManager merge mode ─────────────────────────────────────────

class TestDataManagerMerge:
    def test_load_history_clears_on_default(self):
        from agents.data_ingestion.data_manager import DataManager
        dm = DataManager()

        async def _run():
            ts = pd.date_range("2023-01-01", periods=10, freq="h")
            df1 = pd.DataFrame({
                "timestamp": ts, "open": 100.0, "high": 101.0,
                "low": 99.0, "close": 100.5, "volume": 1000.0
            })
            await dm.load_history("BTC/USDT", "1h", df1)
            assert len(dm._buffers["BTC/USDT"]["1h"]) == 10

            # Load a different (shorter) batch — should replace
            ts2 = pd.date_range("2023-01-02", periods=5, freq="h")
            df2 = pd.DataFrame({
                "timestamp": ts2, "open": 110.0, "high": 111.0,
                "low": 109.0, "close": 110.5, "volume": 2000.0
            })
            await dm.load_history("BTC/USDT", "1h", df2, merge=False)
            assert len(dm._buffers["BTC/USDT"]["1h"]) == 5, (
                "merge=False should clear and replace the buffer"
            )

        asyncio.run(_run())

    def test_load_history_merge_appends_without_overwrite(self):
        from agents.data_ingestion.data_manager import DataManager
        dm = DataManager()

        async def _run():
            ts1 = pd.date_range("2023-01-01", periods=10, freq="h")
            df1 = pd.DataFrame({
                "timestamp": ts1, "open": 100.0, "high": 101.0,
                "low": 99.0, "close": 100.5, "volume": 1000.0
            })
            await dm.load_history("BTC/USDT", "1h", df1)

            # Gap-fill with 5 NEW timestamps (should add to buffer)
            ts2 = pd.date_range("2023-01-01 10:00", periods=5, freq="h")
            df2 = pd.DataFrame({
                "timestamp": ts2, "open": 110.0, "high": 111.0,
                "low": 109.0, "close": 110.5, "volume": 2000.0
            })
            await dm.load_history("BTC/USDT", "1h", df2, merge=True)
            total = len(dm._buffers["BTC/USDT"]["1h"])
            assert total > 10, (
                f"merge=True should append new candles, got {total}"
            )

        asyncio.run(_run())

    def test_load_history_merge_deduplicates(self):
        from agents.data_ingestion.data_manager import DataManager
        dm = DataManager()

        async def _run():
            ts = pd.date_range("2023-01-01", periods=10, freq="h")
            df = pd.DataFrame({
                "timestamp": ts, "open": 100.0, "high": 101.0,
                "low": 99.0, "close": 100.5, "volume": 1000.0
            })
            await dm.load_history("BTC/USDT", "1h", df)
            # Merge same data again — no duplicates
            await dm.load_history("BTC/USDT", "1h", df, merge=True)
            assert len(dm._buffers["BTC/USDT"]["1h"]) == 10, (
                "merge should deduplicate identical timestamps"
            )

        asyncio.run(_run())


# ── Guard 6: Daily report IST check ─────────────────────────────────────────

class TestDailyReportIST:
    def test_should_send_daily_report_uses_ist(self):
        """_should_send_daily_report must compare against IST, not local time."""
        import inspect
        import autonomous_runner as ar
        src = inspect.getsource(ar._should_send_daily_report)
        assert "IST" in src or "Asia/Kolkata" in src or "5, minutes=30" in src, (
            "_should_send_daily_report must use IST timezone"
        )
        assert "datetime.now()" not in src.replace("datetime.now(tz=", ""), (
            "Must use datetime.now(tz=IST), not datetime.now() without timezone"
        )
