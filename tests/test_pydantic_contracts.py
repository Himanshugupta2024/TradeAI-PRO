"""
Unit tests for Phase 2: Data Contracts & Type Safety.
Verifies:
  - WatchlistQuoteItem & WatchlistQuotesResponse Pydantic schema validation.
  - RiskStateResponse Pydantic schema validation.
  - FastAPI endpoints /api/watchlist/quotes and /api/risk/state adhere strictly to Pydantic response models.
  - OpenAPI schema generation includes required response models.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.schemas import (
    WatchlistQuoteItem,
    WatchlistQuotesResponse,
    RiskStateResponse,
)
from server import app

client = TestClient(app)


def test_watchlist_quote_item_defaults():
    """Verify default values on WatchlistQuoteItem ensure fail-safe rendering."""
    item = WatchlistQuoteItem(symbol="RELIANCE")
    assert item.symbol == "RELIANCE"
    assert item.company_name == ""
    assert item.signal == "HOLD"
    assert item.is_actionable is False
    assert item.levels_source == "UNAVAILABLE"
    assert item.status == "OK"


def test_watchlist_quotes_response_model_validation():
    """Verify that a full dictionary conforms to WatchlistQuotesResponse."""
    raw = {
        "quotes": {
            "RELIANCE": {
                "symbol": "RELIANCE",
                "company_name": "Reliance Industries Ltd",
                "ltp": 1280.5,
                "change_pct": 1.25,
                "signal": "HOLD",
                "confidence": 65,
                "is_actionable": False,
                "key_support": 1240.0,
                "key_resistance": 1320.0,
                "breakout_trigger": 1315.0,
                "status": "OK",
            },
            "JPL": {
                "symbol": "JPL",
                "company_name": "JPL",
                "ltp": 0.0,
                "change_pct": 0.0,
                "signal": "INSUFFICIENT_DATA",
                "confidence": None,
                "is_actionable": False,
                "status": "INSUFFICIENT_DATA",
            }
        },
        "count": 2,
    }
    validated = WatchlistQuotesResponse.model_validate(raw)
    assert validated.count == 2
    assert "RELIANCE" in validated.quotes
    assert validated.quotes["RELIANCE"].breakout_trigger == 1315.0
    assert "JPL" in validated.quotes
    assert validated.quotes["JPL"].confidence is None
    assert validated.quotes["JPL"].status == "INSUFFICIENT_DATA"


def test_risk_state_response_model_validation():
    """Verify RiskStateResponse schema bounds and types."""
    raw = {
        "circuit_breaker_active": False,
        "circuit_breaker_reason": "",
        "drawdown_pct": 0.012,
        "drawdown_threshold": 0.05,
        "portfolio_heat_pct": 0.008,
        "portfolio_heat_max": 0.03,
        "consecutive_losses": 0,
        "max_consecutive_losses": 3,
        "daily_loss_pct": 0.004,
        "daily_loss_limit_pct": 0.02,
        "max_open_positions": 5,
    }
    validated = RiskStateResponse.model_validate(raw)
    assert validated.circuit_breaker_active is False
    assert validated.portfolio_heat_pct == 0.008
    assert validated.max_open_positions == 5


def test_endpoint_risk_state_contract():
    """GET /api/risk/state must return JSON adhering strictly to RiskStateResponse."""
    resp = client.get("/api/risk/state")
    assert resp.status_code == 200
    data = resp.json()
    validated = RiskStateResponse.model_validate(data)
    assert isinstance(validated.circuit_breaker_active, bool)
    assert isinstance(validated.drawdown_pct, float)
    assert isinstance(validated.portfolio_heat_pct, float)


def test_endpoint_watchlist_quotes_contract():
    """GET /api/watchlist/quotes must return JSON adhering to WatchlistQuotesResponse."""
    resp = client.get("/api/watchlist/quotes?symbols=RELIANCE,TCS")
    assert resp.status_code == 200
    data = resp.json()
    validated = WatchlistQuotesResponse.model_validate(data)
    assert isinstance(validated.count, int)
    assert isinstance(validated.quotes, dict)
    if "RELIANCE" in validated.quotes:
        q = validated.quotes["RELIANCE"]
        assert q.symbol == "RELIANCE"
        assert isinstance(q.ltp, float)
        assert isinstance(q.change_pct, float)


def test_openapi_contains_response_schemas():
    """Verify that FastAPI OpenAPI schema exports our Pydantic response models."""
    openapi = app.openapi()
    schemas = openapi.get("components", {}).get("schemas", {})
    assert "WatchlistQuotesResponse" in schemas
    assert "WatchlistQuoteItem" in schemas
    assert "RiskStateResponse" in schemas
