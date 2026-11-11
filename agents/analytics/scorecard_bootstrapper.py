"""
Scorecard Bootstrapper — Populates TradeJournal with empirical historical bar-replay trades.

Prevents the "Cold Start / Empty Scorecard" problem by simulating forward-walk trade
resolutions on real historical daily bars for NIFTY 50 equities without lookahead bias.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
import pandas as pd

from agents.analytics.trade_journal import SignalOutcome, get_journal

logger = logging.getLogger(__name__)

DEFAULT_BOOTSTRAP_SYMBOLS = [
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "BHARTIARTL",
    "LT",
    "ITC",
    "KOTAKBANK",
]


async def bootstrap_scorecard_from_history(
    symbols: Optional[List[str]] = None,
    min_trades: int = 40,
    force: bool = False,
) -> int:
    """
    Populates TradeJournal with historical trade outcomes using real daily bars.
    Returns the number of newly added resolved trade outcomes.
    """
    journal = get_journal()
    journal._ensure_loaded()

    existing_resolved = sum(1 for r in journal._records if r.outcome in (1, -1, 0))
    if existing_resolved >= min_trades and not force:
        logger.info(
            "Scorecard already has %d resolved trades (>= %d target). Skipping bootstrap.",
            existing_resolved,
            min_trades,
        )
        return 0

    syms = symbols or DEFAULT_BOOTSTRAP_SYMBOLS

    from data.providers.yahoo import YahooProvider
    provider = YahooProvider(timeout=10.0)

    try:
        ohlcv_map = await provider.get_batch_ohlcv(
            syms,
            period="1y",
            interval="1d",
        )
    except Exception as exc:
        logger.warning("Batch OHLCV fetch for scorecard bootstrap failed: %s", exc)
        return 0

    existing_keys = {(r.symbol, r.timestamp) for r in journal._records}
    added = 0

    for sym in syms:
        df = ohlcv_map.get(sym)
        if df is None or df.empty or len(df) < 80:
            continue

        c = df["close"].values
        h = df["high"].values
        l = df["low"].values
        v = df["volume"].values if "volume" in df.columns else np.ones(len(c))

        # Compute EMA20, EMA50, ATR14
        close_s = pd.Series(c)
        ema20 = close_s.ewm(span=20, adjust=False).mean().values
        ema50 = close_s.ewm(span=50, adjust=False).mean().values

        # Compute ATR14
        high_s = pd.Series(h)
        low_s = pd.Series(l)
        tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        tr = np.insert(tr, 0, h[0] - l[0])
        atr14 = pd.Series(tr).rolling(14, min_periods=1).mean().values

        # Compute RSI14
        delta = close_s.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14, min_periods=1).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14, min_periods=1).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - (100 / (1 + rs))).fillna(50.0).values

        # Scan historical setup bars from index 50 to len - 12 (to allow forward walk)
        n_bars = len(c)
        scan_indices = range(50, n_bars - 12, 4)  # Step every 4 bars to prevent overlapping clusters

        for idx in scan_indices:
            curr_c = c[idx]
            curr_ema20 = ema20[idx]
            curr_ema50 = ema50[idx]
            curr_atr = atr14[idx]
            curr_rsi = rsi[idx]

            if curr_atr <= 0:
                continue

            # Determine setup
            signal = None
            regime = "TRENDING" if abs(curr_ema20 - curr_ema50) / curr_c > 0.015 else "RANGING"

            if curr_ema20 > curr_ema50 and 50.0 <= curr_rsi <= 68.0 and curr_c >= curr_ema20:
                signal = "LONG"
                entry = round(h[idx] * 1.001, 2)
                risk = 1.5 * curr_atr
                sl = round(entry - risk, 2)
                tp = round(entry + 2.0 * risk, 2)  # 2R Target
            elif curr_ema20 < curr_ema50 and 32.0 <= curr_rsi <= 50.0 and curr_c <= curr_ema20:
                signal = "SHORT"
                entry = round(l[idx] * 0.999, 2)
                risk = 1.5 * curr_atr
                sl = round(entry + risk, 2)
                tp = round(entry - 2.0 * risk, 2)  # 2R Target

            if not signal:
                continue

            # Extract bar timestamp
            if "timestamp" in df.columns:
                bar_ts = pd.to_datetime(df["timestamp"].iloc[idx], utc=True).isoformat()
            else:
                bar_ts = f"2026-HIST-BAR-{sym}-{idx}"

            if (sym, bar_ts) in existing_keys:
                continue

            # Forward-walk up to 10 bars to evaluate outcome
            outcome = 0
            realized_r = 0.0
            false_reason = ""
            max_favorable_r = 0.0
            max_adverse_r = 0.0

            forward_slice_h = h[idx + 1 : idx + 11]
            forward_slice_l = l[idx + 1 : idx + 11]

            for step in range(len(forward_slice_h)):
                bar_h = forward_slice_h[step]
                bar_l = forward_slice_l[step]

                if signal == "LONG":
                    step_fav = (bar_h - entry) / risk
                    step_adv = (entry - bar_l) / risk
                    max_favorable_r = max(max_favorable_r, step_fav)
                    max_adverse_r = max(max_adverse_r, step_adv)

                    if bar_h >= tp:
                        outcome = 1
                        realized_r = 2.0
                        break
                    elif bar_l <= sl:
                        outcome = -1
                        realized_r = -1.0
                        false_reason = "POOR_SR_POSITION" if step <= 2 else "WEAK_MOMENTUM"
                        break
                else:  # SHORT
                    step_fav = (entry - bar_l) / risk
                    step_adv = (bar_h - entry) / risk
                    max_favorable_r = max(max_favorable_r, step_fav)
                    max_adverse_r = max(max_adverse_r, step_adv)

                    if bar_l <= tp:
                        outcome = 1
                        realized_r = 2.0
                        break
                    elif bar_h >= sl:
                        outcome = -1
                        realized_r = -1.0
                        false_reason = "POOR_SR_POSITION" if step <= 2 else "WEAK_MOMENTUM"
                        break

            if outcome == 0:
                false_reason = "NO_FOLLOW_THROUGH"
                realized_r = 0.0

            conf = 70.0 + (curr_rsi - 50.0) if signal == "LONG" else 70.0 + (50.0 - curr_rsi)
            conf = float(np.clip(conf, 55.0, 85.0))
            q_grade = "A" if conf >= 75.0 else "B"

            rec = SignalOutcome(
                symbol=sym,
                timestamp=bar_ts,
                signal=signal,
                quality_score=int(conf),
                quality_grade=q_grade,
                regime=regime,
                hour_ist=10,
                confidence=conf,
                data_quality=100.0,
                outcome=outcome,
                false_reason=false_reason if outcome != 1 else "",
                mfe_r=round(float(max_favorable_r), 2),
                mae_r=round(float(max_adverse_r), 2),
                realized_r=realized_r,
            )

            journal._records.append(rec)
            existing_keys.add((sym, bar_ts))
            added += 1

            if len(journal._records) >= min_trades + 20:
                break

    if added > 0:
        journal._save()
        logger.info("Bootstrapped TradeJournal with %d historical resolved trades", added)

    return added
