"""
Unit & Contract Tests for Box C F&O Analytics Engine & Box A Client Integration.
Tests Black-Scholes Newton-Raphson IV solver (math.erf, NO scipy), OptionChain OI walls,
Max Pain, ATM straddle implied moves, fail-closed endpoints, and ATR fallback in Box A.
"""
from __future__ import annotations

import math
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.fno_client import fetch_fno_analytics, fetch_fno_analytics_sync
from feed.fno_analytics import (
    FNOAnalyticsEngine,
    bs_price,
    bs_vega,
    calculate_implied_volatility,
    norm_cdf,
    norm_pdf,
)
from feed.nfo_master import NFOMaster
from feed.option_chain import OptionChain
from feed_server import app


# ── 1. NFO Master Tests ────────────────────────────────────────────────────

def test_nfo_master_strike_step():
    nfo = NFOMaster(cache_file=None)
    assert nfo.get_strike_step("NIFTY 50") == 50.0
    assert nfo.get_strike_step("BANKNIFTY") == 100.0
    assert nfo.get_strike_step("RELIANCE") == 20.0
    assert nfo.get_strike_step("SBIN") == 5.0
    assert nfo.get_strike_step("UNKNOWN_STOCK") == 10.0


def test_nfo_master_atm_strike():
    nfo = NFOMaster(cache_file=None)
    # NIFTY step 50
    assert nfo.get_atm_strike("NIFTY", 24128.0) == 24150.0
    assert nfo.get_atm_strike("NIFTY", 24120.0) == 24100.0
    # RELIANCE step 20
    assert nfo.get_atm_strike("RELIANCE", 2511.0) == 2520.0
    assert nfo.get_atm_strike("RELIANCE", 2508.0) == 2500.0


def test_nfo_master_contract_registration():
    nfo = NFOMaster(cache_file=None)
    nfo.register_contract(
        symbol="RELIANCE",
        token="77701",
        strike=2500.0,
        option_type="CE",
        expiry="28NOV2026",
    )
    contracts = nfo.get_contracts("RELIANCE")
    assert len(contracts) == 1
    assert contracts[0]["token"] == "77701"
    assert contracts[0]["strike"] == 2500.0
    assert contracts[0]["option_type"] == "CE"
    assert nfo.get_contract_by_token("77701") is not None


# ── 2. Option Chain & OI Wall Tests ────────────────────────────────────────

def test_option_chain_walls_and_pcr():
    chain = OptionChain("RELIANCE")

    # Strike 2400: Put OI wall (Support)
    chain.update_by_strike(2400.0, "ce", ltp=110.0, oi=5000, volume=200)
    chain.update_by_strike(2400.0, "pe", ltp=12.0, oi=45000, volume=8000)

    # Strike 2500: ATM
    chain.update_by_strike(2500.0, "ce", ltp=42.0, oi=20000, volume=5000)
    chain.update_by_strike(2500.0, "pe", ltp=38.0, oi=25000, volume=6000)

    # Strike 2600: Call OI wall (Resistance)
    chain.update_by_strike(2600.0, "ce", ltp=14.0, oi=60000, volume=12000)
    chain.update_by_strike(2600.0, "pe", ltp=105.0, oi=3000, volume=100)

    ce_wall, pe_wall = chain.get_walls()
    assert ce_wall == 2600.0  # Max CE OI is 60,000 at 2600
    assert pe_wall == 2400.0  # Max PE OI is 45,000 at 2400

    pcr = chain.get_pcr()
    # Total PE OI = 45000 + 25000 + 3000 = 73000
    # Total CE OI = 5000 + 20000 + 60000 = 85000
    assert pcr["pcr_oi"] == round(73000 / 85000, 2)


def test_option_chain_atm_straddle():
    chain = OptionChain("RELIANCE")
    chain.update_by_strike(2500.0, "ce", ltp=45.5, oi=10000, volume=500)
    chain.update_by_strike(2500.0, "pe", ltp=35.5, oi=12000, volume=600)

    straddle = chain.get_atm_straddle(spot=2504.0)
    assert straddle["atm_strike"] == 2500.0
    assert straddle["ce_ltp"] == 45.5
    assert straddle["pe_ltp"] == 35.5
    assert straddle["straddle_price"] == 81.0  # 45.5 + 35.5


def test_option_chain_max_pain():
    chain = OptionChain("RELIANCE")
    # Low loss at 2500
    chain.update_by_strike(2400.0, "ce", ltp=100.0, oi=1000, volume=10)
    chain.update_by_strike(2400.0, "pe", ltp=5.0, oi=20000, volume=100)
    chain.update_by_strike(2500.0, "ce", ltp=25.0, oi=5000, volume=50)
    chain.update_by_strike(2500.0, "pe", ltp=25.0, oi=5000, volume=50)
    chain.update_by_strike(2600.0, "ce", ltp=5.0, oi=20000, volume=100)
    chain.update_by_strike(2600.0, "pe", ltp=100.0, oi=1000, volume=10)

    pain = chain.get_max_pain()
    assert pain == 2500.0


# ── 3. Black-Scholes & Newton-Raphson IV Tests (Zero Scipy) ───────────────

def test_norm_cdf_and_pdf_accuracy():
    # norm_cdf(0) should be 0.5
    assert abs(norm_cdf(0.0) - 0.5) < 1e-7
    # norm_cdf(1.96) ~= 0.975
    assert abs(norm_cdf(1.95996) - 0.975) < 1e-4
    # norm_pdf(0) ~= 1/sqrt(2pi) ~= 0.39894
    assert abs(norm_pdf(0.0) - (1.0 / math.sqrt(2.0 * math.pi))) < 1e-7


def test_black_scholes_pricing_and_vega():
    spot = 2500.0
    strike = 2500.0
    t = 30.0 / 365.0
    r = 0.068
    sigma = 0.22

    call_p = bs_price(spot, strike, t, r, sigma, is_call=True)
    put_p = bs_price(spot, strike, t, r, sigma, is_call=False)
    vega = bs_vega(spot, strike, t, r, sigma)

    # Call and put prices must be positive and follow put-call parity: C - P = S - K * exp(-r*T)
    assert call_p > 0
    assert put_p > 0
    parity_diff = (call_p - put_p) - (spot - strike * math.exp(-r * t))
    assert abs(parity_diff) < 1e-4
    assert vega > 0


def test_newton_raphson_iv_solver_roundtrip():
    spot = 2500.0
    strike = 2500.0
    t = 15.0 / 365.0
    r = 0.068
    known_sigma = 0.25

    # Compute theoretical price
    market_price = bs_price(spot, strike, t, r, known_sigma, is_call=True)
    # Recover sigma via Newton-Raphson solver
    recovered_sigma = calculate_implied_volatility(
        market_price=market_price,
        spot=spot,
        strike=strike,
        t_years=t,
        rate=r,
        is_call=True,
    )
    assert recovered_sigma is not None
    assert abs(recovered_sigma - known_sigma) < 0.001


def test_fno_analytics_engine_expected_moves():
    engine = FNOAnalyticsEngine()
    chain = engine.get_or_create_chain("RELIANCE")
    # ATM strike 2500 with CE=30, PE=30 -> Straddle=60
    chain.update_by_strike(2500.0, "ce", ltp=30.0, oi=10000)
    chain.update_by_strike(2500.0, "pe", ltp=30.0, oi=10000)

    res = engine.compute_symbol_analytics("RELIANCE", spot=2500.0, days_to_expiry=5.0)
    assert res["straddle_price"] == 60.0
    # 1D implied move: 60 / 2500 = 2.40%
    assert res["implied_move_1d_pct"] == 2.40
    # 5D implied move: 2.40 * sqrt(5) ~= 5.37%
    assert res["implied_move_5d_pct"] == pytest.approx(5.37, abs=0.05)
    assert res["envelope_source"] == "SHOONYA_OPTIONS_CHAIN"


# ── 4. Feed Server Endpoint Tests ──────────────────────────────────────────

def test_feed_server_fno_analytics_fail_closed(monkeypatch):
    monkeypatch.delenv("FEED_INTERNAL_TOKEN", raising=False)
    client = TestClient(app)
    resp = client.get("/fno/analytics?symbol=RELIANCE")
    assert resp.status_code == 503
    assert "FEED_INTERNAL_TOKEN" in resp.json()["detail"]


def test_feed_server_fno_analytics_authorized(monkeypatch):
    monkeypatch.setenv("FEED_INTERNAL_TOKEN", "valid-secret-token")
    client = TestClient(app)
    resp = client.get(
        "/fno/analytics?symbol=RELIANCE&spot=2500",
        headers={"X-Internal-Token": "valid-secret-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "RELIANCE"
    assert "ce_wall" in data
    assert "pe_wall" in data
    assert "pcr_oi" in data
    assert "implied_move_5d_pct" in data


def test_feed_server_update_chain_endpoint(monkeypatch):
    monkeypatch.setenv("FEED_INTERNAL_TOKEN", "valid-secret-token")
    client = TestClient(app)
    payload = [
        {"strike": 2400.0, "pe": {"ltp": 15.0, "oi": 50000, "volume": 1000}},
        {"strike": 2600.0, "ce": {"ltp": 20.0, "oi": 70000, "volume": 2000}},
    ]
    resp = client.post(
        "/fno/update-chain?symbol=RELIANCE",
        json=payload,
        headers={"X-Internal-Token": "valid-secret-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["strikes_updated"] == 2

    # Query analytics to verify walls updated
    resp2 = client.get(
        "/fno/analytics?symbol=RELIANCE&spot=2500",
        headers={"X-Internal-Token": "valid-secret-token"},
    )
    d = resp2.json()
    assert d["ce_wall"] == 2600.0
    assert d["pe_wall"] == 2400.0


# ── 5. Box A FNO Client Failover & Fallback Tests ───────────────────────────

def test_fno_client_unconfigured_falls_back_to_atr(monkeypatch):
    monkeypatch.delenv("FEED_SERVICE_URL", raising=False)
    res = fetch_fno_analytics_sync("INFY", spot=1500.0, atr=25.0)
    assert res["envelope_source"] == "ATR_FALLBACK"
    assert res["ce_wall"] is None
    assert res["implied_move_5d_pct"] > 0


@pytest.mark.asyncio
async def test_fno_client_async_fallback(monkeypatch):
    monkeypatch.delenv("FEED_SERVICE_URL", raising=False)
    res = await fetch_fno_analytics("TCS", spot=3500.0, atr=50.0)
    assert res["envelope_source"] == "ATR_FALLBACK"
    assert res["symbol"] == "TCS"
    assert res["spot_price"] == 3500.0


# ── 6. Scale-Free F&O Feature Extraction Tests ──────────────────────────────

def test_extract_scale_free_fno_features_invariance():
    from agents.ai_engine.fno_features import FNO_FEATURE_NAMES, extract_scale_free_fno_features

    telemetry = {
        "ce_wall": 2600.0,
        "pe_wall": 2400.0,
        "pcr_oi": 1.25,
        "pcr_change_5d": 0.10,
        "implied_move_5d_pct": 5.2,
        "iv_rank": 0.65,
        "iv_skew": -0.05,
        "oi_quadrant": "LONG_BUILDUP",
        "rollover_pct": 0.78,
    }

    # Base price 2500, ATR 50 -> ce_wall distance is (2600 - 2500)/50 = 2.0 ATR
    feats = extract_scale_free_fno_features(
        symbol="RELIANCE",
        spot=2500.0,
        atr=50.0,
        fno_telemetry=telemetry,
        future_price=2505.0,
        vix=15.0,
    )

    # Verify all 12 canonical names present
    assert set(feats.keys()) == set(FNO_FEATURE_NAMES)
    assert feats["fno_dist_to_ce_wall_atr"] == 2.0
    assert feats["fno_dist_to_pe_wall_atr"] == 2.0
    assert feats["fno_pcr_oi"] == 1.25
    assert feats["fno_oi_quadrant"] == 1.0  # LONG_BUILDUP
    assert feats["fno_vix_regime"] == 0.5  # 15.0 is normal vol
    assert feats["fno_basis_pct"] == pytest.approx(0.2, abs=0.01)

    # Scale invariance: Double spot and ATR and walls -> ATR-distance remains strictly 2.0!
    scaled_telemetry = dict(telemetry)
    scaled_telemetry["ce_wall"] = 5200.0
    scaled_telemetry["pe_wall"] = 4800.0
    scaled_feats = extract_scale_free_fno_features(
        symbol="RELIANCE",
        spot=5000.0,
        atr=100.0,
        fno_telemetry=scaled_telemetry,
        future_price=5010.0,
        vix=15.0,
    )
    assert scaled_feats["fno_dist_to_ce_wall_atr"] == 2.0
    assert scaled_feats["fno_dist_to_pe_wall_atr"] == 2.0
    assert scaled_feats["fno_basis_pct"] == pytest.approx(0.2, abs=0.01)


def test_extract_scale_free_fno_features_safe_defaults_when_empty():
    from agents.ai_engine.fno_features import extract_scale_free_fno_features

    feats = extract_scale_free_fno_features(symbol="NEWSTOCK", spot=1000.0, atr=15.0)
    for k, v in feats.items():
        assert not math.isnan(v), f"Feature {k} was NaN"
        assert not math.isinf(v), f"Feature {k} was inf"
    assert feats["fno_pcr_oi"] == 1.0
    assert feats["fno_iv_rank"] == 0.50


# ── 7. Box A Gateway F&O Endpoints Test ────────────────────────────────────

@pytest.mark.asyncio
async def test_box_a_fno_gateway_endpoints():
    from server import app as box_a_app
    client = TestClient(box_a_app)

    # Test single symbol
    resp = client.get("/api/fno/symbol/RELIANCE?spot=2500&atr=30")
    assert resp.status_code == 200
    d = resp.json()
    assert d["symbol"] == "RELIANCE"
    assert "implied_move_5d_pct" in d

    # Test overview
    resp2 = client.get("/api/fno/overview")
    assert resp2.status_code == 200
    d2 = resp2.json()
    assert d2["count"] > 0
    assert "RELIANCE" in d2["data"]



def test_fno_null_walls_never_claim_box_c_model(monkeypatch):
    """Box C returning null walls / NO_CHAIN_DATA must rewrite provenance to ATR_FALLBACK."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from api import fno_client

    fno_client._FNO_CACHE.clear()
    monkeypatch.setenv("FEED_SERVICE_URL", "http://mock-box-c.local")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "symbol": "TCS",
        "spot_price": 2640.0,
        "ce_wall": None,
        "pe_wall": None,
        "max_pain": None,
        "envelope_source": "BOX_C_MODEL",
    }
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("api.fno_client._get_async_client", return_value=mock_client):
        res = asyncio.run(fno_client.fetch_fno_analytics("TCS", spot=2640.0, atr=40.0))
        assert res["envelope_source"] == "ATR_FALLBACK"
