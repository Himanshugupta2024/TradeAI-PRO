"""
Social Sentiment Tracker for Meme Coins.

Tracks Twitter/X mentions, Reddit activity, and Telegram buzz
to gauge community hype and viral potential.

All sources are FREE and require no API keys.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger(__name__)


class SocialSentimentTracker:
    """
    Track social sentiment from free public sources:
    - Reddit RSS feeds (r/CryptoCurrency, r/SatoshiStreetBets, r/CryptoMoonShots)
    - Twitter/X public search (via nitter instances)
    - Google Trends (relative search volume)
    """
    
    def __init__(self):
        self.cache: Dict[str, Any] = {}
        self.cache_ttl = 300  # 5 minutes
        
    async def get_sentiment(self, symbol: str) -> Dict[str, Any]:
        """
        Get aggregated social sentiment for a meme coin.
        
        Returns:
            {
                "reddit_mentions_24h": int,
                "reddit_sentiment": float (-1 to +1),
                "twitter_buzz": str ("low" | "medium" | "high"),
                "trending_score": float (0-100),
                "viral_potential": str ("low" | "medium" | "high"),
            }
        """
        cache_key = f"social_{symbol}"
        now = datetime.now().timestamp()
        
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if now - cached["timestamp"] < self.cache_ttl:
                return cached["data"]
        
        # Fetch from multiple sources
        reddit_data = await self._fetch_reddit_sentiment(symbol)
        twitter_buzz = await self._estimate_twitter_buzz(symbol)
        trending = await self._check_trending_status(symbol)
        
        # Aggregate
        result = {
            "reddit_mentions_24h": reddit_data["mentions"],
            "reddit_sentiment": reddit_data["sentiment"],
            "twitter_buzz": twitter_buzz,
            "trending_score": trending,
            "viral_potential": self._calculate_viral_potential(
                reddit_data["mentions"],
                twitter_buzz,
                trending
            ),
        }
        
        self.cache[cache_key] = {
            "timestamp": now,
            "data": result,
        }
        
        return result
    
    async def _fetch_reddit_sentiment(self, symbol: str) -> Dict[str, Any]:
        """Fetch Reddit mentions and sentiment from RSS feeds."""
        subreddits = [
            "CryptoCurrency",
            "SatoshiStreetBets",
            "CryptoMoonShots",
            "memecoin",
            "dogecoin" if symbol.upper() == "DOGE" else None,
            "shib" if symbol.upper() == "SHIB" else None,
        ]
        subreddits = [s for s in subreddits if s]
        
        mentions = 0
        positive = 0
        negative = 0
        
        bullish_words = {"moon", "bullish", "buy", "pump", "gem", "rocket", "🚀", "💎"}
        bearish_words = {"dump", "bearish", "sell", "rug", "scam", "dead"}
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            for sub in subreddits:
                try:
                    url = f"https://www.reddit.com/r/{sub}/.rss"
                    response = await client.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0"
                    })
                    
                    if response.status_code != 200:
                        continue
                        
                    text = response.text.lower()
                    symbol_lower = symbol.lower()
                    
                    # Count mentions
                    pattern = rf'\b{re.escape(symbol_lower)}\b'
                    found = len(re.findall(pattern, text))
                    mentions += found
                    
                    # Sentiment analysis (simple keyword matching)
                    for word in bullish_words:
                        if word in text and symbol_lower in text:
                            positive += text.count(word)
                    
                    for word in bearish_words:
                        if word in text and symbol_lower in text:
                            negative += text.count(word)
                            
                except Exception as e:
                    logger.debug(f"Reddit fetch failed for r/{sub}: {e}")
                    continue
        
        # Calculate sentiment
        if positive + negative > 0:
            sentiment = (positive - negative) / (positive + negative)
        else:
            sentiment = 0.0
            
        return {
            "mentions": mentions,
            "sentiment": round(sentiment, 2),
        }
    
    async def _estimate_twitter_buzz(self, symbol: str) -> str:
        """
        Estimate Twitter buzz level using public nitter instances.
        
        Returns: "low" | "medium" | "high"
        """
        # Nitter instances (Twitter frontend without API)
        nitter_instances = [
            "nitter.net",
            "nitter.poast.org",
            "nitter.privacydev.net",
        ]
        
        symbol_upper = symbol.upper()
        mentions = 0
        
        async with httpx.AsyncClient(timeout=8.0) as client:
            for instance in nitter_instances:
                try:
                    # Search for recent mentions
                    url = f"https://{instance}/search?f=tweets&q=%24{symbol_upper}"
                    response = await client.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0"
                    })
                    
                    if response.status_code == 200:
                        # Count tweet containers in HTML
                        mentions = response.text.count('class="timeline-item')
                        break  # Use first working instance
                        
                except Exception as e:
                    logger.debug(f"Twitter buzz check failed for {instance}: {e}")
                    continue
        
        # Classify buzz level
        if mentions >= 50:
            return "high"
        elif mentions >= 15:
            return "medium"
        else:
            return "low"
    
    async def _check_trending_status(self, symbol: str) -> float:
        """
        Check if coin is trending using CoinGecko trending API.
        
        Returns: trending score 0-100
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://api.coingecko.com/api/v3/search/trending",
                    headers={"Accept": "application/json"}
                )
                
                if response.status_code != 200:
                    return 0.0
                    
                data = response.json()
                coins = data.get("coins", [])
                
                # Check if our symbol is in trending
                for idx, coin_data in enumerate(coins):
                    coin = coin_data.get("item", {})
                    coin_symbol = coin.get("symbol", "").upper()
                    
                    if coin_symbol == symbol.upper():
                        # Higher score for top positions
                        position_score = (len(coins) - idx) / len(coins) * 100
                        return round(position_score, 1)
                        
                return 0.0
                
        except Exception as e:
            logger.debug(f"Trending check failed: {e}")
            return 0.0
    
    def _calculate_viral_potential(
        self,
        reddit_mentions: int,
        twitter_buzz: str,
        trending_score: float,
    ) -> str:
        """
        Calculate overall viral potential.
        
        Returns: "low" | "medium" | "high" | "extreme"
        """
        score = 0
        
        # Reddit mentions
        if reddit_mentions >= 20:
            score += 3
        elif reddit_mentions >= 10:
            score += 2
        elif reddit_mentions >= 5:
            score += 1
            
        # Twitter buzz
        if twitter_buzz == "high":
            score += 3
        elif twitter_buzz == "medium":
            score += 2
        elif twitter_buzz == "low":
            score += 1
            
        # Trending status
        if trending_score >= 70:
            score += 3
        elif trending_score >= 40:
            score += 2
        elif trending_score >= 10:
            score += 1
            
        # Classify
        if score >= 8:
            return "extreme"
        elif score >= 6:
            return "high"
        elif score >= 3:
            return "medium"
        else:
            return "low"


# Global instance
_tracker = SocialSentimentTracker()


async def get_social_sentiment(symbol: str) -> Dict[str, Any]:
    """
    Public API to get social sentiment for a meme coin.
    
    Args:
        symbol: Base symbol (e.g., "PEPE", "DOGE")
        
    Returns:
        Dict with sentiment metrics
    """
    return await _tracker.get_sentiment(symbol)
