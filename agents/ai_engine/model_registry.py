"""
TradeAI-PRO — Central AI Model Registry & Warmup Engine.

Loads and initializes all machine learning & deep learning predictors once at
application startup into singleton memory, configuring single-threaded CPU execution
for Render Free Tier (512MB RAM constraint).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)


class ModelRegistry:
    """Manages lazy and eager initialization of all TradeAI ML models."""

    def __init__(self):
        self._is_warmed_up = False
        self._warmup_summary: dict[str, Any] = {}

    def warmup_all(self) -> dict[str, Any]:
        """
        Pre-load and initialize all model weights and predictors once off the main thread.
        Constrains PyTorch to 1 CPU thread.
        """
        if self._is_warmed_up:
            return self._warmup_summary

        t0 = time.perf_counter()
        log.info("[ModelRegistry] Beginning AI Model warmup sequence...")

        # 1. Thread settings for CPU-only deployment (Render Free Tier)
        try:
            import torch
            torch.set_num_threads(1)
            torch.set_num_interop_threads(1)
        except Exception:
            pass

        summary: dict[str, Any] = {
            "xgboost": False,
            "lightgbm": False,
            "lstm": False,
            "logistic": False,
            "anomaly_detector": False,
            "meta_learner": False,
            "calibrator": False,
        }

        # 2. XGBoost Predictor
        try:
            from agents.ai_engine.xgboost_model import get_xgb_predictor
            xgb = get_xgb_predictor()
            summary["xgboost"] = xgb.is_fitted
        except Exception as exc:
            log.debug("[ModelRegistry] XGBoost warmup skipped: %s", exc)

        # 3. LightGBM Predictor
        try:
            from agents.ai_engine.lightgbm_model import get_lgbm_predictor
            lgbm = get_lgbm_predictor()
            summary["lightgbm"] = lgbm.is_fitted
        except Exception as exc:
            log.debug("[ModelRegistry] LightGBM warmup skipped: %s", exc)

        # 4. Logistic Baseline Predictor
        try:
            from agents.ai_engine.logistic_baseline import get_logistic_predictor
            logit = get_logistic_predictor()
            summary["logistic"] = logit.is_fitted
        except Exception as exc:
            log.debug("[ModelRegistry] Logistic warmup skipped: %s", exc)

        # 5. Trading LSTM Model
        try:
            from agents.ai_engine.lstm_model import get_lstm_model
            lstm = get_lstm_model(input_size=10)
            summary["lstm"] = lstm is not None
        except Exception as exc:
            log.debug("[ModelRegistry] LSTM warmup skipped: %s", exc)

        # 6. Market Anomaly Detector
        try:
            from agents.ai_engine.anomaly_detector import get_anomaly_detector
            anom = get_anomaly_detector()
            summary["anomaly_detector"] = anom.is_fitted
        except Exception as exc:
            log.debug("[ModelRegistry] Anomaly detector warmup skipped: %s", exc)

        # 7. Meta Stacking Learner
        try:
            from agents.ai_engine.meta_learner import get_meta_learner
            meta = get_meta_learner()
            summary["meta_learner"] = meta.is_fitted
        except Exception as exc:
            log.debug("[ModelRegistry] Meta learner warmup skipped: %s", exc)

        # 8. Probability Calibrator
        try:
            from agents.ai_engine.calibrator import get_calibrator
            calib = get_calibrator()
            summary["calibrator"] = calib.is_fitted
        except Exception as exc:
            log.debug("[ModelRegistry] Calibrator warmup skipped: %s", exc)

        elapsed = time.perf_counter() - t0
        summary["duration_seconds"] = round(elapsed, 2)
        summary["warmed_up"] = True
        summary["initialized"] = True
        summary["models"] = {
            k: v
            for k, v in summary.items()
            if k not in ("duration_seconds", "warmed_up", "initialized")
        }
        self._warmup_summary = summary
        self._is_warmed_up = True

        log.info(
            "[ModelRegistry] Warmup sequence completed in %.2fs. Models: %s",
            elapsed,
            {k: v for k, v in summary.items() if k not in ("duration_seconds", "warmed_up")},
        )
        return summary

    def get_status(self) -> dict[str, Any]:
        """Return current status of all registered models."""
        if not self._is_warmed_up:
            return {"warmed_up": False}
        return dict(self._warmup_summary)


# Global singleton
_REGISTRY: ModelRegistry | None = None


def get_model_registry() -> ModelRegistry:
    """Return the global ModelRegistry singleton."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ModelRegistry()
    return _REGISTRY
