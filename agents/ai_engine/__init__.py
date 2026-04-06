"""
AI Prediction Engine package.

Provides unified interface regardless of whether PyTorch is available.
Falls back to statistical predictor on Python 3.14+.
"""

from agents.ai_engine.fallback_predictor import predict_direction, predict_batch  # noqa: F401

try:
    from agents.ai_engine.lstm_model import TradingLSTM, TORCH_AVAILABLE  # noqa: F401
    from agents.ai_engine.trainer import prepare_sequences, train_model  # noqa: F401
except ImportError:
    TORCH_AVAILABLE = False
