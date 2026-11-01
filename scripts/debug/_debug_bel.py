"""Quick diagnostic: run BEL through the scanner and print every stage."""
import sys
sys.path.insert(0, ".")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s - %(message)s")

from agents.feature_engine.indicators import calculate_indicators
from agents.market_intelligence.nse_fetcher import fetch_arbitrary_symbol
from agents.market_intelligence.swing_scanner import scan_swing_setups

print("=" * 60)
print("Fetching BEL.NS daily data...")
df = fetch_arbitrary_symbol("BEL.NS", "1y", "1d")
if df is None or df.empty:
    df = fetch_arbitrary_symbol("BEL", "1y", "1d")
if df is None or df.empty:
    print("FAIL: Could not fetch BEL data")
    sys.exit(1)

df = calculate_indicators(df)
last = df.iloc[-1]
print(f"Close={float(last['close']):.2f}  EMA20={float(last['EMA_20']):.2f}  "
      f"EMA50={float(last['EMA_50']):.2f}  ATR={float(last['ATR_14']):.2f}  "
      f"RSI={float(last['RSI_14']):.1f}  Bars={len(df)}")

# Proximity check (what pullback_ema checks)
ema20 = float(last['EMA_20'])
ema50 = float(last['EMA_50'])
close = float(last['close'])
proximity = abs(close - ema20) / ema20 * 100
print(f"Proximity to EMA20: {proximity:.2f}%  (need < 1.5%)")
print(f"EMA20 > EMA50: {ema20 > ema50}  close > ema50: {close > ema50}")

print("=" * 60)
stages = {}
ctx = {"index_symbol": "NIFTY 50", "index_regime": "UNKNOWN", "index_trend": "NEUTRAL"}
setups = scan_swing_setups(df, "BEL", market_context=ctx, stages_out=stages, allow_fallback=True)

print("\n--- SCAN STAGES ---")
for k, v in stages.items():
    print(f"  {k}: {v}")

print(f"\n--- SETUPS FOUND: {len(setups)} ---")
for s in setups:
    print(f"  Pattern={s.pattern} | Dir={s.direction} | Entry={s.entry_zone} "
          f"SL={s.stop_loss} T1={s.target_1} T2={s.target_2} RR={s.risk_reward}")
print("=" * 60)
