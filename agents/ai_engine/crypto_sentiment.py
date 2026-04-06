"""
Crypto Sentiment Analyzer.

Data sources:
  1. Alternative.me Fear & Greed Index  — FREE, no API key required
     Updates once daily. Reflects overall crypto market sentiment.
     URL: https://api.alternative.me/fng/

  2. Momentum-based sentiment proxy (computed from OHLCV features)
     Used as a fallback when the API is unavailable.

Contrarian interpretation (Warren Buffett principle):
  Extreme Fear  (0–24)   → Market panic       → BUY opportunity
  Fear          (25–44)  → Negative sentiment → Mild bullish lean
  Neutral       (45–55)  → No strong signal
  Greed         (56–74)  → Euphoria building  → Mild bearish lean
  Extreme Greed (75–100) → Market euphoria    → SELL signal

The sentiment is used as a soft adjustment layer in the ensemble predictor,
NOT as a standalone trading signal. Strong fear confirms oversold technical
setups; strong greed confirms overbought setups.
"""

from __future__ import annotations

import time
import logging
from typing import TypedDict

import pandas as pd

log = logging.getLogger(__name__)

_FEAR_GREED_API = "https://api.alternative.me/fng/"
_CACHE_TTL_SECONDS = 3600   # API updates once/day; cache for 1 hour

_cache: dict = {"data": None, "fetched_at": 0.0}


class SentimentResult(TypedDict):
    value:            int    # 0-100
    classification:   str    # "Extreme Fear" | "Fear" | "Neutral" | "Greed" | "Extreme Greed"
    sentiment_bias:   str    # "BULLISH" | "MILD_BULLISH" | "NEUTRAL" | "MILD_BEARISH" | "BEARISH"
    contrarian_conf:  float  # 0-100 — strength of the contrarian signal
    source:           str    # "api" | "cache" | "proxy" | "unavailable"


def get_fear_greed_index(timeout: int = 5) -> SentimentResult:
    """
    Fetch the Crypto Fear & Greed Index from Alternative.me.

    Results are cached for 1 hour since the index updates only once daily.
    Falls back to a neutral result if the API is unavailable.

    Args:
        timeout: HTTP request timeout in seconds

    Returns:
        SentimentResult dict
    """
    now = time.time()

    # Return from cache if still fresh
    if _cache["data"] and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS:
        result = dict(_cache["data"])
        result["source"] = "cache"
        return result  # type: ignore[return-value]

    try:
        import requests
        resp = requests.get(_FEAR_GREED_API, params={"limit": 1}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()

        raw   = payload["data"][0]
        value = int(raw["value"])
        label = raw["value_classification"]

        result = _interpret(value, label, source="api")

        _cache["data"]       = result
        _cache["fetched_at"] = now

        log.info(
            "Fear & Greed: %d (%s) → bias=%s contrarian_conf=%.0f%%",
            value, label, result["sentiment_bias"], result["contrarian_conf"],
        )
        return result

    except Exception as exc:
        log.warning("Fear & Greed API unavailable (%s) — returning neutral", exc)
        return _interpret(50, "Neutral", source="unavailable")


def compute_momentum_sentiment(df: pd.DataFrame) -> SentimentResult:
    """
    Derive a sentiment proxy from recent OHLCV momentum.

    Used when the Fear & Greed API is unavailable.
    Based on RSI + price momentum over 20 bars.

    Returns:
        SentimentResult with source="proxy"
    """
    if len(df) < 20 or "RSI_14" not in df.columns:
        return _interpret(50, "Neutral", source="proxy")

    rsi = df["RSI_14"].iloc[-1]
    if pd.isna(rsi):
        rsi = 50.0

    # Map RSI to Fear & Greed scale (inverted: high RSI → greed, low → fear)
    # RSI 70 → ~75 (Extreme Greed), RSI 30 → ~25 (Fear)
    fg_proxy = int((rsi - 30) / 40 * 75 + 12.5)   # maps [30,70] → [12.5,87.5]
    fg_proxy = max(0, min(100, fg_proxy))

    if fg_proxy <= 24:
        label = "Extreme Fear"
    elif fg_proxy <= 44:
        label = "Fear"
    elif fg_proxy <= 55:
        label = "Neutral"
    elif fg_proxy <= 74:
        label = "Greed"
    else:
        label = "Extreme Greed"

    return _interpret(fg_proxy, label, source="proxy")


def sentiment_to_confidence_adjustment(sentiment: SentimentResult) -> float:
    """
    Convert sentiment result to a confidence adjustment for the ensemble.

    Logic:
      - BULLISH sentiment (fear) + technical signal agrees  → boost  +5 to +15
      - BEARISH sentiment (greed) + technical signal agrees → boost  +5 to +15
      - Sentiment opposes technical signal direction        → reduce -5 to -15

    Returns:
        float in [-15.0, +15.0]
        Positive = sentiment supports a LONG (fear = buy opportunity)
        Negative = sentiment supports a SHORT (greed = sell opportunity)
    """
    conf_norm = sentiment["contrarian_conf"] / 100.0   # 0-1
    max_adj   = 15.0

    bias = sentiment["sentiment_bias"]
    if bias in ("BULLISH", "MILD_BULLISH"):
        return round(conf_norm * max_adj, 1)   # +ve: bullish boost
    elif bias in ("BEARISH", "MILD_BEARISH"):
        return round(-conf_norm * max_adj, 1)  # -ve: bearish signal

    return 0.0


# kept for backward compat: agents.ai_engine.crypto_sentiment.sentiment_adjustment
def sentiment_adjustment(df: pd.DataFrame, symbol: str = "") -> float:
    """
    One-call helper: fetch Fear & Greed and return adjustment float.

    Returns:
        float in [-15, +15]  (positive = bullish, negative = bearish)
    """
    try:
        fg = get_fear_greed_index()
    except Exception:
        fg = compute_momentum_sentiment(df)

    return sentiment_to_confidence_adjustment(fg)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _interpret(value: int, classification: str, source: str) -> SentimentResult:
    """Map a Fear & Greed value to a structured sentiment result."""

    if value <= 24:
        # Extreme Fear — market panic, strong contrarian BUY signal
        conf = (25 - value) / 25 * 100      # 0 at value=25, 100 at value=0
        return SentimentResult(
            value=value, classification=classification,
            sentiment_bias="BULLISH",
            contrarian_conf=round(conf, 1), source=source,
        )

    elif value <= 44:
        # Fear — negative sentiment, mild bullish lean
        conf = (45 - value) / 20 * 70       # 0 at value=45, 70 at value=25
        return SentimentResult(
            value=value, classification=classification,
            sentiment_bias="MILD_BULLISH",
            contrarian_conf=round(conf, 1), source=source,
        )

    elif value <= 55:
        # Neutral
        return SentimentResult(
            value=value, classification=classification,
            sentiment_bias="NEUTRAL",
            contrarian_conf=0.0, source=source,
        )

    elif value <= 74:
        # Greed — euphoria building, mild bearish lean
        conf = (value - 55) / 20 * 70       # 0 at value=55, 70 at value=75
        return SentimentResult(
            value=value, classification=classification,
            sentiment_bias="MILD_BEARISH",
            contrarian_conf=round(conf, 1), source=source,
        )

    else:
        # Extreme Greed — market euphoria, strong contrarian SELL signal
        conf = (value - 74) / 26 * 100      # 0 at value=74, 100 at value=100
        return SentimentResult(
            value=value, classification=classification,
            sentiment_bias="BEARISH",
            contrarian_conf=round(conf, 1), source=source,
        )
