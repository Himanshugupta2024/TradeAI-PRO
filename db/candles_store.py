"""
TradeAI-PRO — Asynchronous 5-Minute Candle Store for Supabase & SQLite.
Non-blocking persistence of 5m candles into candles_5m table.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd
from sqlalchemy import text

from db.connection import get_session
from db.models import Candle5m

logger = logging.getLogger(__name__)


def upsert_candles_5m_sync(symbol: str, df: pd.DataFrame, source: str = "YAHOO") -> int:
    """
    Synchronously upsert completed 5m candle bars into candles_5m table.
    Bounded to last 50 completed bars to prevent large transaction blocks.
    """
    if df is None or df.empty or len(df) == 0:
        return 0

    clean_sym = symbol.strip().upper().replace(".NS", "")
    tail_df = df.tail(50).copy()

    records = []
    for _, row in tail_df.iterrows():
        ts_val = row.get("timestamp")
        if ts_val is None or pd.isna(ts_val):
            continue
        if isinstance(ts_val, str):
            ts_dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
        elif hasattr(ts_val, "to_pydatetime"):
            ts_dt = ts_val.to_pydatetime()
        elif isinstance(ts_val, datetime):
            ts_dt = ts_val
        else:
            continue

        records.append({
            "symbol": clean_sym,
            "timestamp": ts_dt,
            "open": float(row.get("open", 0.0)),
            "high": float(row.get("high", 0.0)),
            "low": float(row.get("low", 0.0)),
            "close": float(row.get("close", 0.0)),
            "volume": float(row.get("volume", 0.0)),
            "source": source,
        })

    if not records:
        return 0

    try:
        with get_session() as session:
            # Batch upsert via dialect-aware merge / insert
            for r in records:
                try:
                    existing = session.query(Candle5m).filter_by(
                        symbol=r["symbol"], timestamp=r["timestamp"]
                    ).first()
                    if existing:
                        existing.open = r["open"]
                        existing.high = r["high"]
                        existing.low = r["low"]
                        existing.close = r["close"]
                        existing.volume = r["volume"]
                        existing.source = r["source"]
                    else:
                        new_candle = Candle5m(**r)
                        session.add(new_candle)
                except Exception:
                    continue
            session.commit()
            return len(records)
    except Exception as exc:
        logger.debug("[CandlesStore] Upsert failed for %s: %s", clean_sym, exc)
        return 0


async def persist_candles_5m_async(symbol: str, df: pd.DataFrame, source: str = "YAHOO") -> None:
    """Non-blocking fire-and-forget background worker for 5m candle upsert."""
    try:
        await asyncio.to_thread(upsert_candles_5m_sync, symbol, df, source)
    except Exception as exc:
        logger.debug("[CandlesStore] Async persist error: %s", exc)
