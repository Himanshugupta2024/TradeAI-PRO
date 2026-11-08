import pytest
from agents.market_intelligence.nse_fetcher import search_symbols, CRYPTO_SEARCH_SYMBOLS, CRYPTO_ALIASES
from agents.data_ingestion.binance_fetcher import _fetch_binance_futures_fallback
from fastapi.testclient import TestClient
from server import app


def test_crypto_search_symbols_expanded():
    """Verify that top volume and meme crypto pairs are present in search map."""
    expected_coins = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "PEPE/USDT",
        "SHIB/USDT", "SUI/USDT", "WIF/USDT", "BONK/USDT", "CHILLGUY/USDT",
        "PNUT/USDT", "MOODENG/USDT"
    ]
    for coin in expected_coins:
        assert coin in CRYPTO_SEARCH_SYMBOLS, f"{coin} missing from CRYPTO_SEARCH_SYMBOLS"


def test_search_symbols_crypto_query():
    """Verify query matching for bare and slash tickers."""
    sol_results = search_symbols("SOL")
    assert any(r["symbol"] == "SOL/USDT" and r["type"] == "CRYPTO" for r in sol_results)

    doge_results = search_symbols("DOGE")
    assert any(r["symbol"] == "DOGE/USDT" and r["type"] == "CRYPTO" for r in doge_results)

    pepe_results = search_symbols("PEPE")
    assert any(r["symbol"] == "PEPE/USDT" and r["type"] == "CRYPTO" for r in pepe_results)

    # Verify NSE equity search remains intact
    vedl_results = search_symbols("VEDL")
    assert any(r["symbol"] == "VEDL" and r["type"] == "NSE" for r in vedl_results)


def test_crypto_alias_resolution_just_a_chill_guy():
    """Verify natural name queries like 'just a chill guy' resolve to CHILLGUY/USDT."""
    chill_full = search_symbols("just a chill guy")
    assert any(r["symbol"] == "CHILLGUY/USDT" for r in chill_full)

    chill_short = search_symbols("chill guy")
    assert any(r["symbol"] == "CHILLGUY/USDT" for r in chill_short)

    chill_ticker = search_symbols("chillguy")
    assert any(r["symbol"] == "CHILLGUY/USDT" for r in chill_ticker)

    pnut_res = search_symbols("peanut")
    assert any(r["symbol"] == "PNUT/USDT" for r in pnut_res)

    moodeng_res = search_symbols("moo deng")
    assert any(r["symbol"] == "MOODENG/USDT" for r in moodeng_res)


def test_binance_futures_fallback_parsing(monkeypatch):
    """Unit test for Binance futures JSON response parsing without live network."""
    import urllib.request
    import json
    fake_data = [
        [1700000000000, "0.10", "0.12", "0.09", "0.11", "50000.0", 1700003600000, "5500.0", 100, "25000.0", "2750.0", "0"],
        [1700003600000, "0.11", "0.13", "0.10", "0.12", "60000.0", 1700007200000, "6600.0", 120, "30000.0", "3300.0", "0"],
    ]
    class FakeResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps(fake_data).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: FakeResponse())
    df = _fetch_binance_futures_fallback("CHILLGUY/USDT", "1h")
    assert not df.empty
    assert "close" in df.columns
    assert len(df) == 2


def test_proxy_binance_24hr_futures_fallback(monkeypatch):
    """Verify backend proxy for 24hr ticker falls back from Spot to Futures for meme coins."""
    import httpx
    from api import routes

    # Invalidate cache
    routes._binance_symbol_cache.clear()

    class FakeHttpxResponse:
        def __init__(self, status_code, json_val):
            self.status_code = status_code
            self._json = json_val
        def json(self):
            return self._json

    async def fake_get(self, url, headers=None):
        if "fapi.binance.com" in url:
            return FakeHttpxResponse(200, {"symbol": "CHILLGUYUSDT", "lastPrice": "0.0085", "quoteVolume": "1500000.0"})
        # Spot endpoints return 400 for futures-only symbols
        return FakeHttpxResponse(400, {"code": -1121, "msg": "Invalid symbol."})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    client = TestClient(app)
    resp = client.get("/api/crypto/proxy/binance/24hr?symbol=CHILLGUYUSDT")
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "CHILLGUYUSDT"
    assert data["lastPrice"] == "0.0085"


def test_proxy_binance_klines_futures_fallback(monkeypatch):
    """Verify backend proxy for klines falls back from Spot to Futures for meme coins."""
    import httpx
    from api import routes

    routes._binance_klines_cache.clear()

    fake_klines = [
        [1700000000000, "0.0080", "0.0085", "0.0079", "0.0084", "10000.0"],
        [1700003600000, "0.0084", "0.0089", "0.0082", "0.0087", "12000.0"]
    ]

    class FakeHttpxResponse:
        def __init__(self, status_code, json_val):
            self.status_code = status_code
            self._json = json_val
        def json(self):
            return self._json

    async def fake_get(self, url, headers=None):
        if "fapi.binance.com" in url:
            return FakeHttpxResponse(200, fake_klines)
        return FakeHttpxResponse(400, {"code": -1121, "msg": "Invalid symbol."})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    client = TestClient(app)
    resp = client.get("/api/crypto/proxy/binance/klines?symbol=CHILLGUYUSDT&interval=1h&limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2


@pytest.mark.integration
def test_binance_futures_fallback_chillguy():
    """Verify that Binance Futures public REST returns candles for CHILLGUY."""
    df = _fetch_binance_futures_fallback("CHILLGUY/USDT", "1h")
    assert not df.empty, "Binance futures fallback returned empty DataFrame for CHILLGUY/USDT"
    assert "close" in df.columns
    assert len(df) > 10
