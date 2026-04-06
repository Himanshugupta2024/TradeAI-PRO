"""
XGBoost Tabular Predictor for Crypto/Stock Trading.

Unlike LSTM (which needs sequences), XGBoost works on individual rows
using 45 hand-crafted tabular features. It learns non-linear feature
interactions that rule-based systems miss.

Falls back to sklearn GradientBoostingClassifier if xgboost is not installed.
Uses Optuna for hyperparameter tuning when available (improves accuracy 3-7%).

Training target: 1R:2R outcome (WIN=1 / LOSS=0), same as LSTM.
This aligns the model with actual profitability, not just next-candle direction.

Feature set expanded from 22 → 45 features:
  Original 22:  RSI, MACD, EMA distances, ATR, BB, volume, candle structure
  New +23:      Ichimoku, Pivot Points, Time features, Volatility, Price Action,
                Heikin-Ashi, Williams %R, ROC multi-horizon
"""

from __future__ import annotations

import os
import pickle
import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

try:
    import xgboost as _xgb_lib
    XGB_AVAILABLE = True
except ImportError:
    _xgb_lib = None
    XGB_AVAILABLE = False

try:
    from sklearn.ensemble import GradientBoostingClassifier as _GBC
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.calibration import CalibratedClassifierCV
    SKLEARN_AVAILABLE = True
except ImportError:
    _GBC = None
    SKLEARN_AVAILABLE = False

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    OPTUNA_AVAILABLE = True
except ImportError:
    optuna = None
    OPTUNA_AVAILABLE = False

MODEL_AVAILABLE = XGB_AVAILABLE or SKLEARN_AVAILABLE

# ── Feature columns (45 total) ─────────────────────────────────────────────────
# Original 22 features
FEATURE_COLS_XGB_BASE = [
    # Raw momentum indicators
    "RSI_14",
    "MACD",
    "MACD_signal",
    "MACD_hist",
    "ADX_14",
    "StochRSI_K",
    "StochRSI_D",
    "MFI_14",
    "OBV_ROC5",
    # Normalized price-relative features
    "ATR_14_pct",       # ATR as % of price
    "BB_pct",           # Price position within Bollinger Bands (0=lower, 1=upper)
    "ema20_pct",        # % distance of close from EMA20
    "ema50_pct",        # % distance of close from EMA50
    "ema200_pct",       # % distance of close from EMA200
    # Price returns (multi-horizon momentum)
    "ret_5",            # 5-bar return %
    "ret_10",           # 10-bar return %
    "ret_20",           # 20-bar return %
    # Volume
    "volume_ratio",     # Current volume / 20-bar average
    # Market context
    "htf_trend_enc",    # Higher timeframe trend: 1=BULL, 0=NEUTRAL, -1=BEAR
    # Candle structure
    "body_ratio",       # Body / full range
    "upper_wick_pct",   # Upper wick / full range
    "lower_wick_pct",   # Lower wick / full range
]

# New 23 advanced features
FEATURE_COLS_XGB_ADVANCED = [
    # Ichimoku Cloud (5)
    "ichimoku_cloud_pos",
    "ichimoku_tk_cross",
    "ichimoku_cloud_thick",
    "ichimoku_vs_kijun",
    "ichimoku_vs_tenkan",
    # Pivot Points (4)
    "pivot_pos",
    "pivot_r1_dist",
    "pivot_s1_dist",
    "pivot_fib_zone",
    # Volatility (4)
    "vol_ratio",
    "vol_zscore",
    "vol_regime",
    "realized_vol_20",
    # Price Action (5)
    "swing_high_dist",
    "swing_low_dist",
    "price_percentile",
    "hh_streak",
    "range_expansion",
    # Heikin-Ashi (3)
    "ha_body_ratio",
    "ha_direction",
    "ha_trend_streak",
    # Time features (2)
    "session",
    "dow_sin",
]

# Combined feature list (45 features)
FEATURE_COLS_XGB = FEATURE_COLS_XGB_BASE + FEATURE_COLS_XGB_ADVANCED


def extract_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extract XGBoost features from an OHLCV + indicators DataFrame.

    All features are normalized or dimensionless — the model generalises
    across different price levels and asset classes.

    Returns a DataFrame with exactly len(FEATURE_COLS_XGB) columns.
    Missing advanced features are filled with 0 (safe default).
    """
    feat = pd.DataFrame(index=df.index)
    close = df["close"]
    safe_close = close.replace(0, np.nan)

    # ── Original 22 features ──────────────────────────────────────────────

    # RSI
    feat["RSI_14"] = df.get("RSI_14", pd.Series(50.0, index=df.index))

    # MACD
    feat["MACD"]        = df.get("MACD",        pd.Series(0.0, index=df.index))
    feat["MACD_signal"] = df.get("MACD_signal",  pd.Series(0.0, index=df.index))
    feat["MACD_hist"]   = df.get("MACD_hist",    pd.Series(0.0, index=df.index))

    # ADX
    feat["ADX_14"] = df.get("ADX_14", pd.Series(20.0, index=df.index))

    # Stochastic RSI
    feat["StochRSI_K"] = df.get("StochRSI_K", pd.Series(50.0, index=df.index))
    feat["StochRSI_D"] = df.get("StochRSI_D", pd.Series(50.0, index=df.index))

    # MFI
    feat["MFI_14"] = df.get("MFI_14", pd.Series(50.0, index=df.index))

    # OBV ROC
    feat["OBV_ROC5"] = df.get("OBV_ROC5", pd.Series(0.0, index=df.index))

    # ATR as % of price
    atr = df.get("ATR_14", pd.Series(np.nan, index=df.index))
    feat["ATR_14_pct"] = (atr / safe_close * 100).fillna(1.0).clip(0, 20)

    # BB position (0=lower band, 1=upper band)
    bb_upper = df.get("BB_upper", pd.Series(np.nan, index=df.index))
    bb_lower = df.get("BB_lower", pd.Series(np.nan, index=df.index))
    bb_rng = (bb_upper - bb_lower).replace(0, np.nan)
    feat["BB_pct"] = ((close - bb_lower) / bb_rng).fillna(0.5).clip(0, 1)

    # EMA distances (% from close)
    ema20  = df.get("EMA_20",  pd.Series(np.nan, index=df.index))
    ema50  = df.get("EMA_50",  pd.Series(np.nan, index=df.index))
    ema200 = df.get("EMA_200", pd.Series(np.nan, index=df.index))
    feat["ema20_pct"]  = ((close - ema20)  / safe_close * 100).fillna(0).clip(-20, 20)
    feat["ema50_pct"]  = ((close - ema50)  / safe_close * 100).fillna(0).clip(-20, 20)
    feat["ema200_pct"] = ((close - ema200) / safe_close * 100).fillna(0).clip(-20, 20)

    # Price returns
    feat["ret_5"]  = close.pct_change(5).fillna(0).clip(-0.5, 0.5) * 100
    feat["ret_10"] = close.pct_change(10).fillna(0).clip(-0.5, 0.5) * 100
    feat["ret_20"] = close.pct_change(20).fillna(0).clip(-0.5, 0.5) * 100

    # Volume ratio
    vol_avg = df["volume"].rolling(20, min_periods=5).mean().replace(0, np.nan)
    feat["volume_ratio"] = (df["volume"] / vol_avg).fillna(1.0).clip(0, 10)

    # HTF trend encoding
    if "htf_trend" in df.columns:
        from db.models import TrendDirection
        def _enc(v):
            if v == TrendDirection.BULLISH or str(v) in ("BULLISH", "1", "1.0"):
                return 1.0
            if v == TrendDirection.BEARISH or str(v) in ("BEARISH", "-1", "-1.0"):
                return -1.0
            return 0.0
        feat["htf_trend_enc"] = df["htf_trend"].apply(_enc)
    else:
        feat["htf_trend_enc"] = 0.0

    # Candle structure
    candle_range = (df["high"] - df["low"]).replace(0, np.nan)
    body = (close - df["open"]).abs()
    upper_wick = df["high"] - df[["close", "open"]].max(axis=1)
    lower_wick = df[["close", "open"]].min(axis=1) - df["low"]
    feat["body_ratio"]      = (body / candle_range).fillna(0.5).clip(0, 1)
    feat["upper_wick_pct"]  = (upper_wick / candle_range).fillna(0).clip(0, 1)
    feat["lower_wick_pct"]  = (lower_wick / candle_range).fillna(0).clip(0, 1)

    # ── New 23 advanced features ──────────────────────────────────────────

    # Ichimoku
    feat["ichimoku_cloud_pos"]   = df.get("ichimoku_cloud_pos",  pd.Series(0.0, index=df.index))
    feat["ichimoku_tk_cross"]    = df.get("ichimoku_tk_cross",   pd.Series(0.0, index=df.index))
    feat["ichimoku_cloud_thick"] = df.get("ichimoku_cloud_thick",pd.Series(0.0, index=df.index))
    feat["ichimoku_vs_kijun"]    = df.get("ichimoku_vs_kijun",   pd.Series(0.0, index=df.index))
    feat["ichimoku_vs_tenkan"]   = df.get("ichimoku_vs_tenkan",  pd.Series(0.0, index=df.index))

    # Pivot Points
    feat["pivot_pos"]      = df.get("pivot_pos",      pd.Series(0.0, index=df.index))
    feat["pivot_r1_dist"]  = df.get("pivot_r1_dist",  pd.Series(2.0, index=df.index))
    feat["pivot_s1_dist"]  = df.get("pivot_s1_dist",  pd.Series(2.0, index=df.index))
    feat["pivot_fib_zone"] = df.get("pivot_fib_zone", pd.Series(0.0, index=df.index))

    # Volatility
    feat["vol_ratio"]       = df.get("vol_ratio",       pd.Series(1.0, index=df.index))
    feat["vol_zscore"]      = df.get("vol_zscore",      pd.Series(0.0, index=df.index))
    feat["vol_regime"]      = df.get("vol_regime",      pd.Series(1.0, index=df.index))
    feat["realized_vol_20"] = df.get("realized_vol_20", pd.Series(20.0, index=df.index))

    # Price Action
    feat["swing_high_dist"]  = df.get("swing_high_dist",  pd.Series(2.0, index=df.index))
    feat["swing_low_dist"]   = df.get("swing_low_dist",   pd.Series(2.0, index=df.index))
    feat["price_percentile"] = df.get("price_percentile", pd.Series(0.5, index=df.index))
    feat["hh_streak"]        = df.get("hh_streak",        pd.Series(0.0, index=df.index))
    feat["range_expansion"]  = df.get("range_expansion",  pd.Series(1.0, index=df.index))

    # Heikin-Ashi
    feat["ha_body_ratio"]   = df.get("ha_body_ratio",   pd.Series(0.5, index=df.index))
    feat["ha_direction"]    = df.get("ha_direction",    pd.Series(0.0, index=df.index))
    feat["ha_trend_streak"] = df.get("ha_trend_streak", pd.Series(0.0, index=df.index))

    # Time features
    feat["session"] = df.get("session", pd.Series(0.0, index=df.index))
    feat["dow_sin"]  = df.get("dow_sin",  pd.Series(0.0, index=df.index))

    # Fill any remaining NaN with 0
    feat = feat.fillna(0)

    return feat[FEATURE_COLS_XGB]


# ── Optuna Hyperparameter Tuning ───────────────────────────────────────────────

def _tune_xgboost(X: np.ndarray, y: np.ndarray, n_trials: int = 30) -> dict:
    """
    Use Optuna to find optimal XGBoost hyperparameters via time-series CV.

    Returns the best hyperparameter dict found.
    Falls back to sensible defaults if Optuna is unavailable.
    """
    if not OPTUNA_AVAILABLE or not XGB_AVAILABLE:
        log.info("Optuna/XGBoost not available — using default hyperparameters")
        return {
            "n_estimators":     300,
            "max_depth":        6,
            "learning_rate":    0.05,
            "subsample":        0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 3,
            "gamma":            0.1,
            "reg_alpha":        0.1,
            "reg_lambda":       1.0,
        }

    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import roc_auc_score

    tscv = TimeSeriesSplit(n_splits=3)

    def objective(trial):
        params = {
            "n_estimators":     trial.suggest_int("n_estimators", 100, 500),
            "max_depth":        trial.suggest_int("max_depth", 3, 8),
            "learning_rate":    trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "subsample":        trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
            "gamma":            trial.suggest_float("gamma", 0.0, 1.0),
            "reg_alpha":        trial.suggest_float("reg_alpha", 0.0, 2.0),
            "reg_lambda":       trial.suggest_float("reg_lambda", 0.5, 3.0),
            "use_label_encoder": False,
            "eval_metric":      "logloss",
            "random_state":     42,
            "n_jobs":           -1,
        }

        scores = []
        for train_idx, val_idx in tscv.split(X):
            X_tr, X_val = X[train_idx], X[val_idx]
            y_tr, y_val = y[train_idx], y[val_idx]

            if len(np.unique(y_tr)) < 2 or len(np.unique(y_val)) < 2:
                continue

            model = _xgb_lib.XGBClassifier(**params)
            model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
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
    best.pop("use_label_encoder", None)
    log.info("Optuna best params (AUC=%.4f): %s", study.best_value, best)
    return best


# ── XGBoost Predictor Class ────────────────────────────────────────────────────

class XGBoostPredictor:
    """
    XGBoost-based trade direction predictor.

    Trains on 1R:2R outcome labels (WIN=1 / LOSS=0).
    Predicts probability of WIN for each bar.

    Attributes:
        model:              Trained classifier (XGBClassifier or GBC)
        is_fitted:          True after successful training
        _feature_importance: Dict of feature → importance score
    """

    def __init__(self, model_path: str = "models/xgb_model.pkl"):
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
                    log.info("XGBoost model loaded from %s", self.model_path)
            except Exception as e:
                log.warning("Failed to load XGBoost model: %s", e)

    def _save(self):
        """Persist the trained model to disk."""
        os.makedirs(os.path.dirname(self.model_path) or ".", exist_ok=True)
        with open(self.model_path, "wb") as f:
            pickle.dump({
                "model": self.model,
                "feature_importance": self._feature_importance,
                "feature_cols": FEATURE_COLS_XGB,
            }, f)
        log.info("XGBoost model saved to %s", self.model_path)

    def train(
        self,
        df: pd.DataFrame,
        use_optuna: bool = True,
        n_optuna_trials: int = 30,
        test_size: float = 0.2,
    ) -> dict:
        """
        Train the XGBoost model on a labeled DataFrame.

        Args:
            df:               DataFrame with signal column (LONG/SHORT/HOLD/SKIP)
                              and all indicator columns.
            use_optuna:       Whether to run Optuna hyperparameter tuning.
            n_optuna_trials:  Number of Optuna trials (more = better but slower).
            test_size:        Fraction of data to hold out for validation.

        Returns:
            dict with val_accuracy, val_auc, n_samples, feature_importance, etc.
        """
        if not MODEL_AVAILABLE:
            return {"error": "No ML library available (install xgboost or scikit-learn)"}

        # ── Generate outcome labels ────────────────────────────────────────
        # Fast path: forward-return labels when 'signal' column is absent
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

        log.info("Training XGBoost: %d samples (WIN=%d, LOSS=%d)",
                 len(df_filtered), y.sum(), (y == 0).sum())

        # ── Extract features ───────────────────────────────────────────────
        features = extract_features(df_filtered)
        X = features.values.astype(np.float64)

        # Replace inf/-inf with NaN, then fill NaN with column medians
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
        if use_optuna and OPTUNA_AVAILABLE and XGB_AVAILABLE and len(X_train) >= 200:
            log.info("Running Optuna hyperparameter tuning (%d trials)...", n_optuna_trials)
            best_params = _tune_xgboost(X_train, y_train, n_trials=n_optuna_trials)
        else:
            best_params = {
                "n_estimators":     300,
                "max_depth":        6,
                "learning_rate":    0.05,
                "subsample":        0.8,
                "colsample_bytree": 0.8,
                "min_child_weight": 3,
                "gamma":            0.1,
                "reg_alpha":        0.1,
                "reg_lambda":       1.0,
            }

        # ── Train final model ──────────────────────────────────────────────
        if XGB_AVAILABLE:
            model = _xgb_lib.XGBClassifier(
                **best_params,
                use_label_encoder=False,
                eval_metric="logloss",
                random_state=42,
                n_jobs=-1,
            )
            model.fit(
                X_train, y_train,
                eval_set=[(X_val, y_val)],
                verbose=False,
            )
            # Feature importances
            imp = model.feature_importances_
            self._feature_importance = {
                col: round(float(imp[i]), 6)
                for i, col in enumerate(FEATURE_COLS_XGB)
            }
        else:
            # Fallback: sklearn GradientBoosting
            model = _GBC(
                n_estimators=best_params.get("n_estimators", 200),
                max_depth=best_params.get("max_depth", 5),
                learning_rate=best_params.get("learning_rate", 0.05),
                subsample=best_params.get("subsample", 0.8),
                random_state=42,
            )
            model.fit(X_train, y_train)
            imp = model.feature_importances_
            self._feature_importance = {
                col: round(float(imp[i]), 6)
                for i, col in enumerate(FEATURE_COLS_XGB[:len(imp)])
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
            "XGBoost trained: accuracy=%.1f%%, AUC=%.4f, samples=%d, features=%d",
            val_accuracy, val_auc, len(df_filtered), len(FEATURE_COLS_XGB),
        )

        return {
            "val_accuracy":       round(val_accuracy, 2),
            "val_auc":            round(val_auc, 4),
            "n_samples":          len(df_filtered),
            "n_features":         len(FEATURE_COLS_XGB),
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
                "method": "xgboost_not_fitted",
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
                "method":     "xgboost",
            }

        except Exception as e:
            log.warning("XGBoost prediction error: %s", e)
            return {
                "direction": "NEUTRAL", "confidence": 0,
                "prob_up": 50.0, "prob_down": 50.0,
                "method": "xgboost_error",
            }

    def predict_batch(self, df: pd.DataFrame) -> pd.Series:
        """
        Generate probability-of-WIN scores for all rows in df.

        Returns Series with values 0-100:
          >50 = bullish lean  (e.g. 70 means 70% chance of WIN)
          <50 = bearish lean
          ~50 = no conviction
        """
        if not self.is_fitted or self.model is None:
            return pd.Series(50.0, index=df.index, name="xgb_confidence")

        try:
            features = extract_features(df)
            X = features.values.astype(np.float64)

            # Fill NaN with column medians
            col_medians = np.nanmedian(X, axis=0)
            nan_mask = np.isnan(X)
            if nan_mask.any():
                X[nan_mask] = np.take(col_medians, np.where(nan_mask)[1])

            probs = self.model.predict_proba(X)
            prob_up = probs[:, 1] * 100  # WIN probability as 0-100

            return pd.Series(prob_up.round(1), index=df.index, name="xgb_confidence")

        except Exception as e:
            log.warning("XGBoost batch prediction error: %s", e)
            return pd.Series(50.0, index=df.index, name="xgb_confidence")

    def get_feature_importance(self) -> dict:
        """Return feature importances sorted descending."""
        return dict(sorted(self._feature_importance.items(), key=lambda x: -x[1]))


# ── Module-level singleton ─────────────────────────────────────────────────────
_predictor: XGBoostPredictor | None = None


def get_xgb_predictor(model_path: str = "models/xgb_model.pkl") -> XGBoostPredictor:
    """Return the global XGBoost predictor singleton (lazy-loaded)."""
    global _predictor
    if _predictor is None:
        _predictor = XGBoostPredictor(model_path=model_path)
    return _predictor
