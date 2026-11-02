"""
Edge Score Engine - Main orchestrator for macro intelligence layer.

Combines multiple edge scoring components (macro, COT, sentiment, risk, momentum,
seasonality, carry) into a single EdgeScore (-100 to +100) that modifies technical
signal confidence.

EdgeScore Formula:
EdgeScore = (macro×0.30 + cot×0.25 + sentiment×0.20 + risk×0.10 + momentum×0.15)
            + seasonal_bonus + carry_bonus

Where:
- Base score: -100 to +100 (weighted average of 5 components)
- Seasonal bonus: -20 to +20
- Carry bonus: -10 to +10
- Final EdgeScore: clamped to [-100, +100]
"""

import json
import logging
import os
import tempfile
import threading

_cache_lock = threading.RLock()
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yfinance as yf

from config.settings import (
    EDGE_SCORES_CACHE_FILE,
    EDGE_SCORE_CACHE_TTL,
    EDGE_FALLBACK_ENABLED,
    EDGE_WEIGHT_MACRO,
    EDGE_WEIGHT_COT,
    EDGE_WEIGHT_SENTIMENT,
    EDGE_WEIGHT_RISK,
    EDGE_WEIGHT_MOMENTUM,
    NSE_TICKER_MAP,
)
from agents.macro.macro_scorer import MacroScorer
from agents.macro.cot_scorer import COTScorer
from agents.macro.retail_sentiment import SentimentScorer
from agents.macro.risk_gauge import RiskGauge
from agents.macro.seasonality import SeasonalScorer
from agents.macro.carry_scanner import CarryScanner

logger = logging.getLogger(__name__)


@dataclass
class EdgeScoreBreakdown:
    """Detailed breakdown of EdgeScore components"""
    macro_score: float          # -100 to +100
    cot_score: float            # -100 to +100
    sentiment_score: float      # -100 to +100
    risk_score: float           # -100 to +100
    momentum_score: float       # -100 to +100
    seasonal_bonus: float       # -20 to +20
    carry_bonus: float          # -10 to +10
    edge_score: float           # -100 to +100 (final)
    timestamp: datetime
    cache_hit: bool

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization"""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "EdgeScoreBreakdown":
        """Create from dict (JSON deserialization)"""
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


class EdgeScoreEngine:
    """Main orchestrator for edge scoring"""

    def __init__(
        self,
        cache_ttl_seconds: int = EDGE_SCORE_CACHE_TTL,
        fallback_enabled: bool = EDGE_FALLBACK_ENABLED,
    ) -> None:
        """
        Initialize edge score engine.

        Args:
            cache_ttl_seconds: Cache TTL for EdgeScore (default 1 hour)
            fallback_enabled: Use stale cache on API failure
        """
        self.cache_file = Path(EDGE_SCORES_CACHE_FILE)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.fallback_enabled = fallback_enabled

        # Initialize component scorers
        self.macro_scorer = MacroScorer()
        self.cot_scorer = COTScorer()
        self.sentiment_scorer = SentimentScorer()
        self.risk_gauge = RiskGauge()
        self.seasonal_scorer = SeasonalScorer()
        self.carry_scanner = CarryScanner()

        # Component weights (must sum to 1.0)
        self.weights = {
            "macro": EDGE_WEIGHT_MACRO,
            "cot": EDGE_WEIGHT_COT,
            "sentiment": EDGE_WEIGHT_SENTIMENT,
            "risk": EDGE_WEIGHT_RISK,
            "momentum": EDGE_WEIGHT_MOMENTUM,
        }

        # Validate weights sum to 1.0
        weight_sum = sum(self.weights.values())
        if abs(weight_sum - 1.0) > 0.01:
            logger.warning(
                f"Component weights sum to {weight_sum:.2f}, not 1.0. "
                f"Normalizing weights."
            )
            # Normalize weights
            for key in self.weights:
                self.weights[key] /= weight_sum

    def get_edge_score(
        self,
        symbol: str,
        asset_class: str,  # "forex" | "crypto" | "nse" | "index"
    ) -> EdgeScoreBreakdown:
        """
        Get EdgeScore for a symbol with full breakdown.

        Args:
            symbol: Trading symbol (e.g., "EUR/USD", "BTC/USDT", "RELIANCE")
            asset_class: Asset classification for routing

        Returns:
            EdgeScoreBreakdown with all components
        """
        # Try cache first
        cached = self._load_cache(symbol)
        if cached and self._is_cache_fresh(cached):
            logger.info(f"Using fresh EdgeScore from cache for {symbol}")
            cached.cache_hit = True
            return cached

        # Cache miss or stale — calculate fresh EdgeScore
        try:
            logger.info(f"Calculating fresh EdgeScore for {symbol}")
            breakdown = self._calculate_edge_score(symbol, asset_class)
            breakdown.cache_hit = False

            # Save to cache
            self._save_cache(symbol, breakdown)

            return breakdown

        except Exception as e:
            logger.error(f"Failed to calculate EdgeScore for {symbol}: {e}")

            # Fallback to stale cache
            if cached and self.fallback_enabled:
                logger.warning(f"Using stale cache as fallback for {symbol}")
                cached.cache_hit = True
                return cached

            # Return neutral EdgeScore if all else fails
            return self._neutral_edge_score(symbol)

    def _calculate_edge_score(
        self,
        symbol: str,
        asset_class: str,
    ) -> EdgeScoreBreakdown:
        """
        Calculate EdgeScore from all components.

        Args:
            symbol: Trading symbol
            asset_class: Asset classification

        Returns:
            EdgeScoreBreakdown
        """
        # Fetch all component scores
        macro_score = self.macro_scorer.get_macro_score()
        cot_score = self.cot_scorer.get_cot_score(symbol)
        sentiment_score = self.sentiment_scorer.get_sentiment_score(symbol)
        risk_score = self.risk_gauge.get_risk_score()

        # Momentum score from live market data (asset-class aware)
        momentum_score = self._get_momentum_score(symbol, asset_class)

        # Calculate weighted base score
        base_score = (
            macro_score * self.weights["macro"] +
            cot_score * self.weights["cot"] +
            sentiment_score * self.weights["sentiment"] +
            risk_score * self.weights["risk"] +
            momentum_score * self.weights["momentum"]
        )

        # Get risk regime for carry bonus
        risk_indicators = self.risk_gauge.fetch_risk_indicators()
        risk_regime = risk_indicators.risk_regime

        # Calculate bonuses
        seasonal_bonus = self.seasonal_scorer.get_seasonal_score(symbol)
        carry_bonus = self.carry_scanner.get_carry_score(symbol, risk_regime)

        # Calculate final EdgeScore
        edge_score = base_score + seasonal_bonus + carry_bonus

        # Clamp to [-100, +100]
        edge_score = max(-100.0, min(100.0, edge_score))

        logger.info(
            f"EdgeScore for {symbol}: {edge_score:.1f} "
            f"(base: {base_score:.1f}, seasonal: {seasonal_bonus:.1f}, carry: {carry_bonus:.1f})"
        )

        return EdgeScoreBreakdown(
            macro_score=round(macro_score, 1),
            cot_score=round(cot_score, 1),
            sentiment_score=round(sentiment_score, 1),
            risk_score=round(risk_score, 1),
            momentum_score=round(momentum_score, 1),
            seasonal_bonus=round(seasonal_bonus, 1),
            carry_bonus=round(carry_bonus, 1),
            edge_score=round(edge_score, 1),
            timestamp=datetime.now(timezone.utc),
            cache_hit=False,
        )

    def _get_momentum_score(self, symbol: str, asset_class: str) -> float:
        """
        Compute a robust momentum score using free market data.

        Model (all normalized to [-100, +100]):
        - ROC score: short-horizon percentage change
        - Trend score: EMA(12) vs EMA(26) spread
        - Price score: latest close vs EMA(26)

        The normalization scale adapts to realized volatility so high-volatility
        assets do not saturate scores too aggressively.
        """
        ticker = self._resolve_momentum_ticker(symbol, asset_class)
        if not ticker:
            logger.warning("Momentum ticker resolution failed for %s (%s)", symbol, asset_class)
            return 0.0

        period, interval, roc_lookback, base_scale = self._momentum_profile(asset_class)

        try:
            hist = yf.Ticker(ticker).history(period=period, interval=interval)
            if hist is None or hist.empty:
                logger.warning("Momentum fetch returned empty history for %s (%s)", symbol, ticker)
                return 0.0

            closes = [float(v) for v in hist["Close"].dropna().tolist()]
            min_bars = max(roc_lookback + 1, 30)
            if len(closes) < min_bars:
                logger.warning(
                    "Insufficient bars for momentum %s (%s): %s < %s",
                    symbol,
                    ticker,
                    len(closes),
                    min_bars,
                )
                return 0.0

            anchor = closes[-1 - roc_lookback]
            roc_pct = ((closes[-1] - anchor) / anchor) * 100.0 if anchor else 0.0
            scale = self._adaptive_momentum_scale(closes, roc_lookback, base_scale)
            roc_score = self._normalize_momentum_value(roc_pct, scale)

            ema_fast = self._ema(closes, 12)
            ema_slow = self._ema(closes, 26)
            if ema_fast is None or ema_slow is None or ema_slow == 0:
                return round(roc_score, 1)

            ema_spread_pct = ((ema_fast - ema_slow) / ema_slow) * 100.0
            trend_score = self._normalize_momentum_value(ema_spread_pct, scale * 0.75)

            price_vs_slow_pct = ((closes[-1] - ema_slow) / ema_slow) * 100.0
            price_score = self._normalize_momentum_value(price_vs_slow_pct, scale)

            momentum_score = (roc_score * 0.45) + (trend_score * 0.35) + (price_score * 0.20)
            momentum_score = max(-100.0, min(100.0, momentum_score))
            return round(momentum_score, 1)
        except Exception as exc:
            logger.warning("Momentum scoring failed for %s (%s): %s", symbol, ticker, exc)
            return 0.0

    def _adaptive_momentum_scale(
        self,
        closes: list[float],
        roc_lookback: int,
        base_scale: float,
    ) -> float:
        """Adjust normalization scale using realized return volatility."""
        if len(closes) < 3:
            return base_scale

        window = min(max(roc_lookback * 2, 20), len(closes) - 1)
        start = len(closes) - window
        returns_pct = []
        for i in range(start, len(closes)):
            prev = closes[i - 1]
            if prev:
                returns_pct.append(((closes[i] - prev) / prev) * 100.0)

        if not returns_pct:
            return base_scale

        vol_pct = self._stddev(returns_pct)
        adaptive_scale = max(base_scale, vol_pct * 6.0)
        return min(adaptive_scale, base_scale * 8.0)

    def _momentum_profile(self, asset_class: str) -> tuple[str, str, int, float]:
        """Return period/interval/lookback/normalization scale by asset class."""
        cls = (asset_class or "").lower()
        if cls == "forex":
            return "1mo", "60m", 24, 1.5
        if cls == "crypto":
            return "1mo", "60m", 24, 6.0
        if cls in {"nse", "index"}:
            return "6mo", "1d", 10, 4.0
        return "3mo", "1d", 10, 4.0

    def _resolve_momentum_ticker(self, symbol: str, asset_class: str) -> Optional[str]:
        """Map internal symbols to yFinance tickers for momentum scoring."""
        raw = (symbol or "").strip().upper()
        normalized = raw.replace("-", "/")
        cls = (asset_class or "").lower()

        if cls == "forex":
            if "/" in normalized:
                base, quote = normalized.split("/", 1)
                return f"{base}{quote}=X"
            if len(normalized) == 6 and normalized.isalpha():
                return f"{normalized}=X"
            return None

        if cls == "crypto":
            if "/" in normalized:
                base, quote = normalized.split("/", 1)
            elif "-" in raw:
                base, quote = raw.split("-", 1)
            else:
                return None
            quote = {"USDT": "USD", "USDC": "USD", "BUSD": "USD"}.get(quote, quote)
            return f"{base}-{quote}"

        if cls == "index":
            index_map = {
                "NIFTY": "^NSEI",
                "NIFTY 50": "^NSEI",
                "BANKNIFTY": "^NSEBANK",
                "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
            }
            if normalized in index_map:
                return index_map[normalized]
            if normalized in NSE_TICKER_MAP:
                return NSE_TICKER_MAP[normalized]
            if normalized.startswith("^"):
                return normalized
            return None

        if normalized in NSE_TICKER_MAP:
            return NSE_TICKER_MAP[normalized]
        if normalized.endswith(".NS") or normalized.startswith("^"):
            return normalized
        return f"{normalized}.NS"

    def _normalize_momentum_value(self, value_pct: float, scale_pct: float) -> float:
        """Normalize percent move into bounded score range [-100, +100]."""
        if scale_pct <= 0:
            return 0.0
        score = (value_pct / scale_pct) * 100.0
        return max(-100.0, min(100.0, score))

    def _ema(self, values: list[float], period: int) -> Optional[float]:
        """Simple EMA implementation to avoid extra indicator dependencies."""
        if period <= 0 or len(values) < period:
            return None

        ema = sum(values[:period]) / period
        alpha = 2.0 / (period + 1.0)
        for price in values[period:]:
            ema = (price * alpha) + (ema * (1.0 - alpha))
        return ema

    def _stddev(self, values: list[float]) -> float:
        """Small stddev helper to avoid extra numeric dependencies."""
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        return variance ** 0.5

    def get_top_setups(
        self,
        asset_class: str,
        min_edge_score: float = 50.0,
        limit: int = 10,
    ) -> list[tuple[str, float]]:
        """
        Get top-ranked symbols by EdgeScore.

        Args:
            asset_class: Filter by asset class ("forex" | "crypto" | "nse" | "index")
            min_edge_score: Minimum EdgeScore threshold
            limit: Maximum results to return

        Returns:
            List of (symbol, edge_score) sorted by score descending
        """
        # Get symbol and asset class tuples based on requested asset class
        from config.settings import CRYPTO_SYMBOLS, NSE_SYMBOLS, FOREX_SYMBOLS

        ac_lower = (asset_class or "all").lower()

        if ac_lower == "all":
            # Curated cross-asset liquid shortlist for instant macro comparison
            items = [
                ("RELIANCE", "nse"),
                ("TCS", "nse"),
                ("INFY", "nse"),
                ("HDFCBANK", "nse"),
                ("BTC/USDT", "crypto"),
                ("ETH/USDT", "crypto"),
                ("SOL/USDT", "crypto"),
                ("EUR/USD", "forex"),
                ("USD/INR", "forex"),
            ]
        elif ac_lower == "crypto":
            crypto_top = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT"]
            items = [(s, "crypto") for s in crypto_top]
        elif ac_lower == "nse":
            items = [(s, "nse") for s in NSE_SYMBOLS[:10]]
        elif ac_lower == "forex":
            forex_top = ["EUR/USD", "GBP/USD", "USD/JPY", "USD/INR", "EUR/INR", "AUD/USD"]
            items = [(s, "forex") for s in forex_top]
        elif ac_lower == "index":
            items = [(s, "index") for s in ["NIFTY", "BANKNIFTY", "SPX"]]
        else:
            logger.warning(f"Unknown asset class: {asset_class}")
            return []

        # Calculate EdgeScore in parallel to ensure sub-second response
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = []

        def _calc_item(item):
            sym, a_cls = item
            try:
                breakdown = self.get_edge_score(sym, a_cls)
                if breakdown.edge_score >= min_edge_score:
                    return (sym, breakdown.edge_score)
            except Exception as e:
                logger.error(f"Failed to get EdgeScore for {sym}: {e}")
            return None

        with ThreadPoolExecutor(max_workers=min(len(items), 8)) as executor:
            futs = [executor.submit(_calc_item, it) for it in items]
            for fut in as_completed(futs):
                res = fut.result()
                if res:
                    results.append(res)

        # Sort by EdgeScore descending
        results.sort(key=lambda x: x[1], reverse=True)

        # Limit results
        return results[:limit]

    def _neutral_edge_score(self, symbol: str) -> EdgeScoreBreakdown:
        """
        Return neutral EdgeScore (all zeros).

        Args:
            symbol: Trading symbol

        Returns:
            EdgeScoreBreakdown with all scores = 0
        """
        return EdgeScoreBreakdown(
            macro_score=0.0,
            cot_score=0.0,
            sentiment_score=0.0,
            risk_score=0.0,
            momentum_score=0.0,
            seasonal_bonus=0.0,
            carry_bonus=0.0,
            edge_score=0.0,
            timestamp=datetime.now(timezone.utc),
            cache_hit=False,
        )

    def _load_cache(self, symbol: str) -> Optional[EdgeScoreBreakdown]:
        """Load EdgeScore from cache file (thread-safe)"""
        with _cache_lock:
            if not self.cache_file.exists():
                return None

            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    cache = json.load(f)

                if symbol not in cache:
                    return None

                return EdgeScoreBreakdown.from_dict(cache[symbol])

            except Exception as e:
                logger.error(f"Failed to load cache for {symbol}: {e}")
                return None

    def _save_cache(self, symbol: str, breakdown: EdgeScoreBreakdown) -> None:
        """Save EdgeScore to cache file (thread-safe and atomic)"""
        with _cache_lock:
            try:
                cache = {}
                if self.cache_file.exists():
                    try:
                        with open(self.cache_file, "r", encoding="utf-8") as f:
                            cache = json.load(f)
                    except Exception:
                        cache = {}

                cache[symbol] = breakdown.to_dict()

                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", dir=self.cache_file.parent, delete=False, encoding="utf-8", suffix=".tmp") as tf:
                    json.dump(cache, tf, indent=2)
                    tmp_name = tf.name
                os.replace(tmp_name, self.cache_file)

                logger.info(f"Saved EdgeScore to cache for {symbol}")

            except Exception as e:
                logger.error(f"Failed to save cache for {symbol}: {e}")

    def _is_cache_fresh(self, breakdown: EdgeScoreBreakdown) -> bool:
        """Check if cached EdgeScore is still fresh"""
        age_seconds = (datetime.now(timezone.utc) - breakdown.timestamp).total_seconds()
        return age_seconds < self.cache_ttl_seconds
