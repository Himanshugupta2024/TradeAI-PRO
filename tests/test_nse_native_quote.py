"""Unit tests for nsepython quote adapter (no live NSE calls)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.data_ingestion import nse_native_quote as nnq


def test_parse_equity_quote_extracts_pchange_and_volume():
    payload = {
        "priceInfo": {"pChange": 1.25, "lastPrice": 100.0},
        "preOpenMarket": {"totalTradedVolume": 50000},
    }
    out = nnq._parse_equity_quote(payload)
    assert out == {"price_change_pct": 1.25, "volume": 50000.0}


def test_parse_equity_quote_string_pchange():
    payload = {"priceInfo": {"pChange": "-0.50"}}
    out = nnq._parse_equity_quote(payload)
    assert out["price_change_pct"] == -0.50


def test_parse_equity_quote_missing_returns_none():
    assert nnq._parse_equity_quote({}) is None


def test_parse_index_quote():
    out = nnq._parse_index_quote({"percChange": "0.07", "last": "25682.40"})
    assert out == {"price_change_pct": 0.07, "volume": 0.0}


def test_try_nsepython_respects_disabled(monkeypatch):
    import config.settings as settings_mod

    monkeypatch.setattr(settings_mod, "NSEPYTHON_QUOTES_ENABLED", False)
    assert nnq.try_nsepython_stock_snapshot("RELIANCE") is None


def test_try_nsepython_sensex_returns_none():
    assert nnq.try_nsepython_stock_snapshot("SENSEX") is None
