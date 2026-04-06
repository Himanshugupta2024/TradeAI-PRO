"""
LightGBM Tabular Predictor for Crypto/Stock Trading.

LightGBM is a gradient boosting framework that uses tree-based learning.
It is typically faster and more accurate than XGBoost on tabular financial data
due to its leaf-wise tree growth strategy and histogram-based algorithm.

Key advantages over XGBoost:
  - Faster training (histogram-based splits)
  - Better accuracy on high-dimensional tabular data
  - Native handling of categorical features
  - Lower memory usage

Uses the same 45-feature set as XGBoostPredictor + Optuna tuning.
Training target: 1R:2R outcome (WIN=1 / LOSS=0).

Falls back to sklearn RandomForestClassifier if lightgbm is not installed.

Usage:
    from agents.ai_engine.lightgbm_model import get_lgbm_predictor

    lgbm = get_lgbm_predictor()
    if lgbm.is_fitted:
        conf_series = lgbm.predict_batch(df)
"""

from __future__ import annotations

import os
import pickle
import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

try:
    import lightgbm as lgb
    LGBM_AVAILABLE = True
except ImportError:
    lgb = None
    LGBM_AVAILABLE = False

try:
    from sklearn.ensemble import RandomForestClassifier as _RFC
    from sklearn.model_selection import TimeSeriesSplit
    SKLEARN_AVAILABLE = True
except ImportError:
    _RFC = None
    SKLEARN_AVAILABLE = False

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    OPTUNA_AVAILABLE = True
except ImportError:
    optuna = None
    OPTUNA_AVAILABLE = False

MODEL_AVAILABLE = LGBM_AVAILABLE or SKLEARN_AVAILABLE

# Reuse the same 45-feature set as XGBoost for consistency
from agents.ai_engine.xgboost_model import FEATURE_COLS_XGB, extract_features

FEATURE_COLS_LGBM = FEATURE_COLS_XGB  # Same 45 features


# ── Optuna Hyperparameter Tuning ───────────────────────────────────────────────

def _tune_lightgbm(X: np.ndarray, y: np.ndarray, n_trials: int = 30) -> dict:
    """
    Use Optuna to find optimal LightGBM hyperparameters via time-series CV.

    Returns the best hyperparameter dict found.
    Falls back to sensible defaults if Optuna/LightGBM is unavailable.
    """
    if not OPTUNA_AVAILABLE or not LGBM_AVAILABLE:
        log.info("Optuna/LightGBM not available — using default hyperparameters")
        return {
            "n_estimators":      400,
            "max_depth":         6,
            "learning_rate":     0.05,
            "num_leaves":        63,
            "subsample":         0.8,
            "colsample_bytree":  0.8,
            "min_child_samples": 20,
            "reg_alpha":         0.1,
            "reg_lambda":        1.0,
        }

    from sklearn.metrics import roc_auc_score

    tscv = TimeSeriesSplit(n_splits=3)

    def objective(trial):
        params = {
            "n_estimators":      trial.suggest_int("n_estimators", 100, 600),
            "max_depth":         trial.suggest_int("max_depth", 3, 9),
            "learning_rate":     trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "num_leaves":        trial.suggest_int("num_leaves", 15, 127),
            "subsample":         trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree":  trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "min_child_samples": trial.suggest_int("min_child_samples", 5, 50),
            "reg_alpha":         trial.suggest_float("reg_alpha", 0.0, 2.0),
            "reg_lambda":        trial.suggest_float("reg_lambda", 0.0, 3.0),
            "verbose":           -1,
            "random_state":      42,
            "n_jobs":            -1,
        }

        scores = []
        for train_idx, val_idx in tscv.split(X):
            X_tr, X_val = X[train_idx], X[val_idx]
            y_tr, y_val = y[train_idx], y[val_idx]

            if len(np.unique(y_tr)) < 2 or len(np.unique(y_val)) < 2:
                continue

            model = lgb.LGBMClassifier(**params)
            model.fit(
                X_tr, y_tr,
                eval_set=[(X_val, y_val)],
                callbacks=[lgb.early_stopping(20, verbose=False),
                           lgb.log_evaluation(-1)],
            )
            proba = model.predict_proba(X_val)[:, 1]
            try:
                scores.append(roc_auc_score(y_val, proba))
            except Exception:
                scores.append(0.5)

        return np.mean(scores) if scores else 0.5

    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    best = study.best_params
    log.info("LightGBM Optuna best params (AUC=%.4f): %s", study.best_value, best)
    return best


# ── LightGBM Predictor Class ───────────────────────────────────────────────────

class LightGBMPredictor:
    """
    LightGBM-based trade direction predictor.

    Trains on 1R:2R outcome labels (WIN=1 / LOSS=0).
    Predicts probability of WIN for each bar.

    Interface is identical to XGBoostPredictor for easy ensemble integration.

    Attributes:
        model:              Trained classifier (LGBMClassifier or RFC)
        is_fitted:          True after successful training
        _feature_importance: Dict of feature → importance score
    """

    def __init__(self, model_path: str = "models/lgbm_model.pkl"):
        self.model_path = model_path
        self.model = None
        self.is_fitted = False
        self._feature_importance: dict[str, float] = {}
        self._load()

    def _load(self):
        """Load a previously trained model from disk."""
        if os.path.exists(self.model_path):
            try:
                with open(self.model_path, "rb") as f:
                    saved = pickle.load(f)
                self.model = saved.get("model")
                self._feature_importance = saved.get("feature_importance", {})
                self.is_fitted = self.model is not None
                if self.is_fitted:
                    log.info("LightGBM model loaded from %s", self.model_path)
            except Exception as e:
                log.warning("Failed to load LightGBM model: %s", e)

    def _save(self):
        """Persist the trained model to disk."""
        os.makedirs(os.path.dirname(self.model_path) or ".", exist_ok=True)
        with open(self.model_path, "wb") as f:
            pickle.dump({
                "model": self.model,
                "feature_importance": self._feature_importance,
                "feature_cols": FEATURE_COLS_LGBM,
            }, f)
        log.info("LightGBM model saved to %s", self.model_path)

    def train(
        self,
        df: pd.DataFrame,
        use_optuna: bool = True,
        n_optuna_trials: int = 30,
        test_size: float = 0.2,
    ) -> dict:
        """
        Train the LightGBM model on a labeled DataFrame.

        Args:
            df:               DataFrame with signal column and all indicator columns.
            use_optuna:       Whether to run Optuna hyperparameter tuning.
            n_optuna_trials:  Number of Optuna trials.
            test_size:        Fraction of data to hold out for validation.

        Returns:
            dict with val_accuracy, val_auc, n_samples, feature_importance, etc.
        """
        if not MODEL_AVAILABLE:
            return {"error": "No ML library available (install lightgbm or scikit-learn)"}

        # ── Generate outcome labels ────────────────────────────────────────
        # Fast path: use forward-return labels when 'signal' column is missing
        # (avoids the slow signal_generator pipeline for batch training)
        if "signal" not in df.columns:
            forward_bars = 12
            min_ret = 0.5
            close = df["close"].values.astype(float)
            n = len(close)
            fwd_ret = (close[forward_bars:] - close[:n - forward_bars]) / close[:n - forward_bars] * 100
            up_mask   = fwd_ret >  min_ret
            down_mask = fwd_ret < -min_ret
            keep_mask = up_mask | down_mask
            df_filtered = df.iloc[:n - forward_bars][keep_mask].copy()
            y = up_mask[keep_mask].astype(int)
        else:
            from agents.feature_engine.label_generator import label_1r2r_outcome
            labels = label_1r2r_outcome(df)
            mask = labels != 0
            df_filtered = df[mask].copy()
            y = (labels[mask] == 1).astype(int).values  # 1=WIN, 0=LOSS

        if len(df_filtered) < 50:
            return {"error": f"Insufficient labeled samples: {len(df_filtered)} (need >=50)"}

        log.info("Training LightGBM: %d samples (WIN=%d, LOSS=%d)",
                 len(df_filtered), y.sum(), (y == 0).sum())

        # ── Extract features ───────────────────────────────────────────────
        features = extract_features(df_filtered)
        X = features.values.astype(np.float64)

        # Replace inf/-inf with NaN, then fill with column medians
        X = np.where(np.isinf(X), np.nan, X)
        col_medians = np.nanmedian(X, axis=0)
        nan_mask = np.isnan(X)
        if nan_mask.any():
            X[nan_mask] = np.take(col_medians, np.where(nan_mask)[1])

        # ── Train/validation split (time-series: no shuffle) ──────────────
        split_idx = int(len(X) * (1 - test_size))
        X_train, X_val = X[:split_idx], X[split_idx:]
        y_train, y_val = y[:split_idx], y[split_idx:]

        if len(np.unique(y_train)) < 2:
            return {"error": "Training set has only one class — need more data"}

        # ── Hyperparameter tuning ──────────────────────────────────────────
        if use_optuna and OPTUNA_AVAILABLE and LGBM_AVAILABLE and len(X_train) >= 200:
            log.info("Running Optuna LightGBM tuning (%d trials)...", n_optuna_trials)
            best_params = _tune_lightgbm(X_train, y_train, n_trials=n_optuna_trials)
        else:
            best_params = {
                "n_estimators":      400,
                "max_depth":         6,
                "learning_rate":     0.05,
                "num_leaves":        63,
                "subsample":         0.8,
                "colsample_bytree":  0.8,
                "min_child_samples": 20,
                "reg_alpha":         0.1,
                "reg_lambda":        1.0,
            }

        # ── Train final model ──────────────────────────────────────────────
        if LGBM_AVAILABLE:
            model = lgb.LGBMClassifier(
                **best_params,
                verbose=-1,
                random_state=42,
                n_jobs=-1,
            )
            model.fit(
                X_train, y_train,
                eval_set=[(X_val, y_val)],
                callbacks=[
                    lgb.early_stopping(30, verbose=False),
                    lgb.log_evaluation(-1),
                ],
            )
            # Feature importances (gain-based — more reliable than split count)
            imp = model.booster_.feature_importance(importance_type="gain")
            imp_norm = imp / (imp.sum() + 1e-10)
            self._feature_importance = {
                col: round(float(imp_norm[i]), 6)
                for i, col in enumerate(FEATURE_COLS_LGBM)
            }
        else:
            # Fallback: sklearn RandomForest
            model = _RFC(
                n_estimators=best_params.get("n_estimators", 300),
                max_depth=best_params.get("max_depth", 8),
                random_state=42,
                n_jobs=-1,
            )
            model.fit(X_train, y_train)
            imp = model.feature_importances_
            self._feature_importance = {
                col: round(float(imp[i]), 6)
                for i, col in enumerate(FEATURE_COLS_LGBM[:len(imp)])
            }

        self.model = model
        self.is_fitted = True

        # ── Evaluate ───────────────────────────────────────────────────────
        val_preds = model.predict(X_val)
        val_proba = model.predict_proba(X_val)[:, 1]
        val_accuracy = float((val_preds == y_val).mean() * 100)

        try:
            from sklearn.metrics import roc_auc_score
            val_auc = float(roc_auc_score(y_val, val_proba))
        except Exception:
            val_auc = 0.0

        self._save()

        log.info(
            "LightGBM trained: accuracy=%.1f%%, AUC=%.4f, samples=%d, features=%d",
            val_accuracy, val_auc, len(df_filtered), len(FEATURE_COLS_LGBM),
        )

        return {
            "val_accuracy":       round(val_accuracy, 2),
            "val_auc":            round(val_auc, 4),
            "n_samples":          len(df_filtered),
            "n_features":         len(FEATURE_COLS_LGBM),
            "n_train":            len(X_train),
            "n_val":              len(X_val),
            "best_params":        best_params,
            "feature_importance": self.get_feature_importance(),
        }

    def predict_row(self, df: pd.DataFrame) -> dict:
        """
        Predict direction and confidence for the latest bar in df.

        Returns:
            dict with direction, confidence, prob_up, prob_down, method
        """
        if not self.is_fitted or self.model is None:
            return {
                "direction": "NEUTRAL", "confidence": 0,
                "prob_up": 50.0, "prob_down": 50.0,
                "method": "lightgbm_not_fitted",
            }

        try:
            features = extract_features(df.tail(1))
            X = features.values.astype(np.float64)

            col_medians = np.nanmedian(X, axis=0)
            nan_mask = np.isnan(X)
            if nan_mask.any():
                X[nan_mask] = np.take(col_medians, np.where(nan_mask)[1])

            probs = self.model.predict_proba(X)[0]
            prob_up   = float(probs[1]) * 100
            prob_down = float(probs[0]) * 100
            confidence = abs(prob_up - 50) * 2  # 0-100 scale

            if prob_up >= 55:
                direction = "UP"
            elif prob_up < 45:
                direction = "DOWN"
            else:
                direction = "NEUTRAL"

            return {
                "direction":  direction,
                "confidence": round(confidence, 1),
                "prob_up":    round(prob_up, 1),
                "prob_down":  round(prob_down, 1),
                "method":     "lightgbm",
            }

        except Exception as e:
            log.warning("LightGBM prediction error: %s", e)
            return {
                "direction": "NEUTRAL", "confidence": 0,
                "prob_up": 50.0, "prob_down": 50.0,
                "method": "lightgbm_error",
            }

    def predict_batch(self, df: pd.DataFrame) -> pd.Series:
        """
        Generate probability-of-WIN scores for all rows in df.

        Returns Series with values 0-100:
          >50 = bullish lean
          <50 = bearish lean
          ~50 = no conviction
        """
        if not self.is_fitted or self.model is None:
            return pd.Series(50.0, index=df.index, name="lgbm_confidence")

        try:
            features = extract_features(df)
            X = features.values.astype(np.float64)

            col_medians = np.nanmedian(X, axis=0)
            nan_mask = np.isnan(X)
            if nan_mask.any():
                X[nan_mask] = np.take(col_medians, np.where(nan_mask)[1])

            probs = self.model.predict_proba(X)
            prob_up = probs[:, 1] * 100

            return pd.Series(prob_up.round(1), index=df.index, name="lgbm_confidence")

        except Exception as e:
            log.warning("LightGBM batch prediction error: %s", e)
            return pd.Series(50.0, index=df.index, name="lgbm_confidence")

    def get_feature_importance(self) -> dict:
        """Return feature importances sorted descending."""
        return dict(sorted(self._feature_importance.items(), key=lambda x: -x[1]))


# ── Module-level singleton ─────────────────────────────────────────────────────
_predictor: LightGBMPredictor | None = None


def get_lgbm_predictor(model_path: str = "models/lgbm_model.pkl") -> LightGBMPredictor:
    """Return the global LightGBM predictor singleton (lazy-loaded)."""
    global _predictor
    if _predictor is None:
        _predictor = LightGBMPredictor(model_path=model_path)
    return _predictor
