"""
TradeAI-PRO — Idempotent Signal Identity & Autonomous Decision Persistence.
==========================================================================
Enforces exact-once paper trade execution per signal event.
Prevents duplicate orders across container retries, worker restarts, or race conditions.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from db.connection import SessionLocal
from db.models import AutonomousDecisionRecord
from db.runtime_state import get_runtime_state, set_runtime_state

log = logging.getLogger(__name__)


def generate_signal_id(
    symbol: str,
    strategy: str,
    direction: str,
    candle_time: str | datetime,
    session_date: str,
    version: str = "v1",
) -> str:
    """
    Generate a deterministic, unique signal ID.
    Composition:
      symbol : strategy : direction : candle_time : session_date : version
    """
    sym = symbol.upper().replace("/", "-").replace(".NS", "")
    strat = (strategy or "AUTONOMOUS").upper().strip()
    dir_str = direction.upper().strip()
    c_time = candle_time.isoformat() if isinstance(candle_time, datetime) else str(candle_time)
    s_date = str(session_date).strip()

    raw_key = f"{sym}:{strat}:{dir_str}:{c_time}:{s_date}:{version}"
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
    return f"sig-{sym}-{s_date}-{digest}"


def is_signal_already_executed(signal_id: str) -> bool:
    """Check if a signal has already produced an AUTO_ENTER paper order."""
    if not signal_id:
        return False

    session = SessionLocal()
    try:
        record = session.execute(
            select(AutonomousDecisionRecord).where(
                AutonomousDecisionRecord.signal_id == signal_id,
                AutonomousDecisionRecord.action == "AUTO_ENTER",
            )
        ).scalar_one_or_none()
        return record is not None
    except Exception as exc:
        log.warning("[Idempotency] Failed to check signal existence for %s: %s", signal_id, exc)
        return False
    finally:
        session.close()


def record_autonomous_decision_to_db(
    signal_id: str,
    run_id: str,
    symbol: str,
    action: str,
    direction: Optional[str] = None,
    entry_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    target_price: Optional[float] = None,
    quality_score: Optional[float] = None,
    confluence_score: Optional[float] = None,
    risk_reward: Optional[float] = None,
    market_regime: Optional[str] = None,
    source_freshness: Optional[str] = None,
    paper_trade_id: Optional[int] = None,
    reason: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """
    Persist an autonomous decision to both:
      1. Relational database (autonomous_decision_records)
      2. Memory/Cache key-value store (runtime_state autonomous_feed:v1)
    """
    now_utc = datetime.now(timezone.utc)
    now_naive = now_utc.replace(tzinfo=None)
    meta_json = json.dumps(meta or {})

    # 1. Database record
    session = SessionLocal()
    try:
        with session.begin():
            # Check for existing duplicate signal_id
            existing = session.execute(
                select(AutonomousDecisionRecord).where(AutonomousDecisionRecord.signal_id == signal_id)
            ).scalar_one_or_none()

            if existing is None:
                record = AutonomousDecisionRecord(
                    signal_id=signal_id,
                    run_id=run_id,
                    timestamp=now_naive,
                    symbol=symbol,
                    action=action,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    target_price=target_price,
                    quality_score=quality_score,
                    confluence_score=confluence_score,
                    risk_reward=risk_reward,
                    market_regime=market_regime,
                    source_freshness=source_freshness,
                    paper_trade_id=paper_trade_id,
                    reason=reason,
                    meta_json=meta_json,
                )
                session.add(record)
            elif action in ("MARKET_CLOSED", "SYSTEM_STANDBY"):
                existing.timestamp = now
                existing.run_id = run_id
                existing.reason = reason
    except Exception as exc:
        log.warning("[Idempotency] Database persist error for decision %s: %s", signal_id, exc)
    finally:
        session.close()

    # 2. Runtime state feed update
    feed_entry = {
        "id": signal_id,
        "run_id": run_id,
        "timestamp": now_utc.isoformat(),
        "action": action,
        "symbol": symbol,
        "direction": direction or "HOLD",
        "price": round(entry_price, 2) if entry_price is not None else 0.0,
        "qs_score": round(quality_score, 1) if quality_score is not None else 0.0,
        "ai_confidence": round(confluence_score, 1) if confluence_score is not None else 0.0,
        "risk_reward": round(risk_reward, 2) if risk_reward is not None else 0.0,
        "market_regime": market_regime or "UNKNOWN",
        "source_freshness": source_freshness or "UNKNOWN",
        "trade_id": paper_trade_id,
        "reason": reason or "",
        "asset_class": "CRYPTO" if "/" in symbol else "EQUITY",
    }

    try:
        feed = get_runtime_state("autonomous_feed:v1", []) or []
        # Deduplicate consecutive MARKET_CLOSED / SYSTEM_STANDBY entries
        if feed and feed[0].get("action") in ("MARKET_CLOSED", "SYSTEM_STANDBY") and action in ("MARKET_CLOSED", "SYSTEM_STANDBY"):
            feed[0]["timestamp"] = now_utc.isoformat()
            feed[0]["reason"] = reason or feed[0].get("reason", "")
        else:
            feed.insert(0, feed_entry)
            if len(feed) > 100:
                feed = feed[:100]
        set_runtime_state("autonomous_feed:v1", feed)
    except Exception as exc:
        log.debug("[Idempotency] Runtime state feed persist error: %s", exc)

    return feed_entry
