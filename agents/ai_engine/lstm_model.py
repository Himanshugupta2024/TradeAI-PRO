"""
AI Prediction Engine — LSTM Model Definition.

This module defines the PyTorch LSTM architecture for time-series prediction.
It requires PyTorch (pip install torch) which needs Python <= 3.12.

When PyTorch is unavailable (Python 3.14), the system falls back to
the statistical predictor in `fallback_predictor.py`.
"""

try:
    import torch
    import torch.nn as nn
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False


if TORCH_AVAILABLE:

    class TradingLSTM(nn.Module):
        """
        LSTM model for predicting trade direction.

        Input:  Sequence of feature vectors (RSI, MACD, EMAs, ATR, etc.)
        Output: [P(up), P(down)] probabilities + confidence score
        """

        def __init__(
            self,
            input_size: int = 10,
            hidden_size: int = 128,
            num_layers: int = 2,
            dropout: float = 0.4,
        ):
            super().__init__()

            self.hidden_size = hidden_size
            self.num_layers = num_layers

            self.lstm = nn.LSTM(
                input_size=input_size,
                hidden_size=hidden_size,
                num_layers=num_layers,
                batch_first=True,
                dropout=dropout if num_layers > 1 else 0.0,
            )

            self.dropout = nn.Dropout(dropout)

            # Classification head: UP or DOWN
            self.fc = nn.Sequential(
                nn.Linear(hidden_size, 64),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(64, 2),
            )

        def forward(self, x):
            """
            Args:
                x: Tensor of shape (batch, seq_len, input_size)
            Returns:
                logits: Tensor of shape (batch, 2) — [logit_up, logit_down]
            """
            lstm_out, _ = self.lstm(x)
            # Use the last timestep's hidden state
            last_hidden = lstm_out[:, -1, :]
            last_hidden = self.dropout(last_hidden)
            logits = self.fc(last_hidden)
            return logits

        def get_embeddings(self, x):
            """
            Args:
                x: Tensor of shape (batch, seq_len, input_size)
            Returns:
                last_hidden: Tensor of shape (batch, hidden_size)
            """
            self.eval()
            with torch.inference_mode():
                lstm_out, _ = self.lstm(x)
                last_hidden = lstm_out[:, -1, :]
                return last_hidden

        def predict(self, x):
            """
            Inference: returns direction and confidence.

            Returns:
                dict: {"direction": "UP"/"DOWN", "confidence": 0-100}
            """
            self.eval()
            with torch.inference_mode():
                logits = self.forward(x)
                probs = torch.softmax(logits, dim=1)
                confidence, predicted = torch.max(probs, dim=1)

                direction = "UP" if predicted.item() == 0 else "DOWN"
                return {
                    "direction": direction,
                    "confidence": round(confidence.item() * 100, 1),
                    "prob_up": round(probs[0, 0].item() * 100, 1),
                    "prob_down": round(probs[0, 1].item() * 100, 1),
                }



_LSTM_CACHE = {}
_TORCH_CONFIGURED = False


def get_lstm_model(input_size: int, model_path: str = "models/best_model.pt"):
    """
    Return a cached singleton instance of TradingLSTM with weights loaded once.
    Enforces single-thread CPU execution for Render Free Tier constraint.
    """
    if not TORCH_AVAILABLE:
        return None

    global _TORCH_CONFIGURED
    if not _TORCH_CONFIGURED:
        try:
            torch.set_num_threads(1)
            torch.set_num_interop_threads(1)
        except Exception:
            pass
        _TORCH_CONFIGURED = True

    cache_key = (model_path, input_size)
    if cache_key in _LSTM_CACHE:
        return _LSTM_CACHE[cache_key]

    if not os.path.exists(model_path):
        return None

    try:
        model = TradingLSTM(input_size=input_size)
        state = torch.load(model_path, map_location="cpu", weights_only=True)
        model.load_state_dict(state)
        model.eval()
        _LSTM_CACHE[cache_key] = model
        return model
    except Exception:
        return None
