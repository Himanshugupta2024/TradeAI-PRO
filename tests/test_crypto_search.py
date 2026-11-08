import pytest
from agents.market_intelligence.nse_fetcher import search_symbols, CRYPTO_SEARCH_SYMBOLS


def test_crypto_search_symbols_expanded():
    """Verify that top volume and meme crypto pairs are present in search map."""
    expected_coins = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "PEPE/USDT", "SHIB/USDT", "SUI/USDT", "WIF/USDT", "BONK/USDT"]
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
