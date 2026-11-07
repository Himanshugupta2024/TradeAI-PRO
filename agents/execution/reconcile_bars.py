"""Dated-bar construction for stop reconciliation.

Kept deliberately free of FastAPI / DB / model imports so replay logic stays
unit-testable and cannot pull the whole application in at import time.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

_TIMESTAMP_CANDIDATES = ("timestamp", "date", "datetime", "Datetime", "time")


def bars_from_frame(df: Any) -> list[dict]:
    """Convert an OHLCV frame into replay bars carrying real datetimes.

    ``fetch_ohlcv`` returns a RangeIndex plus a ``timestamp`` column, so
    ``df.iterrows()`` yields 0, 1, 2… as the index. Treating that as a timestamp
    silently destroys the replay window: integer strings compare "before" every
    ISO date, so the replay either skips every bar or evaluates the whole
    history — which is how positions get closed at prices from months before
    they were opened.

    Returns [] when no trustworthy datetime axis exists. Callers must treat that
    as "cannot replay" rather than "no bars matched".
    """
    if df is None or getattr(df, "empty", True):
        return []

    frame = df
    if not isinstance(frame.index, pd.DatetimeIndex):
        for candidate in _TIMESTAMP_CANDIDATES:
            if candidate in getattr(frame, "columns", []):
                frame = frame.set_index(candidate)
                break

    if not isinstance(frame.index, pd.DatetimeIndex):
        try:
            if pd.api.types.is_numeric_dtype(frame.index.dtype):
                # A RangeIndex/Int64 index is row numbering, not time. Passing it
                # through to_datetime silently yields 1970 epochs.
                log.warning("bars_from_frame: numeric index is not a datetime axis")
                return []
            frame = frame.copy()
            frame.index = pd.to_datetime(frame.index, errors="raise")
        except Exception:
            log.warning("bars_from_frame: frame has no usable datetime axis")
            return []

    if len(frame.index) and frame.index[0].year < 1990:
        log.warning("bars_from_frame: implausible timestamps (%s) — refusing", frame.index[0])
        return []

    bars: list[dict] = []
    for ts, row in frame.iterrows():
        try:
            bars.append(
                {
                    "timestamp": ts.isoformat(),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue

    # Replay assumes chronological order; enforce it rather than trusting input.
    bars.sort(key=lambda b: b["timestamp"])
    return bars
