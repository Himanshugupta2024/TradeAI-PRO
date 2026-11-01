"""
Market Anomaly Detector using Isolation Forest (Graduated Guardrail).

Monitors market bars for abnormal conditions (flash crashes, earnings gaps,
extreme volume/ATR shocks, or uncoordinated market divergence) and executes
a 3-tier graduated response:
  Tier 1 - CLEAN:          Normal market state (multiplier = 1.0)
  Tier 2 - MILD_ANOMALY:   Elevated volatility shock (multiplier = 0.75, telemetry flag)
  Tier 3 - SEVERE_ANOMALY: Extreme outlier (multiplier = 0.0, HARD VETO / SKIP)

Features (6 Multi-Factor Anomaly Inputs):
  1. overnight_gap_pct:    (Open_t - Close_{t-1}) / Close_{t-1}
  2. range_to_atr:         (High_t - Low_t) / ATR_14
  3. return_zscore_20:     Rolling 20-bar return z-score
  4. volume_zscore_20:     Rolling 20-bar volume z-score
  5. vwap_dist_pct:        (Close_t - VWAP_t) / VWAP_t
  6. market_divergence:    Symbol return - NIFTY return (or 0.0 if standalone)
"""

from __future__ import annotations

import logging
import os
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from config import settings

log = logging.getLogger(__name__)

ANOMALY_FEATURE_COLS = [
    "overnight_gap_pct",
    "range_to_atr",
    "return_zscore_20",
    "volume_zscore_20",
    "vwap_dist_pct",
    "market_divergence",
]

DEFAULT_MODEL_PATH = "models/anomaly_detector.pkl"


def compute_anomaly_features(
    df: pd.DataFrame,
    market_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Compute the 6 multi-factor anomaly features from an OHLCV DataFrame.

    Args:
        df: DataFrame with open, high, low, close, volume, ATR_14 (or calculates it).
        market_df: Optional index/benchmark DataFrame (e.g. NIFTY 50) with 'close'.

    Returns:
        DataFrame with ANOMALY_FEATURE_COLS, index aligned to df.
    """
    if df.empty or len(df) < 2:
        return pd.DataFrame(0.0, index=df.index, columns=ANOMALY_FEATURE_COLS)

    features = pd.DataFrame(index=df.index)

    close = df["close"]
    open_p = df["open"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # 1. Overnight / bar-to-bar gap percentage
    prev_close = close.shift(1).bfill()
    features["overnight_gap_pct"] = ((open_p - prev_close) / prev_close.replace(0, np.nan)).fillna(0.0)

    # 2. Bar range to ATR ratio
    atr_col = f"ATR_{settings.ATR_PERIOD}"
    if atr_col in df.columns:
        atr = df[atr_col].replace(0, np.nan).bfill().fillna(1.0)
    else:
        # Fallback approximation: simple rolling high-low spread
        atr = (high - low).rolling(14, min_periods=1).mean().replace(0, np.nan).fillna(1.0)
    features["range_to_atr"] = ((high - low) / atr).fillna(1.0).clip(0.0, 20.0)

    # 3. Return z-score (20-period rolling)
    returns = close.pct_change().fillna(0.0)
    ret_mean = returns.rolling(20, min_periods=5).mean().fillna(0.0)
    ret_std = returns.rolling(20, min_periods=5).std().replace(0, np.nan).fillna(1.0)
    features["return_zscore_20"] = ((returns - ret_mean) / ret_std).fillna(0.0).clip(-10.0, 10.0)

    # 4. Volume z-score (20-period rolling)
    vol_mean = volume.rolling(20, min_periods=5).mean().fillna(volume)
    vol_std = volume.rolling(20, min_periods=5).std().replace(0, np.nan).fillna(1.0)
    features["volume_zscore_20"] = ((volume - vol_mean) / vol_std).fillna(0.0).clip(-10.0, 10.0)

    # 5. Distance from rolling intraday VWAP
    if "vwap" in df.columns:
        vwap = df["vwap"]
    else:
        # Approximate rolling VWAP over 20 bars
        typical_price = (high + low + close) / 3.0
        pv = typical_price * volume
        cum_pv = pv.rolling(20, min_periods=1).sum()
        cum_vol = volume.rolling(20, min_periods=1).sum().replace(0, np.nan).fillna(1.0)
        vwap = cum_pv / cum_vol
    features["vwap_dist_pct"] = ((close - vwap) / vwap.replace(0, np.nan)).fillna(0.0).clip(-0.5, 0.5)

    # 6. Market Divergence
    if market_df is not None and not market_df.empty and "close" in market_df.columns:
        market_ret = market_df["close"].pct_change().reindex(df.index).fillna(0.0)
        features["market_divergence"] = (returns - market_ret).clip(-0.5, 0.5)
    else:
        features["market_divergence"] = 0.0

    return features[ANOMALY_FEATURE_COLS].fillna(0.0)


class MarketAnomalyDetector:
    """
    Detects market anomalies using an Isolation Forest with graduated tiers.
    """

    def __init__(
        self,
        mild_threshold: float = 0.05,
        severe_threshold: float = -0.08,
        model_path: str = DEFAULT_MODEL_PATH,
    ):
        self.mild_threshold = mild_threshold
        self.severe_threshold = severe_threshold
        self.model_path = model_path
        self._model: IsolationForest | None = None
        self._is_fitted: bool = False
        self._load_if_available()

    def _load_if_available(self) -> None:
        if os.path.exists(self.model_path):
            try:
                with open(self.model_path, "rb") as f:
                    self._model = pickle.load(f)
                    self._is_fitted = True
                    log.info("[AnomalyDetector] Loaded model from %s", self.model_path)
            except Exception as exc:
                log.warning("[AnomalyDetector] Failed to load %s: %s", self.model_path, exc)

    def fit(
        self,
        df: pd.DataFrame,
        market_df: pd.DataFrame | None = None,
        save: bool = True,
    ) -> MarketAnomalyDetector:
        """
        Fit Isolation Forest on historical training data.
        Uses low tree count (n_estimators=50) for fast execution and low memory.
        """
        feats = compute_anomaly_features(df, market_df=market_df)
        if len(feats) < 20:
            log.warning("[AnomalyDetector] Insufficient data to fit (%d rows)", len(feats))
            return self

        # Contamination is set to 'auto' so decision_function threshold aligns natively
        self._model = IsolationForest(
            n_estimators=50,
            contamination="auto",
            random_state=42,
            n_jobs=1,  # Keep single threaded for Render free-tier
        )
        self._model.fit(feats)
        self._is_fitted = True

        if save:
            try:
                Path(self.model_path).parent.mkdir(parents=True, exist_ok=True)
                with open(self.model_path, "wb") as f:
                    pickle.dump(self._model, f)
                log.info("[AnomalyDetector] Saved model to %s", self.model_path)
            except Exception as exc:
                log.warning("[AnomalyDetector] Failed to save %s: %s", self.model_path, exc)

        return self

    def score_row(self, df_or_row: pd.DataFrame | pd.Series) -> dict[str, Any]:
        """
        Score a single bar (or latest bar of a DataFrame) for anomalous conditions.

        Returns:
            {
                "tier": "CLEAN" | "MILD_ANOMALY" | "SEVERE_ANOMALY",
                "anomaly_score": float,  # decision_function value (higher = more normal)
                "confidence_multiplier": float,  # 1.0, 0.75, or 0.0
                "is_vetoed": bool,  # True if severe anomaly -> NO TRADE
                "reason": str,
                "anomaly_features": dict,
            }
        """
        if isinstance(df_or_row, pd.Series):
            df = pd.DataFrame([df_or_row])
        else:
            df = df_or_row

        if df.empty:
            return {
                "tier": "CLEAN",
                "anomaly_score": 0.10,
                "confidence_multiplier": 1.0,
                "is_vetoed": False,
                "reason": "empty_input",
                "anomaly_features": {},
            }

        feats = compute_anomaly_features(df)
        latest_feats = feats.iloc[-1]

        # If model is fitted, use its decision_function
        if self._is_fitted and self._model is not None:
            try:
                score = float(self._model.decision_function(feats.iloc[[-1]])[0])
            except Exception as exc:
                log.debug("[AnomalyDetector] Prediction failed, using heuristic: %s", exc)
                score = self._heuristic_score(latest_feats)
        else:
            # Fallback heuristic score based on feature bounds
            score = self._heuristic_score(latest_feats)

        # Graduated 3-Tier Evaluation
        if score < self.severe_threshold:
            tier = "SEVERE_ANOMALY"
            multiplier = 0.0
            vetoed = True
            reason = "Extreme market shock detected (flash crash / volume / gap outlier)"
        elif score < self.mild_threshold:
            tier = "MILD_ANOMALY"
            multiplier = 0.75
            vetoed = False
            reason = "Elevated volatility anomaly detected (confidence penalized 25%)"
        else:
            tier = "CLEAN"
            multiplier = 1.0
            vetoed = False
            reason = "Market condition normal"

        return {
            "tier": tier,
            "anomaly_score": round(score, 4),
            "confidence_multiplier": multiplier,
            "is_vetoed": vetoed,
            "reason": reason,
            "anomaly_features": {k: round(float(v), 4) for k, v in latest_feats.items()},
        }

    def _heuristic_score(self, row: pd.Series) -> float:
        """
        Fast heuristic scoring when model is unfitted.
        Returns a score mimicking decision_function (typically centered near 0.05 to 0.15).
        """
        penalty = 0.0
        # Extreme gap > 4%
        if abs(row.get("overnight_gap_pct", 0.0)) > 0.04:
            penalty += 0.15
        # Range > 3.5x ATR
        if row.get("range_to_atr", 1.0) > 3.5:
            penalty += 0.12
        # Return z-score > 3.5 sigma
        if abs(row.get("return_zscore_20", 0.0)) > 3.5:
            penalty += 0.12
        # Volume z-score > 4.0 sigma
        if row.get("volume_zscore_20", 0.0) > 4.0:
            penalty += 0.10

        return 0.12 - penalty


# ── Global Singleton Getter ───────────────────────────────────────────────────

_ANOMALY_DETECTOR: MarketAnomalyDetector | None = None


def get_anomaly_detector() -> MarketAnomalyDetector:
    global _ANOMALY_DETECTOR
    if _ANOMALY_DETECTOR is None:
        _ANOMALY_DETECTOR = MarketAnomalyDetector()
    return _ANOMALY_DETECTOR
