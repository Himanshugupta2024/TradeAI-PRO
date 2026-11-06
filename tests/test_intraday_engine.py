"""IntradayEngine strategy smoke tests with synthetic 5m bars."""

from __future__ import annotations

import pandas as pd
import pytest

import agents.intraday_engine.scanner as intraday_scanner
from agents.intraday_engine.types import IntradaySignal
from agents.intraday_engine.strategies import strat_orb, strat_momentum


def _session_df(last_date: str = "2025-06-10") -> pd.DataFrame:
    base = pd.Timestamp(last_date + " 09:15:00")
    rows = []
    price = 100.0
    for i in range(20):
        ts = base + pd.Timedelta(minutes=5 * i)
        o = price
        h = price + 0.3
        l = price - 0.2
        c = price + 0.05 * (i % 3)
        vol = 1e5 * (1.5 if i >= 6 else 1.0)
        rows.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": vol})
        price = c
    for i in range(20, 30):
        ts = base + pd.Timedelta(minutes=5 * i)
        price += 0.4
        rows.append(
            {
                "timestamp": ts,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.1,
                "close": price + 0.45,
                "volume": 2e5,
            }
        )
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def test_orb_long_breakout():
    df = _session_df()
    sig = strat_orb("RELIANCE", df)
    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.strategy == "ORB"


def test_momentum_detects_direction():
    df = _session_df()
    sig = strat_momentum("TCS", df)
    assert sig is not None
    assert sig.strategy == "MOMENTUM"


def _df_with_last_bar_age_minutes(age_mins: int) -> pd.DataFrame:
    end = pd.Timestamp.now().floor("min") - pd.Timedelta(minutes=int(age_mins))
    start = end - pd.Timedelta(minutes=5 * 30)
    rows = []
    price = 100.0
    for i in range(30):
        ts = start + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": price,
                "high": price + 0.4,
                "low": price - 0.2,
                "close": price + 0.1,
                "volume": 100000.0,
            }
        )
        price += 0.1
    return pd.DataFrame(rows)


def test_scan_skips_stale_bars(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(intraday_scanner.settings, "INTRADAY_MAX_BAR_AGE_MINUTES", 120, raising=False)
    monkeypatch.setattr(intraday_scanner.settings, "INTRADAY_CONFLUENCE_ENABLED", False, raising=False)
    monkeypatch.setattr(intraday_scanner.settings, "INTRADAY_INDEX_CONFLUENCE_GATE", False, raising=False)
    monkeypatch.setattr(intraday_scanner, "should_square_off_now", lambda: False, raising=True)
    monkeypatch.setattr(intraday_scanner, "fetch_5m", lambda _sym: _df_with_last_bar_age_minutes(180), raising=True)

    def _always_signal(symbol: str, _df: pd.DataFrame) -> IntradaySignal:
        return IntradaySignal(symbol=symbol, strategy="ORB", direction="LONG", score=70, entry=1.0, stop_loss=0.9, target=1.2)

    monkeypatch.setattr(intraday_scanner, "STRATS", (_always_signal,), raising=True)
    out = intraday_scanner.scan_intraday_setups(["RELIANCE"])
    assert out == []


def test_scan_accepts_fresh_bars(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(intraday_scanner.settings, "INTRADAY_MAX_BAR_AGE_MINUTES", 120, raising=False)
    monkeypatch.setattr(intraday_scanner.settings, "INTRADAY_CONFLUENCE_ENABLED", False, raising=False)
    monkeypatch.setattr(intraday_scanner.settings, "INTRADAY_INDEX_CONFLUENCE_GATE", False, raising=False)
    monkeypatch.setattr(intraday_scanner, "should_square_off_now", lambda: False, raising=True)
    monkeypatch.setattr(intraday_scanner, "fetch_5m", lambda _sym: _df_with_last_bar_age_minutes(10), raising=True)

    def _always_signal(symbol: str, _df: pd.DataFrame) -> IntradaySignal:
        return IntradaySignal(symbol=symbol, strategy="ORB", direction="LONG", score=70, entry=1.0, stop_loss=0.9, target=1.2)

    monkeypatch.setattr(intraday_scanner, "STRATS", (_always_signal,), raising=True)
    out = intraday_scanner.scan_intraday_setups(["RELIANCE"])
    assert len(out) == 1
    assert out[0]["symbol"] == "RELIANCE"
