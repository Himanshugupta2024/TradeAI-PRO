"""Regression tests for the TATAMOTORS -> TMCV.NS OHLCV cache migration."""

from agents.market_intelligence import nse_fetcher


def test_tatamotors_uses_isolated_ticker_scoped_persistent_cache(monkeypatch):
    monkeypatch.setattr(
        nse_fetcher,
        "_LEGACY_TATAMOTORS_CACHE_PURGED",
        False,
    )
    purged = []
    monkeypatch.setattr(
        nse_fetcher,
        "purge_ohlcv_cache",
        lambda symbol: purged.append(symbol) or 23,
    )

    key = nse_fetcher._persistent_ohlcv_cache_symbol(
        "TATAMOTORS",
        "TMCV.NS",
    )

    assert key == "TATAMOTORS@TMCV.NS"
    assert key != "TATAMOTORS"
    assert purged == ["TATAMOTORS"]

    # The legacy purge is attempted once per process, not on every fetch.
    assert (
        nse_fetcher._persistent_ohlcv_cache_symbol(
            "TATAMOTORS",
            "TMCV.NS",
        )
        == key
    )
    assert purged == ["TATAMOTORS"]


def test_other_symbols_keep_their_existing_persistent_cache_key(monkeypatch):
    monkeypatch.setattr(
        nse_fetcher,
        "_LEGACY_TATAMOTORS_CACHE_PURGED",
        False,
    )
    purged = []
    monkeypatch.setattr(
        nse_fetcher,
        "purge_ohlcv_cache",
        lambda symbol: purged.append(symbol) or 0,
    )

    assert (
        nse_fetcher._persistent_ohlcv_cache_symbol(
            "RELIANCE",
            "RELIANCE.NS",
        )
        == "RELIANCE"
    )
    assert purged == []
