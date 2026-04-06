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

        def predict(self, x):
            """
            Inference: returns direction and confidence.

            Returns:
                dict: {"direction": "UP"/"DOWN", "confidence": 0-100}
            """
            self.eval()
            with torch.no_grad():
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

else:
    # Stub class when PyTorch is not available
    class TradingLSTM:
        def __init__(self, *args, **kwargs):
            raise ImportError(
                "PyTorch is not available on Python 3.14+. "
                "Install Python 3.12 and run: pip install torch"
            )
