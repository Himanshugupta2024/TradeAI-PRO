"""
AI Prediction Engine — Training Pipeline.

Handles data preparation, sequence generation, normalization,
training with early stopping, class balancing, and model saving.

Supports walk-forward validation for time-series data.

Requires PyTorch — see lstm_model.py for compatibility notes.
"""

try:
    import torch
    import torch.nn as nn
    from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

import numpy as np
from utils.atomic_io import atomic_write_json
import pandas as pd
import json
import os
from datetime import datetime


# Feature columns used for LSTM input
FEATURE_COLS = [
    "RSI_14", "MACD", "MACD_signal", "MACD_hist",
    "EMA_20", "EMA_50", "EMA_200", "ATR_14",
    "ADX_14", "BB_upper", "BB_lower",
    "close", "volume",
]


def prepare_sequences(
    df: pd.DataFrame,
    seq_length: int = 60,
    target_col: str = "close",
    feature_cols: list[str] = None,
    target_threshold: float = 0.001,
    use_outcome_labels: bool = True,
) -> tuple:
    """
    Convert a DataFrame into input/target sequences for LSTM.

    Args:
        use_outcome_labels: When True (default), uses label_1r2r_outcome() as
            the training target — i.e., did this bar produce a profitable
            1:2 risk-reward trade within OUTCOME_FORWARD_BARS candles?
            (1=WIN → maps to 1, -1=LOSS → maps to 0; 0=TIMEOUT is dropped.)
            This aligns training with actual scorecard/backtest metrics.

            When False, falls back to next-candle direction classification
            (1=UP, 0=DOWN) filtered by target_threshold.

    Returns:
        X: np.array of shape (n_samples, seq_length, n_features)
        y: np.array of shape (n_samples,) — binary 0/1
        scaler_params: dict with mean/std for denormalization
    """
    if feature_cols is None:
        feature_cols = FEATURE_COLS

    # Only use features that exist in the DataFrame
    available_cols = [c for c in feature_cols if c in df.columns]
    if not available_cols:
        return np.array([]), np.array([]), {}

    df = df.copy().dropna(subset=available_cols)

    if len(df) < seq_length + 1:
        return np.array([]), np.array([]), {}

    # Extract feature matrix
    features = df[available_cols].values.astype(np.float64)

    # Normalize: z-score per feature
    mean = features.mean(axis=0)
    std = features.std(axis=0)
    std[std == 0] = 1.0  # Prevent division by zero
    features_norm = (features - mean) / std

    scaler_params = {
        "mean": mean.tolist(),
        "std": std.tolist(),
        "feature_cols": available_cols,
        "label_mode": "outcome_1r2r" if use_outcome_labels else "next_candle_direction",
    }

    X, y = [], []

    if use_outcome_labels:
        # Compute 1R:2R outcome labels aligned with actual trade results
        from agents.feature_engine.label_generator import label_1r2r_outcome
        outcome_labels = label_1r2r_outcome(df).values  # int8: 1, -1, 0

        for i in range(seq_length, len(features_norm)):
            label = int(outcome_labels[i])
            if label == 0:
                continue   # Timeout — not informative for training
            X.append(features_norm[i - seq_length : i])
            y.append(1 if label == 1 else 0)   # WIN=1, LOSS=0
    else:
        # Original: next-candle direction classification
        closes = df[target_col].values
        for i in range(seq_length, len(features_norm) - 1):
            ret = (closes[i + 1] - closes[i]) / closes[i]
            if abs(ret) < target_threshold:
                continue  # Skip noisy near-zero moves
            X.append(features_norm[i - seq_length : i])
            y.append(1 if ret > 0 else 0)

    if not X:
        return np.array([]), np.array([]), scaler_params

    return np.array(X), np.array(y), scaler_params


def walk_forward_split(
    X: np.ndarray,
    y: np.ndarray,
    n_splits: int = 5,
    train_ratio: float = 0.7,
) -> list[tuple]:
    """
    Walk-forward cross-validation splits for time-series data.

    Unlike k-fold, this preserves temporal ordering:
    each fold trains on older data and validates on newer data.

    Returns:
        List of (X_train, y_train, X_val, y_val) tuples
    """
    n = len(X)
    fold_size = n // n_splits
    splits = []

    for i in range(n_splits):
        val_end = n - (n_splits - i - 1) * fold_size
        val_start = val_end - fold_size
        train_end = val_start
        train_start = max(0, train_end - int(fold_size * train_ratio / (1 - train_ratio)))

        if train_end - train_start < 10 or val_end - val_start < 5:
            continue

        splits.append((
            X[train_start:train_end],
            y[train_start:train_end],
            X[val_start:val_end],
            y[val_start:val_end],
        ))

    return splits


if TORCH_AVAILABLE:

    class SequenceDataset(Dataset):
        """PyTorch Dataset for LSTM sequences."""

        def __init__(self, X: np.ndarray, y: np.ndarray):
            self.X = torch.FloatTensor(X)
            self.y = torch.LongTensor(y)

        def __len__(self):
            return len(self.X)

        def __getitem__(self, idx):
            return self.X[idx], self.y[idx]


    def train_model(
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: np.ndarray,
        y_val: np.ndarray,
        epochs: int = 50,
        batch_size: int = 64,
        learning_rate: float = 0.001,
        patience: int = 8,
        save_path: str = "models/",
        random_seed: int = 42,
    ) -> dict:
        """
        Train the LSTM model with early stopping and class balancing.

        Uses WeightedRandomSampler to handle class imbalance
        (markets often have more UP than DOWN periods or vice versa).

        Args:
            random_seed: Fixed seed for NumPy, Python random, and PyTorch so that
                repeated training runs on the same data produce stable, comparable
                metrics. Set to None to disable determinism.
        """
        from agents.ai_engine.lstm_model import TradingLSTM

        # ── Deterministic seeding ────────────────────────────────────────────
        if random_seed is not None:
            import random as _random
            _random.seed(random_seed)
            np.random.seed(random_seed)
            torch.manual_seed(random_seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(random_seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f"[AI] Training on: {device}")

        input_size = X_train.shape[2]
        model = TradingLSTM(input_size=input_size).to(device)

        train_dataset = SequenceDataset(X_train, y_train)
        val_dataset = SequenceDataset(X_val, y_val)

        # Class balancing via weighted sampler
        class_counts = np.bincount(y_train.astype(int), minlength=2)
        class_weights = 1.0 / np.maximum(class_counts, 1).astype(np.float64)
        sample_weights = class_weights[y_train.astype(int)]
        sampler = WeightedRandomSampler(
            weights=torch.DoubleTensor(sample_weights),
            num_samples=len(sample_weights),
            replacement=True,
        )

        print(f"[AI] Class distribution: DOWN={class_counts[0]}, UP={class_counts[1]}")

        train_loader = DataLoader(train_dataset, batch_size=batch_size, sampler=sampler)
        val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

        # Also use class-weighted loss
        loss_weights = torch.FloatTensor(class_weights / class_weights.sum()).to(device)
        criterion = nn.CrossEntropyLoss(weight=loss_weights)
        optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=1e-5)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, mode="min", factor=0.5, patience=3
        )

        best_val_loss = float("inf")
        epochs_no_improve = 0
        history = {"train_loss": [], "val_loss": [], "val_accuracy": []}

        for epoch in range(epochs):
            # ── Training ──
            model.train()
            train_loss = 0.0
            for X_batch, y_batch in train_loader:
                X_batch, y_batch = X_batch.to(device), y_batch.to(device)
                optimizer.zero_grad()
                outputs = model(X_batch)
                loss = criterion(outputs, y_batch)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()
                train_loss += loss.item()

            train_loss /= len(train_loader)

            # ── Validation ──
            model.eval()
            val_loss = 0.0
            correct = 0
            total = 0
            with torch.no_grad():
                for X_batch, y_batch in val_loader:
                    X_batch, y_batch = X_batch.to(device), y_batch.to(device)
                    outputs = model(X_batch)
                    loss = criterion(outputs, y_batch)
                    val_loss += loss.item()
                    _, predicted = torch.max(outputs, 1)
                    total += y_batch.size(0)
                    correct += (predicted == y_batch).sum().item()

            val_loss /= len(val_loader)
            val_acc = correct / total * 100

            history["train_loss"].append(train_loss)
            history["val_loss"].append(val_loss)
            history["val_accuracy"].append(val_acc)

            scheduler.step(val_loss)

            print(
                f"  Epoch {epoch+1:>3}/{epochs} | "
                f"Train Loss: {train_loss:.4f} | "
                f"Val Loss: {val_loss:.4f} | "
                f"Val Acc: {val_acc:.1f}%"
            )

            # ── Early Stopping ──
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                epochs_no_improve = 0
                os.makedirs(save_path, exist_ok=True)
                torch.save(model.state_dict(), os.path.join(save_path, "best_model.pt"))
                # Save metadata for reproducibility
                meta = {
                    "epoch": epoch + 1,
                    "val_loss": val_loss,
                    "val_accuracy": val_acc,
                    "input_size": input_size,
                    "class_distribution": class_counts.tolist(),
                    "trained_at": datetime.now().isoformat(),
                    "random_seed": random_seed,
                    "batch_size": batch_size,
                    "learning_rate": learning_rate,
                }
                atomic_write_json(os.path.join(save_path, "model_meta.json"), meta, indent=2)
            else:
                epochs_no_improve += 1
                if epochs_no_improve >= patience:
                    print(f"  Early stopping at epoch {epoch+1}")
                    break

        return {
            "best_val_loss": best_val_loss,
            "best_val_accuracy": max(history["val_accuracy"]),
            "epochs_trained": len(history["train_loss"]),
            "history": history,
        }

else:
    def train_model(*args, **kwargs):
        raise ImportError("PyTorch not available. Use fallback_predictor instead.")
