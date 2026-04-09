"""
Phase 6 — Focused tests for:
- Futures context fetch normalization
- Graceful fallback when Binance FAPI is unavailable
- Crypto overview payload schema
- Ranking penalty/boost from futures context
"""
import os
import sys
import types
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_service_module():
    """Import api.service safely (without needing real DB/CCXT at import time)."""
    import importlib
    return importlib.import_module("api.service")


# ── 1. Futures context normalization ──────────────────────────────────────────

class TestFetchBinanceFuturesContext(unittest.TestCase):
    """_fetch_binance_futures_context normalizes raw Binance FAPI responses."""

    def setUp(self):
        self.svc = _make_service_module()

    def _mock_urlopen(self, premium_data, oi_data):
        """Build a urlopen mock that returns different payloads per URL."""
        import json, io
        from unittest.mock import MagicMock

        def _side_effect(url, timeout=6):
            if isinstance(url, str):
                raw_url = url
            else:
                raw_url = getattr(url, 'full_url', str(url))
            resp = MagicMock()
            if 'premiumIndex' in raw_url:
                resp.read.return_value = json.dumps(premium_data).encode()
            elif 'openInterest' in raw_url:
                resp.read.return_value = json.dumps(oi_data).encode()
            else:
                resp.read.return_value = b'[]'
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        return _side_effect

    def test_normal_parse(self):
        """Positive funding + high OI should produce correct labels."""
        premium = [
            {
                "symbol": "BTCUSDT",
                "lastFundingRate": "0.0012",
                "markPrice": "65100.0",
                "indexPrice": "65000.0",
            }
        ]
        oi = {"openInterest": "20000000000", "symbol": "BTCUSDT"}  # ~20B → HIGH

        side = self._mock_urlopen(premium, oi)
        with patch("urllib.request.urlopen", side_effect=side):
            result = self.svc._fetch_binance_futures_context(["BTC/USDT"])

        self.assertIn("BTC/USDT", result)
        ctx = result["BTC/USDT"]
        self.assertAlmostEqual(ctx["funding_rate"], 0.0012, places=5)
        self.assertEqual(ctx["funding_label"], "HIGH_POSITIVE")
        self.assertAlmostEqual(ctx["basis_pct"], round((65100 - 65000) / 65000 * 100, 4), places=3)
        self.assertEqual(ctx["oi_label"], "HIGH")
        self.assertTrue(ctx["advisory"])

    def test_negative_funding(self):
        """Negative funding rate should produce HIGH_NEGATIVE label."""
        premium = [{"symbol": "ETHUSDT", "lastFundingRate": "-0.0015", "markPrice": "3200.0", "indexPrice": "3200.0"}]
        oi = {"openInterest": "50000000", "symbol": "ETHUSDT"}

        side = self._mock_urlopen(premium, oi)
        with patch("urllib.request.urlopen", side_effect=side):
            result = self.svc._fetch_binance_futures_context(["ETH/USDT"])

        ctx = result["ETH/USDT"]
        self.assertEqual(ctx["funding_label"], "HIGH_NEGATIVE")

    def test_neutral_funding(self):
        """Near-zero funding should give NEUTRAL label."""
        premium = [{"symbol": "SOLUSDT", "lastFundingRate": "0.00005", "markPrice": "150.0", "indexPrice": "150.0"}]
        oi = {"openInterest": "5000000", "symbol": "SOLUSDT"}

        side = self._mock_urlopen(premium, oi)
        with patch("urllib.request.urlopen", side_effect=side):
            result = self.svc._fetch_binance_futures_context(["SOL/USDT"])

        ctx = result["SOL/USDT"]
        self.assertEqual(ctx["funding_label"], "NEUTRAL")

    def test_missing_symbol_returns_empty(self):
        """Symbol not in premium data → entry still returned, zeroed."""
        side = self._mock_urlopen([], {})
        with patch("urllib.request.urlopen", side_effect=side):
            result = self.svc._fetch_binance_futures_context(["BNB/USDT"])

        self.assertIn("BNB/USDT", result)
        ctx = result["BNB/USDT"]
        self.assertEqual(ctx["funding_rate"], 0.0)
        self.assertEqual(ctx["funding_label"], "NEUTRAL")


# ── 2. Graceful fallback when FAPI is unavailable ─────────────────────────────

class TestFuturesContextFallback(unittest.TestCase):
    """When Binance FAPI endpoints fail, context returns empty/zeroed data
    and the spot signal pipeline continues to work."""

    def setUp(self):
        self.svc = _make_service_module()

    def test_premiumindex_timeout(self):
        """FAPI timeout → empty dict returned, no exception raised."""
        import socket
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            result = self.svc._fetch_binance_futures_context(["BTC/USDT", "ETH/USDT"])
        # Both symbols should still appear with zeroed values
        self.assertIn("BTC/USDT", result)
        self.assertIn("ETH/USDT", result)
        for sym in ["BTC/USDT", "ETH/USDT"]:
            self.assertEqual(result[sym]["funding_rate"], 0.0)
            self.assertEqual(result[sym]["open_interest"], 0.0)

    def test_json_decode_error(self):
        """Malformed JSON from FAPI → zeroed entry, no crash."""
        import io
        bad_resp = MagicMock()
        bad_resp.read.return_value = b'{invalid json'
        bad_resp.__enter__ = lambda s: s
        bad_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=bad_resp):
            result = self.svc._fetch_binance_futures_context(["SOL/USDT"])
        self.assertIn("SOL/USDT", result)

    def test_futures_modifier_no_cache(self):
        """_crypto_futures_modifier returns (0.0, None) when cache is empty."""
        original = self.svc._crypto_futures_cache.copy()
        try:
            self.svc._crypto_futures_cache.clear()
            delta, hint = self.svc._crypto_futures_modifier("BTC/USDT", "LONG")
            self.assertEqual(delta, 0.0)
            self.assertIsNone(hint)
        finally:
            self.svc._crypto_futures_cache.update(original)


# ── 3. Futures modifier boost / penalty logic ──────────────────────────────────

class TestFuturesModifier(unittest.TestCase):
    """_crypto_futures_modifier applies correct adjustments for known states."""

    def setUp(self):
        self.svc = _make_service_module()
        self._orig = self.svc._crypto_futures_cache.copy()

    def tearDown(self):
        self.svc._crypto_futures_cache.clear()
        self.svc._crypto_futures_cache.update(self._orig)

    def _set_cache(self, sym, label):
        self.svc._crypto_futures_cache[sym] = {
            "funding_label": label,
            "funding_rate_pct": 0.01,
            "open_interest": 1e9,
            "advisory": True,
        }

    def test_long_squeeze_potential(self):
        """LONG + HIGH_NEGATIVE funding → positive delta (shorts paying, squeeze risk for shorts)."""
        self._set_cache("BTC/USDT", "HIGH_NEGATIVE")
        delta, hint = self.svc._crypto_futures_modifier("BTC/USDT", "LONG")
        self.assertGreater(delta, 0)
        self.assertIsNotNone(hint)

    def test_long_crowded_penalty(self):
        """LONG + HIGH_POSITIVE funding → negative delta (longs crowded, washout risk)."""
        self._set_cache("ETH/USDT", "HIGH_POSITIVE")
        delta, hint = self.svc._crypto_futures_modifier("ETH/USDT", "LONG")
        self.assertLess(delta, 0)
        self.assertIsNotNone(hint)

    def test_short_crowded_penalty(self):
        """SHORT + HIGH_NEGATIVE funding → negative delta (shorts crowded, squeeze risk)."""
        self._set_cache("SOL/USDT", "HIGH_NEGATIVE")
        delta, hint = self.svc._crypto_futures_modifier("SOL/USDT", "SHORT")
        self.assertLess(delta, 0)

    def test_neutral_no_delta(self):
        """NEUTRAL funding → zero delta."""
        self._set_cache("BNB/USDT", "NEUTRAL")
        delta, hint = self.svc._crypto_futures_modifier("BNB/USDT", "LONG")
        self.assertEqual(delta, 0.0)
        self.assertIsNone(hint)


# ── 4. Crypto intelligence payload schema ─────────────────────────────────────

class TestCryptoIntelligenceSchema(unittest.TestCase):
    """_compute_crypto_intelligence returns the expected top-level keys
    even when signal caches are empty (cold-start / stub state)."""

    def setUp(self):
        self.svc = _make_service_module()

    def test_required_keys_present(self):
        """Cold-start payload must include all contract keys."""
        # Patch external HTTP calls to avoid real network in CI
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            payload = self.svc._compute_crypto_intelligence()

        required = [
            "top_setups", "avoid_list", "market_guardrails",
            "futures_context", "theme_leadership", "ranking_meta", "updated_at",
        ]
        for key in required:
            self.assertIn(key, payload, f"Missing key: {key}")

    def test_market_guardrails_subkeys(self):
        """market_guardrails must contain mood, mood_score, and why_market_hint."""
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            payload = self.svc._compute_crypto_intelligence()

        grd = payload["market_guardrails"]
        for subkey in ("mood", "mood_score", "why_market_hint"):
            self.assertIn(subkey, grd, f"Missing guardrails subkey: {subkey}")

    def test_futures_context_subkeys(self):
        """futures_context must carry per_symbol, updated_at, and source."""
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            payload = self.svc._compute_crypto_intelligence()

        fc = payload["futures_context"]
        self.assertIn("per_symbol", fc)
        self.assertIn("source", fc)

    def test_top_setups_schema(self):
        """When setups are present each must have the required card fields."""
        import pandas as pd
        import numpy as np

        # Inject a minimal signal DataFrame for one crypto symbol
        sym = "BTC/USDT"
        df = pd.DataFrame([{
            "close": 65000.0,
            "signal": "LONG",
            "quality_score": 72,
            "quality_grade": "B",
            "confidence": 68.0,
            "context_score": 0.12,
            "drift_detected": False,
            "event_risk_active": False,
            "skip_reason": "",
            "stale": False,
            "trend": "BULLISH",
            "regime": "TRENDING",
            "rsi": 44.0,
            "entry": 65000.0,
            "stop_loss": 63000.0,
            "target": 69000.0,
            "conflict": {"has_conflict": False},
            "timestamp": pd.Timestamp.utcnow(),
        }])
        orig = self.svc._signal_cache.copy()
        orig_market = self.svc._market_data.copy()
        try:
            self.svc._signal_cache[sym] = df
            self.svc._market_data[sym] = df
            with patch("urllib.request.urlopen", side_effect=OSError("offline")):
                payload = self.svc._compute_crypto_intelligence()
        finally:
            self.svc._signal_cache.clear()
            self.svc._signal_cache.update(orig)
            self.svc._market_data.clear()
            self.svc._market_data.update(orig_market)

        if not payload["top_setups"]:
            self.skipTest("No setups generated from stub df (threshold gate)")

        s = payload["top_setups"][0]
        for field in ("symbol", "signal", "quality_score", "confidence",
                      "composite_rank", "confluence_score", "theme",
                      "futures_context", "first_seen"):
            self.assertIn(field, s, f"Missing setup field: {field}")

    def test_scoring_version(self):
        """ranking_meta must include the correct scoring version."""
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            payload = self.svc._compute_crypto_intelligence()
        self.assertEqual(payload["ranking_meta"]["scoring_version"], "crypto_phase6_v1")


# ── 5. Composite ranking uses futures modifier ────────────────────────────────

class TestCompositeScoringWithFutures(unittest.TestCase):
    """A LONG setup should score higher when funding is HIGH_NEGATIVE
    (shorts paying = potential squeeze) vs HIGH_POSITIVE (longs crowded)."""

    def setUp(self):
        self.svc = _make_service_module()
        self._orig = self.svc._crypto_futures_cache.copy()

    def tearDown(self):
        self.svc._crypto_futures_cache.clear()
        self.svc._crypto_futures_cache.update(self._orig)

    def _run_with_label(self, sym, signal_label, fr_label):
        self.svc._crypto_futures_cache[sym] = {
            "funding_label": fr_label,
            "funding_rate_pct": 0.01,
        }
        delta, _ = self.svc._crypto_futures_modifier(sym, signal_label)
        return delta

    def test_long_ranks_better_when_shorts_paying(self):
        delta_favor = self._run_with_label("BTC/USDT", "LONG", "HIGH_NEGATIVE")
        delta_unfavor = self._run_with_label("BTC/USDT", "LONG", "HIGH_POSITIVE")
        self.assertGreater(delta_favor, delta_unfavor)

    def test_short_ranks_better_when_longs_crowded(self):
        delta_favor = self._run_with_label("ETH/USDT", "SHORT", "HIGH_POSITIVE")
        delta_unfavor = self._run_with_label("ETH/USDT", "SHORT", "HIGH_NEGATIVE")
        self.assertGreater(delta_favor, delta_unfavor)


if __name__ == "__main__":
    unittest.main()
