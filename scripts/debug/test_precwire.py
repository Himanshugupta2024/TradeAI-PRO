import sys
from config import settings
import logging

logging.basicConfig(level=logging.DEBUG)

def mock_scan_precwire():
    import pandas as pd
    from agents.market_intelligence.swing_scanner import scan_swing_setups
    
    # create a mock df that triggers a breakout and matches the user's data
    n = 30
    timestamps = pd.date_range(end="2026-04-14", periods=n, freq="1D", tz="UTC")
    closes = [300.0] * n
    highs = [310.0] * n
    lows = [290.0] * n
    opens = closes
    volumes = [100000] * n
    
    # Last few days
    closes[-2] = 310.0 # break near high
    # Last day
    closes[-1] = 341.3
    highs[-1] = 345.0
    lows[-1] = 315.0
    
    # RSI = 63
    # EMA20 = 311.14
    # ATR = 18.34
    
    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })
    
    df["EMA_20"] = 300.0
    df.loc[df.index[-1], "EMA_20"] = 311.14
    
    df["EMA_50"] = 280.0
    df["EMA_200"] = 250.0
    df["RSI_14"] = 63.0
    df["ATR_14"] = 18.34
    
    # BB width
    df["BB_width"] = 0.05
    
    market_context = {"index_trend": "BULLISH"}
    
    setups = scan_swing_setups(df, "PRECWIRE", market_context=market_context)
    for s in setups:
        print(f"Setup: {s.pattern}")
        print(f"Entry: {s.entry_zone}")
        print(f"SL: {s.stop_loss}")
        print(f"TP1: {s.target_1}")
        print(f"TP2: {s.target_2}")
        print(f"RR: {s.risk_reward}")
        print(s.description)
        print("-----")

if __name__ == "__main__":
    mock_scan_precwire()