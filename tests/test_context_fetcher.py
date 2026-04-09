"""Unit tests for context fetcher module."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.data_ingestion.context_fetcher import ContextFetcher, ContextSnapshotData
from config import settings


def _fixed_now() -> datetime:
    return datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)


def test_binance_ticker_parse():
    """Should parse Binance ticker fields into normalized numeric payload."""
    fetcher = ContextFetcher(now_provider=_fixed_now)
    fetcher._get_json = lambda url, timeout=10: {
        "priceChangePercent": "2.5",
        "volume": "12345.6",
        "highPrice": "70100",
        "lowPrice": "68200",
        "count": 456789,
    }

    parsed = fetcher._fetch_binance_ticker("BTCUSDT")

    assert parsed["price_change_pct"] == 2.5
    assert parsed["volume"] == 12345.6
    assert parsed["high"] == 70100.0
    assert parsed["low"] == 68200.0
    assert parsed["trade_count"] == 456789.0


def test_cryptopanic_sentiment_calc():
    """Keyword sentiment should clamp between -1 and +1."""
    fetcher = ContextFetcher(now_provider=_fixed_now)

    bullish = ["Bitcoin rally as bulls surge higher", "ETH breakout to new high"]
    bearish = ["Market crash sparks fear and dump", "Altcoins fall as bears push down"]
    neutral = ["Crypto market trades sideways"]

    b_score = fetcher._keyword_sentiment(bullish)
    s_score = fetcher._keyword_sentiment(bearish)
    n_score = fetcher._keyword_sentiment(neutral)

    assert -1.0 <= b_score <= 1.0
    assert -1.0 <= s_score <= 1.0
    assert -1.0 <= n_score <= 1.0
    assert b_score > 0.0
    assert s_score < 0.0


def test_context_score_formula():
    """Context score should follow weighted formula exactly."""
    fetcher = ContextFetcher(now_provider=_fixed_now)
    snapshot = ContextSnapshotData(
        symbol="ETHUSDT",
        timestamp_utc=_fixed_now(),
        price_change_24hr_pct=3.0,
        volume_change_24hr_pct=20.0,
        news_sentiment_24hr=0.5,
        news_sentiment_7d=0.0,
        news_volume_spike=False,
        weekly_trend_bias="BULLISH",
        oi_change_7d_pct=0.0,
        btc_dominance_7d_trend="RISING",
        days_to_next_event=5,
        event_risk_active=False,
        context_score=0.0,
    )

    score = fetcher._compute_context_score(snapshot)
    expected = 0.0
    expected += 0.5 * settings.CONTEXT_NEWS_WEIGHT_24HR + 0.0 * settings.CONTEXT_NEWS_WEIGHT_7D
    expected += 0.20
    expected += min(20.0 / 100.0, 0.15)
    expected -= 0.0
    expected -= 0.10
    expected = max(-1.0, min(1.0, expected))

    assert round(score, 6) == round(expected, 6)


def test_context_fetcher_api_failure():
    """On fetch failure, should return cached snapshot and never crash."""
    fetcher = ContextFetcher(now_provider=_fixed_now)
    fetcher._global_cache = {
        "symbols": {
            "BTCUSDT": {
                "cached_at": _fixed_now().isoformat(),
                "snapshot": {
                    "symbol": "BTCUSDT",
                    "timestamp_utc": _fixed_now().isoformat(),
                    "price_change_24hr_pct": 1.0,
                    "volume_change_24hr_pct": 0.0,
                    "news_sentiment_24hr": 0.1,
                    "news_sentiment_7d": 0.0,
                    "news_volume_spike": False,
                    "weekly_trend_bias": "NEUTRAL",
                    "oi_change_7d_pct": 0.0,
                    "btc_dominance_7d_trend": "FLAT",
                    "days_to_next_event": 99,
                    "event_risk_active": False,
                    "context_score": 0.03,
                },
            }
        }
    }

    fetcher._build_snapshot = lambda symbol, now: (_ for _ in ()).throw(RuntimeError("boom"))

    result = fetcher.get_snapshot_sync("BTCUSDT")
    assert result.symbol == "BTCUSDT"
    assert result.context_score == 0.03


def test_event_risk_flag():
    """Event 1 day away should activate event risk."""
    fetcher = ContextFetcher(now_provider=_fixed_now)
    events = [
        {
            "event_name": "RBI Policy",
            "date": (_fixed_now() + timedelta(days=1)).isoformat(),
            "impact": "HIGH",
            "days_away": 1,
            "relevant_markets": "NSE",
        }
    ]

    days, active = fetcher._derive_event_risk(events, _fixed_now())
    assert days == 1
    assert active is True
