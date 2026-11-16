"""
TradeAI Pro — Dedicated ML Inference Sidecar (Box B).
Runs isolated from core API data fetching to stay strictly under Render 512MB RAM cap.
Serves XGBoost, LightGBM, Logistic Regression, MetaLearner, and Isotonic Calibrators.
Strictly NO network data fetching.
"""
from __future__ import annotations

import io
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict

import pandas as pd
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel

logger = logging.getLogger("ml_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure PyTorch is strictly disabled on Box B to prevent 150MB heap inflation
    os.environ.setdefault("ENABLE_PYTORCH_MODELS", "false")
    os.environ["LIGHTWEIGHT_API_MODE"] = "false"

    logger.info("Starting TradeAI ML Inference Sidecar...")
    try:
        from agents.ai_engine.model_registry import get_model_registry
        registry = get_model_registry()
        registry.warmup_all()
        logger.info("ModelRegistry warmup complete on ML Sidecar.")
    except Exception as exc:
        logger.warning("ModelRegistry warmup error on ML Sidecar: %s", exc)
    yield
    logger.info("Shutting down ML Inference Sidecar...")


app = FastAPI(
    title="TradeAI ML Sidecar",
    version="1.0.0",
    description="Dedicated microservice for real-time ML ensemble predictions",
    lifespan=lifespan,
)


class InferRequest(BaseModel):
    symbol: str = ""
    df_json: str
    offline: bool = True
    force_refresh: bool = False


@app.get("/health/live")
def health_live() -> Dict[str, str]:
    """
    Ultra-lightweight liveness probe for Render health check (< 1ms, zero CPU).
    Never touches model registry, psutil, or database.
    """
    return {"status": "alive"}


@app.get("/health")
def health() -> Dict[str, Any]:
    """Rich telemetry endpoint reporting memory and model status."""
    rss_mb = 0.0
    try:
        import psutil
        proc = psutil.Process()
        rss_mb = round(proc.memory_info().rss / 1024 / 1024, 2)
    except Exception:
        pass

    models_status = {}
    try:
        from agents.ai_engine.model_registry import get_model_registry
        registry = get_model_registry()
        models_status = registry.get_status()
    except Exception as exc:
        models_status = {"error": str(exc)}

    return {
        "status": "ok",
        "service": "ml_sidecar",
        "memory_rss_mb": rss_mb,
        "models": models_status,
    }


@app.post("/infer")
def infer(
    req: InferRequest,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
) -> Dict[str, Any]:
    # Fail-closed auth contract: sidecar MUST be configured with a token
    expected_token = os.environ.get("ML_INTERNAL_TOKEN", "").strip()
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sidecar auth misconfigured: ML_INTERNAL_TOKEN environment variable is not set",
        )
    if x_internal_token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid internal authorization token",
        )

    if not req.df_json:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="df_json is required",
        )

    try:
        df = pd.read_json(io.StringIO(req.df_json), orient="split")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Failed to deserialize df_json: {exc}",
        )

    if df.empty:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Deserialized dataframe is empty",
        )

    from agents.ai_engine.ensemble import predict_advanced
    res = predict_advanced(
        df,
        symbol=req.symbol,
        offline=req.offline,
        force_refresh=req.force_refresh,
    )
    res["source"] = "ML_SIDECAR"
    return res
