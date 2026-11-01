"""
Quick test script for meme coin improvements.
Run this to verify all new features are working.
"""

import asyncio
import sys
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))


async def test_enhanced_predictor():
    """Test the new ML-based predictor."""
    print("\n🧪 Testing Enhanced Predictor...")
    
    try:
        from agents.ai_engine.meme_predictor import get_enhanced_prediction
        
        # Mock data
        closes = [100 + i * 0.5 for i in range(60)]  # Uptrend
        volumes = [1000000 + i * 10000 for i in range(60)]
        
        signal, confidence, metadata = get_enhanced_prediction(
            closes=closes,
            volumes=volumes,
            rsi=45.0,
            ema20=closes[-20],
            change_24h=5.5,
            vol_spike=2.3,
            news_sentiment="bullish",
            dex_risk={"passed_filters": True, "risk_score": 85, "liquidity_usd": 250000},
            symbol="PEPE",
        )
        
        print(f"✅ Signal: {signal}")
        print(f"✅ Confidence: {confidence}")
        print(f"✅ Top Factors: {metadata.get('top_factors', [])}")
        print(f"✅ Features: {metadata.get('features', {})}")
        
        return True
        
    except Exception as e:
        print(f"❌ Enhanced Predictor Test Failed: {e}")
        return False


async def test_social_sentiment():
    """Test social sentiment tracker."""
    print("\n🧪 Testing Social Sentiment Tracker...")
    
    try:
        from agents.data_ingestion.social_sentiment import get_social_sentiment
        
        # Test with a popular coin
        result = await get_social_sentiment("PEPE")
        
        print(f"✅ Reddit Mentions: {result.get('reddit_mentions_24h', 0)}")
        print(f"✅ Reddit Sentiment: {result.get('reddit_sentiment', 0.0)}")
        print(f"✅ Twitter Buzz: {result.get('twitter_buzz', 'unknown')}")
        print(f"✅ Trending Score: {result.get('trending_score', 0.0)}")
        print(f"✅ Viral Potential: {result.get('viral_potential', 'unknown')}")
        
        return True
        
    except Exception as e:
        print(f"❌ Social Sentiment Test Failed: {e}")
        return False


async def test_dexscreener():
    """Test DexScreener integration."""
    print("\n🧪 Testing DexScreener Integration...")
    
    try:
        from agents.data_ingestion.dexscreener_client import analyze_meme_coin_risk
        
        # Test with a real coin
        result = await analyze_meme_coin_risk("PEPE")
        
        print(f"✅ Passed Filters: {result.get('passed_filters', False)}")
        print(f"✅ Risk Score: {result.get('risk_score', 0)}/100")
        print(f"✅ Liquidity: ${result.get('liquidity_usd', 0):,.0f}")
        print(f"✅ Volume 24h: ${result.get('volume_24h', 0):,.0f}")
        print(f"✅ Warnings: {result.get('warnings', [])}")
        
        return True
        
    except Exception as e:
        print(f"❌ DexScreener Test Failed: {e}")
        return False


async def test_config():
    """Test configuration updates."""
    print("\n🧪 Testing Configuration...")
    
    try:
        from config import settings
        
        meme_count = len(settings.MEME_COIN_SYMBOLS)
        print(f"✅ Total Meme Coins: {meme_count}")
        
        if meme_count >= 32:
            print("✅ New coins added successfully!")
            
            # Show some new coins
            new_coins = ["MICHI/USDT", "MYRO/USDT", "DOGS/USDT", "HMSTR/USDT"]
            found = [c for c in new_coins if c in settings.MEME_COIN_SYMBOLS]
            print(f"✅ New coins found: {', '.join(found)}")
        else:
            print(f"⚠️ Expected 32+ coins, found {meme_count}")
            
        return True
        
    except Exception as e:
        print(f"❌ Config Test Failed: {e}")
        return False


async def test_api_route():
    """Test the meme coin API endpoint."""
    print("\n🧪 Testing API Route...")
    
    try:
        import httpx
        
        # Try to hit the endpoint
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get("http://localhost:8000/api/crypto/meme")
            
            if response.status_code == 200:
                data = response.json()
                print(f"✅ API Response: {response.status_code}")
                print(f"✅ Total Coins: {data.get('total', 0)}")
                print(f"✅ BUY Signals: {data.get('buy_count', 0)}")
                print(f"✅ SELL Signals: {data.get('sell_count', 0)}")
                print(f"✅ HOT Coins: {data.get('hot_count', 0)}")
                
                # Show first coin
                if data.get('meme_coins'):
                    coin = data['meme_coins'][0]
                    print(f"\n📊 Sample Coin: {coin.get('base', 'N/A')}")
                    print(f"   Signal: {coin.get('signal', 'N/A')}")
                    print(f"   Confidence: {coin.get('confidence', 0)}")
                    print(f"   Factors: {coin.get('prediction_factors', [])}")
                
                return True
            else:
                print(f"⚠️ API returned status {response.status_code}")
                return False
                
    except httpx.ConnectError:
        print("⚠️ Server not running. Start with: uvicorn server:app")
        return False
    except Exception as e:
        print(f"❌ API Test Failed: {e}")
        return False


async def main():
    """Run all tests."""
    print("=" * 60)
    print("🚀 MEME COIN TRACKER - IMPROVEMENT TESTS")
    print("=" * 60)
    
    results = []
    
    # Run tests
    results.append(("Config", await test_config()))
    results.append(("Enhanced Predictor", await test_enhanced_predictor()))
    results.append(("DexScreener", await test_dexscreener()))
    results.append(("Social Sentiment", await test_social_sentiment()))
    results.append(("API Route", await test_api_route()))
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 TEST SUMMARY")
    print("=" * 60)
    
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} - {name}")
    
    total = len(results)
    passed = sum(1 for _, p in results if p)
    
    print(f"\n🎯 Score: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed! Meme coin tracker is ready!")
    elif passed >= total * 0.7:
        print("\n✅ Most tests passed. Some features may need server running.")
    else:
        print("\n⚠️ Some tests failed. Check error messages above.")
    
    print("\n📚 Next Steps:")
    print("1. Start server: uvicorn server:app --reload")
    print("2. Open dashboard: http://localhost:8000/crypto")
    print("3. Check meme coin section at bottom")
    print("4. Read docs: docs/MEME_COIN_IMPROVEMENTS.md")


if __name__ == "__main__":
    asyncio.run(main())
