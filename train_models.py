"""
Unified Model Training Script — XGBoost + LightGBM.

Trains both ML models with Optuna hyperparameter tuning and
walk-forward cross-validation. Produces a side-by-side accuracy report.

Usage:
    python train_models.py
    python train_models.py --symbol BTC/USDT --timeframe 1h
    python train_models.py --all-symbols
    python train_models.py --all-symbols --no-optuna   # fast mode
    python train_models.py --symbol BTC/USDT --trials 50  # more tuning

The trained models are saved to:
    models/xgb_model.pkl   — XGBoost (auto-loaded by ensemble)
    models/lgbm_model.pkl  — LightGBM (auto-loaded by ensemble)

After training, the ensemble automatically uses both models on next prediction.
"""

import argparse
import os
import sys
import time

import pandas as pd

from config import settings
from config.logger import get_logger

log = get_logger("train_models")


# ── Data Preparation ───────────────────────────────────────────────────────────

def fetch_and_prepare(symbol: str, timeframe: str = "1h", days: int = 730) -> pd.DataFrame:
    """
    Fetch OHLCV, compute all indicators (including advanced features),
    and generate reference signals for label generation.

    Note: We intentionally skip predict_batch() here — it is O(n²) for large
    datasets. The signal_generator's built-in composite formula (O(n)) produces
    enough LONG/SHORT labels for training.
    """
    from agents.data_ingestion.binance_fetcher import fetch_historical
    from agents.feature_engine.indicators import calculate_indicators
    from agents.feature_engine.trend import classify_trend
    from agents.feature_engine.regime_detector import detect_regime
    from agents.signal_engine.signal_generator import generate_signals

    log.info("Fetching %s %s (%d days)...", symbol, timeframe, days)
    df = fetch_historical(symbol, timeframe=timeframe, since_days=days)

    if df is None or len(df) < 100:
        log.error("Not enough data for %s — got %s rows",
                  symbol, len(df) if df is not None else 0)
        return pd.DataFrame()

    log.info("Fetched %d rows — computing indicators + advanced features...", len(df))
    df = calculate_indicators(df)   # includes advanced features via integration
    df = classify_trend(df)
    regime = detect_regime(df)

    log.info("Generating signals (fast mode, regime=%s)...", regime.value)
    df = generate_signals(df, ai_confidence=None, regime=regime, symbol=symbol)

    signal_counts = df["signal"].value_counts().to_dict() if "signal" in df.columns else {}
    adv_cols = [c for c in df.columns if c.startswith(("ichimoku_", "pivot_", "ha_",
                                                         "vol_", "swing_", "realized_",
                                                         "hour_", "dow_", "session",
                                                         "hh_", "ll_", "range_",
                                                         "price_percentile", "close_vs_"))]
    log.info("Prepared: %d rows | Signals: %s | Advanced features: %d",
             len(df), signal_counts, len(adv_cols))
    return df


# ── Single Symbol Training ─────────────────────────────────────────────────────

def train_symbol(
    symbol: str,
    timeframe: str = "1h",
    days: int = 730,
    use_optuna: bool = True,
    n_trials: int = 30,
) -> dict:
    """
    Train XGBoost + LightGBM on one symbol.

    Returns a combined metrics dict.
    """
    import agents.ai_engine.xgboost_model as xm
    import agents.ai_engine.lightgbm_model as lm

    # Reset singletons to force reload after training
    xm._predictor = None
    lm._predictor = None

    df = fetch_and_prepare(symbol, timeframe, days)
    if df.empty:
        return {"symbol": symbol, "error": "No data"}

    results = {"symbol": symbol, "timeframe": timeframe, "rows": len(df)}

    # ── Train XGBoost ──────────────────────────────────────────────────────
    log.info("─── Training XGBoost for %s ───", symbol)
    t0 = time.time()
    try:
        xgb_predictor = xm.get_xgb_predictor()
        xgb_metrics = xgb_predictor.train(df, use_optuna=use_optuna,
                                           n_optuna_trials=n_trials)
        xgb_metrics["train_time_s"] = round(time.time() - t0, 1)
        results["xgboost"] = xgb_metrics

        if "val_accuracy" in xgb_metrics:
            log.info("✓ XGBoost | Accuracy: %.1f%% | AUC: %.4f | Samples: %d | Time: %.1fs",
                     xgb_metrics["val_accuracy"], xgb_metrics.get("val_auc", 0),
                     xgb_metrics["n_samples"], xgb_metrics["train_time_s"])
            # Top 5 features
            top5 = list(xgb_predictor.get_feature_importance().items())[:5]
            log.info("  XGB Top features: %s", top5)
        else:
            log.warning("✗ XGBoost issue: %s", xgb_metrics.get("error", "unknown"))
    except Exception as e:
        log.error("XGBoost training failed: %s", e)
        results["xgboost"] = {"error": str(e)}

    # ── Train LightGBM ─────────────────────────────────────────────────────
    log.info("─── Training LightGBM for %s ───", symbol)
    t0 = time.time()
    try:
        lgbm_predictor = lm.get_lgbm_predictor()
        lgbm_metrics = lgbm_predictor.train(df, use_optuna=use_optuna,
                                             n_optuna_trials=n_trials)
        lgbm_metrics["train_time_s"] = round(time.time() - t0, 1)
        results["lightgbm"] = lgbm_metrics

        if "val_accuracy" in lgbm_metrics:
            log.info("✓ LightGBM | Accuracy: %.1f%% | AUC: %.4f | Samples: %d | Time: %.1fs",
                     lgbm_metrics["val_accuracy"], lgbm_metrics.get("val_auc", 0),
                     lgbm_metrics["n_samples"], lgbm_metrics["train_time_s"])
            # Top 5 features
            top5 = list(lgbm_predictor.get_feature_importance().items())[:5]
            log.info("  LGBM Top features: %s", top5)
        else:
            log.warning("✗ LightGBM issue: %s", lgbm_metrics.get("error", "unknown"))
    except Exception as e:
        log.error("LightGBM training failed: %s", e)
        results["lightgbm"] = {"error": str(e)}

    return results


# ── Summary Report ─────────────────────────────────────────────────────────────

def print_summary(all_results: list[dict]):
    """Print a formatted side-by-side comparison of XGBoost vs LightGBM."""
    print("\n" + "═" * 80)
    print("  TRAINING SUMMARY — XGBoost vs LightGBM")
    print("═" * 80)
    print(f"  {'Symbol':<15} {'TF':<5} {'Rows':<7} "
          f"{'XGB Acc':>8} {'XGB AUC':>8} "
          f"{'LGBM Acc':>9} {'LGBM AUC':>9} "
          f"{'Winner':>8}")
    print("─" * 80)

    for r in all_results:
        sym = r.get("symbol", "?")
        tf  = r.get("timeframe", "?")
        rows = r.get("rows", 0)

        if "error" in r:
            print(f"  {sym:<15} {tf:<5} {rows:<7} ERROR: {r['error']}")
            continue

        xgb  = r.get("xgboost",  {})
        lgbm = r.get("lightgbm", {})

        xgb_acc  = xgb.get("val_accuracy",  "N/A")
        xgb_auc  = xgb.get("val_auc",       "N/A")
        lgbm_acc = lgbm.get("val_accuracy", "N/A")
        lgbm_auc = lgbm.get("val_auc",      "N/A")

        # Determine winner
        winner = "─"
        if isinstance(xgb_auc, float) and isinstance(lgbm_auc, float):
            if lgbm_auc > xgb_auc + 0.005:
                winner = "LightGBM"
            elif xgb_auc > lgbm_auc + 0.005:
                winner = "XGBoost"
            else:
                winner = "Tie"

        xgb_acc_s  = f"{xgb_acc:.1f}%"  if isinstance(xgb_acc,  float) else str(xgb_acc)
        xgb_auc_s  = f"{xgb_auc:.4f}"   if isinstance(xgb_auc,  float) else str(xgb_auc)
        lgbm_acc_s = f"{lgbm_acc:.1f}%"  if isinstance(lgbm_acc, float) else str(lgbm_acc)
        lgbm_auc_s = f"{lgbm_auc:.4f}"   if isinstance(lgbm_auc, float) else str(lgbm_auc)

        print(f"  {sym:<15} {tf:<5} {rows:<7} "
              f"{xgb_acc_s:>8} {xgb_auc_s:>8} "
              f"{lgbm_acc_s:>9} {lgbm_auc_s:>9} "
              f"{winner:>8}")

    print("═" * 80)
    print()
    print("  Models saved to:")
    print("    models/xgb_model.pkl   — XGBoost")
    print("    models/lgbm_model.pkl  — LightGBM")
    print()
    print("  The ensemble will automatically use both models on next prediction.")
    print("  Dynamic weights will adapt based on rolling prediction accuracy.")
    print("═" * 80)


# ── Feature Importance Report ──────────────────────────────────────────────────

def print_feature_importance(n_top: int = 15):
    """Print top features from both trained models."""
    print("\n" + "─" * 60)
    print("  FEATURE IMPORTANCE (Top 15)")
    print("─" * 60)

    try:
        from agents.ai_engine.xgboost_model import get_xgb_predictor
        xgb = get_xgb_predictor()
        if xgb.is_fitted:
            print("\n  XGBoost:")
            for i, (feat, imp) in enumerate(list(xgb.get_feature_importance().items())[:n_top]):
                bar = "█" * int(imp * 200)
                print(f"    {i+1:>2}. {feat:<30} {imp:.4f}  {bar}")
    except Exception as e:
        print(f"  XGBoost importance unavailable: {e}")

    try:
        from agents.ai_engine.lightgbm_model import get_lgbm_predictor
        lgbm = get_lgbm_predictor()
        if lgbm.is_fitted:
            print("\n  LightGBM:")
            for i, (feat, imp) in enumerate(list(lgbm.get_feature_importance().items())[:n_top]):
                bar = "█" * int(imp * 200)
                print(f"    {i+1:>2}. {feat:<30} {imp:.4f}  {bar}")
    except Exception as e:
        print(f"  LightGBM importance unavailable: {e}")

    print("─" * 60)


# ── CLI Entry Point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Train XGBoost + LightGBM crypto/stock predictors",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python train_models.py                          # BTC/USDT 1h, 730 days
  python train_models.py --symbol ETH/USDT        # ETH/USDT 1h
  python train_models.py --timeframe 4h           # 4-hour candles
  python train_models.py --all-symbols            # All configured symbols
  python train_models.py --no-optuna              # Skip hyperparameter tuning
  python train_models.py --trials 50              # More Optuna trials
        """,
    )
    parser.add_argument("--symbol",      default="BTC/USDT",
                        help="Trading pair to train on (default: BTC/USDT)")
    parser.add_argument("--timeframe",   default="1h",
                        help="Candle timeframe: 1h, 4h, 1d (default: 1h)")
    parser.add_argument("--days",        default=730, type=int,
                        help="Days of history to use (default: 730)")
    parser.add_argument("--all-symbols", action="store_true",
                        help="Train on all CRYPTO_SYMBOLS from settings")
    parser.add_argument("--no-optuna",   action="store_true",
                        help="Skip Optuna tuning (faster but less accurate)")
    parser.add_argument("--trials",      default=30, type=int,
                        help="Number of Optuna trials per model (default: 30)")
    args = parser.parse_args()

    os.makedirs("models", exist_ok=True)

    use_optuna = not args.no_optuna

    if use_optuna:
        try:
            import optuna
            log.info("Optuna available — hyperparameter tuning enabled (%d trials)", args.trials)
        except ImportError:
            log.warning("Optuna not installed — run: pip install optuna")
            log.warning("Falling back to default hyperparameters")
            use_optuna = False

    try:
        import lightgbm
        log.info("LightGBM available — version %s", lightgbm.__version__)
    except ImportError:
        log.warning("LightGBM not installed — run: pip install lightgbm")
        log.warning("LightGBM will fall back to RandomForest")

    # ── Select symbols ─────────────────────────────────────────────────────
    if args.all_symbols:
        symbols = settings.CRYPTO_SYMBOLS
        log.info("Training on %d symbols: %s", len(symbols), symbols)
    else:
        symbols = [args.symbol]

    # ── Train ──────────────────────────────────────────────────────────────
    all_results = []
    total_start = time.time()

    for sym in symbols:
        log.info("═" * 60)
        log.info("Training: %s | TF: %s | Days: %d | Optuna: %s | Trials: %d",
                 sym, args.timeframe, args.days, use_optuna, args.trials)
        log.info("═" * 60)

        result = train_symbol(
            symbol=sym,
            timeframe=args.timeframe,
            days=args.days,
            use_optuna=use_optuna,
            n_trials=args.trials,
        )
        all_results.append(result)

    total_time = time.time() - total_start
    log.info("Total training time: %.1f seconds", total_time)

    # ── Reports ────────────────────────────────────────────────────────────
    print_summary(all_results)
    print_feature_importance(n_top=15)


if __name__ == "__main__":
    main()
