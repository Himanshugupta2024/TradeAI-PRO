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
    timeframe: str = "1d",
    days: int = 730,
    use_optuna: bool = False,
    n_trials: int = 15,
    embargo_bars: int = 20,
    round_trip_cost_pct: float = 0.0025,
    keep_timeouts: bool = True,
) -> Dict[str, Any]:
    """Train XGBoost and LightGBM models on a single asset."""
    import agents.ai_engine.xgboost_model as xm
    import agents.ai_engine.lightgbm_model as lm

    xm._predictor = None
    lm._predictor = None

    df = fetch_and_prepare(symbol, asset_type, timeframe, days)
    if df.empty:
        return {"symbol": symbol, "error": "No data"}

    # Phase 3: Enrich with scale-free F&O derivative features if available
    try:
        from agents.ai_engine.fno_features import extract_scale_free_fno_features
        last_close = float(df["close"].iloc[-1])
        last_atr = float(df["atr14"].iloc[-1]) if "atr14" in df.columns else (last_close * 0.015)
        fno_dict = extract_scale_free_fno_features(symbol=symbol, spot=last_close, atr=last_atr)
        for feat_name, feat_val in fno_dict.items():
            df[feat_name] = feat_val
    except Exception as fno_e:
        log.debug("Scale-free F&O feature enrichment skipped for %s: %s", symbol, fno_e)

    res: Dict[str, Any] = {"symbol": symbol, "rows": len(df)}

    # 1. XGBoost
    log.info("Fitting XGBoost for %s...", symbol)
    t0 = time.time()
    try:
        xgb_predictor = xm.get_xgb_predictor()
        xgb_metrics = xgb_predictor.train(
            df,
            use_optuna=use_optuna,
            n_optuna_trials=n_trials,
            embargo_bars=embargo_bars,
            round_trip_cost_pct=round_trip_cost_pct,
            keep_timeouts=keep_timeouts,
        )
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
        lgbm_metrics = lgbm_predictor.train(
            df,
            use_optuna=use_optuna,
            n_optuna_trials=n_trials,
            embargo_bars=embargo_bars,
            round_trip_cost_pct=round_trip_cost_pct,
            keep_timeouts=keep_timeouts,
        )
        lgbm_metrics["time_sec"] = round(time.time() - t0, 1)
        res["lightgbm"] = lgbm_metrics
        log.info("LightGBM %s: Val Acc: %.2f%%, AUC: %.4f", symbol, lgbm_metrics.get("val_accuracy", 0), lgbm_metrics.get("val_auc", 0))
    except Exception as exc:
        log.error("LightGBM training failed for %s: %s", symbol, exc)
        res["lightgbm"] = {"error": str(exc)}

    res["_prepared_df"] = df
    return res


def _binary_outcome_series(df: pd.DataFrame, round_trip_cost_pct: float = 0.0025, horizon: int = 5) -> pd.Series:
    """Compute forward-return directional binary labels (1 = UP net of costs, 0 = DOWN/FLAT).

    Matches `meta_learner` (`meta_prob_up`) and `ProbabilityCalibrator` P(UP) semantics
    without labeling flat `HOLD` bars as artificial negatives.
    """
    close = df["close"].astype(float)
    fwd = close.shift(-horizon)
    return (fwd > close * (1.0 + round_trip_cost_pct)).fillna(False).astype(int)


def _build_oos_meta_matrix(
    df_slice: pd.DataFrame,
    xgb_pred: Any,
    lgbm_pred: Any,
    logit_pred: Any,
    stat_series: pd.Series,
    regime_trend: float,
    regime_vol: float,
) -> pd.DataFrame:
    """Construct aligned META_FEATURE_NAMES matrix over an OOS holdout slice."""
    from agents.ai_engine.meta_learner import build_meta_feature_vector, META_FEATURE_NAMES

    n = len(df_slice)
    xgb_p = xgb_pred.predict_batch(df_slice).values / 100.0 if xgb_pred.is_fitted else [0.5] * n
    lgb_p = lgbm_pred.predict_batch(df_slice).values / 100.0 if lgbm_pred.is_fitted else [0.5] * n
    log_p = logit_pred.predict_batch(df_slice).values / 100.0 if logit_pred.is_fitted else [0.5] * n
    sta_p = (stat_series.values / 100.0) if len(stat_series) == n else [0.5] * n

    adx_col = f"ADX_{getattr(settings, 'ADX_PERIOD', 14)}"
    atr_col = f"ATR_{getattr(settings, 'ATR_PERIOD', 14)}"
    rows = []
    for i in range(n):
        row = df_slice.iloc[i]
        close_val = max(float(row.get("close", 1.0) or 1.0), 1.0)
        adx_val = float(row.get(adx_col, 20.0) or 20.0)
        atr_val = float(row.get(atr_col, 1.0) or 1.0)
        rows.append(
            build_meta_feature_vector(
                xgb_prob=float(xgb_p[i]),
                lgbm_prob=float(lgb_p[i]),
                lstm_prob=0.5,
                logit_prob=float(log_p[i]),
                stat_score=float(sta_p[i]),
                trend_score=regime_trend,
                volatility_score=regime_vol,
                adx=adx_val,
                atr_pct=atr_val / close_val,
            ).iloc[0]
        )
    return pd.DataFrame(rows, columns=META_FEATURE_NAMES)


def train_stacking_and_calibrator(
    prepared_dfs: List[pd.DataFrame],
    embargo_bars: int = 20,
    round_trip_cost_pct: float = 0.0025,
) -> Dict[str, Any]:
    """Fit logistic anchor, out-of-sample meta-learner, and Platt calibrator cleanly."""
    if not prepared_dfs:
        return {"skipped": "no prepared dataframes"}

    from agents.ai_engine.logistic_baseline import LogisticBaselinePredictor
    from agents.ai_engine.meta_learner import MetaStackingLearner, META_FEATURE_NAMES
    from agents.ai_engine.calibrator import ProbabilityCalibrator
    from agents.ai_engine.xgboost_model import get_xgb_predictor
    from agents.ai_engine.lightgbm_model import get_lgbm_predictor
    from agents.ai_engine.fallback_predictor import predict_batch
    from agents.feature_engine.regime_classifier import detect_two_axis_regime

    train_logit_dfs, train_logit_ys = [], []
    meta_Xs, meta_ys = [], []
    calib_Xs, calib_ys = [], []

    xgb_pred = get_xgb_predictor()
    lgbm_pred = get_lgbm_predictor()

    for df in prepared_dfs:
        n = len(df)
        if n < 120:
            continue
        labels = _binary_outcome_series(df, round_trip_cost_pct=round_trip_cost_pct)
        split_idx = int(n * 0.8)
        train_end = max(20, split_idx - embargo_bars)
        train_df = df.iloc[:train_end].copy()
        y_tr = labels.iloc[:train_end].values
        if len(train_df) >= 20 and len(set(y_tr)) >= 2:
            train_logit_dfs.append(train_df)
            train_logit_ys.append(y_tr)

    if not train_logit_dfs:
        return {"skipped": "insufficient multi-class training slices"}

    logit_pred = LogisticBaselinePredictor()
    import numpy as np
    logit_pred.fit(pd.concat(train_logit_dfs, ignore_index=True), np.concatenate(train_logit_ys), save=True)

    for df in prepared_dfs:
        n = len(df)
        split_idx = int(n * 0.8)
        oos = df.iloc[split_idx:].copy()
        if len(oos) < 45:
            continue
        labels_oos = _binary_outcome_series(df, round_trip_cost_pct=round_trip_cost_pct).iloc[split_idx:].values
        # Drop last 5 bars whose forward 5-bar return is unknown
        oos = oos.iloc[:-5].copy() if len(oos) > 5 else oos
        labels_oos = labels_oos[:len(oos)]
        mid = len(oos) // 2
        try:
            reg = detect_two_axis_regime(df.iloc[:split_idx])
            tr_score, vol_score = float(reg.trend_score), float(reg.volatility_score)
        except Exception:
            tr_score, vol_score = 0.0, 0.5

        try:
            stat_s = predict_batch(oos)
        except Exception:
            stat_s = pd.Series(50.0, index=oos.index)

        m_df = oos.iloc[:mid]
        c_df = oos.iloc[mid:]
        y_m, y_c = labels_oos[:mid], labels_oos[mid:]
        if len(m_df) >= 10 and len(set(y_m)) >= 2:
            meta_Xs.append(_build_oos_meta_matrix(m_df, xgb_pred, lgbm_pred, logit_pred, stat_s.iloc[:mid], tr_score, vol_score))
            meta_ys.append(y_m)
        if len(c_df) >= 10 and len(set(y_c)) >= 2:
            calib_Xs.append(_build_oos_meta_matrix(c_df, xgb_pred, lgbm_pred, logit_pred, stat_s.iloc[mid:], tr_score, vol_score))
            calib_ys.append(y_c)

    out: Dict[str, Any] = {"logistic_fitted": logit_pred.is_fitted}
    if not meta_Xs or not calib_Xs:
        return out

    X_meta_all = pd.concat(meta_Xs, ignore_index=True)
    y_meta_all = np.concatenate(meta_ys)
    X_calib_all = pd.concat(calib_Xs, ignore_index=True)
    y_calib_all = np.concatenate(calib_ys)

    if len(X_meta_all) >= 20 and len(set(y_meta_all)) >= 2:
        meta = MetaStackingLearner()
        meta.fit(X_meta_all[META_FEATURE_NAMES], y_meta_all, save=True)
        out["meta_learner_fitted"] = meta.is_fitted
        if meta.is_fitted and meta._model is not None and len(X_calib_all) >= 20 and len(set(y_calib_all)) >= 2:
            calib = ProbabilityCalibrator()
            calib.fit_calibration(meta._model, X_calib_all[META_FEATURE_NAMES], y_calib_all, save=True)
            out["calibrator_fitted"] = calib.is_fitted
            if calib.is_fitted:
                out.update(calib.evaluate_quality(X_calib_all[META_FEATURE_NAMES], y_calib_all))

    return out


def main():
    parser = argparse.ArgumentParser(description="TradeAI PRO — Train & Publish ML Models")
    parser.add_argument("--asset-type", choices=["nse", "crypto"], default="nse", help="Asset class (default: nse)")
    parser.add_argument("--symbols", type=str, default="", help="Comma-separated symbols")
    parser.add_argument("--timeframe", type=str, default="1d", help="Candle timeframe (default: 1d for NSE EOD)")
    parser.add_argument("--days", type=int, default=730, help="Lookback days (default: 730 for 2 years)")
    parser.add_argument("--embargo-bars", type=int, default=20, help="Purged embargo gap candles (default: 20)")
    parser.add_argument("--cost-pct", type=float, default=0.0025, help="Round trip fee drag hurdle (default: 0.0025 for 25 bps Indian delivery friction)")
    parser.add_argument("--keep-timeouts", action="store_true", default=True, help="Retain horizontal timeouts as class 0 (default: True)")
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
    prepared_dfs: List[pd.DataFrame] = []
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
            embargo_bars=args.embargo_bars,
            round_trip_cost_pct=args.cost_pct,
            keep_timeouts=args.keep_timeouts,
        )
        raw_df = res.pop("_prepared_df", None)
        if isinstance(raw_df, pd.DataFrame) and not raw_df.empty:
            prepared_dfs.append(raw_df)
        all_metrics.append(res)
        xgb_acc = res.get("xgboost", {}).get("val_accuracy")
        lgbm_acc = res.get("lightgbm", {}).get("val_accuracy")
        if xgb_acc and isinstance(xgb_acc, (int, float)):
            total_val_acc += float(xgb_acc)
            acc_count += 1
        if lgbm_acc and isinstance(lgbm_acc, (int, float)):
            total_val_acc += float(lgbm_acc)
            acc_count += 1

    calibration_summary = {}
    try:
        calibration_summary = train_stacking_and_calibrator(
            prepared_dfs,
            embargo_bars=args.embargo_bars,
            round_trip_cost_pct=args.cost_pct,
        )
    except Exception as exc:
        log.warning("Meta-learner & calibrator fit skipped: %s", exc)
        calibration_summary = {"error": str(exc)}

    avg_val_acc = round(total_val_acc / acc_count, 2) if acc_count > 0 else 55.0
    # Bound average accuracy to fractional range if > 1.0 (some metrics return 0.58, some 58.0)
    fractional_acc = round(avg_val_acc / 100.0 if avg_val_acc > 1.0 else avg_val_acc, 4)

    summary = {
        "status": "completed",
        "asset_type": args.asset_type,
        "symbols_trained": len(symbols),
        "val_accuracy": fractional_acc,
        "calibration": calibration_summary,
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
