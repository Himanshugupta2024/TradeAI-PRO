import asyncio
import logging
import pandas as pd

# Mock settings to override times/ages
from config import settings
settings.INTRADAY_MAX_BAR_AGE_MINUTES = 999999  # very large age to allow stale data
settings.INTRADAY_SQUARE_OFF_HOUR_IST = 23      # push square-off very late
settings.INTRADAY_SQUARE_OFF_MINUTE_IST = 59

from agents.intraday_engine.scanner import scan_intraday_setups, should_square_off_now
from agents.intraday_engine.fetcher import fetch_5m

logging.basicConfig(level=logging.DEBUG)

async def main():
    symbols = ['TCS', 'RELIANCE', 'INFY']
    portfolio = {'capital': 100000.0, 'open_positions': []}
    
    print(f"Square off now? {should_square_off_now()}")
    for sym in symbols:
        df = fetch_5m(sym)
        if df is None or df.empty:
            print(f"{sym}: fetch_5m returned None/empty")
        else:
            print(f"{sym}: fetched {len(df)} rows. Latest time: {df['timestamp'].max()}")
            
    print("\nStarting scan...")
    results = scan_intraday_setups(symbols, portfolio)
    print(f"Found {len(results)} results")
    for r in results:
        print(r)

if __name__ == '__main__':
    asyncio.run(main())
