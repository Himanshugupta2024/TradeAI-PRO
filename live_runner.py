"""
Live Trading Runner.

The main production loop that orchestrates the full pipeline:
  1. Fetch latest candle data (Binance / NSE)
  2. Calculate indicators + S/R + HTF trend
  3. Run AI prediction (LSTM or statistical fallback)
  4. Generate signals
  5. Execute paper trades (or live via broker API)
  6. Send Telegram alerts
  7. Sleep and repeat

Usage:
  python live_runner.py                        # Paper mode, all symbols
  python live_runner.py --symbol BTC/USDT      # Single symbol
  python live_runner.py --interval 300         # 5-minute poll interval
"""

import sys
import os
import time
import argparse
from datetime import datetime

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
from agents.alerts.telegram_bot import send_signal_alert, send_trade_closed_alert

log = get_logger("runner")


def fetch_data(symbol: str, timeframe: str = "1h", days: int = 30):
    """Fetch recent data for a symbol."""
    if symbol in settings.CRYPTO_SYMBOLS:
        return binance_fetcher.fetch_historical(symbol, timeframe, since_days=days)
    elif symbol in settings.NSE_SYMBOLS:
        return nse_fetcher.fetch_historical(symbol, timeframe, since_days=days)
    else:
        log.error("Unknown symbol: %s", symbol)
        return None


def enrich_data(df, htf_df=None):
    """Run the full feature engineering pipeline."""
    df = calculate_indicators(df)
    df = calculate_support_resistance(df, pivot_window=10, lookback=50)

    if htf_df is not None:
        htf_enriched = calculate_indicators(htf_df)
        htf_enriched = classify_trend(htf_enriched)
        df = propagate_trend_to_ltf(df, htf_enriched)
    else:
        df = classify_trend(df)

    return df


def run_cycle(symbol: str, engine: PaperTradingEngine):
    """
    Run one analysis cycle for a symbol.
    Returns the latest signal dict or None.
    """
    log.info("─── %s ───", symbol)

    # Fetch data
    df = fetch_data(symbol, timeframe="1h", days=14)
    if df is None or df.empty:
        return None

    htf_df = fetch_data(symbol, timeframe="1d", days=60)

    # Enrich
    df = enrich_data(df, htf_df)

    # AI prediction (fallback)
    ai_confidence = predict_batch(df)
    df["ai_confidence"] = ai_confidence

    # Generate signals
    df = generate_signals(df, ai_confidence=ai_confidence)

    # Get the latest candle's signal
    last = df.iloc[-1]
    signal = last.get("signal", "HOLD")
    confidence = last.get("confidence", 0)

    log.info(
        "Signal: %s | Conf: %.0f%% | Close: %.2f | RSI: %.1f | Trend: %s",
        signal, confidence, last["close"],
        last.get("RSI_14", 0), last.get("htf_trend", "N/A"),
    )

    if signal in ("LONG", "SHORT"):
        entry = float(last["close"])
        sl = float(last["stop_loss"])
        tp = float(last["target"])
        trend = str(last.get("htf_trend", ""))
        rsi = float(last.get("RSI_14", 0))

        # Execute paper trade
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


def main():
    parser = argparse.ArgumentParser(description="AI Trading System — Live Runner")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Single symbol to trade (default: all configured)")
    parser.add_argument("--interval", type=int, default=300,
                        help="Poll interval in seconds (default: 300 = 5 min)")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle and exit (for testing)")
    args = parser.parse_args()

    init_db()

    symbols = [args.symbol] if args.symbol else settings.CRYPTO_SYMBOLS + settings.NSE_SYMBOLS
    engine = PaperTradingEngine(initial_capital=100_000)

    log.info("═" * 50)
    log.info("TradeAI PRO — Live Runner")
    log.info("Symbols: %s | Interval: %ds | Capital: ₹%.2f",
             ", ".join(symbols), args.interval, engine.capital)
    log.info("═" * 50)

    cycle = 0
    while True:
        cycle += 1
        log.info("━━━ CYCLE #%d — %s ━━━", cycle,
                 datetime.now().strftime("%d %b %Y, %I:%M:%S %p IST"))

        for symbol in symbols:
            try:
                run_cycle(symbol, engine)
            except Exception as e:
                log.error("Error processing %s: %s", symbol, e, exc_info=True)

        stats = engine.get_stats()
        log.info("📊 Capital: ₹%.2f | Trades: %d | Win Rate: %.1f%%",
                 stats["capital"], stats["total_trades"], stats["win_rate"])

        if args.once:
            break

        log.info("Next cycle in %ds...", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
