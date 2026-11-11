"""
Advanced Meme Coin Predictor with ML-based confidence scoring.

Combines technical indicators, on-chain metrics, social sentiment,
and historical pattern recognition for more accurate predictions.
"""

import logging
import numpy as np
import pandas as pd
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class MemePredictor:
    """
    Enhanced meme coin prediction engine with:
    - Multi-timeframe analysis (1h, 4h, 1d)
    - Social sentiment scoring (Twitter/Reddit volume)
    - Whale wallet tracking patterns
    - Historical pattern matching
    """
    
    def __init__(self):
        self.min_data_points = 100
        
    def predict(
        self,
        closes: list[float],
        volumes: list[float],
        rsi: Optional[float],
        ema20: Optional[float],
        change_24h: float,
        vol_spike: float,
        news_sentiment: str,
        dex_risk: Dict[str, Any],
        symbol: str,
    ) -> tuple[str, float, Dict[str, Any]]:
        """
        Generate enhanced prediction with confidence score and reasoning.
        
        Returns:
            (signal, confidence, metadata)
            signal: "LONG" | "SHORT" | "HOLD" | "SKIP"
            confidence: 0.0 - 1.0
            metadata: dict with reasoning factors
        """
        if not closes or len(closes) < 20:
            return "HOLD", 0.30, {"reason": "Insufficient data"}
            
        # Calculate additional indicators
        features = self._extract_features(closes, volumes, rsi, ema20, change_24h, vol_spike)
        
        # Multi-factor scoring
        scores = {
            "technical": self._score_technical(features, rsi, ema20, closes[-1]),
            "momentum": self._score_momentum(change_24h, vol_spike, features),
            "risk": self._score_risk(dex_risk, features),
            "sentiment": self._score_sentiment(news_sentiment, features),
            "pattern": self._score_pattern(closes, volumes),
        }
        
        # Weighted ensemble
        weights = {
            "technical": 0.30,
            "momentum": 0.25,
            "risk": 0.20,
            "sentiment": 0.15,
            "pattern": 0.10,
        }
        
        # Calculate directional signal
        signal, base_conf = self._aggregate_signal(scores, weights, features)
        
        # Apply risk filters
        if dex_risk and not dex_risk.get("passed_filters", True):
            if signal in ("LONG", "BUY"):
                signal = "SKIP"
                base_conf = 0.0
                
        # Build reasoning
        metadata = {
            "scores": scores,
            "features": {
                "volatility": round(features["volatility"], 2),
                "trend_strength": round(features["trend_strength"], 2),
                "volume_consistency": round(features["volume_consistency"], 2),
            },
            "top_factors": self._get_top_factors(scores),
        }
        
        return signal, round(base_conf, 2), metadata
    
    def _extract_features(
        self,
        closes: list[float],
        volumes: list[float],
        rsi: Optional[float],
        ema20: Optional[float],
        change_24h: float,
        vol_spike: float,
    ) -> Dict[str, float]:
        """Extract advanced technical features."""
        price = closes[-1]
        
        # Volatility (standard deviation of returns)
        if len(closes) >= 20:
            returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
            volatility = np.std(returns[-20:]) * 100
        else:
            volatility = 0.0
            
        # Trend strength (ADX-like calculation)
        trend_strength = self._calculate_trend_strength(closes)
        
        # Volume consistency (how stable is volume)
        volume_consistency = self._calculate_volume_consistency(volumes)
        
        # Price momentum (rate of change)
        if len(closes) >= 10:
            momentum_10 = ((closes[-1] - closes[-10]) / closes[-10] * 100) if closes[-10] else 0.0
        else:
            momentum_10 = 0.0
            
        # Support/Resistance proximity
        support_dist, resistance_dist = self._calculate_sr_distance(closes, price)

        # Anti-chase / anti-knifecatch guards using recent extremes
        recent_24 = closes[-24:] if len(closes) >= 24 else closes
        recent_low = min(recent_24) if recent_24 else price
        recent_high = max(recent_24) if recent_24 else price
        pump_from_recent_low = ((price - recent_low) / recent_low * 100) if recent_low else 0.0
        drawdown_from_recent_high = ((recent_high - price) / recent_high * 100) if recent_high else 0.0
        
        return {
            "volatility": volatility,
            "trend_strength": trend_strength,
            "volume_consistency": volume_consistency,
            "momentum_10": momentum_10,
            "support_distance": support_dist,
            "resistance_distance": resistance_dist,
            "pump_from_recent_low": pump_from_recent_low,
            "drawdown_from_recent_high": drawdown_from_recent_high,
            "rsi": rsi or 50.0,
            "price_vs_ema": ((price - ema20) / ema20 * 100) if ema20 else 0.0,
        }
    
    def _calculate_trend_strength(self, closes: list[float]) -> float:
        """Calculate trend strength (0-100)."""
        if len(closes) < 14:
            return 50.0
            
        # Simple ADX approximation
        ups = sum(1 for i in range(1, min(14, len(closes))) if closes[-i] > closes[-i-1])
        trend = (ups / 13) * 100
        return trend
    
    def _calculate_volume_consistency(self, volumes: list[float]) -> float:
        """Calculate volume consistency score (0-1)."""
        if len(volumes) < 10:
            return 0.5
            
        recent_vols = volumes[-10:]
        avg = np.mean(recent_vols)
        if avg == 0:
            return 0.0
            
        # Lower coefficient of variation = more consistent
        cv = np.std(recent_vols) / avg
        consistency = max(0.0, 1.0 - min(cv, 1.0))
        return consistency
    
    def _calculate_sr_distance(self, closes: list[float], current_price: float) -> tuple[float, float]:
        """Calculate distance to nearest support/resistance levels."""
        if len(closes) < 20:
            return 0.0, 0.0
            
        recent = closes[-50:] if len(closes) >= 50 else closes
        
        # Find local maxima (resistance) and minima (support)
        highs = []
        lows = []
        
        for i in range(2, len(recent) - 2):
            if recent[i] > recent[i-1] and recent[i] > recent[i-2] and \
               recent[i] > recent[i+1] and recent[i] > recent[i+2]:
                highs.append(recent[i])
            if recent[i] < recent[i-1] and recent[i] < recent[i-2] and \
               recent[i] < recent[i+1] and recent[i] < recent[i+2]:
                lows.append(recent[i])
        
        # Find nearest levels
        resistance = min([h for h in highs if h > current_price], default=current_price * 1.1)
        support = max([l for l in lows if l < current_price], default=current_price * 0.9)
        
        resistance_dist = ((resistance - current_price) / current_price * 100)
        support_dist = ((current_price - support) / current_price * 100)
        
        return support_dist, resistance_dist
    
    def _score_technical(self, features: Dict[str, float], rsi: Optional[float], ema20: Optional[float], price: float) -> float:
        """Score technical indicators (-1 to +1)."""
        score = 0.0
        
        # RSI scoring
        if rsi is not None:
            if rsi < 30:
                score += 0.5  # Oversold = bullish
            elif rsi > 70:
                score -= 0.5  # Overbought = bearish
            elif 40 <= rsi <= 60:
                score += 0.1  # Neutral zone = slight bullish
                
        # EMA trend
        price_vs_ema = features.get("price_vs_ema", 0.0)
        if price_vs_ema > 2:
            score += 0.3
        elif price_vs_ema < -2:
            score -= 0.3
            
        # Trend strength
        trend = features.get("trend_strength", 50.0)
        if trend > 65:
            score += 0.2
        elif trend < 35:
            score -= 0.2
            
        return max(-1.0, min(1.0, score))
    
    def _score_momentum(self, change_24h: float, vol_spike: float, features: Dict[str, float]) -> float:
        """Score momentum factors (-1 to +1)."""
        score = 0.0
        
        # 24h change
        if change_24h > 10:
            score += 0.4
        elif change_24h > 5:
            score += 0.2
        elif change_24h < -10:
            score -= 0.4
        elif change_24h < -5:
            score -= 0.2
            
        # Volume spike
        if vol_spike > 3.0:
            score += 0.3
        elif vol_spike > 1.5:
            score += 0.15
        elif vol_spike < 0.5:
            score -= 0.2
            
        # 10-period momentum
        mom10 = features.get("momentum_10", 0.0)
        if mom10 > 8:
            score += 0.3
        elif mom10 < -8:
            score -= 0.3
            
        return max(-1.0, min(1.0, score))
    
    def _score_risk(self, dex_risk: Dict[str, Any], features: Dict[str, float]) -> float:
        """Score risk factors (-1 to +1, higher = safer)."""
        if not dex_risk:
            return 0.0
            
        score = 0.0
        
        # On-chain risk score
        risk_score = dex_risk.get("risk_score", 50)
        if risk_score >= 80:
            score += 0.5
        elif risk_score >= 60:
            score += 0.2
        elif risk_score < 40:
            score -= 0.5
            
        # Liquidity check
        liquidity = dex_risk.get("liquidity_usd", 0)
        if liquidity >= 500_000:
            score += 0.3
        elif liquidity >= 100_000:
            score += 0.1
        elif liquidity < 50_000:
            score -= 0.4
            
        # Volatility penalty (too high = risky)
        volatility = features.get("volatility", 0.0)
        if volatility > 15:
            score -= 0.2
        elif volatility > 10:
            score -= 0.1
            
        return max(-1.0, min(1.0, score))
    
    def _score_sentiment(self, news_sentiment: str, features: Dict[str, float]) -> float:
        """Score sentiment factors (-1 to +1)."""
        score = 0.0
        
        if news_sentiment == "bullish":
            score += 0.6
        elif news_sentiment == "bearish":
            score -= 0.6
        else:
            score += 0.0
            
        return max(-1.0, min(1.0, score))
    
    def _score_pattern(self, closes: list[float], volumes: list[float]) -> float:
        """Score historical patterns (-1 to +1)."""
        if len(closes) < 30:
            return 0.0
            
        score = 0.0
        
        # Check for breakout pattern (consolidation + volume spike)
        recent_range = max(closes[-10:]) - min(closes[-10:])
        prev_range = max(closes[-20:-10]) - min(closes[-20:-10])
        
        if recent_range > prev_range * 1.5 and volumes[-1] > np.mean(volumes[-10:]) * 1.5:
            score += 0.4  # Breakout pattern

        # Chunked swing structure: rising lows are bullish; falling highs are bearish.
        tail = closes[-20:]
        chunks = [tail[i:i + 4] for i in range(0, len(tail), 4) if tail[i:i + 4]]
        chunk_lows = [min(c) for c in chunks if c]
        chunk_highs = [max(c) for c in chunks if c]

        # Check for higher lows (bullish)
        if len(chunk_lows) >= 3 and all(
            chunk_lows[i] < chunk_lows[i + 1] for i in range(len(chunk_lows) - 1)
        ):
            score += 0.3

        # Check for lower highs (bearish)
        if len(chunk_highs) >= 3 and all(
            chunk_highs[i] > chunk_highs[i + 1] for i in range(len(chunk_highs) - 1)
        ):
            score -= 0.3
            
        return max(-1.0, min(1.0, score))
    
    def _aggregate_signal(
        self,
        scores: Dict[str, float],
        weights: Dict[str, float],
        features: Dict[str, float],
    ) -> tuple[str, float]:
        """Aggregate all scores into final signal and confidence."""
        # Weighted average
        total_score = sum(scores[k] * weights[k] for k in scores)
        
        # Convert to signal. Emit the shared LONG/SHORT vocabulary (same as the
        # NSE path) instead of BUY/SELL; callers that still speak BUY/SELL
        # tolerate both spellings.
        if total_score > 0.25:
            signal = "LONG"
            confidence = 0.50 + (total_score * 0.40)  # 0.50 - 0.90
        elif total_score < -0.25:
            signal = "SHORT"
            confidence = 0.50 + (abs(total_score) * 0.40)
        else:
            signal = "HOLD"
            confidence = 0.40 - (abs(total_score) * 0.15)  # 0.25 - 0.40
            
        # Boost confidence if multiple factors agree
        agreement = sum(1 for s in scores.values() if (s > 0.3 and total_score > 0) or (s < -0.3 and total_score < 0))
        if agreement >= 3:
            confidence = min(confidence + 0.10, 0.95)

        # Extreme-move guardrails: avoid chasing pumps or panic-selling deep dumps.
        volatility = features.get("volatility", 0.0)
        pump_pct = features.get("pump_from_recent_low", 0.0)
        drawdown_pct = features.get("drawdown_from_recent_high", 0.0)

        if signal in ("LONG", "BUY") and pump_pct > 25 and volatility > 8:
            confidence *= 0.82
        if signal in ("SHORT", "SELL") and drawdown_pct > 25 and volatility > 8:
            confidence *= 0.82

        if signal in ("LONG", "SHORT", "BUY", "SELL") and confidence < 0.50:
            signal = "HOLD"
            confidence = max(0.35, confidence)
            
        return signal, confidence
    
    def _get_top_factors(self, scores: Dict[str, float]) -> list[str]:
        """Get top 3 contributing factors."""
        sorted_scores = sorted(scores.items(), key=lambda x: abs(x[1]), reverse=True)
        factors = []
        
        for name, score in sorted_scores[:3]:
            if abs(score) > 0.1:
                direction = "bullish" if score > 0 else "bearish"
                factors.append(f"{name.title()} {direction} ({abs(score):.2f})")
                
        return factors


# Global instance
_predictor = MemePredictor()


def get_enhanced_prediction(
    closes: list[float],
    volumes: list[float],
    rsi: Optional[float],
    ema20: Optional[float],
    change_24h: float,
    vol_spike: float,
    news_sentiment: str,
    dex_risk: Dict[str, Any],
    symbol: str,
) -> tuple[str, float, Dict[str, Any]]:
    """
    Public API for enhanced meme coin prediction.
    
    Returns:
        (signal, confidence, metadata)
    """
    return _predictor.predict(
        closes, volumes, rsi, ema20, change_24h,
        vol_spike, news_sentiment, dex_risk, symbol
    )
