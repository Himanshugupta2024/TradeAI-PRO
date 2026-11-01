"""
Unit tests verifying api.service._refresh_market_data vectorized batch integration.
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import pandas as pd
import numpy as np

from api import service as svc


@pytest.mark.asyncio
async def test_service_batch_refresh_uses_router_data():
    """Verify that when router returns batch OHLCV data, service ingests it without per-symbol fetch."""
    # Construct synthetic 5m OHLCV dataframe with enough bars for indicators (>30 bars)
    n_bars = 40
    dates_5m = pd.date_range("2026-11-01 09:15:00", periods=n_bars, freq="5min")
    close_vals = [1500.0 + i * 2.0 for i in range(n_bars)]
    df_5m = pd.DataFrame({
        "timestamp": dates_5m,
        "open": [c - 1.0 for c in close_vals],
        "high": [c + 3.0 for c in close_vals],
        "low": [c - 2.0 for c in close_vals],
        "close": close_vals,
        "volume": [10000.0] * n_bars,
    })

    # 1d HTF data
    dates_1d = pd.date_range("2026-08-01", periods=30, freq="D")
    df_1d = pd.DataFrame({
        "timestamp": dates_1d,
        "open": [1400.0 + i * 5.0 for i in range(30)],
        "high": [1410.0 + i * 5.0 for i in range(30)],
        "low": [1390.0 + i * 5.0 for i in range(30)],
        "close": [1405.0 + i * 5.0 for i in range(30)],
        "volume": [50000.0] * 30,
    })

    # Pick first 2 symbols from TOP_NSE_SYMBOLS for testing
    test_symbols = ["INFY", "TCS"]
    mock_batch_5m = {sym: df_5m.copy() for sym in test_symbols}
    mock_batch_1d = {sym: df_1d.copy() for sym in test_symbols}

    mock_router = AsyncMock()
    mock_router.get_batch_ohlcv = AsyncMock(side_effect=lambda symbols, period="5d", interval="5m": (
        mock_batch_5m if interval == "5m" else mock_batch_1d
    ))

    # Clear test symbol cache
    for sym in test_symbols:
        svc._market_data.pop(sym, None)
        svc._signal_cache.pop(sym, None)

    with patch("data.router.get_default_router", return_value=mock_router), \
         patch("api.service.TOP_NSE_SYMBOLS", test_symbols), \
         patch("config.settings.CRYPTO_SYMBOLS", []), \
         patch("api.service._refresh_crypto_futures_context", new_callable=AsyncMock), \
         patch("api.service._build_swing_symbol_universe", return_value=[]), \
         patch("api.service._recompute_swing_cache", new_callable=AsyncMock), \
         patch("api.service._fire_telegram_signal", return_value=None):

        await svc._refresh_market_data()

    # Verify both symbols are populated in cache
    for sym in test_symbols:
        assert sym in svc._market_data
        assert sym in svc._signal_cache
        res_df = svc._market_data[sym]
        assert not res_df.empty
        assert "close" in res_df.columns
        assert "signal" in res_df.columns
