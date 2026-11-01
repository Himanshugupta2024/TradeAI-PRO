"""
Tests for Crypto Phase 1 Milestone C-2 — MTF (4h) confluence gate (A1).

Rules under test (relaxed mode — default):
  LONG  blocked when 4h trend == BEARISH; BULLISH/NEUTRAL pass.
  SHORT blocked when 4h trend == BULLISH;  BEARISH/NEUTRAL pass.
Strict mode:
  LONG  requires 4h == BULLISH; NEUTRAL blocked.
  SHORT requires 4h == BEARISH; NEUTRAL blocked.
Missing 4h trend (None) → gate skipped gracefully.
Disabled master switch → gate never fires.
"""

from __future__ import annotations

import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import settings  # noqa: E402
from api import service as svc  # noqa: E402


def _signal_df(
    signal: str = "LONG",
    *,
    volume_ratio_20: float | None = 1.8,
    close: float = 65000.0,
) -> pd.DataFrame:
    row = {
        "close": close,
        "signal": signal,
        "quality_score": 72,
        "quality_grade": "B",
        "confidence": 68.0,
        "context_score": 0.10,
        "drift_detected": False,
        "event_risk_active": False,
        "skip_reason": "",
        "stale": False,
        "trend": "BULLISH" if signal == "LONG" else "BEARISH",
        "regime": "TRENDING",
        "rsi": 45.0 if signal == "LONG" else 62.0,
        "entry": close,
        "stop_loss": close * (0.97 if signal == "LONG" else 1.03),
        "target": close * (1.06 if signal == "LONG" else 0.94),
        "conflict": {"has_conflict": False},
        "timestamp": pd.Timestamp.now("UTC"),
    }
    if volume_ratio_20 is None:
        row["volume_ratio_20"] = np.nan
    else:
        row["volume_ratio_20"] = float(volume_ratio_20)
    return pd.DataFrame([row])


@contextmanager
def _inject(symbol: str, df: pd.DataFrame, *, mtf_trend: str | None):
    orig_signal = svc._signal_cache.copy()
    orig_market = svc._market_data.copy()
    orig_futures = svc._crypto_futures_cache.copy()
    orig_mtf = svc._crypto_htf4_trend.copy()
    try:
        svc._signal_cache[symbol] = df
        svc._market_data[symbol] = df
        svc._crypto_htf4_trend[symbol] = mtf_trend
        yield
    finally:
        svc._signal_cache.clear()
        svc._signal_cache.update(orig_signal)
        svc._market_data.clear()
        svc._market_data.update(orig_market)
        svc._crypto_futures_cache.clear()
        svc._crypto_futures_cache.update(orig_futures)
        svc._crypto_htf4_trend.clear()
        svc._crypto_htf4_trend.update(orig_mtf)


def _find(bucket: list[dict], symbol: str) -> dict | None:
    return next((s for s in bucket if s.get("symbol") == symbol), None)


def _run() -> dict:
    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        return svc._compute_crypto_intelligence()


def _only_mtf_gate():
    """Disable volume/funding gates so MTF gate is isolated in assertions."""
    return patch.multiple(
        settings,
        CRYPTO_VOL_GATE_ENABLED=False,
        CRYPTO_FUNDING_GATE_ENABLED=False,
        CRYPTO_MTF_GATE_ENABLED=True,
        CRYPTO_MTF_STRICT=False,
    )


# ── Relaxed mode (default) ──────────────────────────────────────────────────


class TestMtfGateRelaxed(unittest.TestCase):
    def test_long_passes_when_4h_bullish(self):
        sym = "BTC/USDT"
        with _inject(sym, _signal_df("LONG"), mtf_trend="BULLISH"), _only_mtf_gate():
            payload = _run()
        setup = _find(payload["top_setups"], sym)
        self.assertIsNotNone(setup, "BULLISH 4h must pass for LONG")
        self.assertEqual(setup["mtf_trend_4h"], "BULLISH")
        self.assertFalse(
            any(b.startswith("mtf_conflict") for b in setup.get("blockers", []))
        )

    def test_long_passes_when_4h_neutral(self):
        sym = "BTC/USDT"
        with _inject(sym, _signal_df("LONG"), mtf_trend="NEUTRAL"), _only_mtf_gate():
            payload = _run()
        self.assertIsNotNone(
            _find(payload["top_setups"], sym),
            "NEUTRAL 4h must pass for LONG in relaxed mode",
        )

    def test_long_blocked_when_4h_bearish(self):
        sym = "BTC/USDT"
        with _inject(sym, _signal_df("LONG"), mtf_trend="BEARISH"), _only_mtf_gate():
            payload = _run()
        self.assertIsNone(
            _find(payload["top_setups"], sym),
            "BEARISH 4h must block LONG",
        )
        avoided = _find(payload["avoid_list"], sym)
        self.assertIsNotNone(avoided, "Blocked LONG should surface in avoid_list")
        self.assertTrue(
            any(b.startswith("mtf_conflict") for b in avoided.get("blockers", [])),
            f"expected mtf_conflict blocker, got {avoided.get('blockers')}",
        )
        self.assertEqual(avoided["mtf_trend_4h"], "BEARISH")

    def test_short_passes_when_4h_bearish(self):
        sym = "ETH/USDT"
        with _inject(sym, _signal_df("SHORT", close=3200.0), mtf_trend="BEARISH"), _only_mtf_gate():
            payload = _run()
        self.assertIsNotNone(_find(payload["top_setups"], sym))

    def test_short_passes_when_4h_neutral(self):
        sym = "ETH/USDT"
        with _inject(sym, _signal_df("SHORT", close=3200.0), mtf_trend="NEUTRAL"), _only_mtf_gate():
            payload = _run()
        self.assertIsNotNone(_find(payload["top_setups"], sym))

    def test_short_blocked_when_4h_bullish(self):
        sym = "ETH/USDT"
        with _inject(sym, _signal_df("SHORT", close=3200.0), mtf_trend="BULLISH"), _only_mtf_gate():
            payload = _run()
        self.assertIsNone(_find(payload["top_setups"], sym))
        avoided = _find(payload["avoid_list"], sym)
        self.assertIsNotNone(avoided)
        self.assertTrue(
            any(b.startswith("mtf_conflict") for b in avoided.get("blockers", []))
        )


# ── Strict mode ─────────────────────────────────────────────────────────────


class TestMtfGateStrict(unittest.TestCase):
    def _ctx(self):
        return patch.multiple(
            settings,
            CRYPTO_VOL_GATE_ENABLED=False,
            CRYPTO_FUNDING_GATE_ENABLED=False,
            CRYPTO_MTF_GATE_ENABLED=True,
            CRYPTO_MTF_STRICT=True,
        )

    def test_long_blocked_when_4h_neutral_strict(self):
        sym = "BTC/USDT"
        with _inject(sym, _signal_df("LONG"), mtf_trend="NEUTRAL"), self._ctx():
            payload = _run()
        self.assertIsNone(_find(payload["top_setups"], sym))
        avoided = _find(payload["avoid_list"], sym)
        self.assertIsNotNone(avoided)
        self.assertTrue(
            any(b.startswith("mtf_conflict") for b in avoided.get("blockers", []))
        )

    def test_long_allowed_when_4h_bullish_strict(self):
        sym = "BTC/USDT"
        with _inject(sym, _signal_df("LONG"), mtf_trend="BULLISH"), self._ctx():
            payload = _run()
        self.assertIsNotNone(_find(payload["top_setups"], sym))

    def test_short_blocked_when_4h_neutral_strict(self):
        sym = "ETH/USDT"
        with _inject(sym, _signal_df("SHORT", close=3200.0), mtf_trend="NEUTRAL"), self._ctx():
            payload = _run()
        self.assertIsNone(_find(payload["top_setups"], sym))


# ── Edge cases ──────────────────────────────────────────────────────────────


class TestMtfGateEdgeCases(unittest.TestCase):
    def test_missing_mtf_trend_skips_gate(self):
        sym = "BTC/USDT"
        with _inject(sym, _signal_df("LONG"), mtf_trend=None), _only_mtf_gate():
            payload = _run()
        setup = _find(payload["top_setups"], sym)
        self.assertIsNotNone(setup, "Missing 4h trend must NOT block the signal")
        self.assertIsNone(setup["mtf_trend_4h"])
        self.assertFalse(
            any(b.startswith("mtf_conflict") for b in setup.get("blockers", []))
        )

    def test_disabled_switch_bypasses_gate(self):
        sym = "BTC/USDT"
        ctx = patch.multiple(
            settings,
            CRYPTO_VOL_GATE_ENABLED=False,
            CRYPTO_FUNDING_GATE_ENABLED=False,
            CRYPTO_MTF_GATE_ENABLED=False,
        )
        with _inject(sym, _signal_df("LONG"), mtf_trend="BEARISH"), ctx:
            payload = _run()
        setup = _find(payload["top_setups"], sym)
        self.assertIsNotNone(setup, "Disabled MTF gate must let BEARISH 4h pass")

    def test_mtf_gate_combines_with_volume_gate(self):
        """Both blockers should land on the same avoid_list entry."""
        sym = "BTC/USDT"
        df = _signal_df("LONG", volume_ratio_20=0.6)
        ctx = patch.multiple(
            settings,
            CRYPTO_VOL_GATE_ENABLED=True,
            CRYPTO_VOL_MIN_RATIO=1.30,
            CRYPTO_FUNDING_GATE_ENABLED=False,
            CRYPTO_MTF_GATE_ENABLED=True,
            CRYPTO_MTF_STRICT=False,
        )
        with _inject(sym, df, mtf_trend="BEARISH"), ctx:
            payload = _run()
        avoided = _find(payload["avoid_list"], sym)
        self.assertIsNotNone(avoided)
        tags = avoided.get("blockers", [])
        self.assertTrue(any(b.startswith("low_volume") for b in tags))
        self.assertTrue(any(b.startswith("mtf_conflict") for b in tags))


if __name__ == "__main__":
    unittest.main()
