"""
Autonomous Trading Runner.

Production-grade runner with event-driven architecture:
  - Binance WebSocket for real-time crypto data (sub-second)
  - Fallback to CCXT polling when WebSocket unavailable
  - Self-healing with exponential backoff on failures
  - Health heartbeat written to health.json
  - Regime detection per symbol with change alerts
  - Data staleness detection and gap filling
  - Scheduled daily summary at market close
  - Concurrent data fetching via asyncio

Usage:
  python autonomous_runner.py                     # All symbols, event-driven
  python autonomous_runner.py --symbol BTC/USDT   # Single symbol
  python autonomous_runner.py --once              # One-shot test
  python autonomous_runner.py --no-ws             # Force polling mode
"""

import sys
import os
import asyncio
import argparse
import json
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from config.logger import get_logger
from db.connection import init_db
from agents.data_ingestion import binance_fetcher, nse_fetcher
from agents.data_ingestion.binance_ws import BinanceWebSocket
from agents.data_ingestion.data_manager import DataManager
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.support_resistance import calculate_support_resistance
from agents.feature_engine.trend import classify_trend, propagate_trend_to_ltf
from agents.feature_engine.regime_detector import detect_regime, MarketRegime
from agents.ai_engine.fallback_predictor import predict_batch
from agents.signal_engine.signal_generator import generate_signals
from agents.execution.paper_trading import PaperTradingEngine
from agents.alerts.telegram_bot import (
    send_signal_alert, send_trade_closed_alert, send_daily_summary,
    send_regime_change_alert, send_health_alert, validate_bot,
    send_data_feed_alert, send_stale_data_alert, send_websocket_alert,
    send_consecutive_loss_alert,
)

log = get_logger("autonomous")

_executor = ThreadPoolExecutor(max_workers=6)

# Track regimes per symbol to detect changes
_regime_cache: dict[str, MarketRegime] = {}

# Backoff state
_consecutive_failures = 0
_MAX_BACKOFF = 300  # 5 minutes max backoff

# Data manager (centralized candle store)
_data_manager = DataManager()

# Stale data alert tracking (avoid spamming)
_stale_alerted: set[str] = set()


# ── Health Heartbeat ──────────────────────────────────

def write_health(status: str = "alive", extra: dict = None):
    """Write health status to a JSON file for external monitoring."""
    health = {
        "status": status,
        "timestamp": datetime.now().isoformat(),
        "uptime_cycles": extra.get("cycle", 0) if extra else 0,
    }
    if extra:
        health.update(extra)

    try:
        with open(settings.HEALTH_FILE, "w") as f:
            json.dump(health, f, indent=2)
    except Exception:
        pass  # Non-critical


# ── Async Data Fetching ───────────────────────────────

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
    """Run feature engineering pipeline."""
    df = calculate_indicators(df)
    df = calculate_support_resistance(df, pivot_window=10, lookback=50)

    if htf_df is not None:
        htf_enriched = calculate_indicators(htf_df)
        htf_enriched = classify_trend(htf_enriched)
        df = propagate_trend_to_ltf(df, htf_enriched)
    else:
        df = classify_trend(df)

    return df


# ── Per-Symbol Cycle ──────────────────────────────────

async def run_symbol(symbol: str, engine: PaperTradingEngine) -> dict | None:
    """Run one complete analysis cycle for a single symbol."""
    global _regime_cache, _stale_alerted
    log.info("─── %s ───", symbol)

    # Check data staleness for crypto symbols using the data manager
    if symbol in settings.CRYPTO_SYMBOLS:
        staleness = _data_manager.get_staleness_seconds(symbol, "1h")
        if _data_manager.is_stale(symbol, "1h"):
            if symbol not in _stale_alerted:
                log.warning("Stale data for %s (%.0fs since last candle) — falling back to API fetch", symbol, staleness)
                send_stale_data_alert(symbol, staleness, 3600)
                _stale_alerted.add(symbol)
            # Do NOT return early — fall through to fetch fresh data via API below
        else:
            _stale_alerted.discard(symbol)

    # Try data manager first (has WebSocket data), fall back to fetcher
    df = _data_manager.get_dataframe(symbol, "1h", limit=300)
    htf_df = _data_manager.get_dataframe(symbol, "1d", limit=60)

    # If data manager is empty, fetch via API
    if df.empty:
        ltf_task = fetch_data_async(symbol, "1h", days=14)
        htf_task = fetch_data_async(symbol, "1d", days=60)
        df, htf_df = await asyncio.gather(ltf_task, htf_task)

        if df is not None and not df.empty:
            await _data_manager.load_history(symbol, "1h", df)
        if htf_df is not None and not htf_df.empty:
            await _data_manager.load_history(symbol, "1d", htf_df)

    if df is None or df.empty:
        log.warning("No data for %s — skipping", symbol)
        send_data_feed_alert(symbol, "API", "No data returned")
        return None

    # Check for gaps in the data
    gaps = _data_manager.detect_gaps(symbol, "1h")
    if gaps:
        log.warning("%s has %d candle gaps — data may be incomplete", symbol, len(gaps))
        # Try to backfill gaps for crypto
        if symbol in settings.CRYPTO_SYMBOLS:
            for gap_start, gap_end in gaps[:3]:  # Limit to 3 gap fills per cycle
                try:
                    fill_df = await asyncio.get_event_loop().run_in_executor(
                        _executor,
                        lambda s=gap_start, e=gap_end: binance_fetcher.fetch_gap_fill(symbol, "1h", s, e)
                    )
                    if fill_df is not None and not fill_df.empty:
                        await _data_manager.load_history(symbol, "1h", fill_df, merge=True)
                        log.info("Gap-filled %d candles for %s", len(fill_df), symbol)
                except Exception as e:
                    log.error("Gap-fill failed for %s: %s", symbol, e)

            # Re-fetch after gap fill
            df = _data_manager.get_dataframe(symbol, "1h", limit=300)

    # Enrich
    df = await enrich_data_async(df, htf_df)

    # Detect market regime
    regime = detect_regime(df)
    old_regime = _regime_cache.get(symbol)
    if old_regime and old_regime != regime:
        log.info("🔄 %s regime changed: %s → %s", symbol, old_regime.value, regime.value)
        send_regime_change_alert(symbol, old_regime.value, regime.value)
    _regime_cache[symbol] = regime

    # AI prediction
    ai_confidence = predict_batch(df)
    df["ai_confidence"] = ai_confidence

    # Signal generation with regime awareness
    df = generate_signals(df, ai_confidence=ai_confidence, regime=regime)

    last = df.iloc[-1]
    signal = last.get("signal", "HOLD")
    confidence = last.get("confidence", 0)

    # Get current ATR for trailing SL
    atr = float(last.get(f"ATR_{settings.ATR_PERIOD}", 0))

    log.info(
        "Signal: %s | Conf: %.0f%% | Close: %.2f | RSI: %.1f | Trend: %s | Regime: %s",
        signal, confidence, last["close"],
        last.get("RSI_14", 0), last.get("htf_trend", "N/A"), regime.value,
    )

    # Execute trade if signal is actionable
    if signal in ("LONG", "SHORT"):
        entry = float(last["close"])
        sl = float(last["stop_loss"])
        tp = float(last["target"])
        trend = str(last.get("htf_trend", ""))
        rsi = float(last.get("RSI_14", 0))

        trade = engine.execute(symbol, signal, entry, sl, tp, atr=atr)
        if trade:
            send_signal_alert(
                symbol=symbol, direction=signal, entry=entry,
                stop_loss=sl, target=tp, confidence=confidence,
                trend=trend, rsi=rsi, regime=regime.value,
            )
            return {
                "symbol": symbol, "signal": signal, "entry": entry,
                "stop_loss": sl, "target": tp, "confidence": confidence,
                "regime": regime.value,
            }

    # Check open trades against latest prices (with trailing SL)
    if not df.empty:
        last_row = df.iloc[-1]
        prices = {
            symbol: {
                "high": float(last_row["high"]),
                "low": float(last_row["low"]),
                "close": float(last_row["close"]),
            }
        }
        atr_values = {symbol: atr} if atr > 0 else {}
        closed = engine.update_prices(prices, atr_values=atr_values)
        for trade in closed:
            send_trade_closed_alert(
                symbol=trade.symbol, direction=trade.direction,
                entry=trade.entry_price, exit_price=trade.exit_price,
                pnl=trade.pnl, pnl_pct=trade.pnl_pct,
                exit_reason=trade.status,
            )

    # Tick cooldowns
    engine.tick_cooldowns()

    return None


# ── Main Loop ─────────────────────────────────────────

async def run_cycle(symbols: list[str], engine: PaperTradingEngine, cycle: int):
    """Run all symbols concurrently in a single cycle."""
    log.info("━━━ CYCLE #%d — %s ━━━", cycle,
             datetime.now().strftime("%d %b %Y, %I:%M:%S %p IST"))

    tasks = [run_symbol(sym, engine) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for sym, result in zip(symbols, results):
        if isinstance(result, Exception):
            log.error("Error processing %s: %s", sym, result, exc_info=result)

    stats = engine.get_stats()
    log.info("📊 Capital: ₹%.2f | Trades: %d | Win Rate: %.1f%% | Sharpe: %.2f",
             stats["capital"], stats["total_trades"], stats["win_rate"],
             stats.get("sharpe_ratio", 0))


def _should_send_daily_report() -> bool:
    """
    Check if we're at market close time for daily report.
    Comparison is done in IST regardless of server timezone.
    """
    if not settings.DAILY_REPORT_ENABLED:
        return False
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(tz=IST)
    close_hour, close_min = map(int, settings.NSE_MARKET_CLOSE_IST.split(":"))
    return now_ist.hour == close_hour and now_ist.minute == close_min


async def main_async(args):
    """Async entry point with WebSocket event-driven architecture and self-healing."""
    global _consecutive_failures
    init_db()

    # Validate Telegram bot on startup
    validate_bot()

    symbols = [args.symbol] if args.symbol else settings.CRYPTO_SYMBOLS + settings.NSE_SYMBOLS
    crypto_symbols = [s for s in symbols if s in settings.CRYPTO_SYMBOLS]
    nse_symbols = [s for s in symbols if s in settings.NSE_SYMBOLS]
    engine = PaperTradingEngine(initial_capital=100_000)

    log.info("═" * 50)
    log.info("TradeAI PRO — Autonomous Runner v3.0 (Event-Driven)")
    log.info("Symbols: %s | Capital: ₹%.2f", ", ".join(symbols), engine.capital)
    log.info("Mode: %s", "WebSocket + Polling" if (settings.WS_ENABLED and not args.no_ws) else "Polling only")
    log.info("═" * 50)

    send_health_alert("🚀 Autonomous Runner started (v3.0 event-driven)", level="INFO")

    # Initial data load — backfill historical data for all symbols
    log.info("Loading historical data for initial backfill...")
    for symbol in symbols:
        try:
            ltf = await fetch_data_async(symbol, "1h", days=14)
            htf = await fetch_data_async(symbol, "1d", days=60)
            if ltf is not None and not ltf.empty:
                await _data_manager.load_history(symbol, "1h", ltf)
            if htf is not None and not htf.empty:
                await _data_manager.load_history(symbol, "1d", htf)
        except Exception as e:
            log.error("Initial load failed for %s: %s", symbol, e)

    # WebSocket event handler — triggers signal pipeline on candle close
    async def on_candle_close(candle: dict):
        """Called when a candle completes on WebSocket."""
        await _data_manager.update_candle(candle)
        symbol = candle["symbol"]
        log.info("📊 Candle closed: %s %s @ %.2f", symbol, candle["timeframe"], candle["close"])
        
        # Run signal pipeline for this symbol
        try:
            result = await run_symbol(symbol, engine)
            if result:
                log.info("⚡ Signal: %s %s @ %.2f", result["signal"], result["symbol"], result["entry"])
        except Exception as e:
            log.error("Signal pipeline error for %s: %s", symbol, e, exc_info=True)

    async def on_candle_update(candle: dict):
        """Called on every WebSocket tick — update trailing stops."""
        await _data_manager.update_candle(candle)
        symbol = candle["symbol"]

        # Update trailing SL for open trades with latest prices
        if engine.open_trades:
            prices = {
                symbol: {
                    "high": candle["high"],
                    "low": candle["low"],
                    "close": candle["close"],
                }
            }
            # Get ATR from data manager
            df = _data_manager.get_dataframe(symbol, "1h", limit=20)
            atr = 0
            if not df.empty and len(df) > settings.ATR_PERIOD:
                enriched = calculate_indicators(df)
                atr_col = f"ATR_{settings.ATR_PERIOD}"
                if atr_col in enriched.columns:
                    atr = float(enriched[atr_col].iloc[-1]) if pd.notna(enriched[atr_col].iloc[-1]) else 0

            atr_values = {symbol: atr} if atr > 0 else {}
            closed = engine.update_prices(prices, atr_values=atr_values)
            for trade in closed:
                send_trade_closed_alert(
                    symbol=trade.symbol, direction=trade.direction,
                    entry=trade.entry_price, exit_price=trade.exit_price,
                    pnl=trade.pnl, pnl_pct=trade.pnl_pct,
                    exit_reason=trade.status,
                )

    # Start WebSocket for crypto (if enabled and crypto symbols exist)
    ws_client = None
    use_ws = settings.WS_ENABLED and not args.no_ws and crypto_symbols

    if use_ws:
        ws_client = BinanceWebSocket(
            symbols=crypto_symbols,
            timeframes=["1h"],
            on_candle_update=on_candle_update,
            on_candle_close=on_candle_close,
            max_reconnect_attempts=settings.WS_RECONNECT_MAX,
        )
        # Start WebSocket in background
        ws_task = asyncio.create_task(ws_client.start())
        log.info("🔌 Binance WebSocket started for %d crypto symbols", len(crypto_symbols))
    else:
        ws_task = None
        log.info("Using polling mode for all symbols")

    cycle = 0
    daily_report_sent_today = False

    while True:
        cycle += 1

        try:
            # For NSE symbols and fallback: run polling cycle
            # Crypto handled by WebSocket candle-close events (if WS active)
            polling_symbols = nse_symbols if use_ws else symbols

            if polling_symbols:
                await run_cycle(polling_symbols, engine, cycle)

            # If WebSocket is running but stale, fall back to polling for crypto too
            if ws_client and ws_client.seconds_since_last_message > settings.WS_FALLBACK_TIMEOUT:
                log.warning("WebSocket stale (%.0fs) — polling crypto as fallback",
                            ws_client.seconds_since_last_message)
                send_websocket_alert("disconnected", reconnect_count=0)
                await run_cycle(crypto_symbols, engine, cycle)

            _consecutive_failures = 0  # Reset on success

            # Write health heartbeat
            write_health("alive", {
                "cycle": cycle,
                "capital": engine.capital,
                "ws_connected": ws_client.is_connected if ws_client else False,
                "symbols_loaded": len(_data_manager.symbols),
            })

            # Daily report at market close
            if _should_send_daily_report() and not daily_report_sent_today:
                send_daily_summary(engine.get_stats())
                daily_report_sent_today = True
                log.info("📊 Daily report sent")

            # Reset daily report flag at midnight
            if datetime.now().hour == 0:
                daily_report_sent_today = False

        except Exception as e:
            _consecutive_failures += 1
            backoff = min(2 ** _consecutive_failures, _MAX_BACKOFF)

            log.error("💥 Cycle #%d failed (attempt %d): %s",
                      cycle, _consecutive_failures, e, exc_info=True)

            write_health("error", {"cycle": cycle, "error": str(e)})

            if _consecutive_failures == 1:
                send_health_alert(f"⚠️ Cycle #{cycle} failed: {e}", level="WARNING")
            elif _consecutive_failures >= 5:
                send_health_alert(
                    f"🚨 {_consecutive_failures} consecutive failures. Backing off {backoff}s.",
                    level="ERROR",
                )

            log.info("Backing off %ds before retry...", backoff)
            await asyncio.sleep(backoff)
            continue

        if args.once:
            break

        # Interval between polling cycles (crypto gets real-time via WS)
        interval = args.interval
        if use_ws and not nse_symbols:
            # If only crypto & WS is active, can poll less frequently (just for health)
            interval = max(interval, 60)

        log.info("Next cycle in %ds...", interval)
        await asyncio.sleep(interval)

    # Cleanup
    if ws_client:
        await ws_client.stop()
    if ws_task:
        ws_task.cancel()

    # Final summary
    send_daily_summary(engine.get_stats())


def main():
    parser = argparse.ArgumentParser(description="AI Trading System — Autonomous Runner v3.0")
    parser.add_argument("--symbol", type=str, default=None, help="Single symbol to trade")
    parser.add_argument("--interval", type=int, default=300, help="Polling interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--no-ws", action="store_true", dest="no_ws",
                        help="Disable WebSocket, use polling only")
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
