"""
SQLite OHLCV cache helpers.

This module provides a lightweight cache path for market data fetchers:
1) Read cached candles quickly.
2) Determine latest cached timestamp for incremental fetch.
3) Upsert new candles from external sources.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from config.settings import DATABASE_URL


def _sqlite_db_path() -> Path:
    """Resolve sqlite path from SQLAlchemy DATABASE_URL."""
    # Supported examples:
    # sqlite:///./trading.db
    # sqlite:////absolute/path/trading.db
    if not DATABASE_URL.startswith("sqlite:///"):
        return Path("trading.db")
    raw = DATABASE_URL.replace("sqlite:///", "", 1)
    return Path(raw).resolve()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_sqlite_db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_ohlcv_cache_table() -> None:
    """Create cache table/index if missing (safe to call repeatedly)."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ohlcv_cache (
                symbol     TEXT NOT NULL,
                timeframe  TEXT NOT NULL,
                timestamp  TEXT NOT NULL,
                open       REAL,
                high       REAL,
                low        REAL,
                close      REAL,
                volume     REAL,
                source     TEXT DEFAULT 'yfinance',
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, timeframe, timestamp)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ohlcv_sym_tf
            ON ohlcv_cache(symbol, timeframe, timestamp)
            """
        )
        conn.commit()


def get_latest_timestamp(symbol: str, timeframe: str) -> Optional[datetime]:
    """Return latest cached candle timestamp for a symbol/timeframe."""
    ensure_ohlcv_cache_table()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT MAX(timestamp) AS ts
            FROM ohlcv_cache
            WHERE symbol = ? AND timeframe = ?
            """,
            (symbol, timeframe),
        ).fetchone()
    ts = row["ts"] if row else None
    if not ts:
        return None
    try:
        return pd.to_datetime(ts, errors="coerce", format="mixed").to_pydatetime()
    except Exception:
        return None


def upsert_ohlcv(
    symbol: str,
    timeframe: str,
    df: pd.DataFrame,
    source: str = "yfinance",
) -> int:
    """Upsert OHLCV candles into cache and return written row count."""
    if df is None or df.empty:
        return 0
    ensure_ohlcv_cache_table()

    rows = []
    for _, r in df.iterrows():
        ts = pd.to_datetime(r["timestamp"]).isoformat()
        fetched_at = datetime.utcnow().isoformat()
        rows.append(
            (
                symbol,
                timeframe,
                ts,
                float(r.get("open", 0.0)),
                float(r.get("high", 0.0)),
                float(r.get("low", 0.0)),
                float(r.get("close", 0.0)),
                float(r.get("volume", 0.0)),
                source,
                fetched_at,
            )
        )

    for attempt in range(3):
        try:
            with _connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO ohlcv_cache (
                        symbol, timeframe, timestamp, open, high, low, close, volume, source, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, timeframe, timestamp)
                    DO UPDATE SET
                        open=excluded.open,
                        high=excluded.high,
                        low=excluded.low,
                        close=excluded.close,
                        volume=excluded.volume,
                        source=excluded.source,
                        fetched_at=excluded.fetched_at
                    """,
                    rows,
                )
                conn.commit()
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 2:
                raise
            time.sleep(0.15 * (attempt + 1))

    return len(rows)


def period_to_since_dt(period: str) -> Optional[datetime]:
    """Convert yfinance-style period string to a lower-bound datetime."""
    now = datetime.now()
    p = (period or "").strip().lower()
    if not p:
        return None

    try:
        if p.endswith("d"):
            return now - timedelta(days=int(p[:-1]))
        if p.endswith("mo"):
            return now - timedelta(days=int(p[:-2]) * 30)
        if p.endswith("y"):
            return now - timedelta(days=int(p[:-1]) * 365)
    except Exception:
        return None
    return None


def read_cached_ohlcv(
    symbol: str,
    timeframe: str,
    period: Optional[str] = None,
) -> pd.DataFrame:
    """Read cached candles by symbol/timeframe, optionally bounded by period."""
    ensure_ohlcv_cache_table()
    since_dt = period_to_since_dt(period) if period else None

    query = (
        "SELECT timestamp, open, high, low, close, volume "
        "FROM ohlcv_cache WHERE symbol=? AND timeframe=?"
    )
    args: list[object] = [symbol, timeframe]
    if since_dt is not None:
        query += " AND timestamp >= ?"
        args.append(since_dt.isoformat())
    query += " ORDER BY timestamp ASC"

    with _connect() as conn:
        rows = conn.execute(query, args).fetchall()

    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", format="mixed")
    df = df.dropna(subset=["timestamp"]).reset_index(drop=True)
    return df
