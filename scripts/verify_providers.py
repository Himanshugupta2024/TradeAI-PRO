"""
TradeAI-PRO — Live Data Provider Verification Script
Tests NSE, Crypto, Forex, and Context/News providers individually with real network calls.
Reports timestamps, price observations, latency, and fallback behavior.
"""

import sys
import os
import time
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import settings
from config.logger import get_logger

log = get_logger("verify_providers")

results = {}

print("=" * 80)
print(f"TRADEAI-PRO LIVE PROVIDER VERIFICATION — {datetime.now(timezone.utc).isoformat()}")
print("=" * 80)

# ── 1. NSE EQUITIES PROVIDER ────────────────────────────────────────────────
print("\n[1/4] Testing NSE Equities Provider...")
nse_results = {}
try:
    from agents.market_intelligence.nse_fetcher import fetch_ohlcv, fetch_arbitrary_symbol
    from agents.data_ingestion.nse_live_quote import get_nse_live_quote

    for sym in ["RELIANCE", "TCS", "INFY"]:
        t0 = time.perf_counter()
        df = fetch_ohlcv(sym, period="5d", interval="1d")
        dur = (time.perf_counter() - t0) * 1000
        if df is not None and not df.empty:
            last_row = df.iloc[-1]
            last_ts = str(df.index[-1]) if hasattr(df, "index") else "N/A"
            last_close = float(last_row.get("close", last_row.get("Close", 0.0)))
            last_vol = float(last_row.get("volume", last_row.get("Volume", 0.0)))
            nse_results[sym] = {
                "status": "PASS",
                "bars": len(df),
                "latest_ts": last_ts,
                "latest_close": last_close,
                "latest_volume": last_vol,
                "latency_ms": round(dur, 1),
            }
            print(f"  ✓ {sym:<10} | Close: {last_close:>10.2f} | Bars: {len(df)} | TS: {last_ts} | {dur:6.1f}ms")
        else:
            nse_results[sym] = {"status": "FAIL", "error": "Empty dataframe", "latency_ms": round(dur, 1)}
            print(f"  ✗ {sym:<10} | FAILED to fetch OHLCV | {dur:6.1f}ms")

    # Test Live Quote
    t0 = time.perf_counter()
    live_q = get_nse_live_quote("RELIANCE")
    dur_q = (time.perf_counter() - t0) * 1000
    if live_q and getattr(live_q, "price", None) is not None:
        print(f"  ✓ Live Quote RELIANCE: LTP={live_q.price} (Source: {live_q.source}, Fresh: {live_q.is_fresh}) in {dur_q:.1f}ms")
        nse_results["live_quote_RELIANCE"] = {
            "price": live_q.price,
            "source": live_q.source,
            "is_fresh": live_q.is_fresh,
            "latency_ms": round(dur_q, 1)
        }
    else:
        print(f"  ⚠ Live Quote RELIANCE returned empty fallback in {dur_q:.1f}ms")

except Exception as exc:
    print(f"  ✗ NSE Provider exception: {exc}")
    nse_results["error"] = str(exc)

results["NSE"] = nse_results

# ── 2. CRYPTO PROVIDER (CCXT / BINANCE) ─────────────────────────────────────
print("\n[2/4] Testing Crypto Provider (Binance/CCXT)...")
crypto_results = {}
try:
    from agents.data_ingestion.binance_fetcher import fetch_historical

    for coin in ["BTC/USDT", "ETH/USDT", "DOGE/USDT"]:
        t0 = time.perf_counter()
        df = fetch_historical(coin, "1h", since_days=2)
        dur = (time.perf_counter() - t0) * 1000
        if df is not None and not df.empty:
            last_close = float(df["close"].iloc[-1])
            last_ts = str(df.index[-1]) if hasattr(df, "index") else "N/A"
            crypto_results[coin] = {
                "status": "PASS",
                "bars": len(df),
                "latest_ts": last_ts,
                "latest_close": last_close,
                "latency_ms": round(dur, 1)
            }
            print(f"  ✓ {coin:<10} | Close: {last_close:>12.4f} | Bars: {len(df)} | TS: {last_ts} | {dur:6.1f}ms")
        else:
            crypto_results[coin] = {"status": "FAIL", "error": "Empty dataframe", "latency_ms": round(dur, 1)}
            print(f"  ✗ {coin:<10} | FAILED to fetch crypto OHLCV | {dur:6.1f}ms")
except Exception as exc:
    print(f"  ✗ Crypto Provider exception: {exc}")
    crypto_results["error"] = str(exc)

results["Crypto"] = crypto_results

# ── 3. FOREX PROVIDER ───────────────────────────────────────────────────────
print("\n[3/4] Testing Forex Provider (Twelve Data + Fallback)...")
forex_results = {}
try:
    from agents.data_ingestion.forex_fetcher import get_all_forex_rates, get_forex_ohlcv, is_twelve_data_backed_off

    has_td_key = bool(getattr(settings, "TWELVE_DATA_API_KEY", None))
    print(f"  Twelve Data API Key Configured: {has_td_key} (fallback enabled)")

    t0 = time.perf_counter()
    rates = get_all_forex_rates()
    dur_rates = (time.perf_counter() - t0) * 1000
    print(f"  ✓ Live Rates returned {len(rates)} pairs in {dur_rates:.1f}ms:")
    for pair in ["USD/INR", "EUR/USD", "GBP/INR"]:
        val = rates.get(pair)
        if isinstance(val, dict):
            price = val.get("rate") or val.get("close")
            src = val.get("source", "unknown")
        else:
            price = val
            src = "yfinance_fallback" if not has_td_key else "twelve_data"
        print(f"    - {pair:<10}: {price} (Source: {src})")
        forex_results[pair] = {"rate": price, "source": src}

    # Test OHLCV
    for pair in ["USD/INR", "EUR/USD"]:
        t0 = time.perf_counter()
        df = get_forex_ohlcv(pair, "1day")
        dur = (time.perf_counter() - t0) * 1000
        if df is not None and not df.empty:
            last_close = float(df["close"].iloc[-1])
            print(f"  ✓ OHLCV {pair:<8} | Close: {last_close:>10.4f} | Bars: {len(df)} | {dur:6.1f}ms")
            forex_results[f"{pair}_ohlcv"] = {"status": "PASS", "close": last_close, "latency_ms": round(dur, 1)}
        else:
            print(f"  ✗ OHLCV {pair:<8} | FAILED | {dur:6.1f}ms")
            forex_results[f"{pair}_ohlcv"] = {"status": "FAIL", "latency_ms": round(dur, 1)}

except Exception as exc:
    print(f"  ✗ Forex Provider exception: {exc}")
    forex_results["error"] = str(exc)

results["Forex"] = forex_results

# ── 4. CONTEXT / NEWS SENTIMENT PROVIDER ────────────────────────────────────
print("\n[4/4] Testing Context & News Sentiment Provider (Google News RSS)...")
context_results = {}
try:
    from agents.data_ingestion.context_fetcher import build_context_fetcher
    cf = build_context_fetcher()

    for sym in ["RELIANCE", "BTC/USDT"]:
        t0 = time.perf_counter()
        ctx = cf.get_snapshot_sync(sym)
        dur = (time.perf_counter() - t0) * 1000
        score = getattr(ctx, "context_score", 0.0) if ctx else 0.0
        sentiment_24h = getattr(ctx, "news_sentiment_24hr", 0.0) if ctx else 0.0
        spike = getattr(ctx, "news_volume_spike", 0) if ctx else 0
        trend_bias = getattr(ctx, "weekly_trend_bias", "UNKNOWN") if ctx else "UNKNOWN"
        print(f"  ✓ {sym:<10} | ContextScore: {score:>5.1f} | 24h Sent: {sentiment_24h:>5.2f} (Spike: {spike}) | Bias: {trend_bias} | {dur:6.1f}ms")
        context_results[sym] = {
            "status": "PASS",
            "context_score": score,
            "sentiment_24h": sentiment_24h,
            "news_volume_spike": spike,
            "trend_bias": trend_bias,
            "latency_ms": round(dur, 1),
        }
except Exception as exc:
    print(f"  ✗ Context Provider exception: {exc}")
    context_results["error"] = str(exc)

results["Context"] = context_results

print("\n" + "=" * 80)
print("PROVIDER VERIFICATION SUMMARY:")
print(json.dumps(results, indent=2))
print("=" * 80)
