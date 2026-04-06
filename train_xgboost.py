"""
XGBoost Model Training Script.

Fetches historical crypto OHLCV data, computes all indicators,
generates reference signals, and trains the XGBoost predictor.

Usage:
    python train_xgboost.py
    python train_xgboost.py --symbol BTC/USDT --timeframe 1h
    python train_xgboost.py --all-symbols

The trained model is saved to models/xgb_model.pkl and is automatically
loaded by the ensemble at runtime.
"""

import argparse
import asyncio
import os
import sys

import pandas as pd

from config import settings
from config.logger import get_logger

log = get_logger("train_xgboost")


def fetch_and_prepare(symbol: str, timeframe: str = "1h", days: int = 365) -> pd.DataFrame:
    """
    Fetch OHLCV, compute indicators + fast signals for XGBoost label generation.

    Note: We intentionally skip predict_batch() here — it is O(n²) for large datasets
    and would take hours on 17k rows. The signal_generator's built-in composite
    formula (O(n) fast) produces enough LONG/SHORT labels for XGBoost training.
    """
    from agents.data_ingestion.binance_fetcher import fetch_historical
    from agents.feature_engine.indicators import calculate_indicators
    from agents.feature_engine.trend import classify_trend
    from agents.feature_engine.regime_detector import detect_regime
    from agents.signal_engine.signal_generator import generate_signals

    log.info("Fetching %s %s (%d days)...", symbol, timeframe, days)
    df = fetch_historical(symbol, timeframe=timeframe, since_days=days)

    if df is None or len(df) < 100:
        log.error("Not enough data for %s — got %s rows", symbol, len(df) if df is not None else 0)
        return pd.DataFrame()

    log.info("Fetched %d rows — computing indicators...", len(df))
    df = calculate_indicators(df)
    df = classify_trend(df)
    regime = detect_regime(df)

    # Fast signal generation — NO predict_batch (that's O(n^2) and slow)
    # signal_generator uses its built-in composite confidence formula (O(n))
    log.info("Generating signals (fast mode)...")
    df = generate_signals(df, ai_confidence=None, regime=regime, symbol=symbol)

    signal_counts = df["signal"].value_counts().to_dict() if "signal" in df.columns else {}
    log.info("Prepared: %d rows | Signals: %s", len(df), signal_counts)
    return df


def train_on_symbol(symbol: str, timeframe: str = "1h", days: int = 365) -> dict:
    """Train XGBoost on one symbol and return metrics."""
    from agents.ai_engine.xgboost_model import get_xgb_predictor
    import agents.ai_engine.xgboost_model as xm
    xm._predictor = None   # Reset singleton to reload after training

    df = fetch_and_prepare(symbol, timeframe, days)
    if df.empty:
        return {"symbol": symbol, "error": "No data"}

    predictor = get_xgb_predictor()
    metrics = predictor.train(df)
    metrics["symbol"] = symbol

    if "val_accuracy" in metrics:
        log.info("✓ %s trained | Accuracy: %.1f%% | Samples: %d",
                 symbol, metrics["val_accuracy"], metrics["n_samples"])
    else:
        log.warning("✗ %s training issue: %s", symbol, metrics.get("error", "unknown"))

    # Print feature importances
    top_features = predictor.get_feature_importance()
    top5 = list(top_features.items())[:5]
    log.info("  Top features: %s", top5)

    return metrics


def main():
    parser = argparse.ArgumentParser(description="Train XGBoost crypto predictor")
    parser.add_argument("--symbol",     default="BTC/USDT",  help="Trading pair to train on")
    parser.add_argument("--timeframe",  default="1h",         help="Candle timeframe (1h, 4h, 1d)")
    parser.add_argument("--days",       default=730, type=int, help="Days of history to use")
    parser.add_argument("--all-symbols", action="store_true",  help="Train on all CRYPTO_SYMBOLS")
    args = parser.parse_args()

    os.makedirs("models", exist_ok=True)

    if args.all_symbols:
        symbols = settings.CRYPTO_SYMBOLS
        log.info("Training on %d symbols: %s", len(symbols), symbols)
        all_metrics = []
        for sym in symbols:
            m = train_on_symbol(sym, args.timeframe, args.days)
            all_metrics.append(m)
        # Summary
        print("\n─── Training Summary ───")
        for m in all_metrics:
            sym  = m.get("symbol", "?")
            acc  = m.get("val_accuracy", "N/A")
            n    = m.get("n_samples", 0)
            err  = m.get("error", "")
            if err:
                print(f"  {sym:15s} — ERROR: {err}")
            else:
                print(f"  {sym:15s} — Accuracy: {acc}%  Samples: {n}")
    else:
        train_on_symbol(args.symbol, args.timeframe, args.days)
        print(f"\nModel saved to models/xgb_model.pkl")
        print("The ensemble will automatically use it on next prediction.")


if __name__ == "__main__":
    main()
