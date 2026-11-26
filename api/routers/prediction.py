"""
TradeAI PRO — 6-Layer Multi-Class Stock Price Prediction Router.
===============================================================
Routes:
  - GET /api/prediction/{symbol}
  - POST /api/prediction/batch
  - GET /api/prediction/validation-metrics
  - GET /api/prediction/status
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/prediction", tags=["prediction"])


class BatchPredictionRequest(BaseModel):
    symbols: List[str] = Field(..., description="List of NSE stock symbols (e.g. ['RELIANCE', 'VEDL'])")
    force_refresh: bool = Field(False, description="Whether to bypass 60s TTL cache")


@router.get("/status")
async def get_prediction_engine_status() -> Dict[str, Any]:
    """Get status and layer availability for the 6-layer prediction engine."""
    from config import settings
    return {
        "engine": "TradeAI PRO 6-Layer Advanced Prediction Engine",
        "version": "1.0.0",
        "status": "ONLINE",
        "horizon": "2-5 Trading Days (Swing)",
        "lightweight_api_mode": bool(getattr(settings, "LIGHTWEIGHT_API_MODE", True)),
        "layers": {
            "L1_market_data": "ACTIVE (Yahoo daily + ^NSEI + ^INDIAVIX, 15m delay tracking)",
            "L2_technical_analysis": "ACTIVE (EMA, VWAP, RSI, MACD, ATR, Supertrend, ADX, Volume Profile, Pivots)",
            "L3_probabilistic_ensemble": "ACTIVE (3-Class: UP, DOWN, SIDEWAYS sum to 100%)",
            "L4_market_intelligence": "ACTIVE (NIFTY 50 20d RS, India VIX Volatility Gate, 2-Axis Regime)",
            "L5_risk_and_range": "ACTIVE (Dynamic ATR Volatility Envelope & Embargoed Validation)",
            "L6_dashboard_delivery": "ACTIVE (Deterministic Templated Rationales & Structured Contract)",
        },
    }


@router.get("/validation-metrics")
async def get_walk_forward_metrics(
    symbol: str = Query("RELIANCE", description="Symbol to run walk-forward backtest evaluation on"),
) -> Dict[str, Any]:
    """
    Run walk-forward validation with zero-leakage embargo gap on historical data.
    Returns Multi-class Brier score, directional accuracy, profit factor, and max drawdown.
    """
    is_lightweight = os.getenv("LIGHTWEIGHT_API_MODE", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if is_lightweight:
        raise HTTPException(
            status_code=409,
            detail="Walk-forward validation backtest is disabled in LIGHTWEIGHT_API_MODE to protect the 512MB RAM budget.",
        )

    clean_sym = symbol.strip().upper().replace(".NS", "")
    from data.providers.yahoo import YahooProvider

    provider = YahooProvider(timeout=10.0)
    ohlcv_map = await provider.get_batch_ohlcv([clean_sym], period="1y", interval="1d")
    df = ohlcv_map.get(clean_sym)

    if df is None or df.empty or len(df) < 120:
        return {
            "symbol": clean_sym,
            "status": "INSUFFICIENT_DATA",
            "message": "At least 120 daily bars required for walk-forward validation.",
        }

    from agents.ai_engine.backtest_validator import run_walk_forward_backtest

    metrics = await asyncio.to_thread(
        run_walk_forward_backtest, df, n_splits=4, embargo_bars=5, forward_bars=3
    )
    metrics["symbol"] = clean_sym
    return metrics


@router.get("/{symbol}")
async def get_single_stock_prediction(
    symbol: str,
    force: bool = Query(False, description="Force refresh bypass cache"),
) -> Dict[str, Any]:
    """
    Get 6-layer stock price forecast for a single symbol:
      - 3-class probability distribution (UP, DOWN, SIDEWAYS summing to 100%)
      - Volatility price range (lower_range, upper_range, expected_target, stop_loss)
      - Calibrated confidence score
      - Data delay transparency (15 minutes)
    """
    from agents.ai_engine.prediction_engine import predict_stock_price

    clean_sym = symbol.strip().upper()
    if not clean_sym:
        raise HTTPException(status_code=400, detail="Stock symbol must be specified")

    res = await predict_stock_price(clean_sym, force_refresh=force)
    if "source" not in res and res.get("status") == "PREDICTION_ACTIVE":
        res["source"] = "LOCAL_HEURISTIC"
    return res


@router.post("/batch")
async def get_batch_stock_predictions(
    req: BatchPredictionRequest,
) -> Dict[str, Any]:
    """Batch evaluate 6-layer predictions for multiple NSE stocks."""
    if not req.symbols:
        return {"count": 0, "predictions": {}}

    from agents.ai_engine.prediction_engine import batch_predict_stocks

    preds = await batch_predict_stocks(req.symbols, force_refresh=req.force_refresh)
    return {
        "count": len(preds),
        "predictions": preds,
    }
