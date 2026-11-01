"""
Unit tests for News Sentiment Advisory Gate in signal_generator.
"""
import pytest
import pandas as pd
import numpy as np

from agents.signal_engine.signal_generator import generate_signals
from config import settings


def _create_sample_ohlcv_df(direction="BULLISH", n_bars=60):
    dates = pd.date_range("2026-11-01 09:15:00", periods=n_bars, freq="5min")
    if direction == "BULLISH":
        closes = [100.0 + i * 1.5 for i in range(n_bars)]
    else:
        closes = [200.0 - i * 1.5 for i in range(n_bars)]

    df = pd.DataFrame({
        "timestamp": dates,
        "open": [c - 0.5 for c in closes],
        "high": [c + 1.5 for c in closes],
        "low": [c - 1.0 for c in closes],
        "close": closes,
        "volume": [50000.0] * n_bars,
        "RSI_14": [62.0 if direction == "BULLISH" else 35.0] * n_bars,
        "MACD": [1.5 if direction == "BULLISH" else -1.5] * n_bars,
        "MACD_signal": [1.0 if direction == "BULLISH" else -1.0] * n_bars,
        "MACD_hist": [0.5 if direction == "BULLISH" else -0.5] * n_bars,
        "EMA_20": [c - 1.0 if direction == "BULLISH" else c + 1.0 for c in closes],
        "EMA_50": [c - 2.0 if direction == "BULLISH" else c + 2.0 for c in closes],
        "EMA_200": [c - 5.0 if direction == "BULLISH" else c + 5.0 for c in closes],
        "ATR_14": [2.5] * n_bars,
        "htf_trend": ["BULLISH" if direction == "BULLISH" else "BEARISH"] * n_bars,
    })
    return df


def test_sentiment_gate_blocks_long_on_severe_bearish_news(monkeypatch):
    monkeypatch.setattr(settings, "SENTIMENT_GATE_ENABLED", True)
    df = _create_sample_ohlcv_df("BULLISH")

    # 1. Without negative news, bullish setup should qualify for LONG
    res_clean = generate_signals(df.copy(), symbol="INFY")
    assert res_clean.iloc[-1]["signal"] in ("LONG", "HOLD")

    # 2. With severe bearish news (tone -6.5, fraud/probe warning), LONG must be BLOCKED
    bearish_sentiment = {
        "symbol": "INFY",
        "sentiment": "BEARISH",
        "tone_score": -6.5,
        "confidence": 0.9,
    }
    res_blocked = generate_signals(df.copy(), symbol="INFY", news_sentiment=bearish_sentiment)
    last_row = res_blocked.iloc[-1]

    assert last_row["signal"] != "LONG"
    assert last_row["sentiment_tone"] == -6.5
    assert last_row["sentiment_label"] == "BEARISH"


def test_sentiment_gate_blocks_short_on_severe_bullish_news(monkeypatch):
    monkeypatch.setattr(settings, "SENTIMENT_GATE_ENABLED", True)
    df = _create_sample_ohlcv_df("BEARISH")

    # Severe bullish news (tone +7.0, massive buyout offer), SHORT must be BLOCKED
    bullish_sentiment = {
        "symbol": "TCS",
        "sentiment": "BULLISH",
        "tone_score": 7.0,
        "confidence": 0.92,
    }
    res = generate_signals(df.copy(), symbol="TCS", news_sentiment=bullish_sentiment)
    last_row = res.iloc[-1]

    assert last_row["signal"] != "SHORT"
    assert last_row["sentiment_tone"] == 7.0
    assert last_row["sentiment_label"] == "BULLISH"


def test_sentiment_gate_allows_trades_on_neutral_news(monkeypatch):
    monkeypatch.setattr(settings, "SENTIMENT_GATE_ENABLED", True)
    df = _create_sample_ohlcv_df("BULLISH")

    neutral_sentiment = {
        "symbol": "RELIANCE",
        "sentiment": "NEUTRAL",
        "tone_score": 0.5,
        "confidence": 0.5,
    }
    res = generate_signals(df.copy(), symbol="RELIANCE", news_sentiment=neutral_sentiment)
    last_row = res.iloc[-1]

    assert last_row["sentiment_tone"] == 0.5
    assert last_row["sentiment_label"] == "NEUTRAL"
    assert bool(last_row["sentiment_gate_ok"]) is True
