"""
Integration tests for api.service ↔ db.swing_store wiring.

Covers the three glue points added in Milestone 2:

1. _persist_swing_scan — calls swing_store.save_scan via asyncio.to_thread,
   returns the scan_id, and swallows exceptions.
2. _hydrate_swing_cache_from_db — seeds _swing_cache on DB hit, no-ops on miss.
3. _recompute_swing_cache — triggers persistence after a successful scan.

All tests monkeypatch the swing store and the pattern scanner, so they run
fast and do NOT touch real SQLite or real market data.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pandas as pd
import pytest

from api import service as svc


# ─── helpers ─────────────────────────────────────────────────────────────


def _dummy_setup(symbol: str = "RELIANCE.NS", grade: str = "A") -> dict:
    return {
        "symbol": symbol,
        "pattern": "20-Day Range Breakout",
        "direction": "LONG",
        "strength": 78,
        "entry_zone": 100.0,
        "stop_loss": 95.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "risk_reward": 2.5,
        "timeframe": "3-7 days",
        "grade": grade,
        "description": "test setup",
    }


@pytest.fixture()
def reset_swing_cache(monkeypatch):
    """Snapshot + restore module-level _swing_cache and persist date so tests don't bleed.
    Also mocks swing_shortlist_latest.json existence to isolate DB hydration logic.
    """
    import os
    snapshot = dict(svc._swing_cache)
    orig_persist_date = getattr(svc, "_last_swing_persist_date", None)
    svc._last_swing_persist_date = None

    orig_exists = os.path.exists
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda path: False if "swing_shortlist_latest.json" in str(path) else orig_exists(path),
    )

    yield

    svc._swing_cache.clear()
    svc._swing_cache.update(snapshot)
    svc._last_swing_persist_date = orig_persist_date


@pytest.fixture()
def mock_store(monkeypatch):
    """Replace svc._swing_store with a fresh MagicMock (store enabled)."""
    fake = MagicMock()
    fake.save_scan = MagicMock(return_value=42)
    fake.load_latest_scan = MagicMock(return_value=None)
    monkeypatch.setattr(svc, "_swing_store", fake, raising=False)
    monkeypatch.setattr(svc, "_SWING_STORE_ENABLED", True, raising=False)
    return fake


# ─── _persist_swing_scan ────────────────────────────────────────────────


def test_persist_scan_calls_store_and_returns_id(mock_store):
    result = asyncio.run(
        svc._persist_swing_scan(
            [_dummy_setup("RELIANCE.NS")],
            {"index_symbol": "NIFTY"},
            symbols_scanned=50,
            trigger="manual",
        )
    )
    assert result == 42
    mock_store.save_scan.assert_called_once()
    args, kwargs = mock_store.save_scan.call_args
    # positional: setups, market_context
    assert args[0][0]["symbol"] == "RELIANCE.NS"
    assert args[1] == {"index_symbol": "NIFTY"}
    assert kwargs["symbols_scanned"] == 50
    assert kwargs["trigger"] == "manual"


def test_persist_scan_swallows_store_exceptions(monkeypatch):
    fake = MagicMock()
    fake.save_scan = MagicMock(side_effect=RuntimeError("simulated DB outage"))
    monkeypatch.setattr(svc, "_swing_store", fake, raising=False)
    monkeypatch.setattr(svc, "_SWING_STORE_ENABLED", True, raising=False)

    result = asyncio.run(
        svc._persist_swing_scan([_dummy_setup()], None, symbols_scanned=1, trigger="periodic")
    )
    assert result is None   # exception swallowed, returns None


def test_persist_scan_noop_when_store_disabled(monkeypatch):
    monkeypatch.setattr(svc, "_swing_store", None, raising=False)
    monkeypatch.setattr(svc, "_SWING_STORE_ENABLED", False, raising=False)

    result = asyncio.run(
        svc._persist_swing_scan([_dummy_setup()], None, symbols_scanned=1, trigger="periodic")
    )
    assert result is None


# ─── _hydrate_swing_cache_from_db ───────────────────────────────────────


def test_hydrate_populates_cache_on_hit(monkeypatch, reset_swing_cache):
    fake = MagicMock()
    fake.load_latest_scan = MagicMock(
        return_value={
            "id": 7,
            "scan_time": "2026-04-18T03:00:00",
            "trigger": "manual",
            "total_setups": 2,
            "symbols_scanned": 80,
            "setups": [_dummy_setup("A.NS"), _dummy_setup("B.NS", grade="B")],
            "market_context": {"index_symbol": "NIFTY", "index_trend": "BULLISH"},
        }
    )
    monkeypatch.setattr(svc, "_swing_store", fake, raising=False)
    monkeypatch.setattr(svc, "_SWING_STORE_ENABLED", True, raising=False)

    ok = asyncio.run(svc._hydrate_swing_cache_from_db())
    assert ok is True

    assert len(svc._swing_cache["setups"]) == 2
    assert svc._swing_cache["setups"][0]["symbol"] == "A.NS"
    assert svc._swing_cache["total_found"] == 2
    assert svc._swing_cache["symbols_scanned"] == 80
    assert svc._swing_cache["real_data"] is True
    assert svc._swing_cache["hydrated_from_db"] is True
    assert svc._swing_cache["market_context"]["index_trend"] == "BULLISH"


def test_hydrate_noop_on_empty_db(monkeypatch, reset_swing_cache):
    fake = MagicMock()
    fake.load_latest_scan = MagicMock(return_value=None)
    monkeypatch.setattr(svc, "_swing_store", fake, raising=False)
    monkeypatch.setattr(svc, "_SWING_STORE_ENABLED", True, raising=False)

    ok = asyncio.run(svc._hydrate_swing_cache_from_db())
    assert ok is False
    assert svc._swing_cache["setups"] == []
    assert svc._swing_cache.get("hydrated_from_db") is not True


def test_hydrate_swallows_load_exceptions(monkeypatch, reset_swing_cache):
    fake = MagicMock()
    fake.load_latest_scan = MagicMock(side_effect=RuntimeError("db corrupt"))
    monkeypatch.setattr(svc, "_swing_store", fake, raising=False)
    monkeypatch.setattr(svc, "_SWING_STORE_ENABLED", True, raising=False)

    ok = asyncio.run(svc._hydrate_swing_cache_from_db())
    assert ok is False
    assert svc._swing_cache["setups"] == []


def test_hydrate_respects_max_setups_truncation(monkeypatch, reset_swing_cache):
    many_setups = [_dummy_setup(f"SYM{i}.NS") for i in range(10)]
    fake = MagicMock()
    fake.load_latest_scan = MagicMock(
        return_value={
            "id": 1,
            "scan_time": "2026-04-18T03:00:00",
            "trigger": "manual",
            "total_setups": 10,
            "symbols_scanned": 10,
            "setups": many_setups,
            "market_context": None,
        }
    )
    monkeypatch.setattr(svc, "_swing_store", fake, raising=False)
    monkeypatch.setattr(svc, "_SWING_STORE_ENABLED", True, raising=False)
    monkeypatch.setattr(svc.settings, "SWING_MAX_SETUPS_RETURN", 3, raising=False)

    asyncio.run(svc._hydrate_swing_cache_from_db())
    assert len(svc._swing_cache["setups"]) == 3
    assert svc._swing_cache["total_found"] == 10   # UI-truncated ≠ total_found


# ─── _recompute_swing_cache persists on success ─────────────────────────


def test_recompute_persists_on_success(monkeypatch, mock_store, reset_swing_cache):
    # Prime _daily_data_cache with one entry so recompute doesn't early-exit.
    df = pd.DataFrame({"close": [100, 101, 102], "volume": [1_000, 1_100, 1_200]})
    monkeypatch.setattr(svc, "_daily_data_cache", {"RELIANCE.NS": df}, raising=False)

    # Stub the pattern scanner so we don't pull live market data.
    monkeypatch.setattr(svc, "swing_scan_all", lambda *a, **kw: [_dummy_setup("RELIANCE.NS")])

    # Market context helper should not explode without a real regime cache.
    monkeypatch.setattr(
        svc,
        "_build_swing_market_context",
        lambda: {"index_symbol": "NIFTY", "index_regime": "TRENDING", "index_trend": "BULLISH"},
    )

    ok = asyncio.run(svc._recompute_swing_cache(timeout_sec=5.0, trigger="manual"))
    assert ok is True
    assert svc._swing_cache["setups"][0]["symbol"] == "RELIANCE.NS"

    mock_store.save_scan.assert_called_once()
    _args, kwargs = mock_store.save_scan.call_args
    assert kwargs["trigger"] == "manual"
    assert kwargs["symbols_scanned"] == 1


def test_recompute_skips_persistence_when_cache_empty(monkeypatch, mock_store):
    monkeypatch.setattr(svc, "_daily_data_cache", {}, raising=False)
    ok = asyncio.run(svc._recompute_swing_cache(timeout_sec=5.0))
    assert ok is False
    mock_store.save_scan.assert_not_called()


def test_recompute_default_trigger_is_periodic(monkeypatch, mock_store, reset_swing_cache):
    df = pd.DataFrame({"close": [100, 101], "volume": [1_000, 1_100]})
    monkeypatch.setattr(svc, "_daily_data_cache", {"X.NS": df}, raising=False)
    monkeypatch.setattr(svc, "swing_scan_all", lambda *a, **kw: [])
    monkeypatch.setattr(
        svc,
        "_build_swing_market_context",
        lambda: {"index_symbol": "NIFTY", "index_regime": "RANGE", "index_trend": "NEUTRAL"},
    )

    asyncio.run(svc._recompute_swing_cache(timeout_sec=5.0))

    mock_store.save_scan.assert_called_once()
    _args, kwargs = mock_store.save_scan.call_args
    assert kwargs["trigger"] == "periodic"


def test_recompute_continues_when_persistence_fails(monkeypatch, reset_swing_cache):
    """Persistence errors must never fail the scan itself."""
    fake = MagicMock()
    fake.save_scan = MagicMock(side_effect=RuntimeError("simulated"))
    monkeypatch.setattr(svc, "_swing_store", fake, raising=False)
    monkeypatch.setattr(svc, "_SWING_STORE_ENABLED", True, raising=False)

    df = pd.DataFrame({"close": [100, 101], "volume": [1_000, 1_100]})
    monkeypatch.setattr(svc, "_daily_data_cache", {"X.NS": df}, raising=False)
    monkeypatch.setattr(svc, "swing_scan_all", lambda *a, **kw: [_dummy_setup("X.NS")])
    monkeypatch.setattr(
        svc,
        "_build_swing_market_context",
        lambda: {"index_symbol": "NIFTY", "index_regime": "TRENDING", "index_trend": "BULLISH"},
    )

    ok = asyncio.run(svc._recompute_swing_cache(timeout_sec=5.0, trigger="manual"))
    assert ok is True   # scan success even though persistence blew up
    assert svc._swing_cache["setups"][0]["symbol"] == "X.NS"


def test_build_swing_market_context_marks_choppy_from_daily_atr(monkeypatch):
    ix = "NIFTY 50"
    monkeypatch.setattr(svc.settings, "SWING_INDEX_SYMBOL", ix, raising=False)
    monkeypatch.setattr(svc.settings, "SWING_CHOPPY_ATR_RATIO_THRESHOLD", 0.7, raising=False)
    monkeypatch.setattr(svc, "_regime_cache", {ix: "TRENDING"}, raising=False)
    monkeypatch.setattr(
        svc,
        "_signal_cache",
        {ix: pd.DataFrame({"htf_trend": ["BULLISH"]})},
        raising=False,
    )
    monkeypatch.setattr(
        svc,
        "_daily_data_cache",
        {ix: pd.DataFrame({"ATR_14": [10.0] * 19 + [6.0]})},
        raising=False,
    )

    ctx = svc._build_swing_market_context()
    assert ctx["index_trend"] == "CHOPPY"
    assert ctx["index_atr_source"] == "daily_cache"
    assert ctx["index_atr_ratio"] == pytest.approx(0.6122, rel=1e-4)


def test_build_swing_market_context_uses_signal_cache_when_daily_missing(monkeypatch):
    ix = "NIFTY 50"
    monkeypatch.setattr(svc.settings, "SWING_INDEX_SYMBOL", ix, raising=False)
    monkeypatch.setattr(svc.settings, "SWING_CHOPPY_ATR_RATIO_THRESHOLD", 0.7, raising=False)
    monkeypatch.setattr(svc, "_regime_cache", {ix: "TRENDING"}, raising=False)
    monkeypatch.setattr(
        svc,
        "_signal_cache",
        {
            ix: pd.DataFrame(
                {
                    "htf_trend": ["BULLISH"] * 20,
                    "ATR_14": [10.0] * 19 + [12.0],
                }
            )
        },
        raising=False,
    )
    monkeypatch.setattr(svc, "_daily_data_cache", {}, raising=False)

    ctx = svc._build_swing_market_context()
    assert ctx["index_trend"] == "BULLISH"
    assert ctx["index_atr_source"] == "signal_cache"
    assert ctx["index_atr_ratio"] == pytest.approx(1.1881, rel=1e-4)
