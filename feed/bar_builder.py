"""
TradeAI Pro — Real-time Tick Accumulator & 5-Minute Bar Builder.
Maintains rolling in-memory 5-minute OHLCV candles without pandas in the tick path.
Keeps Box C memory strictly under 150-200 MB.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Standard Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))


def bucket_5m(ts: datetime) -> datetime:
    """Bucket timestamp to the nearest 5-minute opening boundary."""
    return ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)


def is_nse_market_hours(ts: datetime) -> bool:
    """
    Check if the timestamp falls within normal NSE cash market trading hours
    (09:15 AM to 03:30 PM IST on weekdays Monday-Friday).
    """
    # 0 = Monday, 4 = Friday, 5 = Saturday, 6 = Sunday
    if ts.weekday() >= 5:
        return False

    h = ts.hour
    m = ts.minute
    if (h == 9 and m >= 15) or (10 <= h < 15) or (h == 15 and m <= 30):
        return True
    return False


class BarBuilder:
    """
    In-memory high-frequency tick accumulator and 5-minute OHLCV bar generator.
    Maintains max 120 rolling 5-minute bars per symbol (~10 trading hours).
    """

    def __init__(self, max_bars: int = 120, enforce_market_hours: bool = True):
        self.max_bars = max_bars
        self.enforce_market_hours = enforce_market_hours
        self._lock = threading.Lock()

        # In-memory stores
        self.ltp: Dict[str, Dict[str, Any]] = {}
        self.bars: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self.max_bars))
        self.day_volume: Dict[str, float] = {}

        # Telemetry
        self.total_ticks: int = 0
        self.last_tick_time: Optional[datetime] = None

    def on_tick(
        self,
        symbol: str,
        price: float,
        volume: Optional[float] = None,
        prev_close: Optional[float] = None,
        tick_time: Optional[datetime] = None,
    ) -> bool:
        """
        Process a single incoming tick.
        Updates latest quote and rolls/updates 5-minute bar.
        Returns True if tick was accepted.
        """
        if not symbol or price <= 0:
            return False

        now = tick_time or datetime.now(IST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)

        vol = float(volume or 0.0)
        c_price = float(prev_close) if prev_close is not None else None

        with self._lock:
            self.total_ticks += 1
            self.last_tick_time = now

            # 1. Always update latest quote / LTP snapshot
            existing_ltp = self.ltp.get(symbol, {})
            self.ltp[symbol] = {
                "lp": price,
                "ts": now.isoformat(),
                "prev_close": c_price or existing_ltp.get("prev_close"),
                "high": max(price, existing_ltp.get("high", price)),
                "low": min(price, existing_ltp.get("low", price)),
                "volume": vol if vol > 0 else existing_ltp.get("volume", 0.0),
            }

            # 2. Gate 5-min bar generation to normal market hours
            if self.enforce_market_hours and not is_nse_market_hours(now):
                return True

            # 3. Bucket into 5-minute bar
            b_ts = bucket_5m(now)
            symbol_bars = self.bars[symbol]

            if not symbol_bars or symbol_bars[-1]["t"] != b_ts:
                # Open a new 5-minute candle
                symbol_bars.append({
                    "t": b_ts,
                    "o": price,
                    "h": price,
                    "l": price,
                    "c": price,
                    "v": 0.0,
                })

            # Update current candle
            cur = symbol_bars[-1]
            if price > cur["h"]:
                cur["h"] = price
            if price < cur["l"]:
                cur["l"] = price
            cur["c"] = price

            # Volume delta logic
            if vol > 0:
                prev_vol = self.day_volume.get(symbol, vol)
                delta = max(0.0, vol - prev_vol)
                cur["v"] += delta
                self.day_volume[symbol] = vol

        return True

    def get_snapshot(self, symbols: List[str]) -> Dict[str, Any]:
        """
        Generate a fast, thread-safe snapshot of quotes and bars for requested symbols.
        """
        want = [s.strip().upper() for s in symbols if s.strip()]
        as_of = datetime.now(IST).isoformat()

        with self._lock:
            quotes = {}
            bars = {}
            stale_symbols = []

            for sym in want:
                q = self.ltp.get(sym)
                if q:
                    quotes[sym] = dict(q)
                else:
                    stale_symbols.append(sym)

                b_list = self.bars.get(sym)
                if b_list:
                    # Format bar timestamp to ISO string
                    bars[sym] = [
                        {**b, "t": b["t"].isoformat()} for b in b_list
                    ]
                else:
                    bars[sym] = []

        return {
            "source": "SHOONYA_LIVE",
            "data_delay_minutes": 0,
            "as_of": as_of,
            "quotes": quotes,
            "bars": bars,
            "stale_symbols": stale_symbols,
        }

    def get_bars(self, symbol: str, limit: int = 120) -> List[Dict[str, Any]]:
        """Retrieve recent 5-minute bars for a specific symbol."""
        sym = symbol.strip().upper()
        with self._lock:
            b_list = self.bars.get(sym)
            if not b_list:
                return []
            slice_bars = list(b_list)[-limit:]
            return [{**b, "t": b["t"].isoformat()} for b in slice_bars]

    def get_stats(self) -> Dict[str, Any]:
        """Telemetry reporting for health probe."""
        with self._lock:
            return {
                "symbols_tracked": len(self.ltp),
                "active_bar_series": len(self.bars),
                "total_ticks": self.total_ticks,
                "last_tick_time": self.last_tick_time.isoformat() if self.last_tick_time else None,
            }
