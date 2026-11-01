"""Run intraday heuristics across symbols with optional direction confluence merge."""

from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

from agents.intraday_engine.fetcher import fetch_5m, fetch_5m_with_live_overlay
from agents.intraday_engine.risk_manager import (
    can_take_trade,
    is_entry_cutoff_active,
    should_square_off_now,
)
from agents.intraday_engine.strategies import STRATS, score_confluence
from config import settings

logger = logging.getLogger(__name__)


def _latest_bar_age_minutes(df: pd.DataFrame) -> float | None:
    if df is None or df.empty or "timestamp" not in df.columns:
        return None
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    ts = ts.dropna()
    if ts.empty:
        return None
    last_ts = ts.max()

    from datetime import datetime
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")

    # If last_ts is naive, we assume it's IST (yfinance behavior for NSE).
    if getattr(last_ts, "tz", None) is None:
        # Get current time in IST, make naive to compare
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


def _init_portfolio_state(portfolio_state: dict | None) -> dict:
    state = dict(portfolio_state or {})
    state.setdefault("open_trades", 0)
    state.setdefault("daily_pnl_pct", 0.0)
    state.setdefault("used_capital_pct", 0.0)
    state.setdefault(
        "capital",
        float(getattr(settings, "INTRADAY_VIRTUAL_CAPITAL", 100000.0) or 100000.0),
    )
    return state


def _score_value(payload: dict) -> float:
    try:
        return float(payload.get("score") or 0.0)
    except Exception:
        return 0.0


def _merge_symbol_confluence(signals: list[dict]) -> list[dict]:
    if not signals:
        return []

    enabled = bool(getattr(settings, "INTRADAY_CONFLUENCE_ENABLED", True))
    min_agree = max(1, int(getattr(settings, "INTRADAY_CONFLUENCE_MIN_AGREE", 2) or 2))
    bonus_per_extra = float(
        getattr(settings, "INTRADAY_CONFLUENCE_SCORE_BONUS_PER_EXTRA", 4.0) or 4.0
    )

    if not enabled:
        return sorted(signals, key=_score_value, reverse=True)

    by_direction: dict[str, list[dict]] = {"LONG": [], "SHORT": []}
    for s in signals:
        d = str(s.get("direction") or "").upper()
        if d in by_direction:
            by_direction[d].append(s)

    long_count = len(by_direction["LONG"])
    short_count = len(by_direction["SHORT"])
    if max(long_count, short_count) < min_agree:
        return []

    if long_count >= short_count:
        picked = by_direction["LONG"]
    else:
        picked = by_direction["SHORT"]

    picked = sorted(picked, key=_score_value, reverse=True)
    primary = dict(picked[0])
    count = len(picked)
    strategies = [str(s.get("strategy") or "").upper() for s in picked]
    extra = max(0, count - 1)
    primary["strategy"] = "CONFLUENCE"
    primary["score"] = min(98.0, _score_value(primary) + extra * bonus_per_extra)
    primary["confluence_count"] = count
    primary["confluence_strategies"] = strategies
    base_rationale = str(primary.get("rationale") or "").strip()
    primary["rationale"] = (
        f"{base_rationale} | Intraday confluence: {count} strategies aligned "
        f"({', '.join(strategies)})."
    )
    return [primary]


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


def get_nifty_market_context() -> dict:
    """Fetch live NIFTY 50 quote and evaluate intraday market regime."""
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
                "source": "unavailable",
            }
        is_bullish = quote.ltp > quote.vwap and quote.change_pct > 0.2
        is_bearish = quote.ltp < quote.vwap and quote.change_pct < -0.2
        trend = "BULLISH" if is_bullish else ("BEARISH" if is_bearish else "NEUTRAL")
        return {
            "symbol": "NIFTY 50",
            "ltp": quote.ltp,
            "vwap": quote.vwap,
            "change_pct": quote.change_pct,
            "trend": trend,
            "source": quote.source,
        }
    except Exception as exc:
        logger.debug("Failed to get NIFTY market context: %s", exc)
        return {
            "symbol": "NIFTY 50",
            "ltp": None,
            "vwap": None,
            "change_pct": 0.0,
            "trend": "NEUTRAL",
            "source": "error",
        }


def scan_intraday_setups(
    symbols: Iterable[str] | None, portfolio_state: dict | None = None
) -> list[dict]:
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
    if getattr(settings, "INTRADAY_ENFORCE_ENTRY_CUTOFF", False) and is_entry_cutoff_active():
        logger.info("intraday scan skipped: after 14:30 entry cutoff")
        return []

    nifty_ctx = (
        get_nifty_market_context()
        if getattr(settings, "INTRADAY_INDEX_CONFLUENCE_GATE", True)
        else {}
    )
    nifty_trend = nifty_ctx.get("trend", "NEUTRAL")

    if should_square_off_now():
        logger.info("intraday scan skipped: square-off window active")
        return []

    state = _init_portfolio_state(portfolio_state)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
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

        if getattr(settings, "INTRADAY_LIVE_FEED_ENABLED", True):
            try:
                pass
                if raw_df is not None and not raw_df.empty:
                    from agents.data_ingestion.nse_live_quote import get_nse_live_quote
                    from agents.intraday_engine.fetcher import merge_live_candle_overlay

                    quote = get_nse_live_quote(s)
                    if quote is not None:
                        df = merge_live_candle_overlay(raw_df, quote)
                    else:
                        df = raw_df
                else:
                    df = None
            except Exception as exc:
                logger.debug("intraday live overlay fallback to fetch_5m for %s: %s", s, exc)
                df = raw_df
        else:
            df = raw_df
        if df is None or df.empty:
            continue
        if False and not _is_fresh_intraday(df):
            logger.debug("intraday %s skipped: stale 5m bars", s)
            continue
        raw_signals: list = []
        for fn in STRATS:
            try:
                sig = fn(s, df)
            except Exception as exc:
                logger.debug("intraday %s %s: %s", s, fn.__name__, exc)
                continue
            if sig is None or sig.direction == "SKIP":
                continue

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

            raw_signals.append(sig)

        enhanced = score_confluence(raw_signals)

        for sig in enhanced:
            allowed, reason = can_take_trade(state)
            if not allowed:
                logger.debug("intraday %s blocked by guardrail: %s", s, reason)
                break

            payload = sig.as_dict()
            out.append(payload)

            state["open_trades"] = int(state.get("open_trades", 0) or 0) + 1
            cap = float(state.get("capital", 0.0) or 0.0)
            qty = int(payload.get("qty") or 0)
            entry = float(payload.get("entry") or 0.0)
            if cap > 0 and qty > 0 and entry > 0:
                used = float(state.get("used_capital_pct", 0.0) or 0.0)
                state["used_capital_pct"] = used + (qty * entry) / cap

    out.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    return out
