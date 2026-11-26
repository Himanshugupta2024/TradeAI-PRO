"""Regression tests for ops guards, async DB offloading, and feed/config hardening."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.execution.paper_trading import PaperTradingEngine
from api import service as svc
from api.routers import admin as admin_router
from api.routers import health as health_router
from api.routers import portfolio as portfolio_router
from api.routers import prediction as prediction_router
from api.routers import swing as swing_router
from config import settings
from server import app


# ── 1. Health Router: Intraday Store Alignment, Probe Auth & Offloading ──────


@pytest.mark.asyncio
async def test_health_check_reads_intraday_signals_and_offloads(monkeypatch):
    loop_tid = threading.get_ident()
    model_tids: list[int] = []
    feed_tids: list[int] = []

    monkeypatch.setattr(health_router, "_HEALTH_RESPONSE_CACHE", {})
    monkeypatch.setattr(health_router, "_HEALTH_RESPONSE_TS", 0.0)
    monkeypatch.setattr(svc, "_signal_cache", {})
    monkeypatch.setattr(svc, "_persisted_signals_snapshot", [])

    def _fake_model_status():
        model_tids.append(threading.get_ident())
        return {"sidecar_active": False, "mode": "Lightweight"}

    def _fake_feed_status():
        feed_tids.append(threading.get_ident())
        return {"state": "EOD", "market_snapshot_health": "EOD", "worker_health": "WORKER_OK"}

    monkeypatch.setattr("agents.ai_engine.ensemble.get_model_status", _fake_model_status)
    monkeypatch.setattr(svc, "_get_feed_status", _fake_feed_status)
    monkeypatch.setattr(health_router, "_check_db_connectivity_with_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(health_router, "_fetch_runtime_state_safe", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "db.intraday_store.load_latest_scan",
        lambda: {
            "scan_time": "2026-11-26T10:00:00",
            "signal_count": 3,
            "freshness_status": "FRESH",
            "signals": [{"symbol": "RELIANCE"}, {"symbol": "TCS"}, {"symbol": "INFY"}],
        },
    )

    res = await health_router.health_check()
    assert res["signals_cached"] == 3
    assert model_tids and model_tids[0] != loop_tid
    assert feed_tids and feed_tids[0] != loop_tid


@pytest.mark.asyncio
async def test_system_data_health_reads_freshness_status(monkeypatch):
    monkeypatch.setattr(health_router, "_check_db_connectivity_with_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(svc, "_get_feed_status", lambda: {"market_snapshot_health": "CURRENT", "worker_health": "WORKER_OK"})
    monkeypatch.setattr(
        "db.intraday_store.load_latest_scan",
        lambda: {
            "scan_time": "2026-11-26T10:05:00",
            "scan_age_seconds": 42,
            "signal_count": 2,
            "freshness_status": "FRESH",
            "signals": [{"symbol": "TCS"}, {"symbol": "INFY"}],
        },
    )
    monkeypatch.setattr("db.runtime_state.get_runtime_state", lambda *_args, **_kwargs: None)

    res = await health_router.get_system_data_health()
    iscan = res["subsystems"]["intraday_scanner"]
    assert iscan["status"] == "HEALTHY"
    assert iscan["freshness_status"] == "FRESH"
    assert iscan["data_status"] == "FRESH"
    assert iscan["signal_count"] == 2
    assert iscan["age_seconds"] == 42


def test_telemetry_probe_auth_and_truthful_sidecar(monkeypatch):
    mini = FastAPI()
    mini.include_router(health_router.router)
    client = TestClient(mini)

    recorded_calls: list[dict] = []

    def _fake_record(**kwargs):
        recorded_calls.append(kwargs)
        return True

    monkeypatch.setattr("db.health_store.record_health_probe", _fake_record)
    monkeypatch.setattr(svc, "_get_feed_status", lambda: {"bar_age_seconds": 15.0})

    # 1. When CRON_SECRET is configured, missing/wrong header -> 403
    monkeypatch.setenv("CRON_SECRET", "probe-secret-123")
    r_missing = client.post("/api/telemetry/probe", json={"box": "edge", "status": "HEALTHY"})
    assert r_missing.status_code == 403

    r_wrong = client.post(
        "/api/telemetry/probe",
        json={"box": "edge", "status": "HEALTHY"},
        headers={"X-Cron-Secret": "wrong"},
    )
    assert r_wrong.status_code == 403

    # 2. Valid secret + no sidecar active -> sidecar_active=False (not hardcoded True)
    monkeypatch.delenv("ML_SIDECAR_URL", raising=False)
    monkeypatch.setattr("agents.ai_engine.ensemble.get_model_status", lambda: {"sidecar_active": False})
    r_ok = client.post(
        "/api/telemetry/probe",
        json={"box": "edge", "status": "HEALTHY"},
        headers={"X-Cron-Secret": "probe-secret-123"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["sidecar_active"] is False
    assert recorded_calls and recorded_calls[-1]["sidecar_active"] is False

    # 3. Payload/health sidecar_active=True propagates truthfully
    r_sidecar = client.post(
        "/api/telemetry/probe",
        json={"box": "edge", "status": "HEALTHY", "sidecar_active": True},
        headers={"X-Cron-Secret": "probe-secret-123"},
    )
    assert r_sidecar.status_code == 200
    assert r_sidecar.json()["sidecar_active"] is True

    # 4. In production without CRON_SECRET, fail closed with 503
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    r_prod = client.post("/api/telemetry/probe", json={"box": "edge", "status": "HEALTHY"})
    assert r_prod.status_code == 503


# ── 2. Swing Router: Intraday Freshness Key & Indicator Thread Offload ───────


@pytest.mark.asyncio
async def test_intraday_latest_uses_freshness_status_not_missing_data_status(monkeypatch):
    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist)
    fake_session = MagicMock(
        is_trading_active=True,
        session_state="ACTIVE_TRADING",
        to_dict=lambda: {"allows_new_entries": True, "data_freshness_mode": "REALTIME"},
    )
    monkeypatch.setattr("agents.intraday_engine.session.evaluate_market_session", lambda: fake_session)
    monkeypatch.setattr("agents.intraday_engine.scanner.get_nifty_market_context", lambda: {})
    monkeypatch.setattr("agents.intraday_engine.risk_manager.should_square_off_now", lambda: False)
    monkeypatch.setattr(
        swing_router.intraday_store,
        "load_latest_scan",
        lambda: {
            "scan_time": now_ist.isoformat(),
            "freshness_status": "FRESH",
            "provider_freshness": "Live Shoonya",
            "signals": [{"symbol": "RELIANCE", "direction": "LONG", "confluence_score": 78.0}],
        },
    )

    res = await swing_router.intraday_latest()
    assert res["data_status"] == "LIVE"
    assert res["allows_new_entries"] is True


@pytest.mark.asyncio
async def test_swing_symbol_offloads_calculate_indicators(monkeypatch):
    loop_tid = threading.get_ident()
    calc_tids: list[int] = []

    dates = pd.date_range("2026-01-01", periods=45, freq="B")
    df_sample = pd.DataFrame(
        {
            "timestamp": dates,
            "open": np.linspace(100.0, 120.0, 45),
            "high": np.linspace(102.0, 122.0, 45),
            "low": np.linspace(98.0, 118.0, 45),
            "close": np.linspace(101.0, 121.0, 45),
            "volume": np.full(45, 100_000.0),
            "ATR_14": np.full(45, 2.0),
        }
    )

    monkeypatch.setattr(svc, "_daily_data_cache", {})
    monkeypatch.setattr(svc, "_market_data", {})
    monkeypatch.setattr(svc, "_build_swing_market_context", lambda: {"index_trend": "BULLISH", "index_regime": "TRENDING"})
    monkeypatch.setattr(swing_router, "fetch_ohlcv", lambda *_a, **_kw: df_sample.copy())

    def _fake_calc(df):
        calc_tids.append(threading.get_ident())
        return df

    monkeypatch.setattr(swing_router, "calculate_indicators", _fake_calc)
    monkeypatch.setattr(swing_router, "compute_sector_momentum_map", lambda _m: {})
    monkeypatch.setattr(swing_router, "scan_swing_setups", lambda *_a, **_kw: [])

    res = await swing_router.get_swing_for_symbol("RELIANCE")
    assert res["symbol"] == "RELIANCE"
    assert calc_tids and calc_tids[0] != loop_tid


# ── 3. Admin Router: Retrain Uses _train_xgboost_sync & Offloads DB Calls ────


@pytest.mark.asyncio
async def test_admin_retrain_uses_service_sync_helper_in_thread(monkeypatch):
    loop_tid = threading.get_ident()
    train_calls: list[tuple] = []

    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "false")
    monkeypatch.setattr(svc, "_retrain_jobs", {})

    def _fake_train(symbols, timeframe, days, forward_bars, min_return_pct):
        train_calls.append((threading.get_ident(), symbols, timeframe, days, forward_bars, min_return_pct))
        return {"val_accuracy": 71.5, "n_samples": 400}

    monkeypatch.setattr(svc, "_train_xgboost_sync", _fake_train)

    resp = await admin_router.retrain_xgboost(
        symbols="BTC/USDT, ETH/USDT",
        timeframe="4h",
        days=180,
        forward_bars=5,
        min_return_pct=1.0,
    )
    assert resp["status"] == "started"
    job_id = resp["job_id"]

    # Let background task finish
    for _ in range(20):
        if svc._retrain_jobs.get(job_id, {}).get("status") != "running":
            break
        await asyncio.sleep(0.02)

    assert svc._retrain_jobs[job_id]["status"] == "completed"
    assert train_calls and train_calls[0][0] != loop_tid
    assert train_calls[0][1] == ["BTC/USDT", "ETH/USDT"]


@pytest.mark.asyncio
async def test_admin_sync_db_routes_offload_to_thread(monkeypatch):
    loop_tid = threading.get_ident()
    seen: dict[str, int] = {}

    monkeypatch.setattr(
        "db.runtime_state.get_runtime_state",
        lambda key, default=None: (
            seen.__setitem__(key, threading.get_ident())
            or ([{"id": "1", "action": "HOLD", "symbol": "TCS", "timestamp": "2026-11-26T10:00:00Z"}] if key == "autonomous_feed:v1" else None)
        ),
    )
    monkeypatch.setattr(
        "db.runtime_state.set_runtime_state",
        lambda key, _val: seen.__setitem__(f"set:{key}", threading.get_ident()),
    )
    monkeypatch.setattr(
        "agents.analytics.feedback_loop.get_learning_stats",
        lambda: seen.__setitem__("learning_stats", threading.get_ident()) or {"insights": {}, "weights": {}},
    )
    monkeypatch.setattr(
        "agents.analytics.feedback_loop.get_recent_outcomes",
        lambda limit=100: seen.__setitem__("outcomes", threading.get_ident()) or [],
    )
    monkeypatch.setattr("api.model_sync.current_status", lambda: seen.__setitem__("model_status", threading.get_ident()) or {"files_present": []})

    from starlette.responses import Response
    await admin_router.get_autonomous_feed(Response())
    await admin_router.toggle_autonomous({"enabled": False})
    await admin_router.get_model_bundle_status()
    await admin_router.get_learning_stats_endpoint()
    await admin_router.get_learning_outcomes(limit=10)

    for label in ("autonomous_feed:v1", "set:autonomous_trading_enabled", "model_status", "learning_stats", "outcomes"):
        assert label in seen and seen[label] != loop_tid, f"{label} did not run off event loop"


# ── 4. Portfolio Router: DataFrame Resolution, Dead Helper Removal, Offload ──


@pytest.mark.asyncio
async def test_paper_resolve_extracts_dataframe_prices_and_offloads(monkeypatch, tmp_path):
    loop_tid = threading.get_ident()
    engine = PaperTradingEngine(initial_capital=100000.0, log_file=str(tmp_path / "resolve.json"))
    trade = engine.execute(
        symbol="RELIANCE",
        direction="LONG",
        entry=2500.0,
        sl=2450.0,
        tp=2600.0,
        atr=20.0,
    )
    assert trade is not None

    df_rel = pd.DataFrame({"open": [2500.0], "high": [2620.0], "low": [2495.0], "close": [2610.0]})
    monkeypatch.setattr(svc, "_engine", engine)
    monkeypatch.setattr(svc, "_market_data", {"RELIANCE": df_rel})

    orig_update = engine.update_prices
    update_tids: list[int] = []

    def _wrapped_update(prices, *args, **kwargs):
        update_tids.append(threading.get_ident())
        return orig_update(prices, *args, **kwargs)

    monkeypatch.setattr(engine, "update_prices", _wrapped_update)

    res = await portfolio_router.resolve_paper_trades()
    assert res["ok"] is True
    assert res["resolved"] == 1
    assert res["remaining_open"] == 0
    assert update_tids and update_tids[0] != loop_tid


def test_portfolio_removed_unused_fetch_autonomous_status_sync():
    assert not hasattr(portfolio_router, "_fetch_autonomous_status_sync")


@pytest.mark.asyncio
async def test_portfolio_sync_db_calls_offloaded(monkeypatch, tmp_path):
    loop_tid = threading.get_ident()
    engine = PaperTradingEngine(initial_capital=100000.0, log_file=str(tmp_path / "offload.json"))
    t1 = engine.execute(symbol="TCS", direction="LONG", entry=3500.0, sl=3400.0, tp=3700.0, atr=30.0)
    assert t1 is not None
    monkeypatch.setattr(svc, "_engine", engine)
    monkeypatch.setattr(svc, "get_engine_for_account", lambda _acc="default": engine)

    tids: dict[str, int] = {}
    orig_risk = engine.get_risk_state
    orig_reset = engine.reset_circuit_breaker
    orig_close = engine.close_manual

    monkeypatch.setattr(engine, "get_risk_state", lambda: tids.__setitem__("risk", threading.get_ident()) or orig_risk())
    monkeypatch.setattr(engine, "reset_circuit_breaker", lambda rebase_capital=False: tids.__setitem__("reset", threading.get_ident()) or orig_reset(rebase_capital=rebase_capital))
    monkeypatch.setattr(engine, "close_manual", lambda trade_id, exit_price: tids.__setitem__("close", threading.get_ident()) or orig_close(trade_id=trade_id, exit_price=exit_price))

    await portfolio_router.get_risk_state()
    await portfolio_router.reset_risk_circuit(rebase_capital=False)
    await portfolio_router.close_paper_trade_endpoint(trade_id=t1.id, payload={"exit_price": 3550.0}, account_id="default")

    assert tids.get("risk") not in (None, loop_tid)
    assert tids.get("reset") not in (None, loop_tid)
    assert tids.get("close") not in (None, loop_tid)


# ── 5. Prediction Router: Lightweight Guard & Backtest Thread Offload ────────


def test_prediction_validation_metrics_guarded_in_lightweight_mode(monkeypatch):
    client = TestClient(app)
    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "true")
    resp = client.get("/api/prediction/validation-metrics?symbol=RELIANCE")
    assert resp.status_code == 409
    assert "LIGHTWEIGHT_API_MODE" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_prediction_validation_metrics_offloads_walk_forward_backtest(monkeypatch):
    loop_tid = threading.get_ident()
    bt_tids: list[int] = []

    monkeypatch.setenv("LIGHTWEIGHT_API_MODE", "false")
    n = 150
    df = pd.DataFrame(
        {
            "high": np.linspace(102, 182, n),
            "low": np.linspace(98, 178, n),
            "close": np.linspace(100, 180, n),
            "volume": np.full(n, 1000.0),
        }
    )

    async def _mock_batch(self, symbols, period="1y", interval="1d"):
        return {"RELIANCE": df}

    def _fake_bt(data, n_splits=4, embargo_bars=5, forward_bars=3):
        bt_tids.append(threading.get_ident())
        return {"status": "VALIDATED", "brier_score": 0.18, "directional_accuracy": 62.0}

    monkeypatch.setattr("data.providers.yahoo.YahooProvider.get_batch_ohlcv", _mock_batch)
    monkeypatch.setattr("agents.ai_engine.backtest_validator.run_walk_forward_backtest", _fake_bt)

    res = await prediction_router.get_walk_forward_metrics(symbol="RELIANCE")
    assert res["status"] == "VALIDATED"
    assert res["symbol"] == "RELIANCE"
    assert bt_tids and bt_tids[0] != loop_tid


# ── 6. Feed Server: OAuth Callback Fail-Closed Auth & No Token Leak ──────────


def test_feed_server_oauth_callback_fail_closed_and_post(monkeypatch):
    import feed_server

    with TestClient(feed_server.app) as client:
        # Missing env secret -> 503 fail-closed
        monkeypatch.delenv("FEED_INTERNAL_TOKEN", raising=False)
        r_503 = client.post("/oauth/callback?code=abc")
        assert r_503.status_code == 503

        # Wrong token -> 403
        monkeypatch.setenv("FEED_INTERNAL_TOKEN", "feed-secret-xyz")
        r_403 = client.post("/oauth/callback?code=abc", headers={"X-Internal-Token": "bad"})
        assert r_403.status_code == 403

        # Valid token -> 200 without susertoken_prefix
        with patch("feed_server.feed_manager.exchange_auth_code", return_value="secret_suser_token_999"), \
             patch("feed_server.feed_manager.login_with_token", return_value=True), \
             patch("feed_server.feed_manager._start_websocket"):
            r_200 = client.post("/oauth/callback?code=abc", headers={"X-Internal-Token": "feed-secret-xyz"})
            assert r_200.status_code == 200
            body = r_200.json()
            assert body["token_acquired"] is True
            assert "susertoken_prefix" not in body


# ── 7. Settings: validate_runtime_config Checks ON_RENDER & SERVICE_ROLE ─────


def test_validate_runtime_config_on_render_and_service_roles(monkeypatch):
    monkeypatch.setattr(settings, "IS_PRODUCTION", False)
    monkeypatch.setattr(settings, "ON_RENDER", True)
    monkeypatch.setenv("RENDER", "true")

    # Default (trading-api) on Render requires PostgreSQL DATABASE_URL + ADMIN_API_KEY
    monkeypatch.delenv("SERVICE_ROLE", raising=False)
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///./trading.db")
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    with pytest.raises(ValueError, match="persistent PostgreSQL"):
        settings.validate_runtime_config()

    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://u:p@host:5432/db")
    with pytest.raises(ValueError, match="ADMIN_API_KEY"):
        settings.validate_runtime_config()

    # trading-ml (ml_sidecar) requires PostgreSQL DATABASE_URL but NOT ADMIN_API_KEY
    monkeypatch.setenv("SERVICE_ROLE", "ml_sidecar")
    settings.validate_runtime_config()

    # trading-feed (feed_service) needs neither DATABASE_URL nor ADMIN_API_KEY
    monkeypatch.setenv("SERVICE_ROLE", "feed_service")
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    settings.validate_runtime_config()


# ── 8. XGBoost Model: prepare_training_data Contract & Short/Zero Guards ─────


def test_xgboost_prepare_training_data_contract():
    from agents.ai_engine.xgboost_model import FEATURE_COLS_XGB, prepare_training_data

    n = 60
    idx = pd.date_range("2026-01-01", periods=n, freq="1h")
    # Oscillating close ensures both positive and negative 5-bar returns > 0.5%
    close = 100.0 + 4.0 * np.sin(np.linspace(0, 6 * np.pi, n))
    df = pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.linspace(1000.0, 2000.0, n),
            "RSI_14": np.linspace(30.0, 70.0, n),
            "MACD": np.linspace(-1.0, 1.0, n),
            "MACD_signal": np.zeros(n),
            "MACD_hist": np.linspace(-0.5, 0.5, n),
            "ADX_14": np.full(n, 25.0),
            "ATR_14": np.full(n, 1.5),
            "EMA_20": close * 0.99,
            "EMA_50": close * 0.98,
            "EMA_200": close * 0.95,
        },
        index=idx,
    )

    X, y = prepare_training_data(df, forward_bars=5, min_return_pct=0.5)
    assert X.ndim == 2
    assert X.shape[1] == len(FEATURE_COLS_XGB)
    assert X.shape[0] == len(y) > 0
    assert X.dtype == np.float64
    assert set(np.unique(y)) == {0, 1}
    assert np.isfinite(X).all()

    # Short DataFrame guard (len(df) <= forward_bars)
    X_short, y_short = prepare_training_data(df.iloc[:5], forward_bars=5, min_return_pct=0.5)
    assert X_short.shape == (0, len(FEATURE_COLS_XGB))
    assert X_short.dtype == np.float64
    assert len(y_short) == 0

    # Zero close prices must not crash or yield non-finite labels
    df_zero = df.copy()
    df_zero["close"] = 0.0
    X_zero, y_zero = prepare_training_data(df_zero, forward_bars=5, min_return_pct=0.5)
    assert X_zero.shape == (0, len(FEATURE_COLS_XGB))
    assert len(y_zero) == 0
