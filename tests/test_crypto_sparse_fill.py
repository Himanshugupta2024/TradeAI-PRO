"""
Crypto Phase 1 C-3 — sparse fill (B1+B2): relaxed quality/confidence floor
when premium count is below CRYPTO_SPARSE_TARGET; tier=exploratory vs premium.
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


def _row(
    signal: str = "LONG",
    *,
    quality_score: int,
    confidence: float,
    close: float = 100.0,
    volume_ratio_20: float = 2.0,
) -> pd.DataFrame:
    return pd.DataFrame([{
        "close": close,
        "signal": signal,
        "quality_score": quality_score,
        "quality_grade": "C",
        "confidence": float(confidence),
        "context_score": 0.0,
        "drift_detected": False,
        "event_risk_active": False,
        "skip_reason": "",
        "stale": False,
        "trend": "BULLISH" if signal == "LONG" else "BEARISH",
        "regime": "TRENDING",
        "rsi": 50.0,
        "entry": close,
        "stop_loss": close * (0.97 if signal == "LONG" else 1.03),
        "target": close * (1.05 if signal == "LONG" else 0.95),
        "conflict": {"has_conflict": False},
        "timestamp": pd.Timestamp.now("UTC"),
        "volume_ratio_20": float(volume_ratio_20),
    }])


@contextmanager
def _inject_many(rows: dict[str, pd.DataFrame]):
    orig_sig = svc._signal_cache.copy()
    orig_mkt = svc._market_data.copy()
    orig_fut = svc._crypto_futures_cache.copy()
    orig_mtf = svc._crypto_htf4_trend.copy()
    try:
        for sym, df in rows.items():
            svc._signal_cache[sym] = df
            svc._market_data[sym] = df
            svc._crypto_htf4_trend[sym] = "NEUTRAL"
        yield
    finally:
        svc._signal_cache.clear()
        svc._signal_cache.update(orig_sig)
        svc._market_data.clear()
        svc._market_data.update(orig_mkt)
        svc._crypto_futures_cache.clear()
        svc._crypto_futures_cache.update(orig_fut)
        svc._crypto_htf4_trend.clear()
        svc._crypto_htf4_trend.update(orig_mtf)


def _run() -> dict:
    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        return svc._compute_crypto_intelligence()


class TestSparseFill(unittest.TestCase):
    def test_exploratory_fills_when_no_premium(self):
        """Below strict floor but above relaxed → tier exploratory fills top."""
        syms = [f"COIN{i}/USDT" for i in range(6)]
        rows = {s: _row("LONG", quality_score=42, confidence=36.0, close=1.0 + i) for i, s in enumerate(syms)}
        ctx = patch.multiple(
            settings,
            CRYPTO_VOL_GATE_ENABLED=False,
            CRYPTO_FUNDING_GATE_ENABLED=False,
            CRYPTO_MTF_GATE_ENABLED=False,
            CRYPTO_SPARSE_FILL_ENABLED=True,
            CRYPTO_SPARSE_TARGET=5,
            CRYPTO_QUALITY_FLOOR=45,
            CRYPTO_CONF_FLOOR=35,
            CRYPTO_QUALITY_FLOOR_RELAXED=40,
            CRYPTO_CONF_FLOOR_RELAXED=28,
        )
        with _inject_many(rows), ctx:
            payload = _run()

        tops = payload["top_setups"]
        self.assertEqual(len(tops), 5, "Should back-fill up to sparse_target")
        meta = payload["ranking_meta"]
        self.assertTrue(meta.get("sparse_fill_applied"))
        self.assertEqual(meta.get("tier_premium_count"), 0)
        self.assertEqual(meta.get("tier_exploratory_count"), 5)
        for t in tops:
            self.assertEqual(t.get("tier"), "exploratory")

    def test_premium_only_when_sparse_disabled(self):
        syms = ["A/USDT", "B/USDT"]
        rows = {s: _row("LONG", quality_score=42, confidence=36.0) for s in syms}
        ctx = patch.multiple(
            settings,
            CRYPTO_VOL_GATE_ENABLED=False,
            CRYPTO_FUNDING_GATE_ENABLED=False,
            CRYPTO_MTF_GATE_ENABLED=False,
            CRYPTO_SPARSE_FILL_ENABLED=False,
            CRYPTO_QUALITY_FLOOR=45,
            CRYPTO_CONF_FLOOR=35,
        )
        with _inject_many(rows), ctx:
            payload = _run()
        self.assertEqual(len(payload["top_setups"]), 0)
        self.assertFalse(payload["ranking_meta"].get("sparse_fill_applied"))

    def test_no_sparse_when_premium_meets_target(self):
        """Enough premium rows → exploratory pool ignored."""
        rows = {f"P{i}/USDT": _row("LONG", quality_score=60, confidence=40.0, close=float(i + 1)) for i in range(6)}
        ctx = patch.multiple(
            settings,
            CRYPTO_VOL_GATE_ENABLED=False,
            CRYPTO_FUNDING_GATE_ENABLED=False,
            CRYPTO_MTF_GATE_ENABLED=False,
            CRYPTO_SPARSE_FILL_ENABLED=True,
            CRYPTO_SPARSE_TARGET=5,
            CRYPTO_QUALITY_FLOOR=45,
            CRYPTO_CONF_FLOOR=35,
            CRYPTO_QUALITY_FLOOR_RELAXED=40,
            CRYPTO_CONF_FLOOR_RELAXED=28,
        )
        with _inject_many(rows), ctx:
            payload = _run()
        meta = payload["ranking_meta"]
        self.assertFalse(meta.get("sparse_fill_applied"))
        self.assertGreaterEqual(meta.get("tier_premium_count", 0), 5)
        self.assertEqual(meta.get("tier_exploratory_count", -1), 0)
        for t in payload["top_setups"]:
            self.assertEqual(t.get("tier"), "premium")

    def test_exploratory_blocked_by_gate_not_promoted(self):
        """Relaxed score but failing hard gate → avoid_list, not sparse fill."""
        sym = "LOWVOL/USDT"
        df = _row("LONG", quality_score=42, confidence=36.0, volume_ratio_20=0.5)
        ctx = patch.multiple(
            settings,
            CRYPTO_VOL_GATE_ENABLED=True,
            CRYPTO_VOL_MIN_RATIO=1.30,
            CRYPTO_FUNDING_GATE_ENABLED=False,
            CRYPTO_MTF_GATE_ENABLED=False,
            CRYPTO_SPARSE_FILL_ENABLED=True,
            CRYPTO_SPARSE_TARGET=5,
        )
        with _inject_many({sym: df}), ctx:
            payload = _run()
        self.assertEqual(len(payload["top_setups"]), 0)
        av = next((a for a in payload["avoid_list"] if a.get("symbol") == sym), None)
        self.assertIsNotNone(av)
        self.assertTrue(any(b.startswith("low_volume") for b in av.get("blockers", [])))


if __name__ == "__main__":
    unittest.main()
