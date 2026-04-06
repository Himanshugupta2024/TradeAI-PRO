"""Tests for dashboard market mood and AI summary helpers."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server


def test_market_mood_shape_and_bounds():
    """Mood payload should include required keys with score in [0, 100]."""
    server._movers_cache["gainers"] = [
        {"symbol": "RELIANCE", "pct_change": 2.1},
        {"symbol": "TCS", "pct_change": 1.2},
    ]
    server._movers_cache["losers"] = [
        {"symbol": "SBIN", "pct_change": -1.4},
    ]

    # Keep signal/regime caches empty to validate graceful fallback behavior.
    server._signal_cache.clear()
    server._regime_cache.clear()

    payload = server._compute_market_mood()

    assert "mood" in payload
    assert "score" in payload
    assert "breadth" in payload
    assert "signals" in payload
    assert 0.0 <= payload["score"] <= 100.0
    assert payload["mood"] in {"BULLISH", "NEUTRAL", "CAUTIOUS", "BEARISH"}


def test_ai_market_summary_contains_three_lines():
    """Summary helper should output 3 readable lines and a combined summary."""
    server._movers_cache["gainers"] = [{"symbol": "INFY", "pct_change": 1.8}]
    server._movers_cache["losers"] = [{"symbol": "ITC", "pct_change": -1.1}]

    mood_payload = {
        "mood": "NEUTRAL",
        "score": 54.3,
        "breadth": {"gainers": 4, "losers": 3, "breadth_pct": 14.3},
        "signals": {"long": 10, "short": 8, "hold": 12, "bias_pct": 6.7},
    }

    summary = server._build_ai_market_summary(mood_payload)

    assert "lines" in summary
    assert isinstance(summary["lines"], list)
    assert len(summary["lines"]) == 3
    assert all(isinstance(line, str) and line for line in summary["lines"])
    assert "summary" in summary
    assert isinstance(summary["summary"], str) and len(summary["summary"]) > 20
