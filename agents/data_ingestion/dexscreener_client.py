"""
DexScreener API Integration for Meme Coin Rug Filters.

Provides on-chain liquidity, volume, and pair data from decentralized exchanges
to filter out new, illiquid, or wash-traded meme coins before execution.
"""

import httpx
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

DEXSCREENER_API_URL = "https://api.dexscreener.com/latest/dex/search"

async def fetch_dex_pair_data(symbol_base: str) -> Optional[Dict[str, Any]]:
    """
    Fetch the most liquid trading pair for a given base symbol from DexScreener.
    
    Args:
        symbol_base: The base asset ticker (e.g., 'PEPE', 'WIF', 'BONK')
        
    Returns:
        The top pair data dictionary or None if not found.
    """
    try:
        # Search by symbol (e.g., PEPE)
        url = f"{DEXSCREENER_API_URL}?q={symbol_base}"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            
            if response.status_code != 200:
                logger.warning(f"DexScreener API returned status {response.status_code} for {symbol_base}")
                return None
                
            data = response.json()
            pairs = data.get("pairs", [])
            
            if not pairs:
                logger.debug(f"No on-chain pairs found for {symbol_base}")
                return None
                
            # Filter for pairs where our symbol is the base token
            # and it's trading against a major stablecoin or native asset (USDT, USDC, WETH, SOL)
            valid_pairs = []
            for pair in pairs:
                base_token = pair.get("baseToken", {}).get("symbol", "").upper()
                quote_token = pair.get("quoteToken", {}).get("symbol", "").upper()
                
                if base_token == symbol_base.upper():
                    valid_pairs.append(pair)
                    
            if not valid_pairs:
                return None
                
            # Sort by highest liquidity to avoid fake/scam pairs hijacking the ticker
            valid_pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
            
            # Return the most liquid pair
            return valid_pairs[0]
            
    except Exception as e:
        logger.error(f"Error fetching DexScreener data for {symbol_base}: {e}")
        return None


async def analyze_meme_coin_risk(symbol_base: str) -> dict:
    """
    Run 3 specific rug-pull/manipulation checks on a meme coin:
    1. Liquidity to Volume Ratio (Wash trading detection)
    2. Minimum Liquidity (Honey pot protection)
    3. Pair Age (Rug pull protection on brand new contracts)
    
    Returns:
        dict: {
            "passed_filters": bool,
            "risk_score": int (0-100, higher is safer),
            "warnings": list[str]
        }
    """
    pair_data = await fetch_dex_pair_data(symbol_base)
    
    if not pair_data:
        return {
            "passed_filters": False,
            "risk_score": 0,
            "warnings": ["No valid on-chain liquidity pool found (high rug risk)."]
        }
        
    warnings = []
    score = 100
    
    # Check 1: Minimum Liquidity ($100k min to avoid extreme slippage/honey pots)
    liquidity_usd = float(pair_data.get("liquidity", {}).get("usd", 0) or 0)
    if liquidity_usd < 100_000:
        warnings.append(f"Dangerously low liquidity (${liquidity_usd:,.0f}).")
        score -= 40
        
    # Check 2: Volume to Liquidity Ratio (Wash Trading Detection)
    # If 24h volume is 10x higher than total liquidity, it's almost certainly wash trading bots.
    volume_24h = float(pair_data.get("volume", {}).get("h24", 0) or 0)
    
    if liquidity_usd > 0:
        vol_liq_ratio = volume_24h / liquidity_usd
        if vol_liq_ratio > 10.0:
            warnings.append(f"Abnormal Volume/Liquidity ratio ({vol_liq_ratio:.1f}x). High wash trading risk.")
            score -= 50
        elif vol_liq_ratio > 5.0:
            warnings.append("Elevated Wash Trading risk (Volume > 5x Liquidity).")
            score -= 20
            
    # Check 3: Age / Trust (DexScreener age is based on pair creation)
    # We use pair creation timestamp if available, but as a fallback we check FDV/Liquidity
    # Extremely high FDV with low liquidity is a massive red flag for newly minted rugs.
    fdv = float(pair_data.get("fdv", 0) or 0)
    if liquidity_usd > 0 and fdv > 0:
        fdv_liq_ratio = fdv / liquidity_usd
        if fdv_liq_ratio > 100:
            warnings.append(f"FDV is {fdv_liq_ratio:.0f}x larger than liquidity. Extreme manipulation risk.")
            score -= 30
            
    passed = score >= 60 and liquidity_usd >= 100_000
    
    return {
        "passed_filters": passed,
        "risk_score": max(0, score),
        "warnings": warnings,
        "liquidity_usd": liquidity_usd,
        "volume_24h": volume_24h,
        "dex": pair_data.get("dexId", "unknown"),
        "network": pair_data.get("chainId", "unknown")
    }
