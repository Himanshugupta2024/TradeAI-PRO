"""
Async Live Trading Runner.

Concurrent version of live_runner.py — fetches data for all symbols
simultaneously using asyncio + ThreadPoolExecutor (since ccxt/yfinance
are sync libraries). This dramatically reduces cycle time when
trading 5+ symbols.

Usage:
  python async_runner.py                        # All symbols, 5-min
  python async_runner.py --symbol BTC/USDT      # Single symbol
  python async_runner.py --once                 # One-shot test
"""

import sys
import os
import asyncio
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from config.logger import get_logger
from db.connection import init_db
from agents.data_ingestion import binance_fetcher, nse_fetcher
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.support_resistance import calculate_support_resistance
from agents.feature_engine.trend import classify_trend, propagate_trend_to_ltf
from agents.ai_engine.fallback_predictor import predict_batch
from agents.signal_engine.signal_generator import generate_signals
from agents.execution.paper_trading import PaperTradingEngine
from agents.alerts.telegram_bot import (
    send_signal_alert, send_trade_closed_alert, send_daily_summary,
)

log = get_logger("async_runner")

# Thread pool for offloading blocking I/O (ccxt, yfinance)
_executor = ThreadPoolExecutor(max_workers=6)


# ── Async Data Fetching ────────────────────────────────

async def fetch_data_async(symbol: str, timeframe: str, days: int = 30):
    """Fetch data in a thread to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()

    if symbol in settings.CRYPTO_SYMBOLS:
        return await loop.run_in_executor(
            _executor, lambda: binance_fetcher.fetch_historical(symbol, timeframe, since_days=days)
        )
    elif symbol in settings.NSE_SYMBOLS:
        return await loop.run_in_executor(
            _executor, lambda: nse_fetcher.fetch_historical(symbol, timeframe, since_days=days)
        )
    else:
        log.error("Unknown symbol: %s", symbol)
        return None


async def enrich_data_async(df, htf_df=None):
    """Run enrichment (CPU-bound, but fast enough to stay on main thread)."""
    df = calculate_indicators(df)
    df = calculate_support_resistance(df, pivot_window=10, lookback=50)

    if htf_df is not None:
        htf_enriched = calculate_indicators(htf_df)
        htf_enriched = classify_trend(htf_enriched)
        df = propagate_trend_to_ltf(df, htf_enriched)
    else:
        df = classify_trend(df)

    return df


# ── Per-Symbol Cycle ───────────────────────────────────

async def run_symbol(symbol: str, engine: PaperTradingEngine) -> dict | None:
    """Run one complete analysis cycle for a single symbol."""
    log.info("─── %s ───", symbol)

    # Fetch LTF and HTF data concurrently
    ltf_task = fetch_data_async(symbol, "1h", days=14)
    htf_task = fetch_data_async(symbol, "1d", days=60)
    df, htf_df = await asyncio.gather(ltf_task, htf_task)

    if df is None or df.empty:
        log.warning("No data for %s — skipping", symbol)
        return None

    # Enrich
    df = await enrich_data_async(df, htf_df)

    # AI prediction
    ai_confidence = predict_batch(df)
    df["ai_confidence"] = ai_confidence

    # Signal generation
    df = generate_signals(df, ai_confidence=ai_confidence)

    last = df.iloc[-1]
    signal = last.get("signal", "HOLD")
    confidence = last.get("confidence", 0)

    log.info(
        "Signal: %s | Conf: %.0f%% | Close: %.2f | RSI: %.1f | Trend: %s",
        signal, confidence, last["close"],
        last.get("RSI_14", 0), last.get("htf_trend", "N/A"),
    )

    # Execute trade if signal is actionable
    if signal in ("LONG", "SHORT"):
        entry = float(last["close"])
        sl = float(last["stop_loss"])
        tp = float(last["target"])
        trend = str(last.get("htf_trend", ""))
        rsi = float(last.get("RSI_14", 0))

        trade = engine.execute(symbol, signal, entry, sl, tp)
        if trade:
            send_signal_alert(
                symbol=symbol, direction=signal, entry=entry,
                stop_loss=sl, target=tp, confidence=confidence,
                trend=trend, rsi=rsi,
            )
            return {
                "symbol": symbol, "signal": signal, "entry": entry,
                "stop_loss": sl, "target": tp, "confidence": confidence,
            }

    # Check open trades against latest prices
    if not df.empty:
        last_row = df.iloc[-1]
        prices = {
            symbol: {
                "high": float(last_row["high"]),
                "low": float(last_row["low"]),
                "close": float(last_row["close"]),
            }
        }
        closed = engine.update_prices(prices)
        for trade in closed:
            send_trade_closed_alert(
                symbol=trade.symbol, direction=trade.direction,
                entry=trade.entry_price, exit_price=trade.exit_price,
                pnl=trade.pnl, pnl_pct=trade.pnl_pct,
                exit_reason=trade.status,
            )

    return None


# ── Main Loop ──────────────────────────────────────────

async def run_cycle(symbols: list[str], engine: PaperTradingEngine, cycle: int):
    """Run all symbols concurrently in a single cycle."""
    log.info("━━━ CYCLE #%d — %s ━━━", cycle,
             datetime.now().strftime("%d %b %Y, %I:%M:%S %p IST"))

    # Launch all symbols concurrently
    tasks = [run_symbol(sym, engine) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Log any errors
    for sym, result in zip(symbols, results):
        if isinstance(result, Exception):
            log.error("Error processing %s: %s", sym, result, exc_info=result)

    stats = engine.get_stats()
    log.info("📊 Capital: ₹%.2f | Trades: %d | Win Rate: %.1f%%",
             stats["capital"], stats["total_trades"], stats["win_rate"])


async def main_async(args):
    """Async entry point."""
    init_db()

    symbols = [args.symbol] if args.symbol else settings.CRYPTO_SYMBOLS + settings.NSE_SYMBOLS
    engine = PaperTradingEngine(initial_capital=100_000)

    log.info("═" * 50)
    log.info("TradeAI PRO — Async Runner")
    log.info("Symbols: %s | Interval: %ds | Capital: ₹%.2f",
             ", ".join(symbols), args.interval, engine.capital)
    log.info("═" * 50)

    cycle = 0
    while True:
        cycle += 1
        await run_cycle(symbols, engine, cycle)

        if args.once:
            break

        log.info("Next cycle in %ds...", args.interval)
        await asyncio.sleep(args.interval)

    # Daily summary
    send_daily_summary(engine.get_stats())


def main():
    parser = argparse.ArgumentParser(description="AI Trading System — Async Runner")
    parser.add_argument("--symbol", type=str, default=None)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
