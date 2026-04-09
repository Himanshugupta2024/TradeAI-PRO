"""
Unit tests for backend robustness and error handling.

Covers:
  - Indicator calculation with edge cases
  - Data validation with various malformed inputs
  - Regime detection with different market patterns
  - Paper trading risk management
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.regime_detector import detect_regime, MarketRegime
from agents.execution.paper_trading import PaperTradingEngine
from agents.data_ingestion.validator import validate as validate_ohlcv


class TestIndicatorRobustness:
    """Test indicator calculation robustness."""
    
    def test_indicators_with_empty_dataframe(self):
        """Empty DataFrame should return empty without crashing."""
        df = pd.DataFrame()
        result = calculate_indicators(df)
        assert result.empty
    
    def test_indicators_with_minimal_data(self):
        """Minimal data (5 rows) should calculate without error."""
        df = pd.DataFrame({
            'timestamp': pd.date_range('2026-01-01', periods=5, freq='5min'),
            'open': [100, 101, 102, 103, 104],
            'high': [101, 102, 103, 104, 105],
            'low': [99, 100, 101, 102, 103],
            'close': [100.5, 101.5, 102.5, 103.5, 104.5],
            'volume': [1000, 1100, 1200, 1300, 1400],
        })
        
        result = calculate_indicators(df)
        assert not result.empty
        assert len(result) == len(df)
        assert 'RSI_14' in result.columns
    
    def test_indicators_with_nan_values(self):
        """Indicators should handle NaN gracefully."""
        df = pd.DataFrame({
            'timestamp': pd.date_range('2026-01-01', periods=50, freq='5min'),
            'open': [100.0] * 50,
            'high': [101.0] * 50,
            'low': [99.0] * 50,
            'close': [100.0] * 50,
            'volume': [1000.0] * 50,
        })
        df.loc[10, 'close'] = np.nan
        
        result = calculate_indicators(df)
        assert not result.empty
        # Should complete without crashing
        assert 'candle_consecutive' in result.columns
    
    def test_indicators_with_zero_volume(self):
        """Indicators like VWAP should handle zero volume."""
        df = pd.DataFrame({
            'timestamp': pd.date_range('2026-01-01', periods=30, freq='5min'),
            'open': np.random.uniform(100, 110, 30),
            'high': np.random.uniform(105, 115, 30),
            'low': np.random.uniform(95, 105, 30),
            'close': np.random.uniform(100, 110, 30),
            'volume': [0] * 30,
        })
        
        result = calculate_indicators(df)
        assert 'VWAP' in result.columns
        assert result['VWAP'].notna().any()


class TestRegimeDetection:
    """Test regime detection with various market patterns."""
    
    def _make_test_df(self, pattern: str) -> pd.DataFrame:
        """Create test DataFrame with specific pattern."""
        periods = 100
        if pattern == "uptrend":
            close = np.linspace(100, 150, periods)
        elif pattern == "downtrend":
            close = np.linspace(150, 100, periods)
        elif pattern == "ranging":
            close = np.sin(np.linspace(0, 4*np.pi, periods)) * 5 + 125
        else:
            close = 125 + np.random.randn(periods) * 2
        
        return pd.DataFrame({
            'timestamp': pd.date_range('2026-01-01', periods=periods, freq='1h'),
            'open': close - 0.5,
            'high': close + 2,
            'low': close - 2,
            'close': close,
            'volume': np.random.uniform(1000, 5000, periods),
        })
    
    def test_regime_detection_no_crash(self):
        """Regime detection should not crash on any pattern."""
        for pattern in ["uptrend", "downtrend", "ranging"]:
            df = self._make_test_df(pattern)
            df = calculate_indicators(df)
            regime = detect_regime(df)
            assert regime in [MarketRegime.TRENDING, MarketRegime.RANGING, MarketRegime.HIGH_VOLATILITY]


class TestDataValidation:
    """Test OHLCV data validation robustness."""
    
    def test_valid_ohlcv_data(self):
        """Valid data should validate without issues."""
        df = pd.DataFrame({
            'timestamp': pd.date_range('2026-01-01', periods=50, freq='1h'),
            'open': np.random.uniform(100, 110, 50),
            'high': np.random.uniform(110, 120, 50),
            'low': np.random.uniform(90, 100, 50),
            'close': np.random.uniform(100, 110, 50),
            'volume': np.random.uniform(1000, 10000, 50),
        })
        
        validated_df, report = validate_ohlcv(df, "TEST", "1h")
        assert report.is_safe
        assert report.quality_pct > 50
    
    def test_empty_ohlcv_data(self):
        """Empty DataFrame should fail gracefully."""
        df = pd.DataFrame({
            'timestamp': [], 'open': [], 'high': [],
            'low': [], 'close': [], 'volume': [],
        })
        validated_df, report = validate_ohlcv(df, "TEST", "1h")
        assert report is not None
        assert not report.is_safe  # Empty data should not be safe
        assert len(report.issues) > 0  # Should have at least one issue
    
    def test_validator_with_sparse_data(self):
        """Validator should handle sparse/incomplete data."""
        df = pd.DataFrame({
            'timestamp': pd.date_range('2026-01-01', periods=50, freq='1h'),
            'open': np.random.uniform(100, 110, 50),
            'high': np.random.uniform(110, 120, 50),
            'low': np.random.uniform(90, 100, 50),
            'close': np.random.uniform(100, 110, 50),
            'volume': [0.0] * 25 + [1000.0] * 25,
        })
        
        validated_df, report = validate_ohlcv(df, "TEST", "1h")
        assert report is not None
        assert isinstance(report.quality_pct, float)


class TestPaperTradingRiskManagement:
    """Test paper trading engine risk controls."""

    def test_trade_valid_execution(self, tmp_path):
        """Valid trade should execute."""
        log_file = tmp_path / "paper_trades.json"
        engine = PaperTradingEngine(initial_capital=100_000, log_file=str(log_file))
        
        trade = engine.execute(
            symbol="TEST",
            direction="LONG",
            entry=100.0,
            sl=95.0,
            tp=115.0,
            atr=0.0,
        )
        
        assert trade is not None
        assert trade.symbol == "TEST"
        assert trade.direction == "LONG"
        assert trade.status == "OPEN"
    
    def test_trade_min_risk_reward(self, tmp_path):
        """Trade with low RR should be rejected."""
        log_file = tmp_path / "paper_trades.json"
        engine = PaperTradingEngine(initial_capital=100_000, log_file=str(log_file))
        
        trade = engine.execute(
            symbol="TEST",
            direction="LONG",
            entry=100.0,
            sl=99.0,   # Risk = 1
            tp=100.5,  # Reward = 0.5 (RR too low)
            atr=0.0,
        )
        
        assert trade is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
