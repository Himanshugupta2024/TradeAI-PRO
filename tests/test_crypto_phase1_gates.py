"""
Tests for Crypto Phase 1 accuracy gates in api.service._compute_crypto_intelligence.

Covers:
  A2 - Volume confirmation gate (CRYPTO_VOL_MIN_RATIO).
  A4 - Funding rate hard gate (CRYPTO_FUNDING_MAX_LONG / CRYPTO_FUNDING_MIN_SHORT).

Each test injects a single crypto symbol into _signal_cache + _market_data +
_crypto_futures_cache, runs the aggregator, and asserts the setup is either
present in top_setups (gate passed) or moved to avoid_list with the expected
blocker tag. Top setups include tier="premium" | "exploratory" (C-3 sparse fill).
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


# ── helpers ─────────────────────────────────────────────────────────────────


def _signal_df(
    signal: str = "LONG",
    *,
    volume_ratio_20: float | None = 1.8,
    close: float = 65000.0,
) -> pd.DataFrame:
    """Minimal DataFrame that satisfies the schema the aggregator reads."""
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
def _inject_symbol(
    symbol: str,
    df: pd.DataFrame,
    *,
    funding_rate: float | None = None,
):
    """Scope in a single symbol into the service caches and restore on exit."""
    orig_signal = svc._signal_cache.copy()
    orig_market = svc._market_data.copy()
    orig_futures = svc._crypto_futures_cache.copy()
    try:
        svc._signal_cache[symbol] = df
        svc._market_data[symbol] = df
        if funding_rate is not None:
            svc._crypto_futures_cache[symbol] = {
                "symbol": symbol,
                "funding_rate": float(funding_rate),
                "funding_rate_pct": float(funding_rate) * 100,
                "funding_label": "NEUTRAL",
                "open_interest": 5e8,
                "oi_label": "MODERATE",
                "basis_pct": 0.0,
                "advisory": True,
                "source": "test",
            }
        yield
    finally:
        svc._signal_cache.clear()
        svc._signal_cache.update(orig_signal)
        svc._market_data.clear()
        svc._market_data.update(orig_market)
        svc._crypto_futures_cache.clear()
        svc._crypto_futures_cache.update(orig_futures)


def _find_symbol(bucket: list[dict], symbol: str) -> dict | None:
    return next((s for s in bucket if s.get("symbol") == symbol), None)


def _run() -> dict:
    """Run the aggregator with mood/FAPI calls neutralized."""
    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        return svc._compute_crypto_intelligence()


# ── A2: volume confirmation gate ────────────────────────────────────────────


class TestVolumeGate(unittest.TestCase):
    """CRYPTO_VOL_MIN_RATIO must block LONG/SHORT whose last bar had weak volume."""

    def test_passes_when_volume_above_threshold(self):
        sym = "BTC/USDT"
        df = _signal_df("LONG", volume_ratio_20=1.8)
        with _inject_symbol(sym, df):
            with patch.object(settings, "CRYPTO_VOL_MIN_RATIO", 1.30), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", False):
                payload = _run()

        setup = _find_symbol(payload["top_setups"], sym)
        self.assertIsNotNone(setup, "High-volume LONG should reach top_setups")
        self.assertEqual(setup["volume_ratio"], 1.8)
        self.assertFalse(
            any(b.startswith("low_volume") for b in setup.get("blockers", [])),
            f"Should not have a low_volume blocker: {setup.get('blockers')}",
        )

    def test_blocks_long_when_volume_too_low(self):
        sym = "BTC/USDT"
        df = _signal_df("LONG", volume_ratio_20=0.8)
        with _inject_symbol(sym, df):
            with patch.object(settings, "CRYPTO_VOL_MIN_RATIO", 1.30), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", False):
                payload = _run()

        self.assertIsNone(
            _find_symbol(payload["top_setups"], sym),
            "Low-volume LONG must NOT appear in top_setups",
        )
        avoided = _find_symbol(payload["avoid_list"], sym)
        self.assertIsNotNone(avoided, "Low-volume LONG should land in avoid_list")
        self.assertTrue(
            any(b.startswith("low_volume") for b in avoided.get("blockers", [])),
            f"avoid_list entry must carry low_volume blocker: {avoided.get('blockers')}",
        )

    def test_blocks_short_when_volume_too_low(self):
        sym = "ETH/USDT"
        df = _signal_df("SHORT", volume_ratio_20=0.7, close=3200.0)
        with _inject_symbol(sym, df):
            with patch.object(settings, "CRYPTO_VOL_MIN_RATIO", 1.30), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", False):
                payload = _run()

        self.assertIsNone(_find_symbol(payload["top_setups"], sym))
        avoided = _find_symbol(payload["avoid_list"], sym)
        self.assertIsNotNone(avoided)
        self.assertTrue(any(b.startswith("low_volume") for b in avoided["blockers"]))

    def test_disabled_gate_lets_low_volume_through(self):
        sym = "SOL/USDT"
        df = _signal_df("LONG", volume_ratio_20=0.5, close=150.0)
        with _inject_symbol(sym, df):
            with patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", False), \
                 patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", False):
                payload = _run()

        setup = _find_symbol(payload["top_setups"], sym)
        self.assertIsNotNone(setup, "With gate off, low-volume LONG must still rank")
        self.assertFalse(
            any(b.startswith("low_volume") for b in setup.get("blockers", [])),
            "Disabled gate should not add low_volume blockers",
        )

    def test_missing_volume_ratio_skips_gate(self):
        """NaN volume_ratio_20 should not create a blocker — absence of data != fail."""
        sym = "XRP/USDT"
        df = _signal_df("LONG", volume_ratio_20=None, close=0.55)
        with _inject_symbol(sym, df):
            with patch.object(settings, "CRYPTO_VOL_MIN_RATIO", 1.30), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", False):
                payload = _run()

        setup = _find_symbol(payload["top_setups"], sym)
        self.assertIsNotNone(setup, "Missing volume data must not block the setup")
        self.assertIsNone(setup["volume_ratio"])


# ── A4: funding rate gate ───────────────────────────────────────────────────


class TestFundingGate(unittest.TestCase):
    """Asymmetric gate: crowded longs block LONG, crowded shorts block SHORT."""

    def test_passes_when_funding_neutral(self):
        sym = "BTC/USDT"
        df = _signal_df("LONG", volume_ratio_20=1.8)
        with _inject_symbol(sym, df, funding_rate=0.0001):
            with patch.object(settings, "CRYPTO_FUNDING_MAX_LONG", 0.0005), \
                 patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", False):
                payload = _run()

        setup = _find_symbol(payload["top_setups"], sym)
        self.assertIsNotNone(setup, "Neutral funding should not block LONG")
        self.assertAlmostEqual(setup["funding_rate"], 0.0001, places=6)

    def test_blocks_long_when_funding_too_high(self):
        sym = "BTC/USDT"
        df = _signal_df("LONG", volume_ratio_20=1.8)
        with _inject_symbol(sym, df, funding_rate=0.0012):
            with patch.object(settings, "CRYPTO_FUNDING_MAX_LONG", 0.0005), \
                 patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", False):
                payload = _run()

        self.assertIsNone(_find_symbol(payload["top_setups"], sym))
        avoided = _find_symbol(payload["avoid_list"], sym)
        self.assertIsNotNone(avoided)
        self.assertTrue(
            any(b.startswith("crowded_longs") for b in avoided["blockers"]),
            f"avoid_list entry should tag crowded_longs: {avoided['blockers']}",
        )

    def test_blocks_short_when_funding_too_negative(self):
        sym = "ETH/USDT"
        df = _signal_df("SHORT", volume_ratio_20=1.8, close=3200.0)
        with _inject_symbol(sym, df, funding_rate=-0.0015):
            with patch.object(settings, "CRYPTO_FUNDING_MIN_SHORT", -0.0005), \
                 patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", False):
                payload = _run()

        self.assertIsNone(_find_symbol(payload["top_setups"], sym))
        avoided = _find_symbol(payload["avoid_list"], sym)
        self.assertIsNotNone(avoided)
        self.assertTrue(any(b.startswith("crowded_shorts") for b in avoided["blockers"]))

    def test_asymmetric_short_passes_when_funding_only_mildly_positive(self):
        """Positive funding must NOT block SHORT — gate is asymmetric."""
        sym = "SOL/USDT"
        df = _signal_df("SHORT", volume_ratio_20=1.8, close=150.0)
        with _inject_symbol(sym, df, funding_rate=0.002):   # crowded LONGs
            with patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", False):
                payload = _run()

        setup = _find_symbol(payload["top_setups"], sym)
        self.assertIsNotNone(setup, "Positive funding should NOT block a SHORT signal")

    def test_missing_funding_skips_gate(self):
        sym = "MATIC/USDT"
        df = _signal_df("LONG", volume_ratio_20=1.8, close=0.85)
        # No funding entry at all — must pass through
        with _inject_symbol(sym, df, funding_rate=None):
            with patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", False):
                payload = _run()

        setup = _find_symbol(payload["top_setups"], sym)
        self.assertIsNotNone(setup, "Missing funding data must not block the setup")
        self.assertIsNone(setup["funding_rate"])

    def test_disabled_gate_lets_crowded_long_through(self):
        sym = "BTC/USDT"
        df = _signal_df("LONG", volume_ratio_20=1.8)
        with _inject_symbol(sym, df, funding_rate=0.01):   # very crowded longs
            with patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", False), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", False):
                payload = _run()

        setup = _find_symbol(payload["top_setups"], sym)
        self.assertIsNotNone(setup, "With funding gate off, crowded LONG must still rank")
        self.assertFalse(
            any(b.startswith("crowded_longs") for b in setup.get("blockers", [])),
            "Disabled funding gate should not add crowded_longs blocker",
        )


# ── Combined gates ──────────────────────────────────────────────────────────


class TestCombinedGates(unittest.TestCase):
    """Both gates active: failing EITHER should land the setup in avoid_list."""

    def test_both_blockers_surfaced(self):
        sym = "BTC/USDT"
        df = _signal_df("LONG", volume_ratio_20=0.5)
        with _inject_symbol(sym, df, funding_rate=0.002):
            with patch.object(settings, "CRYPTO_VOL_MIN_RATIO", 1.30), \
                 patch.object(settings, "CRYPTO_FUNDING_MAX_LONG", 0.0005), \
                 patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", True):
                payload = _run()

        self.assertIsNone(_find_symbol(payload["top_setups"], sym))
        avoided = _find_symbol(payload["avoid_list"], sym)
        self.assertIsNotNone(avoided)
        b = avoided["blockers"]
        self.assertTrue(any(x.startswith("low_volume") for x in b))
        self.assertTrue(any(x.startswith("crowded_longs") for x in b))

    def test_both_gates_pass_in_top_setups(self):
        sym = "ETH/USDT"
        df = _signal_df("LONG", volume_ratio_20=1.7, close=3200.0)
        with _inject_symbol(sym, df, funding_rate=0.0001):
            with patch.object(settings, "CRYPTO_VOL_GATE_ENABLED", True), \
                 patch.object(settings, "CRYPTO_FUNDING_GATE_ENABLED", True):
                payload = _run()

        setup = _find_symbol(payload["top_setups"], sym)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["volume_ratio"], 1.7)
        self.assertEqual(setup["funding_rate"], 0.0001)
        self.assertEqual(setup["blockers"], [])


if __name__ == "__main__":
    unittest.main()
