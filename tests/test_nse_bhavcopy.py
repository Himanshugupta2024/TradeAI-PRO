"""
Unit tests for NSEBhavcopyProvider.
"""
import io
import zipfile
import pytest
from datetime import date
from unittest.mock import patch, MagicMock
from pathlib import Path
import pandas as pd

from data.providers.nse_bhavcopy import NSEBhavcopyProvider


def _create_mock_bhavcopy_zip_bytes() -> bytes:
    """Create in-memory zip bytes containing mock NSE UDiFF CSV."""
    csv_content = (
        "TradDt,BizDt,Sgmt,Src,FinInstrmId,FinInstrmTp,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal\n"
        "2026-10-30,2026-10-30,CM,NSE,1,STK,INFY,EQ,1500.0,1520.0,1490.0,1515.0,1495.0,2000000,3030000000\n"
        "2026-10-30,2026-10-30,CM,NSE,2,STK,TCS,EQ,3500.0,3550.0,3480.0,3530.0,3490.0,1000000,3530000000\n"
        "2026-10-30,2026-10-30,CM,NSE,3,BND,GOI712,GS,100.0,100.5,99.8,100.2,100.0,50000,5010000\n"
        "2026-10-30,2026-10-30,CM,NSE,4,STK,BADPRICE,EQ,0.0,0.0,0.0,0.0,10.0,100,0\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("BhavCopy_NSE_CM_0_0_0_20261030_F_0000.csv", csv_content)
    return buf.getvalue()


def test_nse_bhavcopy_download_and_parse(tmp_path):
    provider = NSEBhavcopyProvider(cache_dir=tmp_path)
    zip_bytes = _create_mock_bhavcopy_zip_bytes()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = zip_bytes

    target_d = date(2026, 10, 30)  # Friday

    with patch("requests.get", return_value=mock_resp):
        df = provider.fetch_daily_bhavcopy(target_date=target_d)

    assert not df.empty
    # Must only contain Series 'EQ' with valid prices > 0 (INFY and TCS, excluding GOI712 bond and BADPRICE)
    assert len(df) == 2
    symbols = set(df["symbol"].tolist())
    assert symbols == {"INFY", "TCS"}

    # Verify column renaming
    for col in ["date", "symbol", "open", "high", "low", "close", "prev_close", "volume", "turnover"]:
        assert col in df.columns

    # Verify compressed CSV file was created in cache
    cache_file = tmp_path / "bhavcopy_20261030.csv.gz"
    assert cache_file.exists()


def test_nse_bhavcopy_cache_hit_avoids_network(tmp_path):
    provider = NSEBhavcopyProvider(cache_dir=tmp_path)
    zip_bytes = _create_mock_bhavcopy_zip_bytes()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = zip_bytes

    target_d = date(2026, 10, 30)

    # 1st call populates cache
    with patch("requests.get", return_value=mock_resp) as mock_get:
        df1 = provider.fetch_daily_bhavcopy(target_date=target_d)
        assert mock_get.call_count == 1

    # 2nd call should read parquet and NOT call requests.get
    with patch("requests.get") as mock_get_2:
        df2 = provider.fetch_daily_bhavcopy(target_date=target_d)
        assert mock_get_2.call_count == 0
        assert len(df2) == len(df1)


def test_nse_bhavcopy_holiday_backtracking(tmp_path):
    provider = NSEBhavcopyProvider(cache_dir=tmp_path)
    zip_bytes = _create_mock_bhavcopy_zip_bytes()

    # Suppose target is Sunday 2026-11-01 -> Friday 2026-10-30 is active
    # requests.get will 404 for Nov 1 and Oct 31 (weekends are skipped by date check anyway),
    # and 200 for Oct 30.
    def _mock_get(url, **kwargs):
        resp = MagicMock()
        if "20261030" in url:
            resp.status_code = 200
            resp.content = zip_bytes
        else:
            resp.status_code = 404
            resp.content = b""
        return resp

    with patch("requests.get", side_effect=_mock_get):
        df = provider.fetch_daily_bhavcopy(target_date=date(2026, 11, 1), max_backtrack_days=4)

    assert not df.empty
    assert "INFY" in df["symbol"].values


def test_nse_bhavcopy_single_and_batch_eod(tmp_path):
    provider = NSEBhavcopyProvider(cache_dir=tmp_path)
    zip_bytes = _create_mock_bhavcopy_zip_bytes()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = zip_bytes

    with patch("requests.get", return_value=mock_resp):
        # Single lookup
        infy = provider.get_symbol_eod("INFY.NS", target_date=date(2026, 10, 30))
        assert infy is not None
        assert infy["symbol"] == "INFY"
        assert infy["close"] == 1515.0
        assert infy["prev_close"] == 1495.0
        assert infy["change_pct"] == round(((1515.0 - 1495.0) / 1495.0) * 100.0, 2)

        # Batch lookup
        batch = provider.get_batch_eod(["INFY", "TCS", "NONEXISTENT"], target_date=date(2026, 10, 30))
        assert len(batch) == 2
        assert "INFY" in batch
        assert "TCS" in batch
        assert batch["TCS"]["close"] == 3530.0
