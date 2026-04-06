"""
LSTM Model Training Script — Requires Python 3.12 + PyTorch.

Run this ONLY from the venv_torch environment:

    # Activate the PyTorch virtual environment first
    venv_torch\\Scripts\\activate

    # Then run this script
    python train_lstm.py
    python train_lstm.py --symbol BTC/USDT --timeframe 1h --epochs 80
    python train_lstm.py --all-symbols

The trained model is saved to models/best_model.pt + models/model_meta.json
and is automatically used by the ensemble predictor.
"""

import argparse
import os
import sys

# Check Python version before importing torch
if sys.version_info >= (3, 14):
    print("[ERROR] PyTorch requires Python <= 3.12")
    print("  Run: venv_torch\\Scripts\\activate")
    print("  Then: python train_lstm.py")
    sys.exit(1)

try:
    import torch
    print(f"[OK] PyTorch {torch.__version__} | Device: {'CUDA' if torch.cuda.is_available() else 'CPU'}")
except ImportError:
    print("[ERROR] PyTorch not installed in this environment.")
    print("  Run: pip install torch --index-url https://download.pytorch.org/whl/cpu")
    sys.exit(1)

import numpy as np
import pandas as pd

from config import settings
from config.logger import get_logger

log = get_logger("train_lstm")


def fetch_and_prepare(symbol: str, timeframe: str = "1h", days: int = 730) -> pd.DataFrame:
    """Fetch + indicators. Does NOT use predict_batch (too slow for large datasets)."""
    from agents.data_ingestion.binance_fetcher import fetch_historical
    from agents.feature_engine.indicators import calculate_indicators
    from agents.feature_engine.trend import classify_trend

    log.info("Fetching %s %s (%d days)...", symbol, timeframe, days)
    df = fetch_historical(symbol, timeframe=timeframe, since_days=days)

    if df is None or len(df) < 100:
        log.error("Insufficient data: %d rows", len(df) if df is not None else 0)
        return pd.DataFrame()

    df = calculate_indicators(df)
    df = classify_trend(df)
    log.info("Ready: %d rows | %d indicator columns", len(df), len(df.columns) - 6)
    return df


def train_on_symbol(
    symbol: str,
    timeframe: str = "1h",
    days: int = 730,
    epochs: int = 60,
    seq_length: int = 60,
    batch_size: int = 64,
) -> dict:
    """Train LSTM on one symbol and return metrics."""
    from agents.ai_engine.trainer import prepare_sequences, train_model, FEATURE_COLS
    from agents.ai_engine.lstm_model import TradingLSTM

    df = fetch_and_prepare(symbol, timeframe, days)
    if df.empty:
        return {"symbol": symbol, "error": "no data"}

    # Use next-candle direction (not 1R:2R) for LSTM — faster + more samples
    X, y, scaler_params = prepare_sequences(
        df,
        seq_length=seq_length,
        use_outcome_labels=False,   # next-candle direction (simpler, more data)
        target_threshold=0.001,
    )

    if len(X) < 100:
        return {"symbol": symbol, "error": f"only {len(X)} sequences"}

    log.info("LSTM sequences: %d | Shape: %s", len(X), X.shape)

    # Walk-forward split: train on 80%, validate on last 20%
    split = int(len(X) * 0.8)
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]

    os.makedirs("models", exist_ok=True)
    metrics = train_model(
        X_train, y_train, X_val, y_val,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=0.001,
        patience=10,
        save_path="models/",
    )

    # Save scaler params alongside model
    import json
    scaler_params["symbol"] = symbol
    scaler_params["timeframe"] = timeframe
    scaler_params["seq_length"] = seq_length
    with open("models/scaler_params.json", "w") as f:
        json.dump(scaler_params, f, indent=2)

    log.info(
        "LSTM trained: best_val_acc=%.1f%% in %d epochs",
        metrics.get("best_val_accuracy", 0),
        metrics.get("epochs_trained", 0),
    )
    return {"symbol": symbol, **metrics}


def main():
    parser = argparse.ArgumentParser(description="Train LSTM crypto predictor")
    parser.add_argument("--symbol",     default="BTC/USDT")
    parser.add_argument("--timeframe",  default="1h")
    parser.add_argument("--days",       default=730, type=int)
    parser.add_argument("--epochs",     default=60,  type=int)
    parser.add_argument("--seq-length", default=60,  type=int)
    parser.add_argument("--all-symbols", action="store_true")
    args = parser.parse_args()

    os.makedirs("models", exist_ok=True)

    symbols = settings.CRYPTO_SYMBOLS if args.all_symbols else [args.symbol]

    print(f"\nTraining LSTM on: {symbols}")
    print(f"Timeframe: {args.timeframe} | Days: {args.days} | Epochs: {args.epochs}\n")

    all_metrics = []
    for sym in symbols:
        print(f"--- {sym} ---")
        m = train_on_symbol(
            sym, args.timeframe, args.days, args.epochs, args.seq_length
        )
        all_metrics.append(m)

    print("\n=== LSTM Training Summary ===")
    for m in all_metrics:
        sym = m.get("symbol", "?")
        acc = m.get("best_val_accuracy", "N/A")
        ep  = m.get("epochs_trained", "?")
        err = m.get("error", "")
        if err:
            print(f"  {sym:15s} — ERROR: {err}")
        else:
            print(f"  {sym:15s} — Val Acc: {acc:.1f}%  ({ep} epochs)")

    print("\nModel saved to models/best_model.pt")
    print("The ensemble will automatically use LSTM on next prediction.")


if __name__ == "__main__":
    main()
