"""
Binance WebSocket Client — Real-time OHLCV streaming.

Connects to Binance's public kline (candlestick) WebSocket streams
for sub-second price updates. No API key required.

Falls back to CCXT polling if WebSocket connection fails.
"""

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd

from config import settings
from config.logger import get_logger

log = get_logger(__name__)

WS_BASE_URL = "wss://stream.binance.com:9443/ws"

# Map our timeframes to Binance kline intervals
_TF_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


class BinanceWebSocket:
    """
    Async WebSocket client for Binance kline (candlestick) streams.

    Subscribes to multiple symbol/timeframe streams via a single connection.
    Calls user-provided callbacks on each candle update and candle close.

    Usage:
        ws = BinanceWebSocket(
            symbols=["BTC/USDT", "ETH/USDT"],
            timeframes=["1h"],
            on_candle_update=my_update_handler,
            on_candle_close=my_close_handler,
        )
        await ws.start()
    """

    def __init__(
        self,
        symbols: list[str] = None,
        timeframes: list[str] = None,
        on_candle_update=None,
        on_candle_close=None,
        max_reconnect_attempts: int = 50,
    ):
        self.symbols = symbols or settings.CRYPTO_SYMBOLS
        self.timeframes = timeframes or ["1h"]
        self.on_candle_update = on_candle_update
        self.on_candle_close = on_candle_close
        self.max_reconnect_attempts = max_reconnect_attempts

        self._ws = None
        self._running = False
        self._reconnect_count = 0
        self._last_message_time = 0.0

        # Latest candle per symbol/timeframe
        self._current_candles: dict[str, dict[str, dict]] = defaultdict(dict)

    def _build_stream_url(self) -> str:
        """Build the combined stream URL for all symbol/timeframe pairs."""
        streams = []
        for symbol in self.symbols:
            # Convert "BTC/USDT" → "btcusdt"
            ws_symbol = symbol.replace("/", "").lower()
            for tf in self.timeframes:
                binance_tf = _TF_MAP.get(tf, tf)
                streams.append(f"{ws_symbol}@kline_{binance_tf}")

        stream_path = "/".join(streams)
        return f"wss://stream.binance.com:9443/stream?streams={stream_path}"

    def _parse_kline(self, data: dict) -> dict | None:
        """Parse a Binance kline WebSocket message into our candle format."""
        try:
            stream = data.get("stream", "")
            kline_data = data.get("data", {}).get("k", {})
            if not kline_data:
                return None

            # Reconstruct our symbol format from the stream name
            # e.g. "btcusdt@kline_1h" → lookup in symbols
            ws_symbol = stream.split("@")[0].upper()
            symbol = None
            for s in self.symbols:
                if s.replace("/", "") == ws_symbol:
                    symbol = s
                    break

            if not symbol:
                return None

            return {
                "symbol": symbol,
                "timeframe": kline_data.get("i", ""),
                "timestamp": datetime.fromtimestamp(
                    kline_data["t"] / 1000, tz=timezone.utc
                ).replace(tzinfo=None),
                "open": float(kline_data["o"]),
                "high": float(kline_data["h"]),
                "low": float(kline_data["l"]),
                "close": float(kline_data["c"]),
                "volume": float(kline_data["v"]),
                "is_closed": kline_data.get("x", False),
            }
        except (KeyError, ValueError, TypeError) as e:
            log.warning("Failed to parse kline: %s", e)
            return None

    async def start(self):
        """Start the WebSocket connection with auto-reconnection."""
        try:
            import websockets
        except ImportError:
            log.error("websockets library not installed. Run: pip install websockets")
            return

        self._running = True
        url = self._build_stream_url()
        log.info("Connecting to Binance WebSocket: %d streams", len(self.symbols) * len(self.timeframes))

        while self._running and self._reconnect_count < self.max_reconnect_attempts:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    self._reconnect_count = 0
                    log.info("WebSocket connected successfully")

                    async for message in ws:
                        if not self._running:
                            break

                        self._last_message_time = time.time()
                        data = json.loads(message)
                        candle = self._parse_kline(data)

                        if candle is None:
                            continue

                        symbol = candle["symbol"]
                        tf = candle["timeframe"]

                        # Store the latest candle
                        self._current_candles[symbol][tf] = candle

                        # Fire update callback (every tick)
                        if self.on_candle_update:
                            try:
                                await self.on_candle_update(candle)
                            except Exception as e:
                                log.error("Candle update callback error: %s", e)

                        # Fire close callback (candle completed)
                        if candle["is_closed"] and self.on_candle_close:
                            try:
                                await self.on_candle_close(candle)
                            except Exception as e:
                                log.error("Candle close callback error: %s", e)

            except Exception as e:
                self._reconnect_count += 1
                backoff = min(2 ** self._reconnect_count, 60)
                log.warning(
                    "WebSocket disconnected (attempt %d/%d): %s. Reconnecting in %ds...",
                    self._reconnect_count, self.max_reconnect_attempts, e, backoff
                )
                await asyncio.sleep(backoff)

        if self._reconnect_count >= self.max_reconnect_attempts:
            log.error("Max reconnection attempts reached. WebSocket stopped.")
        self._running = False

    async def stop(self):
        """Gracefully stop the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            log.info("WebSocket connection closed")

    def get_latest_candle(self, symbol: str, timeframe: str) -> dict | None:
        """Get the most recent candle data for a symbol/timeframe."""
        return self._current_candles.get(symbol, {}).get(timeframe)

    def get_all_latest(self) -> dict[str, dict[str, dict]]:
        """Get all latest candles. Returns {symbol: {timeframe: candle_dict}}."""
        return dict(self._current_candles)

    @property
    def is_connected(self) -> bool:
        return self._running and self._ws is not None

    @property
    def seconds_since_last_message(self) -> float:
        if self._last_message_time == 0:
            return float("inf")
        return time.time() - self._last_message_time
