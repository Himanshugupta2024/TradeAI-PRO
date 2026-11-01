"""
Pydantic schemas for API request and response validation.
Ensures strict validation of trading geometry, quantities, prices, and parameters.
"""

from __future__ import annotations

import math
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class PaperTradeRequest(BaseModel):
    """
    Validation schema for manual and automated paper trade execution.
    Enforces trade geometry:
      - LONG / BUY:  stop_loss < entry < target
      - SHORT / SELL: target < entry < stop_loss
    """
    symbol: str = Field(..., min_length=1, description="Trading symbol, e.g. RELIANCE, BTC/USDT")
    direction: str = Field(..., description="Trade direction: LONG, SHORT, BUY, or SELL")
    entry: float = Field(..., gt=0, description="Entry price > 0")
    stop_loss: float = Field(..., gt=0, description="Stop loss price > 0")
    target: float = Field(..., gt=0, description="Target price > 0")
    atr: float = Field(default=0.0, ge=0.0, description="ATR value >= 0")
    confidence: float = Field(default=0.0, ge=0.0, le=100.0, description="Model/signal confidence")
    rsi: float | None = Field(default=None, ge=0.0, le=100.0, description="RSI indicator value")
    rsi_at_entry: float | None = Field(default=None, ge=0.0, le=100.0)
    min_rr: float | None = Field(default=None, gt=0, description="Minimum acceptable R:R ratio")
    quantity: float | None = Field(default=None, gt=0, description="Explicit quantity if overriding position sizer")
    strategy: str = Field(default="MANUAL")
    regime: str = Field(default="TRENDING")
    volume_above_avg: bool = Field(default=False)

    @field_validator("symbol")
    @classmethod
    def clean_symbol(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("symbol cannot be empty")
        return s

    @field_validator("direction")
    @classmethod
    def clean_direction(cls, v: str) -> str:
        d = v.strip().upper()
        if d in ("BUY", "LONG"):
            return "LONG"
        if d in ("SELL", "SHORT"):
            return "SHORT"
        raise ValueError(f"Invalid direction '{v}'. Must be LONG, SHORT, BUY, or SELL.")

    @field_validator("entry", "stop_loss", "target", "atr")
    @classmethod
    def check_finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("Numerical values must be finite (not NaN or Inf)")
        return v

    @model_validator(mode="after")
    def validate_geometry(self) -> PaperTradeRequest:
        if self.direction == "LONG":
            if not (self.stop_loss < self.entry < self.target):
                raise ValueError(
                    f"Invalid trade geometry for LONG: stop_loss ({self.stop_loss}) "
                    f"must be strictly less than entry ({self.entry}), and entry must be strictly less than target ({self.target})"
                )
        elif self.direction == "SHORT":
            if not (self.target < self.entry < self.stop_loss):
                raise ValueError(
                    f"Invalid trade geometry for SHORT: target ({self.target}) "
                    f"must be strictly less than entry ({self.entry}), and entry must be strictly less than stop_loss ({self.stop_loss})"
                )
        return self


class ClosePaperTradeRequest(BaseModel):
    """Schema for closing a paper trade."""
    exit_price: float | None = Field(
        default=None,
        ge=0.0,
        description="Optional exit price (0.0 or None uses current market price)",
    )

    @field_validator("exit_price")
    @classmethod
    def check_exit_finite(cls, v: float | None) -> float | None:
        if v is not None and (math.isnan(v) or math.isinf(v)):
            raise ValueError("exit_price must be finite")
        return v
