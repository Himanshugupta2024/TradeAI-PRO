"""Tests for dashboard/API symbol search ranking (no network)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.market_intelligence.nse_fetcher import search_symbols


def test_search_symbols_ind_prefers_indigo_over_hindalco():
    rows = search_symbols("IND")
    symbols = [r["symbol"] for r in rows if r["type"] == "NSE"]
    assert "INDIGO" in symbols
    assert "HINDALCO" in symbols
    assert symbols.index("INDIGO") < symbols.index("HINDALCO")


def test_search_symbols_infy_before_substring_only_hits():
    rows = search_symbols("INF")
    symbols = [r["symbol"] for r in rows if r["type"] == "NSE"]
    assert "INFY" in symbols
    assert symbols.index("INFY") == 0


def test_search_symbols_sbin_before_sbilife_for_sb_prefix():
    rows = search_symbols("SB")
    symbols = [r["symbol"] for r in rows if r["type"] == "NSE"]
    assert "SBIN" in symbols
    assert "SBILIFE" in symbols
    assert symbols.index("SBIN") < symbols.index("SBILIFE")
