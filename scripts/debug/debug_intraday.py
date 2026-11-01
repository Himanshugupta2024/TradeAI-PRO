import asyncio
import logging
import pandas as pd
from agents.intraday_engine.scanner import scan_intraday_setups, should_square_off_now, _is_fresh_intraday, _latest_bar_age_minutes
from agents.intraday_engine.fetcher import fetch_5m

logging.basicConfig(level=logging.DEBUG)

async def main():
    try:
        symbols = ['TCS.NS', 'RELIANCE.NS']
        portfolio = {'capital': 100000.0, 'open_positions': []}
        
        print(f"Square off now? {should_square_off_now()}")
        for sym in symbols:
            df = fetch_5m(sym)
            if df is None or df.empty:
                print(f"{sym}: fetch_5m returned None/empty")
            else:
                age = _latest_bar_age_minutes(df)
                fresh = _is_fresh_intraday(df)
                print(f"{sym}: df shape {df.shape}, latest timestamp: {df['timestamp'].max()}, age_mins: {age}, fresh: {fresh}")
                
        print("\nStarting scan...")
        results = scan_intraday_setups(symbols, portfolio)
        print("Results:", results)
    except Exception as e:
        print("Exception occurred:", str(e))
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(main())
