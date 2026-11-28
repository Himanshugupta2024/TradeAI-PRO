"""Unit & contract tests for Multi-Asset Cockpit, Box C (pp80) NFO OI, and Risk Partitioning."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def test_pp80_urls_and_env_defaults_in_configs():
    render_doc = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    services = {svc["name"]: svc for svc in render_doc["services"]}
    api_envs = {e["key"]: e for e in services["trading-api"]["envVars"]}

    assert api_envs["FEED_SERVICE_URL"]["value"] == "https://trading-feed-pp80.onrender.com"
    assert api_envs["DEFAULT_CRYPTO_FOREX_ENABLED"]["value"] == "true"
    assert int(api_envs["PAPER_MAX_OPEN_EQUITY"]["value"]) == 5
    assert int(api_envs["PAPER_MAX_OPEN_GLOBAL"]["value"]) == 4

    wrangler_raw = (ROOT / "wrangler.jsonc").read_text(encoding="utf-8")
    assert "https://trading-feed-pp80.onrender.com" in wrangler_raw
    assert '"OFFHOURS_CRYPTO_DISPATCH": "true"' in wrangler_raw


def test_nfo_master_normalization_expiry_and_slicing(tmp_path):
    from feed.nfo_master import NFOMaster, normalize_nfo_underlying

    assert normalize_nfo_underlying("NIFTY 50") == "NIFTY"
    assert normalize_nfo_underlying("NIFTY BANK") == "BANKNIFTY"
    assert normalize_nfo_underlying("RELIANCE.NS") == "RELIANCE"

    m = NFOMaster(cache_file=str(tmp_path / "nfo.json"))
    exp_near = (date.today() + timedelta(days=3)).strftime("%d-%b-%Y").upper()
    exp_far = (date.today() + timedelta(days=35)).strftime("%d-%b-%Y").upper()

    for idx, strike in enumerate((25400, 25500, 25600, 25700)):
        m.register_contract("NIFTY", f"tk_ce_{idx}", strike, "CE", exp_near, 75, f"NIFTY{strike}CE")
        m.register_contract("NIFTY", f"tk_pe_{idx}", strike, "PE", exp_near, 75, f"NIFTY{strike}PE")
    m.register_contract("NIFTY", "tk_far", 25500, "CE", exp_far, 75, "NIFTYFARCE")
    m.register_contract("RELIANCE", "tk_rel", 1400, "CE", exp_near, 250, "RELIANCE1400CE")

    assert m.get_nearest_expiry("NIFTY 50") == exp_near
    assert m.list_underlyings() == ["NIFTY", "RELIANCE"]

    sl = m.get_strike_slice("NIFTY 50", spot=25520.0, half_width=1)
    strikes_returned = sorted({c["strike"] for c in sl})
    assert strikes_returned == [25400.0, 25500.0, 25600.0]


def test_shoonya_refresh_fno_underlying_and_engine_bounds(tmp_path):
    from feed.fno_analytics import FNOAnalyticsEngine
    from feed.nfo_master import NFOMaster
    from feed.shoonya_client import ShoonyaFeedManager

    exp_near = (date.today() + timedelta(days=4)).strftime("%d-%b-%Y").upper()
    nfo = NFOMaster(cache_file=str(tmp_path / "nfo2.json"))
    nfo.register_contract("RELIANCE", "1001", 1400, "CE", exp_near, 250, "RELIANCE1400CE")
    nfo.register_contract("RELIANCE", "1002", 1400, "PE", exp_near, 250, "RELIANCE1400PE")

    engine = FNOAnalyticsEngine(max_chains=10)
    mgr = ShoonyaFeedManager(nfo_master=nfo, fno_engine=engine)
    mgr.is_simulated = True
    assert mgr.refresh_fno_underlying("RELIANCE", spot=1400.0) is False

    mgr.is_simulated = False
    mock_api = MagicMock()
    mock_api.get_option_chain.return_value = {
        "stat": "Ok",
        "values": [
            {"tsym": "RELIANCE1400CE", "strprc": "1400", "optt": "CE", "lp": "24.5", "oi": "520000", "v": "11000"},
            {"tsym": "RELIANCE1400PE", "strprc": "1400", "optt": "PE", "lp": "21.0", "oi": "610000", "v": "12500"},
            {"tsym": "RELIANCE1460CE", "strprc": "1460", "optt": "CE", "lp": "6.0", "oi": "840000", "v": "9000"},
        ],
    }
    mgr.api = mock_api

    assert mgr.refresh_fno_underlying("RELIANCE", spot=1400.0) is True
    analytics = engine.compute_symbol_analytics("RELIANCE", spot=1400.0)
    assert analytics["envelope_source"] in ("BOX_C_STREAM", "SHOONYA_OPTIONS_CHAIN")
    assert analytics["ce_wall"] == 1460.0
    assert analytics["pe_wall"] == 1400.0
    assert analytics["pcr_oi"] > 0

    for i in range(15):
        engine.get_or_create_chain(f"SYM{i}")
    assert len(engine.chains) <= 10


def test_derivatives_oi_no_hardcoded_pcr_and_uses_box_c(monkeypatch):
    import agents.data_ingestion.derivatives_oi as doi

    monkeypatch.setattr(
        "agents.market_intelligence.nse_fetcher.fetch_arbitrary_symbol",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "api.fno_client.fetch_fno_analytics_sync",
        lambda *a, **k: {"envelope_source": "ATR_FALLBACK", "pcr_oi": None},
    )
    unbacked = doi._fetch_derivatives_sync("NIFTY")
    assert unbacked["status"] == "unavailable"
    assert unbacked["oi_available"] is False
    assert unbacked["put_call_ratio"] is None

    monkeypatch.setattr(
        "api.fno_client.fetch_fno_analytics_sync",
        lambda *a, **k: {
            "envelope_source": "BOX_C_STREAM",
            "pcr_oi": 1.32,
            "total_oi": 5000000,
            "ce_wall": 25800.0,
            "pe_wall": 25400.0,
            "max_pain": 25600.0,
        },
    )
    live = doi._fetch_derivatives_sync("NIFTY")
    assert live["status"] == "ok"
    assert live["oi_available"] is True
    assert live["put_call_ratio"] == 1.32
    assert live["source"] == "BOX_C_STREAM"


def test_market_control_multi_asset_env_default(monkeypatch):
    from agents.execution.market_control import STATE_KEY, get_market_control_state
    from db.runtime_state import set_runtime_state

    set_runtime_state(STATE_KEY, None)
    monkeypatch.setenv("DEFAULT_CRYPTO_FOREX_ENABLED", "true")
    state = get_market_control_state()
    assert state["crypto_forex_enabled"] is True


def test_cycle_scheduler_dual_cadence():
    from api.cycle_scheduler import evaluate_tick

    now = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
    # Closed NSE + multi-asset off-hours enabled -> dispatch crypto
    d1 = evaluate_tick(
        now=now,
        market_open=False,
        token_present=True,
        crypto_forex_enabled=True,
        last_offhours_dispatch_at=None,
    )
    assert d1["action"] == "dispatch"
    assert d1["universe"] == "crypto"

    # Respects 60m off-hours cooldown
    recent = (now - timedelta(minutes=20)).isoformat()
    d2 = evaluate_tick(
        now=now,
        market_open=False,
        token_present=True,
        crypto_forex_enabled=True,
        last_offhours_dispatch_at=recent,
    )
    assert d2["action"] == "skip"
    assert d2["reason"] == "offhours_cooldown"


def test_partitioned_slot_buckets_prevent_crypto_starving_nse(tmp_path, monkeypatch):
    from agents.execution.currency import FXRateService
    from agents.execution.paper_trading import PaperTradingEngine
    from agents.execution.autonomous_daemon import AutonomousExecutionDaemon
    from config import settings

    FXRateService.get_instance().set_test_rate("USDT", "INR", 85.0)
    FXRateService.get_instance().set_test_rate("USD", "INR", 84.5)
    monkeypatch.setenv("PAPER_MAX_OPEN_EQUITY", "5")
    monkeypatch.setenv("PAPER_MAX_OPEN_GLOBAL", "4")
    monkeypatch.setenv("PAPER_MAX_OPEN_FOREX", "2")
    monkeypatch.setattr("agents.execution.runtime_controls.effective_int", lambda k, d: 5 if k == "PAPER_TRADING_MAX_OPEN" else d)
    # Isolate partitioned slot-bucket limits from total portfolio heat/leverage bounds.
    monkeypatch.setattr(settings, "MAX_PORTFOLIO_HEAT_PCT", 0.20)
    monkeypatch.setattr(settings, "MAX_PORTFOLIO_LEVERAGE", 5.0)

    eng = PaperTradingEngine(initial_capital=500000.0, log_file=str(tmp_path / "paper_state.json"))
    eng.reset_account(new_capital=500000.0, clear_trades=True)

    # Fill all 4 GLOBAL slots (2 Crypto + 2 Forex) using ~10% stop distances so
    # 1x portfolio leverage still leaves capacity for NSE Equity slots.
    for sym, entry, sl, tp in [
        ("BTC/USDT", 90000.0, 81000.0, 108000.0),
        ("ETH/USDT", 3200.0, 2880.0, 3840.0),
        ("EUR/USD", 1.0800, 0.9800, 1.2800),
        ("GBP/USD", 1.2600, 1.1400, 1.5000),
    ]:
        t = eng.execute(symbol=sym, direction="LONG", entry=entry, sl=sl, tp=tp, min_rr=1.5)
        assert t is not None, f"Failed opening global trade {sym}: {eng.last_rejection_reason}"

    # 3rd Forex / 5th Global must be blocked
    assert eng.execute(symbol="AUD/USD", direction="LONG", entry=0.65, sl=0.58, tp=0.79, min_rr=1.5) is None
    assert "GLOBAL" in eng.last_rejection_reason.upper() or "GLOBAL" in eng.last_rejection_reason

    # NSE Equities MUST still execute because EQUITY bucket has 5 free slots!
    nse_trade = eng.execute(symbol="RELIANCE", direction="LONG", entry=1400.0, sl=1260.0, tp=1680.0, min_rr=1.5)
    assert nse_trade is not None, f"NSE trade blocked unexpectedly: {eng.last_rejection_reason}"
    buckets = eng.get_slot_buckets_summary()
    assert buckets["equity"]["open"] == 1
    assert buckets["global"]["open"] == 4
    assert buckets["forex"]["open"] == 2

    # Daemon respects separate equity capacity even with full global bucket
    daemon = AutonomousExecutionDaemon(max_positions=5)
    daemon.enabled = True
    monkeypatch.setattr("agents.execution.runtime_controls.is_trading_halted", lambda: (False, ""))
    monkeypatch.setattr(eng, "_has_correlated_open_position", lambda _s: False)
    now_iso = datetime.now(timezone.utc).isoformat()
    res = daemon.evaluate_signals(
        signals=[{
            "symbol": "TCS",
            "signal": "LONG",
            "entry": 3500.0,
            "stop_loss": 3150.0,
            "target": 4200.0,
            "quant_score": 82.0,
            "confidence": 75.0,
            "regime": "TRENDING",
            "timestamp": now_iso,
        }],
        current_prices={"TCS": {"close": 3500.0, "atr": 40.0, "timestamp": now_iso}},
        engine=eng,
    )
    assert len(res) == 1
    assert res[0].symbol == "TCS"


def test_forex_paper_trade_endpoint_and_validation(monkeypatch, tmp_path):
    from agents.execution.currency import FXRateService
    from agents.execution.paper_trading import PaperTradingEngine
    from api.routers.forex import router
    from api.security import require_admin

    FXRateService.get_instance().set_test_rate("USD", "INR", 84.5)
    monkeypatch.setattr("api.routers.forex.is_crypto_forex_enabled", lambda: True)
    test_eng = PaperTradingEngine(initial_capital=500000.0, log_file=str(tmp_path / "fx_router_state.json"))
    monkeypatch.setattr("agents.execution.paper_trading.PaperTradingEngine", lambda *a, **k: test_eng)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    client = TestClient(app)

    bad_sym = client.post("/api/forex/paper-trade", json={
        "symbol": "RELIANCE",
        "direction": "LONG",
        "entry": 1400.0,
        "stop_loss": 1370.0,
        "target": 1460.0,
    })
    assert bad_sym.status_code == 400

    ok_res = client.post("/api/forex/paper-trade", json={
        "symbol": "EUR_USD",
        "direction": "LONG",
        "entry": 1.0800,
        "stop_loss": 1.0700,
        "target": 1.1000,
        "atr": 0.005,
        "confidence": 72.0,
    })
    assert ok_res.status_code == 200
    payload = ok_res.json()
    assert payload["executed"] is True
    assert payload["paper_only"] is True
    assert payload["symbol"] == "EUR/USD"
