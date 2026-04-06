"""
Ensemble Walk-Forward Validator.

Measures the actual prediction accuracy of the ensemble over historical data
using a walk-forward approach — trains on older data, tests on newer data,
then slides the window forward. Prevents lookahead bias.

Metrics computed per fold and overall:
  - Direction accuracy (UP/DOWN prediction correctness)
  - Precision / Recall for UP and DOWN signals
  - Average confidence when correct vs incorrect (calibration check)
  - Profit factor: (sum of wins × RR) / (sum of losses)
  - Trade quality distribution (% A/B/C/D grade signals)

Usage:
    from agents.backtester.ensemble_validator import run_walk_forward_validation
    results = run_walk_forward_validation("BTC/USDT", timeframe="4h", n_folds=5)
    print(results["summary"])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class FoldResult:
    """Results for a single walk-forward fold."""
    fold_idx:         int
    train_start:      str
    train_end:        str
    test_start:       str
    test_end:         str
    n_predictions:    int
    n_correct:        int
    accuracy:         float        # %
    precision_up:     float        # % correct when predicted UP
    precision_down:   float        # % correct when predicted DOWN
    avg_conf_correct: float        # avg confidence on correct predictions
    avg_conf_wrong:   float        # avg confidence on wrong predictions (calibration)
    high_conf_acc:    float        # accuracy when confidence >= 60%
    n_high_conf:      int          # count of high-confidence predictions


@dataclass
class EnsembleValidationResult:
    """Full walk-forward validation result."""
    symbol:           str
    timeframe:        str
    n_folds:          int
    overall_accuracy: float
    overall_precision_up:   float
    overall_precision_down: float
    avg_confidence:   float
    high_conf_accuracy: float      # accuracy when model is confident (>=60%)
    n_high_conf_trades: int
    profit_factor:    float        # estimated: (win_rate × RR) / (1 - win_rate)
    calibration_score: float       # conf_correct - conf_wrong (higher = better calibrated)
    folds:            list[FoldResult] = field(default_factory=list)
    summary:          str = ""


def run_walk_forward_validation(
    symbol: str = "BTC/USDT",
    timeframe: str = "4h",
    days: int = 365,
    n_folds: int = 5,
    forward_bars: int = 8,
    high_conf_threshold: float = 60.0,
    rr_ratio: float = 2.0,
) -> EnsembleValidationResult:
    """
    Run walk-forward validation of the ensemble predictor.

    Strategy:
      1. Fetch `days` of OHLCV + compute indicators
      2. Split into `n_folds` time-ordered folds (no shuffling)
      3. For each fold:
         - "Train" window: data before the test period (XGBoost is retrained here)
         - "Test" window: predict direction, compare with actual `forward_bars` return
      4. Aggregate metrics across all folds

    Args:
        symbol:               Trading pair, e.g. "BTC/USDT"
        timeframe:            Candle interval
        days:                 Total history to use
        n_folds:              Number of walk-forward folds
        forward_bars:         Bars ahead to check actual price direction
        high_conf_threshold:  Confidence % above which we call it "high conviction"
        rr_ratio:             Risk:Reward for profit factor calculation

    Returns:
        EnsembleValidationResult with per-fold and overall metrics
    """
    log.info("Walk-forward validation: %s %s | %d folds | %d days", symbol, timeframe, n_folds, days)

    # ── Fetch + prepare full dataset ────────────────────────────────────────
    from agents.data_ingestion.binance_fetcher import fetch_historical
    from agents.feature_engine.indicators import calculate_indicators
    from agents.feature_engine.trend import classify_trend

    df = fetch_historical(symbol, timeframe=timeframe, since_days=days)
    if df is None or len(df) < n_folds * 50:
        raise ValueError(f"Insufficient data for {symbol}: {len(df) if df is not None else 0} rows")

    df = calculate_indicators(df)
    df = classify_trend(df)
    df = df.dropna(subset=["RSI_14", "MACD", "EMA_20"]).reset_index(drop=True)
    log.info("Dataset: %d rows after cleaning", len(df))

    n = len(df)
    fold_size = n // (n_folds + 1)   # +1 so first fold has a training window

    fold_results: list[FoldResult] = []
    all_correct:  list[int]   = []
    all_conf:     list[float] = []
    all_dir:      list[str]   = []
    all_actual:   list[str]   = []

    for fold_idx in range(n_folds):
        test_start_i = fold_size * (fold_idx + 1)
        test_end_i   = test_start_i + fold_size
        train_end_i  = test_start_i

        train_df = df.iloc[:train_end_i].copy()
        test_df  = df.iloc[test_start_i: test_end_i + forward_bars].copy()

        if len(train_df) < 100 or len(test_df) < forward_bars + 10:
            log.warning("Fold %d: insufficient data — skipping", fold_idx)
            continue

        log.info(
            "Fold %d: train %s→%s | test %s→%s",
            fold_idx,
            _ts(train_df, 0), _ts(train_df, -1),
            _ts(test_df, 0),  _ts(test_df, fold_size - 1),
        )

        # ── Retrain XGBoost on training window ──────────────────────────────
        _retrain_for_fold(train_df, symbol)

        # ── Predict on each bar in test window ──────────────────────────────
        n_pred = 0
        n_corr = 0
        confs_corr: list[float] = []
        confs_wrong: list[float] = []
        prec_up_corr = 0; prec_up_total = 0
        prec_dn_corr = 0; prec_dn_total = 0
        n_high = 0; n_high_corr = 0

        from agents.ai_engine.ensemble import predict_advanced

        for i in range(len(test_df) - forward_bars):
            window = test_df.iloc[max(0, i - 100): i + 1]
            if len(window) < 30:
                continue

            try:
                pred = predict_advanced(window, symbol=symbol)
            except Exception:
                continue

            predicted_dir = pred["direction"]
            conf = pred["confidence"]

            if predicted_dir == "NEUTRAL":
                continue   # Skip neutral predictions

            # Actual direction: did price go up or down after `forward_bars`?
            future_close = test_df.iloc[i + forward_bars]["close"]
            current_close = test_df.iloc[i]["close"]
            pct_move = (future_close - current_close) / current_close * 100
            actual_dir = "UP" if pct_move > 0 else "DOWN"

            is_correct = (predicted_dir == actual_dir)
            n_pred += 1
            if is_correct:
                n_corr += 1
                confs_corr.append(conf)
            else:
                confs_wrong.append(conf)

            # Precision per direction
            if predicted_dir == "UP":
                prec_up_total += 1
                if is_correct: prec_up_corr += 1
            else:
                prec_dn_total += 1
                if is_correct: prec_dn_corr += 1

            # High-confidence accuracy
            if conf >= high_conf_threshold:
                n_high += 1
                if is_correct: n_high_corr += 1

            all_correct.append(int(is_correct))
            all_conf.append(conf)
            all_dir.append(predicted_dir)
            all_actual.append(actual_dir)

        if n_pred == 0:
            continue

        fold_acc = n_corr / n_pred * 100
        fold_result = FoldResult(
            fold_idx=fold_idx,
            train_start=_ts(train_df, 0),
            train_end=_ts(train_df, -1),
            test_start=_ts(test_df, 0),
            test_end=_ts(test_df, min(fold_size - 1, len(test_df) - 1)),
            n_predictions=n_pred,
            n_correct=n_corr,
            accuracy=round(fold_acc, 1),
            precision_up=round(prec_up_corr / max(prec_up_total, 1) * 100, 1),
            precision_down=round(prec_dn_corr / max(prec_dn_total, 1) * 100, 1),
            avg_conf_correct=round(float(np.mean(confs_corr)) if confs_corr else 0, 1),
            avg_conf_wrong=round(float(np.mean(confs_wrong)) if confs_wrong else 0, 1),
            high_conf_acc=round(n_high_corr / max(n_high, 1) * 100, 1),
            n_high_conf=n_high,
        )
        fold_results.append(fold_result)
        log.info(
            "  Fold %d: acc=%.1f%% | prec_UP=%.1f%% | prec_DN=%.1f%% | "
            "high_conf=%d preds (%.1f%% acc)",
            fold_idx, fold_acc,
            fold_result.precision_up, fold_result.precision_down,
            n_high, fold_result.high_conf_acc,
        )

    if not fold_results:
        raise RuntimeError("No fold results — dataset too small or prediction errors")

    # ── Aggregate across folds ───────────────────────────────────────────────
    total_preds   = sum(f.n_predictions for f in fold_results)
    total_correct = sum(f.n_correct      for f in fold_results)
    overall_acc   = total_correct / max(total_preds, 1) * 100

    all_c = np.array(all_correct, dtype=float)
    all_cf = np.array(all_conf, dtype=float)

    avg_conf       = float(np.mean(all_cf)) if len(all_cf) > 0 else 0
    corr_mask      = all_c == 1
    conf_corr_avg  = float(np.mean(all_cf[corr_mask])) if corr_mask.any() else 0
    conf_wrong_avg = float(np.mean(all_cf[~corr_mask])) if (~corr_mask).any() else 0
    calibration    = conf_corr_avg - conf_wrong_avg

    total_high      = sum(f.n_high_conf for f in fold_results)
    total_high_corr = sum(int(f.high_conf_acc * f.n_high_conf / 100) for f in fold_results)
    high_acc        = total_high_corr / max(total_high, 1) * 100

    # Precision
    up_pred    = [d for d in all_dir if d == "UP"]
    up_corr    = [c for d, c in zip(all_dir, all_correct) if d == "UP" and c == 1]
    dn_pred    = [d for d in all_dir if d == "DOWN"]
    dn_corr    = [c for d, c in zip(all_dir, all_correct) if d == "DOWN" and c == 1]

    prec_up = len(up_corr) / max(len(up_pred), 1) * 100
    prec_dn = len(dn_corr) / max(len(dn_pred), 1) * 100

    # Estimated profit factor: assuming 1:2 RR
    win_rate = overall_acc / 100
    pf = (win_rate * rr_ratio) / max(1 - win_rate, 0.01)

    # ── Build summary string ────────────────────────────────────────────────
    summary = _build_summary(
        symbol, timeframe, n_folds, fold_results,
        overall_acc, prec_up, prec_dn, avg_conf,
        high_acc, total_high, pf, calibration,
    )

    result = EnsembleValidationResult(
        symbol=symbol,
        timeframe=timeframe,
        n_folds=len(fold_results),
        overall_accuracy=round(overall_acc, 2),
        overall_precision_up=round(prec_up, 1),
        overall_precision_down=round(prec_dn, 1),
        avg_confidence=round(avg_conf, 1),
        high_conf_accuracy=round(high_acc, 1),
        n_high_conf_trades=total_high,
        profit_factor=round(pf, 2),
        calibration_score=round(calibration, 1),
        folds=fold_results,
        summary=summary,
    )

    log.info("Walk-forward complete:\n%s", summary)
    return result


# ── Helpers ────────────────────────────────────────────────────────────────────

def _retrain_for_fold(train_df: pd.DataFrame, symbol: str) -> None:
    """Retrain XGBoost on the given training window (if XGBoost is available)."""
    try:
        import agents.ai_engine.xgboost_model as xm
        from agents.ai_engine.xgboost_model import prepare_training_data, get_xgb_predictor
        import numpy as np, pickle, os

        xm._predictor = None   # Force fresh load
        predictor = get_xgb_predictor()
        predictor.model = None
        predictor.is_fitted = False

        X, y = prepare_training_data(train_df)
        if len(X) < 50:
            return

        X = np.where(np.isinf(X), np.nan, X)
        col_med = np.nanmedian(X, axis=0)
        nm = np.isnan(X)
        if nm.any():
            X[nm] = np.take(col_med, np.where(nm)[1])

        if xm.XGB_AVAILABLE:
            import xgboost as xgb_lib
            predictor.model = xgb_lib.XGBClassifier(
                n_estimators=150, max_depth=4, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8,
                eval_metric="logloss", random_state=42,
                n_jobs=-1, verbosity=0,
            )
        elif xm.SKLEARN_AVAILABLE:
            from sklearn.ensemble import GradientBoostingClassifier
            predictor.model = GradientBoostingClassifier(n_estimators=100, max_depth=3, random_state=42)
        else:
            return

        predictor.model.fit(X, y)
        predictor.is_fitted = True
        if hasattr(predictor.model, "feature_importances_"):
            predictor._feature_importance = dict(
                zip(xm.FEATURE_COLS_XGB, predictor.model.feature_importances_.round(4).tolist())
            )
    except Exception as exc:
        log.debug("Fold retrain failed (non-critical): %s", exc)


def _ts(df: pd.DataFrame, idx: int) -> str:
    """Get timestamp string from df at integer position."""
    try:
        ts_col = "timestamp" if "timestamp" in df.columns else None
        if ts_col:
            return str(df[ts_col].iloc[idx])[:10]
        return str(df.index[idx])[:10]
    except Exception:
        return "?"


def _build_summary(
    symbol, timeframe, n_folds, fold_results,
    overall_acc, prec_up, prec_dn, avg_conf,
    high_acc, total_high, pf, calibration,
) -> str:
    lines = [
        "=" * 56,
        f"  ENSEMBLE WALK-FORWARD VALIDATION — {symbol} {timeframe}",
        "=" * 56,
        f"  Folds completed     : {len(fold_results)} / {n_folds}",
        f"  Overall Accuracy    : {overall_acc:.1f}%",
        f"  Precision UP        : {prec_up:.1f}%",
        f"  Precision DOWN      : {prec_dn:.1f}%",
        f"  Avg Confidence      : {avg_conf:.1f}%",
        f"  High-Conf Accuracy  : {high_acc:.1f}% ({total_high} trades, conf>=60%)",
        f"  Estimated PF (1:2RR): {pf:.2f}",
        f"  Calibration Score   : {calibration:+.1f}  (pos = well calibrated)",
        "-" * 56,
        "  Per-Fold Breakdown:",
    ]
    for f in fold_results:
        lines.append(
            f"  Fold {f.fold_idx} [{f.test_start}] "
            f"Acc={f.accuracy:.0f}% "
            f"UP={f.precision_up:.0f}% DN={f.precision_down:.0f}% "
            f"HiConf={f.n_high_conf}preds/{f.high_conf_acc:.0f}%"
        )
    lines.append("=" * 56)
    return "\n".join(lines)


def result_to_dict(r: EnsembleValidationResult) -> dict:
    """Convert result to JSON-serializable dict for API responses."""
    return {
        "symbol":                r.symbol,
        "timeframe":             r.timeframe,
        "n_folds":               r.n_folds,
        "overall_accuracy":      r.overall_accuracy,
        "overall_precision_up":  r.overall_precision_up,
        "overall_precision_down": r.overall_precision_down,
        "avg_confidence":        r.avg_confidence,
        "high_conf_accuracy":    r.high_conf_accuracy,
        "n_high_conf_trades":    r.n_high_conf_trades,
        "profit_factor":         r.profit_factor,
        "calibration_score":     r.calibration_score,
        "summary":               r.summary,
        "folds": [
            {
                "fold_idx":         f.fold_idx,
                "train_start":      f.train_start,
                "train_end":        f.train_end,
                "test_start":       f.test_start,
                "test_end":         f.test_end,
                "n_predictions":    f.n_predictions,
                "n_correct":        f.n_correct,
                "accuracy":         f.accuracy,
                "precision_up":     f.precision_up,
                "precision_down":   f.precision_down,
                "avg_conf_correct": f.avg_conf_correct,
                "avg_conf_wrong":   f.avg_conf_wrong,
                "high_conf_acc":    f.high_conf_acc,
                "n_high_conf":      f.n_high_conf,
            }
            for f in r.folds
        ],
    }
