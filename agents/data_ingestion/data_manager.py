"""
Central Data Manager — In-memory candle buffer with DB persistence.

Maintains a rolling window of candles per symbol/timeframe.
Receives real-time updates from WebSocket and backfills gaps from CCXT.
Provides a unified interface for the signal pipeline.
"""

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta

import pandas as pd

from config import settings
from config.logger import get_logger
from db.connection import SessionLocal
from db.models import Candle

log = get_logger(__name__)

# Timeframe → expected interval in seconds
_TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# How many candles to keep in memory per symbol/timeframe
MAX_BUFFER_SIZE = 500


class DataManager:
    """
    Centralized candle data store with real-time and historical data.

    - Maintains an in-memory deque of candles per symbol/timeframe
    - Persists completed candles to DB
    - Detects gaps and provides staleness info
    - Thread-safe via asyncio locks
    """

    def __init__(self, buffer_size: int = MAX_BUFFER_SIZE):
        self.buffer_size = buffer_size
        # {symbol: {timeframe: deque of candle dicts}}
        self._buffers: dict[str, dict[str, deque]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=buffer_size))
        )
        # Track the latest candle timestamp per symbol/timeframe
        self._last_timestamps: dict[str, dict[str, datetime]] = defaultdict(dict)
        self._lock = asyncio.Lock()

    async def load_history(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        merge: bool = False,
    ):
        """
        Load historical candles into the buffer.

        Args:
            merge: If True, incoming candles are merged with existing buffer
                   (new timestamps are appended; existing ones are skipped).
                   Use merge=True for gap-fills so they don't overwrite the
                   full buffer with a partial slice.
                   If False (default), the buffer is cleared before loading.
        """
        if df.empty:
            return

        async with self._lock:
            buf = self._buffers[symbol][timeframe]

            if merge and buf:
                # Build a set of existing timestamps for fast dedup
                existing_ts = {c["timestamp"] for c in buf}
            else:
                buf.clear()
                existing_ts = set()

            added = 0
            for _, row in df.iterrows():
                ts = pd.Timestamp(row["timestamp"]).to_pydatetime()
                # Strip tz info for consistent naive-UTC storage
                if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                if ts in existing_ts:
                    continue
                candle = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp": ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "is_closed": True,
                }
                buf.append(candle)
                existing_ts.add(ts)
                added += 1

            if buf:
                # Keep buffer sorted by timestamp after merge
                if merge and added > 0:
                    sorted_candles = sorted(buf, key=lambda c: c["timestamp"])
                    buf.clear()
                    buf.extend(sorted_candles)
                self._last_timestamps[symbol][timeframe] = buf[-1]["timestamp"]
                action = "Merged" if merge else "Loaded"
                log.info(
                    "%s %d historical candles for %s %s (buffer=%d)",
                    action, added, symbol, timeframe, len(buf),
                )

    async def update_candle(self, candle: dict):
        """
        Update the buffer with a real-time candle tick from WebSocket.

        If the candle is still open, update the latest entry in-place.
        If the candle is closed, append it and persist to DB.
        """
        symbol = candle["symbol"]
        tf = candle["timeframe"]

        async with self._lock:
            buf = self._buffers[symbol][tf]
            ts = candle["timestamp"]

            if candle.get("is_closed", False):
                # Completed candle — append and persist
                if buf and buf[-1]["timestamp"] == ts:
                    # Replace the in-progress candle with final version
                    buf[-1] = candle
                else:
                    buf.append(candle)

                self._last_timestamps[symbol][tf] = ts
                self._persist_candle(candle)
            else:
                # In-progress candle — update the last entry or append
                if buf and buf[-1]["timestamp"] == ts:
                    buf[-1] = candle
                else:
                    buf.append(candle)

    def _persist_candle(self, candle: dict):
        """Save a completed candle to the database."""
        try:
            session = SessionLocal()
            existing = (
                session.query(Candle)
                .filter_by(
                    symbol=candle["symbol"],
                    timeframe=candle["timeframe"],
                    timestamp=candle["timestamp"],
                )
                .first()
            )
            if existing:
                existing.open = candle["open"]
                existing.high = candle["high"]
                existing.low = candle["low"]
                existing.close = candle["close"]
                existing.volume = candle["volume"]
            else:
                session.add(Candle(
                    symbol=candle["symbol"],
                    timeframe=candle["timeframe"],
                    timestamp=candle["timestamp"],
                    open=candle["open"],
                    high=candle["high"],
                    low=candle["low"],
                    close=candle["close"],
                    volume=candle["volume"],
                ))
            session.commit()
        except Exception as e:
            log.error("Failed to persist candle %s %s: %s", candle["symbol"], candle["timestamp"], e)
            session.rollback()
        finally:
            session.close()

    def get_dataframe(self, symbol: str, timeframe: str, limit: int = None) -> pd.DataFrame:
        """
        Get candle data as a DataFrame for the signal pipeline.

        Args:
            symbol: e.g. "BTC/USDT"
            timeframe: e.g. "1h"
            limit: optional max number of candles

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
        """
        buf = self._buffers.get(symbol, {}).get(timeframe, deque())
        if not buf:
            return pd.DataFrame()

        candles = list(buf)
        if limit:
            candles = candles[-limit:]

        df = pd.DataFrame(candles)
        df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df.reset_index(drop=True)

    def get_staleness_seconds(self, symbol: str, timeframe: str) -> float:
        """
        How many seconds since the last completed candle.

        Returns float("inf") if no data exists.
        """
        last_ts = self._last_timestamps.get(symbol, {}).get(timeframe)
        if last_ts is None:
            return float("inf")
        # Normalize both sides to naive UTC for a safe comparison
        now = datetime.utcnow()
        ts = last_ts
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)  # stored as naive UTC; strip tz if any
        return (now - ts).total_seconds()

    def is_stale(self, symbol: str, timeframe: str, multiplier: float = 2.0) -> bool:
        """
        Check if data is stale (no candle received for > multiplier * expected interval).
        """
        expected = _TF_SECONDS.get(timeframe, 3600)
        return self.get_staleness_seconds(symbol, timeframe) > expected * multiplier

    def detect_gaps(self, symbol: str, timeframe: str) -> list[tuple[datetime, datetime]]:
        """
        Detect missing candles in the buffer.

        Returns list of (gap_start, gap_end) tuples.
        """
        buf = self._buffers.get(symbol, {}).get(timeframe, deque())
        if len(buf) < 2:
            return []

        expected_interval = timedelta(seconds=_TF_SECONDS.get(timeframe, 3600))
        # Allow 10% tolerance
        max_gap = expected_interval * 1.1

        gaps = []
        candles = list(buf)
        for i in range(1, len(candles)):
            prev_ts = candles[i - 1]["timestamp"]
            curr_ts = candles[i]["timestamp"]
            if isinstance(prev_ts, str):
                prev_ts = pd.Timestamp(prev_ts).to_pydatetime()
            if isinstance(curr_ts, str):
                curr_ts = pd.Timestamp(curr_ts).to_pydatetime()
            delta = curr_ts - prev_ts
            if delta > max_gap:
                gaps.append((prev_ts, curr_ts))

        return gaps

    def get_latest_price(self, symbol: str, timeframe: str = "1h") -> dict | None:
        """Get the latest price data for a symbol."""
        buf = self._buffers.get(symbol, {}).get(timeframe, deque())
        if not buf:
            return None
        candle = buf[-1]
        return {
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "timestamp": candle["timestamp"],
        }

    @property
    def symbols(self) -> list[str]:
        """List all symbols with data."""
        return list(self._buffers.keys())
