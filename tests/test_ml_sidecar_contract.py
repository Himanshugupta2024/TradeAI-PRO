"""
Unit tests for the ML Inference Sidecar (Box B) and ML Client (Box A) Contract.
Verifies DataFrame serialization, naive-IST datetime preservation, authentication,
fail-open fallback behavior, and PyTorch isolation.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.ml_client import serialize_df, deserialize_df
from ml_server import app


def _create_sample_df(n_bars: int = 70) -> pd.DataFrame:
    base_time = datetime(2026, 11, 16, 9, 15)
    dates = [base_time + timedelta(minutes=5 * i) for i in range(n_bars)]
    data = {
        "timestamp": dates,
        "open": [100.0 + i * 0.1 for i in range(n_bars)],
        "high": [102.0 + i * 0.1 for i in range(n_bars)],
        "low": [99.0 + i * 0.1 for i in range(n_bars)],
        "close": [101.0 + i * 0.1 for i in range(n_bars)],
        "volume": [1000 + i * 10 for i in range(n_bars)],
        "rsi": [55.0 for _ in range(n_bars)],
        "ema_20": [100.5 + i * 0.1 for i in range(n_bars)],
        "ema_50": [99.5 + i * 0.1 for i in range(n_bars)],
        "ema_200": [95.0 + i * 0.1 for i in range(n_bars)],
    }
    return pd.DataFrame(data)


def test_df_serialization_roundtrip_equality():
    df = _create_sample_df(65)
    serialized = serialize_df(df, max_rows=60)
    recovered = deserialize_df(serialized)

    assert len(recovered) == 60
    assert list(recovered.columns) == list(df.columns)
    assert (recovered["close"].values == df["close"].tail(60).values).all()
    assert recovered["timestamp"].iloc[0] == df["timestamp"].iloc[-60]


def test_ml_server_health_endpoint():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "ml_sidecar"
    assert "memory_rss_mb" in data
    assert "models" in data


def test_ml_server_infer_auth_guard(monkeypatch):
    monkeypatch.setenv("ML_INTERNAL_TOKEN", "super-secret-pass-123")
    client = TestClient(app)
    df = _create_sample_df(60)
    df_json = serialize_df(df)

    # 1. Missing header -> 403 Forbidden
    resp = client.post("/infer", json={"symbol": "RELIANCE", "df_json": df_json})
    assert resp.status_code == 403
    assert "Invalid internal authorization token" in resp.json()["detail"]

    # 2. Wrong header -> 403 Forbidden
    resp = client.post(
        "/infer",
        json={"symbol": "RELIANCE", "df_json": df_json},
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp.status_code == 403

    # 3. Valid header -> 200 OK
    resp = client.post(
        "/infer",
        json={"symbol": "RELIANCE", "df_json": df_json},
        headers={"X-Internal-Token": "super-secret-pass-123"},
    )
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["source"] == "ML_SIDECAR"
    assert "direction" in res_data
    assert "confidence" in res_data


def test_ml_server_infer_empty_payload():
    client = TestClient(app)
    resp = client.post("/infer", json={"symbol": "RELIANCE", "df_json": ""})
    assert resp.status_code == 400


def test_ensemble_lightweight_fallback_honesty(monkeypatch):
    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "true")
    monkeypatch.delenv("ML_SIDECAR_URL", raising=False)

    from agents.ai_engine.ensemble import predict_advanced
    df = _create_sample_df(60)
    result = predict_advanced(df, symbol="TEST_FALLBACK", force_refresh=True)

    assert result["source"] == "LOCAL_HEURISTIC"
    assert result["lightweight_mode"] is True
    assert "direction" in result
    assert "confidence" in result


def test_ensemble_remote_sidecar_routing(monkeypatch):
    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "true")
    monkeypatch.setenv("ML_SIDECAR_URL", "http://mock-ml-sidecar.local")

    mock_response = {
        "direction": "UP",
        "confidence": 84.5,
        "calibrated_trade_win_probability": 84.5,
        "source": "ML_SIDECAR",
    }

    # Mock infer_remote_sync
    import api.ml_client as mlc
    monkeypatch.setattr(mlc, "infer_remote_sync", lambda symbol, df, timeout=None: mock_response)

    from agents.ai_engine.ensemble import predict_advanced
    df = _create_sample_df(60)
    result = predict_advanced(df, symbol="TEST_MOCK_SIDECAR", force_refresh=True)

    assert result["source"] == "ML_SIDECAR"
    assert result["direction"] == "UP"
    assert result["confidence"] == 84.5


def test_pytorch_guard_prevents_torch_import(monkeypatch):
    monkeypatch.setenv("ENABLE_PYTORCH_MODELS", "false")
    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "false")

    from agents.ai_engine.ensemble import predict_advanced
    df = _create_sample_df(60)
    result = predict_advanced(df, symbol="TEST_TORCH_GUARD", force_refresh=True)

    # With ENABLE_PYTORCH_MODELS=false, lstm vote should remain None / unexecuted
    votes = result.get("model_votes", {})
    assert votes.get("lstm") is None
