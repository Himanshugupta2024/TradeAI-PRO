import asyncio
import pandas as pd
from agents.market_intelligence.swing_scanner import scan_all_symbols
from agents.market_intelligence.nse_fetcher import fetch_ohlcv
from agents.feature_engine.indicators import calculate_indicators

async def test_scan():
    try:
        print("Fetching some data...")
        symbols = ["TATASTEEL", "RELIANCE", "SBI"] # some stocks to test
        symbol_data = {}
        for sym in symbols:
            df = await asyncio.to_thread(fetch_ohlcv, sym, "1y", "1d")
            if df is not None and not df.empty and len(df) >= 30:
                df = calculate_indicators(df)
                symbol_data[sym] = df
                print(f"Loaded {sym}")
        
        print("Starting scan_all_symbols()...")
        results = scan_all_symbols(symbol_data)
        print(f"Scan complete. Found {len(results)} setups.")
    except Exception as e:
        print(f"Error during scan: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_scan())
