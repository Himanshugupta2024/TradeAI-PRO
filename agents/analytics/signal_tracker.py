"""Signal Win Rate Tracker for TradeAI PRO.

Logs every signal emitted and scores outcomes 24h later.
Uses existing SQLite/PostgreSQL DB — no new dependencies.

How it works:
1. log_signal()     — called whenever a BUY/SELL/HOLD signal fires
2. score_outcomes() — background job runs every hour, fills price_24h_later
3. get_win_rate()   — API reads stats for dashboard display
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, text

from db.connection import SessionLocal
from db.models import SignalLog

logger = logging.getLogger(__name__)

_WIN_THRESHOLD_PCT  = 3.0  # BUY signal is WIN if price goes up >= 3%
_LOSS_THRESHOLD_PCT = -3.0 # BUY signal is LOSS if price goes down >= 3%


def log_signal(
    symbol: str,
    signal: str,
    price: float,
    confidence: float = 0.0,
    rsi: Optional[float] = None,
    context_score: Optional[float] = None,
) -> None:
    """Log a signal emission. Call this every time a signal is generated."""
    if not symbol or not signal or not price:
        return
    try:
        with SessionLocal() as db:
            db.add(SignalLog(
                symbol=symbol,
                signal=signal,
                confidence=confidence,
                price_at_signal=price,
                rsi_at_signal=rsi,
                context_score=context_score,
                emitted_at=datetime.utcnow(),
            ))
            db.commit()
    except Exception as e:
        logger.debug("SignalLog write failed for %s: %s", symbol, e)


def score_outcomes(current_prices: dict[str, float]) -> int:
    """
    Fill outcome for signals emitted >= 24h ago that are still unscored.
    Pass current_prices as {symbol: price} dict.
    Returns count of signals scored.

    Call this from the background refresh loop (e.g. every 30 min).
    """
    cutoff = datetime.utcnow() - timedelta(hours=24)
    scored = 0
    try:
        with SessionLocal() as db:
            pending = (
                db.query(SignalLog)
                .filter(
                    SignalLog.emitted_at <= cutoff,
                    SignalLog.outcome.is_(None),
                )
                .limit(100)
                .all()
            )

            for row in pending:
                price_now = current_prices.get(row.symbol)
                if price_now is None or row.price_at_signal <= 0:
                    continue

                pct_move = ((price_now - row.price_at_signal) / row.price_at_signal) * 100

                if row.signal == "BUY":
                    outcome = "WIN" if pct_move >= _WIN_THRESHOLD_PCT else (
                        "LOSS" if pct_move <= _LOSS_THRESHOLD_PCT else "NEUTRAL"
                    )
                elif row.signal == "SELL":
                    outcome = "WIN" if pct_move <= _LOSS_THRESHOLD_PCT else (
                        "LOSS" if pct_move >= _WIN_THRESHOLD_PCT else "NEUTRAL"
                    )
                else:
                    outcome = "NEUTRAL"

                row.price_24h_later = price_now
                row.outcome         = outcome
                row.outcome_pct     = round(pct_move, 3)
                row.scored_at       = datetime.utcnow()
                scored += 1

            if scored:
                db.commit()
                logger.info("Scored %d signal outcomes", scored)

    except Exception as e:
        logger.warning("score_outcomes failed: %s", e)

    return scored


def get_win_rate(symbol: Optional[str] = None, last_n: int = 50) -> dict:
    """
    Return win rate stats for one coin or all coins.
    Used by /api/signals/winrate endpoint.
    """
    try:
        with SessionLocal() as db:
            q = db.query(SignalLog).filter(SignalLog.outcome.isnot(None))
            if symbol:
                q = q.filter(SignalLog.symbol == symbol)
            rows = q.order_by(SignalLog.emitted_at.desc()).limit(last_n).all()

            if not rows:
                return {
                    "symbol": symbol or "ALL",
                    "total_signals": 0,
                    "win_rate_pct": None,
                    "avg_outcome_pct": None,
                    "message": "Not enough data yet — needs 24h of live signals",
                }

            wins    = sum(1 for r in rows if r.outcome == "WIN")
            losses  = sum(1 for r in rows if r.outcome == "LOSS")
            neutral = sum(1 for r in rows if r.outcome == "NEUTRAL")
            total   = len(rows)
            decided = wins + losses  # exclude neutral for win rate %

            win_rate = round((wins / decided * 100), 1) if decided > 0 else None

            outcomes_pct = [r.outcome_pct for r in rows if r.outcome_pct is not None]
            avg_outcome  = round(sum(outcomes_pct) / len(outcomes_pct), 2) if outcomes_pct else None

            return {
                "symbol":          symbol or "ALL",
                "total_signals":   total,
                "wins":            wins,
                "losses":          losses,
                "neutral":         neutral,
                "win_rate_pct":    win_rate,
                "avg_outcome_pct": avg_outcome,
                "last_signals": [
                    {
                        "signal":      r.signal,
                        "price":       r.price_at_signal,
                        "outcome":     r.outcome,
                        "outcome_pct": r.outcome_pct,
                        "emitted_at":  r.emitted_at.isoformat() if r.emitted_at else None,
                    }
                    for r in rows[:10]
                ],
            }

    except Exception as e:
        logger.warning("get_win_rate failed: %s", e)
        return {"error": str(e)}


def get_leaderboard(limit: int = 10) -> list[dict]:
    """
    Top coins by win rate (min 5 scored signals needed).
    Used by /api/signals/leaderboard endpoint.
    """
    try:
        with SessionLocal() as db:
            rows = (
                db.query(
                    SignalLog.symbol,
                    func.count(SignalLog.id).label("total"),
                    func.sum(
                        func.cast(SignalLog.outcome == "WIN", type_=func.Integer.__class__)
                    ).label("wins"),
                    func.avg(SignalLog.outcome_pct).label("avg_pct"),
                )
                .filter(SignalLog.outcome.isnot(None))
                .group_by(SignalLog.symbol)
                .having(func.count(SignalLog.id) >= 5)
                .order_by(text("wins DESC"))
                .limit(limit)
                .all()
            )

            return [
                {
                    "symbol":       r.symbol,
                    "total":        r.total,
                    "wins":         int(r.wins or 0),
                    "win_rate_pct": round((int(r.wins or 0) / r.total) * 100, 1) if r.total else None,
                    "avg_pct":      round(float(r.avg_pct or 0), 2),
                }
                for r in rows
            ]
    except Exception as e:
        logger.warning("get_leaderboard failed: %s", e)
        return []
