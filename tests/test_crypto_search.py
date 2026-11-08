import json
import urllib.request

import pytest
from agents.market_intelligence.nse_fetcher import search_symbols, CRYPTO_SEARCH_SYMBOLS, CRYPTO_ALIASES
from agents.data_ingestion.binance_fetcher import _fetch_binance_futures_fallback


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


def test_binance_futures_fallback_parses_klines_without_network(monkeypatch):
    """Verify kline parsing deterministically without depending on Binance uptime."""
    base_ms = 1_800_000_000_000
    candles = [
        [
            base_ms + i * 3_600_000,
            "100.0",
            "105.0",
            "98.0",
            str(101.0 + i),
            "1000.0",
            base_ms + (i + 1) * 3_600_000 - 1,
            "101000.0",
            25,
            "500.0",
            "50500.0",
            "0",
        ]
        for i in range(12)
    ]

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return json.dumps(candles).encode("utf-8")

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: FakeResponse(),
    )

    df = _fetch_binance_futures_fallback("CHILLGUY/USDT", "1h")
    assert len(df) == 12
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert df["close"].iloc[-1] == 112.0
    assert df["timestamp"].is_monotonic_increasing


@pytest.mark.integration
def test_binance_futures_fallback_chillguy():
    """Live Binance Futures contract availability check (requires public network access)."""
    df = _fetch_binance_futures_fallback("CHILLGUY/USDT", "1h")
    assert not df.empty, "Binance futures fallback returned empty DataFrame for CHILLGUY/USDT"
    assert "close" in df.columns
    assert len(df) > 10
