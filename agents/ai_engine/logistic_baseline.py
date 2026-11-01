"""
Linear Baseline (Logistic Regression) Predictor.

Acts as an Occam's razor anchor alongside complex non-linear models
(XGBoost, LightGBM, LSTM). Learns linear feature relationships across
12 standardized momentum, trend, and volatility features.

If all complex models produce extreme confidence while the linear baseline
shows disagreement, the meta-learner automatically dampens overconfidence.
"""

from __future__ import annotations

import logging
import os
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from config import settings

log = logging.getLogger(__name__)

DEFAULT_LOGISTIC_MODEL_PATH = "models/logistic_model.pkl"
DEFAULT_LOGISTIC_SCALER_PATH = "models/logistic_scaler.pkl"

LOGISTIC_FEATURE_COLS = [
    "rsi_norm",
    "macd_hist_pct",
    "ema20_dist_pct",
    "ema50_dist_pct",
    "ema200_dist_pct",
    "adx_norm",
    "atr_pct",
    "bb_width",
    "return_5",
    "return_20",
    "volume_ratio",
    "candle_position",
]


def extract_logistic_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extract and normalize the 12 linear baseline features from OHLCV + indicators.
    """
    if df.empty or len(df) < 2:
        return pd.DataFrame(0.0, index=df.index, columns=LOGISTIC_FEATURE_COLS)

    feats = pd.DataFrame(index=df.index)
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # 1. RSI normalized around 0 (-2.0 to +2.0)
    rsi = df.get("RSI_14", pd.Series(50.0, index=df.index)).fillna(50.0)
    feats["rsi_norm"] = ((rsi - 50.0) / 25.0).clip(-2.0, 2.0)

    # 2. MACD histogram normalized by close
    macd_hist = df.get("MACD_hist", pd.Series(0.0, index=df.index)).fillna(0.0)
    feats["macd_hist_pct"] = (macd_hist / close.replace(0, np.nan)).fillna(0.0).clip(-0.05, 0.05)

    # 3-5. EMA distances
    for col, out_name in [("EMA_20", "ema20_dist_pct"), ("EMA_50", "ema50_dist_pct"), ("EMA_200", "ema200_dist_pct")]:
        ema = df.get(col, close).fillna(close)
        feats[out_name] = ((close - ema) / ema.replace(0, np.nan)).fillna(0.0).clip(-0.20, 0.20)

    # 6. ADX normalized
    adx_col = f"ADX_{settings.ADX_PERIOD}"
    adx = df.get(adx_col, pd.Series(20.0, index=df.index)).fillna(20.0)
    feats["adx_norm"] = (adx / 50.0).clip(0.0, 2.0)

    # 7. ATR percentage of close
    atr_col = f"ATR_{settings.ATR_PERIOD}"
    atr = df.get(atr_col, pd.Series(1.0, index=df.index)).fillna(1.0)
    feats["atr_pct"] = (atr / close.replace(0, np.nan)).fillna(0.01).clip(0.0, 0.10)

    # 8. Bollinger Band width
    bb_width = df.get("BB_width", pd.Series(0.04, index=df.index)).fillna(0.04)
    feats["bb_width"] = bb_width.clip(0.0, 0.50)

    # 9-10. Multi-horizon returns
    feats["return_5"] = close.pct_change(5).fillna(0.0).clip(-0.15, 0.15)
    feats["return_20"] = close.pct_change(20).fillna(0.0).clip(-0.30, 0.30)

    # 11. Volume ratio vs 20 SMA
    vol_sma = volume.rolling(20, min_periods=1).mean().replace(0, np.nan).fillna(1.0)
    feats["volume_ratio"] = (volume / vol_sma).fillna(1.0).clip(0.1, 10.0)

    # 12. Candle position within range
    rng = (high - low).replace(0, np.nan).fillna(1.0)
    feats["candle_position"] = ((close - low) / rng).fillna(0.5).clip(0.0, 1.0)

    return feats[LOGISTIC_FEATURE_COLS].fillna(0.0)


class LogisticBaselinePredictor:
    """
    Occam's razor regularizer model using Logistic Regression.
    """

    def __init__(
        self,
        model_path: str = DEFAULT_LOGISTIC_MODEL_PATH,
        scaler_path: str = DEFAULT_LOGISTIC_SCALER_PATH,
    ):
        self.model_path = model_path
        self.scaler_path = scaler_path
        self._model: LogisticRegression | None = None
        self._scaler: StandardScaler | None = None
        self._is_fitted: bool = False
        self._load_if_available()

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted

    def _load_if_available(self) -> None:
        if os.path.exists(self.model_path) and os.path.exists(self.scaler_path):
            try:
                with open(self.model_path, "rb") as fm, open(self.scaler_path, "rb") as fs:
                    self._model = pickle.load(fm)
                    self._scaler = pickle.load(fs)
                    self._is_fitted = True
                    log.info("[LogisticBaseline] Loaded model from %s", self.model_path)
            except Exception as exc:
                log.warning("[LogisticBaseline] Failed to load %s: %s", self.model_path, exc)

    def fit(
        self,
        df: pd.DataFrame,
        y: pd.Series | np.ndarray,
        save: bool = True,
    ) -> LogisticBaselinePredictor:
        """Fit scaler and logistic regression model on training data."""
        X = extract_logistic_features(df)
        if len(X) < 20:
            log.warning("[LogisticBaseline] Insufficient data to fit (%d rows)", len(X))
            return self

        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X)

        self._model = LogisticRegression(
            C=1.0,
            solver="lbfgs",
            max_iter=200,
            random_state=42,
        )
        self._model.fit(X_scaled, y)
        self._is_fitted = True

        if save:
            try:
                Path(self.model_path).parent.mkdir(parents=True, exist_ok=True)
                with open(self.model_path, "wb") as fm, open(self.scaler_path, "wb") as fs:
                    pickle.dump(self._model, fm)
                    pickle.dump(self._scaler, fs)
                log.info("[LogisticBaseline] Saved model and scaler.")
            except Exception as exc:
                log.warning("[LogisticBaseline] Save failed: %s", exc)

        return self

    def predict_row(self, df_or_row: pd.DataFrame | pd.Series) -> dict[str, Any]:
        """
        Predict direction and win probability for the latest row.

        Returns:
            {
                "direction": "UP" | "DOWN" | "NEUTRAL",
                "confidence": float (0-100),
                "prob_up": float (0-100),
                "prob_down": float (0-100),
            }
        """
        if isinstance(df_or_row, pd.Series):
            df = pd.DataFrame([df_or_row])
        else:
            df = df_or_row

        if df.empty:
            return {"direction": "NEUTRAL", "confidence": 0.0, "prob_up": 50.0, "prob_down": 50.0}

        X = extract_logistic_features(df)
        latest_X = X.iloc[[-1]]

        if self._is_fitted and self._model is not None and self._scaler is not None:
            try:
                X_scaled = self._scaler.transform(latest_X)
                # Probability of class 1 (UP / WIN)
                prob_up = float(self._model.predict_proba(X_scaled)[0, 1]) * 100.0
            except Exception as exc:
                log.debug("[LogisticBaseline] Predict failed, using heuristic: %s", exc)
                prob_up = self._heuristic_prob(latest_X.iloc[0])
        else:
            prob_up = self._heuristic_prob(latest_X.iloc[0])

        prob_down = 100.0 - prob_up
        confidence = abs(prob_up - 50.0) * 2.0

        if prob_up > 55.0:
            direction = "UP"
        elif prob_up < 45.0:
            direction = "DOWN"
        else:
            direction = "NEUTRAL"

        return {
            "direction": direction,
            "confidence": round(confidence, 1),
            "prob_up": round(prob_up, 1),
            "prob_down": round(prob_down, 1),
        }

    def predict_batch(self, df: pd.DataFrame) -> pd.Series:
        """
        Generate continuous probability score series (0 to 100) for all rows.
        """
        if df.empty:
            return pd.Series(dtype=float)

        X = extract_logistic_features(df)
        if self._is_fitted and self._model is not None and self._scaler is not None:
            try:
                X_scaled = self._scaler.transform(X)
                probs = self._model.predict_proba(X_scaled)[:, 1] * 100.0
                return pd.Series(probs, index=df.index, name="logistic_prob").round(1)
            except Exception:
                pass

        # Vectorized heuristic fallback
        scores = []
        for _, row in X.iterrows():
            scores.append(self._heuristic_prob(row))
        return pd.Series(scores, index=df.index, name="logistic_prob").round(1)

    def _heuristic_prob(self, row: pd.Series) -> float:
        """Simple linear heuristic combination when model is unfitted."""
        score = 50.0
        score += float(row.get("rsi_norm", 0.0)) * 10.0
        score += float(row.get("macd_hist_pct", 0.0)) * 250.0
        score += float(row.get("ema20_dist_pct", 0.0)) * 60.0
        score += float(row.get("return_5", 0.0)) * 80.0
        return float(np.clip(score, 10.0, 90.0))


_LOGISTIC_PREDICTOR: LogisticBaselinePredictor | None = None


def get_logistic_predictor() -> LogisticBaselinePredictor:
    global _LOGISTIC_PREDICTOR
    if _LOGISTIC_PREDICTOR is None:
        _LOGISTIC_PREDICTOR = LogisticBaselinePredictor()
    return _LOGISTIC_PREDICTOR
