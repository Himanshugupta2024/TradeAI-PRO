"""
Unit tests for the ML Inference Sidecar (Box B) and ML Client (Box A) Contract.
Verifies DataFrame serialization, naive-IST datetime preservation, authentication,
fail-closed security, cheap liveness, fail-open fallback behavior, and PyTorch isolation.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.ml_client import serialize_df, deserialize_df
from ml_server import app


def _create_sample_df(n_bars: int = 260) -> pd.DataFrame:
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


def test_df_serialization_roundtrip_equality_250_bars():
    df = _create_sample_df(260)
    serialized = serialize_df(df, max_rows=250)
    recovered = deserialize_df(serialized)

    assert len(recovered) == 250
    assert list(recovered.columns) == list(df.columns)
    assert (recovered["close"].values == df["close"].tail(250).values).all()
    assert recovered["timestamp"].iloc[0] == df["timestamp"].iloc[-250]


def test_df_serialization_datetime_index_roundtrip():
    base_time = datetime(2026, 11, 16, 9, 15)
    dates = [base_time + timedelta(minutes=5 * i) for i in range(30)]
    df = pd.DataFrame(
        {"close": [100.0 + i for i in range(30)], "volume": [1000 for _ in range(30)]},
        index=pd.DatetimeIndex(dates),
    )
    serialized = serialize_df(df, max_rows=25)
    recovered = deserialize_df(serialized)

    assert len(recovered) == 25
    assert (recovered.index == df.index[-25:]).all()


def test_ml_server_cheap_liveness():
    """Verify /health/live is ultra-lightweight and returns in microseconds."""
    client = TestClient(app)
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_ml_server_health_endpoint():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "ml_sidecar"
    assert "memory_rss_mb" in data
    assert "models" in data


def test_ml_server_infer_fail_closed_auth(monkeypatch):
    client = TestClient(app)
    df = _create_sample_df(60)
    df_json = serialize_df(df)

    # 1. When ML_INTERNAL_TOKEN is NOT set on server -> 503 Service Unavailable (Fail closed!)
    monkeypatch.delenv("ML_INTERNAL_TOKEN", raising=False)
    resp = client.post(
        "/infer",
        json={"symbol": "RELIANCE", "df_json": df_json},
        headers={"X-Internal-Token": "some-token"},
    )
    assert resp.status_code == 503
    assert "Sidecar auth misconfigured" in resp.json()["detail"]

    # 2. When ML_INTERNAL_TOKEN is set, but caller provides wrong token -> 403 Forbidden
    monkeypatch.setenv("ML_INTERNAL_TOKEN", "super-secret-pass-123")
    resp = client.post(
        "/infer",
        json={"symbol": "RELIANCE", "df_json": df_json},
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp.status_code == 403

    # 3. When caller provides missing token -> 403 Forbidden
    resp = client.post("/infer", json={"symbol": "RELIANCE", "df_json": df_json})
    assert resp.status_code == 403

    # 4. Valid token -> 200 OK
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


def test_ml_server_infer_empty_payload(monkeypatch):
    monkeypatch.setenv("ML_INTERNAL_TOKEN", "test-token")
    client = TestClient(app)
    resp = client.post(
        "/infer",
        json={"symbol": "RELIANCE", "df_json": ""},
        headers={"X-Internal-Token": "test-token"},
    )
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

    votes = result.get("model_votes", {})
    assert votes.get("lstm") is None


def test_get_model_status_sidecar_awareness(monkeypatch):
    from agents.ai_engine.ensemble import get_model_status
    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "true")

    # 1. With ML_SIDECAR_URL set -> reports active sidecar models
    monkeypatch.setenv("ML_SIDECAR_URL", "https://trading-ml-8umr.onrender.com")
    status_with_sidecar = get_model_status()
    assert status_with_sidecar["sidecar_active"] is True
    assert status_with_sidecar["active_inference_mode"] == "ML_SIDECAR_ENSEMBLE"
    assert "ML Sidecar" in status_with_sidecar["badge_short"]
    assert "XGBoost" in status_with_sidecar["badge_short"]
    assert status_with_sidecar["models_executing"]["xgboost"] is True
    assert status_with_sidecar["models_executing"]["lightgbm"] is True

    # 2. Without ML_SIDECAR_URL set -> honest fallback to lightweight heuristic
    monkeypatch.delenv("ML_SIDECAR_URL", raising=False)
    status_fallback = get_model_status()
    assert status_fallback.get("sidecar_active") is not True
    assert "Lightweight" in status_fallback["mode"]
    assert "⚡" in status_fallback["badge_short"]

