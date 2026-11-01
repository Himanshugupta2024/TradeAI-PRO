"""
Stacking Meta-Learner with Regime Interaction Features.

Dynamically combines base model predictions (XGBoost, LightGBM, LSTM,
Logistic Baseline, Statistical Heuristic) with Two-Axis Market Regime
and context features.

Key innovation: Explicit interaction terms allow a linear/logistic meta-estimator
to dynamically scale model trust without requiring deep non-linear stacks:
  - In strong trends (high trend_score): LSTM & XGBoost interaction terms dominate.
  - In ranging markets (low trend_score): Statistical anchor & LightGBM dominate.
  - In high volatility / disagreement: confidence is naturally dampened.

Option B Fail-Safe:
  If any critical model is absent or unfitted, the system cleanly bypasses
  the meta-learner and falls back to guardrailed dynamic weighting.
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

log = logging.getLogger(__name__)

DEFAULT_META_MODEL_PATH = "models/meta_learner.pkl"

META_FEATURE_NAMES = [
    "xgb_prob",
    "lgbm_prob",
    "lstm_prob",
    "logit_prob",
    "stat_score",
    "trend_score",
    "volatility_score",
    "adx_norm",
    "atr_pct",
    # Interaction terms
    "xgb_x_trend",
    "lstm_x_trend",
    "stat_x_range",
    "lgbm_x_range",
    "stat_x_vol",
    "model_disagreement",
]


def build_meta_feature_vector(
    xgb_prob: float,
    lgbm_prob: float,
    lstm_prob: float,
    logit_prob: float,
    stat_score: float,
    trend_score: float,
    volatility_score: float,
    adx: float,
    atr_pct: float,
) -> pd.DataFrame:
    """
    Construct the meta-feature row including interaction terms.
    All probabilities are normalized to 0.0 - 1.0.
    """
    probs = [xgb_prob, lgbm_prob, lstm_prob, logit_prob, stat_score]
    disagreement = float(max(probs) - min(probs))
    range_score = float(max(0.0, 1.0 - abs(trend_score)))
    adx_norm = float(np.clip(adx / 50.0, 0.0, 2.0))

    row = {
        "xgb_prob": float(xgb_prob),
        "lgbm_prob": float(lgbm_prob),
        "lstm_prob": float(lstm_prob),
        "logit_prob": float(logit_prob),
        "stat_score": float(stat_score),
        "trend_score": float(trend_score),
        "volatility_score": float(volatility_score),
        "adx_norm": adx_norm,
        "atr_pct": float(atr_pct),
        # Interaction features
        "xgb_x_trend": float(xgb_prob * trend_score),
        "lstm_x_trend": float(lstm_prob * trend_score),
        "stat_x_range": float(stat_score * range_score),
        "lgbm_x_range": float(lgbm_prob * range_score),
        "stat_x_vol": float(stat_score * volatility_score),
        "model_disagreement": disagreement,
    }

    return pd.DataFrame([row], columns=META_FEATURE_NAMES)


class MetaStackingLearner:
    """
    Stacking classifier that learns dynamic model weighting across regimes.
    """

    def __init__(self, model_path: str = DEFAULT_META_MODEL_PATH):
        self.model_path = model_path
        self._model: LogisticRegression | None = None
        self._is_fitted: bool = False
        self._load_if_available()

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted

    def _load_if_available(self) -> None:
        if os.path.exists(self.model_path):
            try:
                with open(self.model_path, "rb") as f:
                    self._model = pickle.load(f)
                    self._is_fitted = True
                    log.info("[MetaLearner] Loaded meta-model from %s", self.model_path)
            except Exception as exc:
                log.warning("[MetaLearner] Failed to load %s: %s", self.model_path, exc)

    def fit(
        self,
        X_meta: pd.DataFrame,
        y: pd.Series | np.ndarray,
        save: bool = True,
    ) -> MetaStackingLearner:
        """
        Fit meta logistic regression on Out-of-Fold base predictions.
        """
        if len(X_meta) < 20:
            log.warning("[MetaLearner] Insufficient data to fit (%d rows)", len(X_meta))
            return self

        self._model = LogisticRegression(
            C=1.0,
            solver="lbfgs",
            max_iter=300,
            random_state=42,
        )
        self._model.fit(X_meta[META_FEATURE_NAMES], y)
        self._is_fitted = True

        if save:
            try:
                Path(self.model_path).parent.mkdir(parents=True, exist_ok=True)
                with open(self.model_path, "wb") as f:
                    pickle.dump(self._model, f)
                log.info("[MetaLearner] Saved meta-model to %s", self.model_path)
            except Exception as exc:
                log.warning("[MetaLearner] Save failed: %s", exc)

        return self

    def predict_meta(
        self,
        X_meta: pd.DataFrame,
    ) -> dict[str, Any]:
        """
        Predict probability of win using the meta-model.
        Returns:
            {
                "meta_active": bool,
                "meta_prob_up": float (0-100),
                "model_disagreement": float,
            }
        """
        if not self._is_fitted or self._model is None:
            return {
                "meta_active": False,
                "meta_prob_up": 50.0,
                "model_disagreement": 0.0,
            }

        try:
            feats = X_meta[META_FEATURE_NAMES]
            prob_up = float(self._model.predict_proba(feats)[0, 1]) * 100.0
            disagreement = float(feats["model_disagreement"].iloc[0])
            return {
                "meta_active": True,
                "meta_prob_up": round(prob_up, 2),
                "model_disagreement": round(disagreement, 3),
            }
        except Exception as exc:
            log.debug("[MetaLearner] Prediction failed: %s", exc)
            return {
                "meta_active": False,
                "meta_prob_up": 50.0,
                "model_disagreement": 0.0,
            }


_META_LEARNER: MetaStackingLearner | None = None


def get_meta_learner() -> MetaStackingLearner:
    global _META_LEARNER
    if _META_LEARNER is None:
        _META_LEARNER = MetaStackingLearner()
    return _META_LEARNER
