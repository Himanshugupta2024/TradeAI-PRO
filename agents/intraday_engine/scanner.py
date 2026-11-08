"""
Deterministic Intraday Opportunity Scanning Pipeline.

Execution Flow:
  1. Indian Market Session & Holiday Verification (09:15-15:30 IST)
  2. Data Freshness & Quality Gate (validates 5m bar structure & age)
  3. Real-Time NIFTY 50 Market Regime Detection (VWAP & Direction)
  4. Multi-Strategy Technical Scanning (ORB, VWAP, CPR, Supertrend, EMA, RSI, Momentum, Gap-Go)
  5. Multi-Strategy Confluence Agreement & Scoring
  6. Transparent 0-100 Multi-Factor Confluence Decomposition
  7. Risk/Reward Validation (ATR-sized SL/TP, min R:R >= 1.5)
  8. Expiration & Validity Assignment (15-20 min lifecycle)
  9. Deterministic Ranking with 'Why Ranked #1' Explanations
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from agents.intraday_engine.confluence_model import calculate_confluence_breakdown
from agents.intraday_engine.fetcher import fetch_5m
from agents.intraday_engine.risk_manager import (
    can_take_trade,
    is_entry_cutoff_active,
    should_square_off_now,
)
from agents.intraday_engine.session import evaluate_market_session
from agents.intraday_engine.strategies import STRATS, score_confluence
from agents.intraday_engine.types import IntradaySignal
from config import settings

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

CORE_FNO_SYMBOLS = [
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "TCS",
    "ITC",
    "KOTAKBANK",
    "LT",
    "SBIN",
    "BHARTIARTL",
    "AXISBANK",
    "BAJFINANCE",
    "TATAMOTORS",
    "MARUTI",
    "SUNPHARMA",
    "TITAN",
    "TATASTEEL",
    "NTPC",
    "POWERGRID",
    "M&M",
]


def _latest_bar_age_minutes(df: pd.DataFrame) -> Optional[float]:
    if df is None or df.empty or "timestamp" not in df.columns:
        return None
    ts = pd.to_datetime(df["timestamp"], errors="coerce").dropna()
    if ts.empty:
        return None
    last_ts = ts.max()

    if getattr(last_ts, "tz", None) is None:
        now = pd.Timestamp(datetime.now(_IST).replace(tzinfo=None))
    else:
        now = pd.Timestamp.now(tz=last_ts.tz)

    return float((now - last_ts).total_seconds() / 60.0)


def _is_fresh_intraday(df: pd.DataFrame) -> bool:
    max_age = int(getattr(settings, "INTRADAY_MAX_BAR_AGE_MINUTES", 120) or 0)
    if max_age <= 0:
        return True
    age_mins = _latest_bar_age_minutes(df)
    if age_mins is None:
        return False
    return age_mins <= float(max_age)


def _init_portfolio_state(portfolio_state: Optional[dict]) -> dict:
    state = dict(portfolio_state or {})
    state.setdefault("open_trades", 0)
    state.setdefault("daily_pnl_pct", 0.0)
    state.setdefault("used_capital_pct", 0.0)
    state.setdefault(
        "capital",
        float(getattr(settings, "INTRADAY_VIRTUAL_CAPITAL", 100000.0) or 100000.0),
    )
    return state


_nifty_context_cache: Dict[str, Any] = {"data": None, "timestamp": 0.0}
_NIFTY_CONTEXT_TTL = 60.0  # 60 seconds cache

def get_nifty_market_context() -> Dict[str, Any]:
    """Fetch live NIFTY 50 quote and evaluate intraday market regime with 60s bounded cache."""
    now_ts = time.time()
    cached = _nifty_context_cache.get("data")
    if cached is not None and (now_ts - _nifty_context_cache.get("timestamp", 0.0)) < _NIFTY_CONTEXT_TTL:
        return cached

    try:
        from agents.data_ingestion.nse_live_quote import get_nse_live_quote

        quote = get_nse_live_quote("NIFTY 50")
        if quote is None:
            return {
                "symbol": "NIFTY 50",
                "ltp": None,
                "vwap": None,
                "change_pct": 0.0,
                "trend": "NEUTRAL",
                "regime": "RANGE",
                "source": "unavailable",
            }

        is_bullish = quote.ltp > quote.vwap and quote.change_pct > 0.15
        is_bearish = quote.ltp < quote.vwap and quote.change_pct < -0.15
        trend = "BULLISH" if is_bullish else ("BEARISH" if is_bearish else "NEUTRAL")

        regime = "TRENDING_UP" if (trend == "BULLISH" and quote.change_pct > 0.5) else (
            "TRENDING_DOWN" if (trend == "BEARISH" and quote.change_pct < -0.5) else (
                "TREND" if trend != "NEUTRAL" else "RANGE"
            )
        )

        res = {
            "symbol": "NIFTY 50",
            "ltp": quote.ltp,
            "vwap": quote.vwap,
            "change_pct": quote.change_pct,
            "trend": trend,
            "regime": regime,
            "source": quote.source,
            "rvol": quote.rvol,
        }
        _nifty_context_cache["data"] = res
        _nifty_context_cache["timestamp"] = now_ts
        return res
    except Exception as exc:
        logger.debug("Failed to get NIFTY market context: %s", exc)
        return {
            "symbol": "NIFTY 50",
            "ltp": None,
            "vwap": None,
            "change_pct": 0.0,
            "trend": "NEUTRAL",
            "regime": "RANGE",
            "source": "error",
            "rvol": 1.0,
        }


def scan_intraday_setups(
    symbols: Iterable[str] | None, portfolio_state: dict | None = None
) -> list[dict]:
    """
    Master pipeline: Scans intraday tactical setups across tracked symbols.
    Returns ranked, validated, non-stale signals with transparent confluence factor breakdowns.
    """
    session = evaluate_market_session()

    # Enforce strict market session if configured
    if getattr(settings, "INTRADAY_ENFORCE_MARKET_HOURS", False) and not session.is_trading_active:
        logger.info("intraday scan skipped: market session inactive (%s)", session.session_label)
        return []

    if getattr(settings, "INTRADAY_ENFORCE_ENTRY_CUTOFF", False) and is_entry_cutoff_active():
        logger.info("intraday scan skipped: after 14:30 entry cutoff")
        return []

    if should_square_off_now():
        logger.info("intraday scan skipped: square-off window active")
        return []

    if symbols is None:
        try:
            from api import service as svc

            extra = [
                s
                for s in (getattr(svc, "TOP_NSE_SYMBOLS", []) or [])
                if s not in CORE_FNO_SYMBOLS
            ]
            target_symbols = list(CORE_FNO_SYMBOLS) + extra[:10]
        except Exception:
            target_symbols = list(CORE_FNO_SYMBOLS)
    else:
        target_symbols = list(symbols)

    if not target_symbols:
        return []

    nifty_ctx = (
        get_nifty_market_context()
        if getattr(settings, "INTRADAY_INDEX_CONFLUENCE_GATE", True)
        else {}
    )
    nifty_trend = nifty_ctx.get("trend", "NEUTRAL")

    state = _init_portfolio_state(portfolio_state)
    raw_candidates: list[IntradaySignal] = []
    seen: set[tuple[str, str]] = set()

    now_ist = datetime.now(_IST)
    now_ist_str = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
    valid_until_dt = now_ist + timedelta(minutes=20)
    valid_until_str = valid_until_dt.strftime("%Y-%m-%d %H:%M:%S IST")

    for sym in target_symbols:
        s = str(sym or "").strip()
        if not s:
            continue

        raw_df = fetch_5m(s)
        if raw_df is None or raw_df.empty:
            continue

        if not _is_fresh_intraday(raw_df):
            logger.debug("intraday %s skipped: stale 5m bars", s)
            continue

        data_status = "DELAYED"

        if getattr(settings, "INTRADAY_LIVE_FEED_ENABLED", True):
            try:
                from agents.data_ingestion.nse_live_quote import (
                    get_nse_live_quote,
                    validate_quote_for_trade,
                )
                from agents.intraday_engine.fetcher import merge_live_candle_overlay

                quote = get_nse_live_quote(s)
                if quote is not None:
                    is_eligible, reason = validate_quote_for_trade(quote, trade_type="INTRADAY")
                    if is_eligible:
                        df = merge_live_candle_overlay(raw_df, quote)
                        data_status = "LIVE"
                    else:
                        logger.info(
                            "intraday %s blocked by staleness gate: %s (source=%s, freshness=%s)",
                            s,
                            reason,
                            getattr(quote, "source", None),
                            getattr(quote, "freshness_seconds", None),
                        )
                        continue
                else:
                    logger.debug("intraday %s skipped: no live quote available", s)
                    continue
            except Exception as exc:
                logger.debug("intraday live overlay fallback error for %s: %s", s, exc)
                continue
        else:
            if getattr(settings, "INTRADAY_REQUIRE_LIVE_FEED", True):
                logger.info(
                    "intraday %s blocked: live feed is disabled and live quotes are required for trade signals (fail-closed)",
                    s,
                )
                continue
            df = raw_df

        if df is None or df.empty:
            continue

        symbol_signals: list[IntradaySignal] = []
        for fn in STRATS:
            try:
                sig = fn(s, df)
            except Exception as exc:
                logger.debug("intraday %s %s: %s", s, fn.__name__, exc)
                continue

            if sig is None or sig.direction in ("SKIP", "HOLD"):
                continue

            # Index Confluence Gate
            if getattr(settings, "INTRADAY_INDEX_CONFLUENCE_GATE", True):
                if sig.direction == "LONG" and nifty_trend == "BEARISH":
                    logger.debug("intraday %s LONG blocked by NIFTY BEARISH regime", s)
                    continue
                elif sig.direction == "SHORT" and nifty_trend == "BULLISH":
                    logger.debug("intraday %s SHORT blocked by NIFTY BULLISH regime", s)
                    continue

            key = (sig.symbol, sig.strategy)
            if key in seen:
                continue
            seen.add(key)

            sig.data_status = data_status
            sig.session_state = session.session_state
            sig.generated_at = now_ist_str
            sig.valid_until = valid_until_str

            symbol_signals.append(sig)

        # Multi-strategy agreement within symbol
        enhanced = score_confluence(symbol_signals)
        raw_candidates.extend(enhanced)

    # Calculate multi-factor Confluence Breakdown & enrich each candidate
    enriched_signals: list[dict] = []
    for sig in raw_candidates:
        allowed, reason = can_take_trade(state)
        if not allowed:
            logger.debug("intraday %s blocked by portfolio guardrail: %s", sig.symbol, reason)
            continue

        conf_data = calculate_confluence_breakdown(
            symbol=sig.symbol,
            strategy=sig.strategy,
            direction=sig.direction,
            raw_strategy_score=sig.score,
            tech_snapshot=sig.technical_snapshot or {},
            rvol=sig.rvol,
            nifty_context=nifty_ctx,
            risk_reward=sig.risk_reward or 1.5,
            agreeing_strategies=[sig.strategy],
        )

        sig.confluence_score = conf_data["confluence_score"]
        sig.classification = conf_data["classification"]
        sig.factors = conf_data["factors"]
        sig.reasons = conf_data["reasons"]
        sig.risks = conf_data["risks"]
        sig.why_ranked = conf_data["why_ranked"]

        payload = sig.as_dict()
        enriched_signals.append(payload)

        state["open_trades"] = int(state.get("open_trades", 0) or 0) + 1
        cap = float(state.get("capital", 0.0) or 0.0)
        qty = int(payload.get("qty") or 0)
        entry = float(payload.get("entry") or 0.0)
        if cap > 0 and qty > 0 and entry > 0:
            used = float(state.get("used_capital_pct", 0.0) or 0.0)
            state["used_capital_pct"] = used + (qty * entry) / cap

    # Rank by Confluence Score descending
    enriched_signals.sort(key=lambda x: float(x.get("confluence_score") or x.get("score") or 0), reverse=True)

    # Assign sequential Rank (1, 2, 3...)
    for idx, s_payload in enumerate(enriched_signals, start=1):
        s_payload["rank"] = idx

    return enriched_signals
