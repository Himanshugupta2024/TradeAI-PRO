"""
Unit tests for NSE Official Security Bhavdata Ingestion Engine & Institutional Delivery Scoring.
Verifies download parsing, weekend/holiday lookback, disk caching, and SwingScanner delivery integration.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agents.data_ingestion.nse_bhavcopy import (
    NSEBhavcopyFetcher,
    bhavcopy_fetcher,
    get_bhavcopy_symbol_quote,
    get_latest_nse_bhavcopy,
)
from agents.market_intelligence.swing_scanner import (
    SwingSetup,
    scan_swing_setups,
)


SAMPLE_RAW_CSV = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
HDFCBANK, EQ, 20-Nov-2026, 821.80, 815.50, 850.35, 815.50, 846.00, 846.00, 836.50, 70110384, 586473.12, 452100, 44310000, 63.20
TCS, EQ, 20-Nov-2026, 2342.70, 2325.00, 2414.80, 2317.00, 2376.20, 2376.20, 2368.10, 2960416, 70105.12, 120500, 770900, 26.04
SPEC_STOCK, EQ, 20-Nov-2026, 100.00, 101.00, 108.00, 99.00, 105.00, 105.00, 103.00, 1000000, 1030.00, 5000, 150000, 15.00
BOND_SYM, GB, 20-Nov-2026, 100.00, 100.00, 100.00, 100.00, 100.00, 100.00, 100.00, 1000, 1.00, 10, -, -
"""


def test_bhavcopy_parser_filters_eq_and_normalizes():
    df = NSEBhavcopyFetcher.parse_raw_csv(SAMPLE_RAW_CSV)
    assert df is not None
    assert len(df) == 3  # BOND_SYM with GB series is excluded, only EQ kept
    assert "HDFCBANK" in df.index
    assert "TCS" in df.index
    assert "SPEC_STOCK" in df.index

    # Check HDFCBANK fields
    hdfc = df.loc["HDFCBANK"]
    assert hdfc["close"] == 846.0
    assert hdfc["prev_close"] == 821.8
    assert hdfc["delivery_pct"] == 63.20
    assert hdfc["delivery_qty"] == 44310000.0

    # Check TCS fields
    tcs = df.loc["TCS"]
    assert tcs["close"] == 2376.2
    assert tcs["prev_close"] == 2342.7
    assert tcs["delivery_pct"] == 26.04


def test_bhavcopy_date_formatting():
    fetcher = NSEBhavcopyFetcher()
    dt = datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc)
    assert fetcher.get_target_date_str(dt) == "20112026"
    assert fetcher.get_iso_date_str(dt) == "2026-11-20"


def test_bhavcopy_fetch_with_cache(tmp_path):
    fetcher = NSEBhavcopyFetcher(cache_dir=tmp_path)
    target_dt = datetime(2026, 11, 20)

    # First fetch with mock requests.get
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = SAMPLE_RAW_CSV
    mock_resp.content = SAMPLE_RAW_CSV.encode("utf-8")

    with patch("requests.get", return_value=mock_resp) as mock_get:
        df1 = fetcher.fetch_bhavcopy_for_date(target_dt)
        assert df1 is not None
        assert mock_get.call_count == 1
        assert "HDFCBANK" in df1.index

        # Second fetch should hit memory/disk cache without network call
        df2 = fetcher.fetch_bhavcopy_for_date(target_dt)
        assert df2 is not None
        assert mock_get.call_count == 1  # No additional network request


def test_bhavcopy_lookback_weekend_to_friday(tmp_path):
    fetcher = NSEBhavcopyFetcher(cache_dir=tmp_path)
    sunday_dt = datetime(2026, 11, 22, 10, 0)  # Sunday

    def mock_fetch(dt):
        if dt.strftime("%Y-%m-%d") == "2026-11-20":  # Friday
            return NSEBhavcopyFetcher.parse_raw_csv(SAMPLE_RAW_CSV, target_date=dt)
        return None

    with patch.object(fetcher, "fetch_bhavcopy_for_date", side_effect=mock_fetch):
        df, resolved_dt = fetcher.fetch_latest_bhavcopy(start_date=sunday_dt)
        assert df is not None
        assert resolved_dt.strftime("%Y-%m-%d") == "2026-11-20"
        assert len(df) == 3


def test_swing_setup_institutional_delivery_bonus():
    """Verify SwingSetup gets institutional accumulation tag and +10 bonus when delivery_pct >= 50%."""
    dates = pd.date_range(end=datetime.now(), periods=50, freq="D")
    df = pd.DataFrame({
        "timestamp": dates,
        "open": 100.0,
        "high": 105.0,
        "low": 98.0,
        "close": 104.0,
        "volume": 100000.0,
        "RSI_14": 55.0,
        "EMA_20": 100.0,
        "EMA_50": 98.0,
        "ATR_14": 2.0,
        "delivery_pct": 63.2,
        "delivery_qty": 44310000.0,
    })

    s = SwingSetup(
        symbol="HDFCBANK",
        pattern="Range Breakout",
        direction="LONG",
        strength=70,
        entry_zone=846.0,
        stop_loss=820.0,
        target_1=880.0,
        target_2=910.0,
        risk_reward=2.5,
        timeframe="5-10 days",
        description="Consolidation breakout above resistance",
        indicators={"rsi": 55.0},
    )

    with patch("agents.market_intelligence.swing_scanner.dedupe_swing_setups_one_per_symbol", return_value=[s]):
        with patch("agents.market_intelligence.swing_scanner.apply_swing_regime_gate", return_value=[s]):
            setups = scan_swing_setups(df, symbol="HDFCBANK", allow_fallback=False)
            assert len(setups) == 1
            out = setups[0]
            assert out.delivery_pct == 63.2
            assert out.institutional_accumulation is True
            assert "INSTITUTIONAL ACCUMULATION" in out.delivery_tag
            assert out.strength == 80  # 70 + 10 bonus


def test_swing_setup_speculative_volume_penalty():
    """Verify Breakout setup with delivery_pct < 25% gets SPECULATIVE VOLUME tag and -5 penalty."""
    dates = pd.date_range(end=datetime.now(), periods=50, freq="D")
    df = pd.DataFrame({
        "timestamp": dates,
        "open": 100.0,
        "high": 105.0,
        "low": 98.0,
        "close": 104.0,
        "volume": 100000.0,
        "RSI_14": 55.0,
        "EMA_20": 100.0,
        "EMA_50": 98.0,
        "ATR_14": 2.0,
        "delivery_pct": 15.0,
        "delivery_qty": 150000.0,
    })

    s = SwingSetup(
        symbol="SPEC_STOCK",
        pattern="Range Breakout",
        direction="LONG",
        strength=70,
        entry_zone=105.0,
        stop_loss=100.0,
        target_1=115.0,
        target_2=120.0,
        risk_reward=2.0,
        timeframe="5-10 days",
        description="Speculative pump above resistance",
        indicators={"rsi": 55.0},
    )

    with patch("agents.market_intelligence.swing_scanner.dedupe_swing_setups_one_per_symbol", return_value=[s]):
        with patch("agents.market_intelligence.swing_scanner.apply_swing_regime_gate", return_value=[s]):
            setups = scan_swing_setups(df, symbol="SPEC_STOCK", allow_fallback=False)
            assert len(setups) == 1
            out = setups[0]
            assert out.delivery_pct == 15.0
            assert out.institutional_accumulation is False
            assert "SPECULATIVE VOLUME" in out.delivery_tag
            assert out.strength == 65  # 70 - 5 penalty
