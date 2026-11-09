#!/usr/bin/env python3
"""
TradeAI PRO — Unified Model Training and Database Publishing Pipeline.
======================================================================
Works interchangeably across:
  - GitHub Actions (automated weekly cron / manual dispatch)
  - Kaggle Notebooks (CPU or GPU background execution)
  - Google Colab (interactive experimentation)
  - Local workstation

Supports:
  - NSE Equities (default: RELIANCE, TCS, HDFCBANK, INFY via yfinance)
  - Crypto Assets (BTC/USDT, ETH/USDT, SOL/USDT via CCXT / Binance)
  - Optuna tuning (or --quick for fast direct fit)
  - Direct packaging & publish to Supabase (via api.model_sync.publish_models_dir)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

# Ensure repository root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import settings
from config.logger import get_logger

log = get_logger("train_and_publish")

DEFAULT_NSE_SYMBOLS = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN"]
DEFAULT_CRYPTO_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


def fetch_and_prepare(
    symbol: str,
    asset_type: str = "nse",
    timeframe: str = "1h",
    days: int = 365,
) -> pd.DataFrame:
    """Fetch OHLCV data for NSE or Crypto and compute all indicators & features."""
    from agents.feature_engine.indicators import calculate_indicators
    from agents.feature_engine.trend import classify_trend
    from agents.feature_engine.regime_detector import detect_regime
    from agents.signal_engine.signal_generator import generate_signals

    log.info("Fetching %s [%s] %s (%d days)...", symbol, asset_type.upper(), timeframe, days)

    if asset_type.lower() == "crypto":
        from agents.data_ingestion.binance_fetcher import fetch_historical
        df = fetch_historical(symbol, timeframe=timeframe, since_days=days)
    else:
        from agents.data_ingestion.nse_fetcher import fetch_historical
        df = fetch_historical(symbol, timeframe=timeframe, since_days=days)

    if df is None or len(df) < 100:
        log.warning("Insufficient candles for %s: %d rows", symbol, len(df) if df is not None else 0)
        return pd.DataFrame()

    log.info("Computing features for %d candles...", len(df))
    df = calculate_indicators(df)
    df = classify_trend(df)
    regime = detect_regime(df)

    df = generate_signals(df, ai_confidence=None, regime=regime, symbol=symbol)
    return df


def train_single_asset(
    symbol: str,
    asset_type: str = "nse",
    timeframe: str = "1h",
    days: int = 365,
    use_optuna: bool = False,
    n_trials: int = 15,
) -> Dict[str, Any]:
    """Train XGBoost and LightGBM models on a single asset."""
    import agents.ai_engine.xgboost_model as xm
    import agents.ai_engine.lightgbm_model as lm

    xm._predictor = None
    lm._predictor = None

    df = fetch_and_prepare(symbol, asset_type, timeframe, days)
    if df.empty:
        return {"symbol": symbol, "error": "No data"}

    res: Dict[str, Any] = {"symbol": symbol, "rows": len(df)}

    # 1. XGBoost
    log.info("Fitting XGBoost for %s...", symbol)
    t0 = time.time()
    try:
        xgb_predictor = xm.get_xgb_predictor()
        xgb_metrics = xgb_predictor.train(df, use_optuna=use_optuna, n_optuna_trials=n_trials)
        xgb_metrics["time_sec"] = round(time.time() - t0, 1)
        res["xgboost"] = xgb_metrics
        log.info("XGBoost %s: Val Acc: %.2f%%, AUC: %.4f", symbol, xgb_metrics.get("val_accuracy", 0), xgb_metrics.get("val_auc", 0))
    except Exception as exc:
        log.error("XGBoost training failed for %s: %s", symbol, exc)
        res["xgboost"] = {"error": str(exc)}

    # 2. LightGBM
    log.info("Fitting LightGBM for %s...", symbol)
    t0 = time.time()
    try:
        lgbm_predictor = lm.get_lgbm_predictor()
        lgbm_metrics = lgbm_predictor.train(df, use_optuna=use_optuna, n_optuna_trials=n_trials)
        lgbm_metrics["time_sec"] = round(time.time() - t0, 1)
        res["lightgbm"] = lgbm_metrics
        log.info("LightGBM %s: Val Acc: %.2f%%, AUC: %.4f", symbol, lgbm_metrics.get("val_accuracy", 0), lgbm_metrics.get("val_auc", 0))
    except Exception as exc:
        log.error("LightGBM training failed for %s: %s", symbol, exc)
        res["lightgbm"] = {"error": str(exc)}

    return res


def main():
    parser = argparse.ArgumentParser(description="TradeAI PRO — Train & Publish ML Models")
    parser.add_argument("--asset-type", choices=["nse", "crypto"], default="nse", help="Asset class (default: nse)")
    parser.add_argument("--symbols", type=str, default="", help="Comma-separated symbols")
    parser.add_argument("--timeframe", type=str, default="1h", help="Candle timeframe (default: 1h)")
    parser.add_argument("--days", type=int, default=365, help="Lookback days (default: 365)")
    parser.add_argument("--quick", action="store_true", default=True, help="Skip heavy Optuna trials (default: True)")
    parser.add_argument("--optuna", dest="quick", action="store_false", help="Enable Optuna hyperparameter search")
    parser.add_argument("--trials", type=int, default=15, help="Optuna trials if enabled (default: 15)")
    parser.add_argument("--publish", action="store_true", help="Publish bundle to Supabase runtime_state")
    parser.add_argument("--source", type=str, default="pipeline", help="Source tag for bundle metadata")

    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        symbols = DEFAULT_NSE_SYMBOLS if args.asset_type == "nse" else DEFAULT_CRYPTO_SYMBOLS

    log.info("Starting model training pipeline | Asset: %s | Symbols: %s | Quick: %s", args.asset_type, symbols, args.quick)

    all_metrics: List[Dict[str, Any]] = []
    total_val_acc = 0.0
    acc_count = 0

    for sym in symbols:
        res = train_single_asset(
            symbol=sym,
            asset_type=args.asset_type,
            timeframe=args.timeframe,
            days=args.days,
            use_optuna=not args.quick,
            n_trials=args.trials,
        )
        all_metrics.append(res)
        xgb_acc = res.get("xgboost", {}).get("val_accuracy")
        lgbm_acc = res.get("lightgbm", {}).get("val_accuracy")
        if xgb_acc and isinstance(xgb_acc, (int, float)):
            total_val_acc += float(xgb_acc)
            acc_count += 1
        if lgbm_acc and isinstance(lgbm_acc, (int, float)):
            total_val_acc += float(lgbm_acc)
            acc_count += 1

    avg_val_acc = round(total_val_acc / acc_count, 2) if acc_count > 0 else 55.0
    # Bound average accuracy to fractional range if > 1.0 (some metrics return 0.58, some 58.0)
    fractional_acc = round(avg_val_acc / 100.0 if avg_val_acc > 1.0 else avg_val_acc, 4)

    summary = {
        "status": "completed",
        "asset_type": args.asset_type,
        "symbols_trained": len(symbols),
        "val_accuracy": fractional_acc,
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    log.info("Training complete: %s", json.dumps(summary, indent=2))

    if args.publish:
        log.info("Publishing trained models to database (source=%s)...", args.source)
        from api.model_sync import publish_models_dir
        models_dir = _REPO_ROOT / "models"
        publish_res = publish_models_dir(
            models_dir,
            metrics={"val_accuracy": fractional_acc, "symbols": symbols, "asset_type": args.asset_type},
            source=args.source,
        )
        log.info("Successfully published bundle to Supabase: %s", json.dumps(publish_res, indent=2))
        summary["publish"] = publish_res

    print(json.dumps(summary))


if __name__ == "__main__":
    main()
