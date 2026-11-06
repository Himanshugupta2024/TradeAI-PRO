"""
Unified Mark-to-Market Valuation & Quote Resolution Service for TradeAI PRO.

Ensures /api/portfolio, /api/paper/open, and /api/dashboard/overview all share
a single authoritative, venue-aware mark-to-market pricing pipeline.
Never silently presents entry price as current quote or missing valuation as zero P&L.
Exposes stale status, missing symbol counts, and complete currency attribution.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
import pandas as pd

from config.logger import get_logger
from agents.execution.currency import FXRateService, resolve_instrument_identity, to_decimal, quantize_inr
from db.runtime_state import get_runtime_state

log = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Freshness budgets. During the NSE session a 30-minute-old tick is stale;
# after the close the last session's bar is legitimately the newest data, so
# the budget widens instead of flagging every position as stale overnight.
NSE_OPEN_MAX_AGE_SEC = 30 * 60
NSE_CLOSED_MAX_AGE_SEC = 18 * 60 * 60
NSE_WEEKEND_MAX_AGE_SEC = 90 * 60 * 60
CRYPTO_MAX_AGE_SEC = 90 * 60
MAX_STALE_REFETCH = 8


def _coerce_timestamp(value):
    """Parse a bar timestamp defensively; naive values are assumed IST."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value


def _timestamp_iso(value):
    parsed = _coerce_timestamp(value)
    return parsed.isoformat() if parsed else None


def _quote_age_seconds(timestamp, now=None):
    parsed = _coerce_timestamp(timestamp)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return max((reference - parsed).total_seconds(), 0.0)


def _stale_threshold_seconds(symbol, now=None):
    if "/" in str(symbol):
        return CRYPTO_MAX_AGE_SEC
    reference = (now or datetime.now(timezone.utc)).astimezone(IST)
    minutes = reference.hour * 60 + reference.minute
    if reference.weekday() < 5 and (9 * 60 + 15) <= minutes <= (15 * 60 + 30):
        return NSE_OPEN_MAX_AGE_SEC
    if reference.weekday() >= 5:
        return NSE_WEEKEND_MAX_AGE_SEC
    return NSE_CLOSED_MAX_AGE_SEC


def _assess_quote(symbol, close, high, low, source, timestamp, now=None):
    """Build a quote dict with a *real* age and freshness verdict."""
    age = _quote_age_seconds(timestamp, now=now)
    stale = age is not None and age > _stale_threshold_seconds(symbol, now=now)
    return {
        "close": close,
        "high": high,
        "low": low,
        "source": source,
        "timestamp": _timestamp_iso(timestamp) or (now or datetime.now(timezone.utc)).isoformat(),
        "age_seconds": round(age, 1) if age is not None else None,
        "is_verified": age is not None,
        "is_stale": bool(stale),
    }


async def resolve_open_trades_valuation(engine, svc, *, refresh_stale: bool = False) -> Dict[str, Any]:
    """
    Compute unified mark-to-market valuation across all open positions.
    Reused identically across /api/paper/open, /api/portfolio, and /api/dashboard/overview.
    """
    open_trades = list(engine.open_trades) if engine else []
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    if not open_trades:
        cap = float(engine.capital) if engine else 100000.0
        init_cap = float(engine.initial_capital) if engine else 100000.0
        return {
            "open_trades": [],
            "summary": {
                "initial_capital": round(init_cap, 2),
                "cash_capital": round(cap, 2),
                "total_equity": round(cap, 2),
                "unrealized_pnl": 0.0,
                "realized_pnl": round(float(sum(engine._trade_total_pnl(t) for t in engine.trades if t.status != "OPEN")), 2) if engine else 0.0,
                "deployed_margin": 0.0,
                "free_cash": round(cap, 2),
                "open_positions": 0,
                "missing_quotes_count": 0,
                "stale_quotes_count": 0,
                "unpriced_quotes_count": 0,
                "valuation_status": "COMPLETE",
                "valued_at": now_iso,
            },
        }

    # 1. Gather all existing in-memory quotes, with their real bar timestamps
    quotes: Dict[str, Dict[str, Any]] = {}
    if svc and hasattr(svc, "_market_data"):
        for sym, d in svc._market_data.items():
            if isinstance(d, pd.DataFrame) and not d.empty and "close" in d.columns:
                last_r = d.iloc[-1]
                quotes[sym] = _assess_quote(
                    sym,
                    float(last_r["close"]),
                    float(last_r.get("high", last_r["close"])),
                    float(last_r.get("low", last_r["close"])),
                    "IN_MEMORY_FEED",
                    last_r.get("timestamp") if "timestamp" in d.columns else None,
                    now=now_utc,
                )
            elif isinstance(d, dict) and "close" in d:
                quotes[sym] = _assess_quote(
                    sym,
                    float(d["close"]),
                    float(d.get("high", d["close"])),
                    float(d.get("low", d["close"])),
                    "IN_MEMORY_FEED",
                    d.get("timestamp"),
                    now=now_utc,
                )

    # 2. Check latest market snapshot in runtime_state for missing symbols
    missing_syms = [t.symbol for t in open_trades if t.symbol not in quotes]
    if missing_syms:
        try:
            snap = get_runtime_state("latest_market_snapshot", {}) or {}
            snap_prices = snap.get("prices", {})
            for sym in missing_syms:
                if sym in snap_prices:
                    sp = snap_prices[sym]
                    quotes[sym] = {
                        "close": float(sp.get("close", 0.0)),
                        "high": float(sp.get("high", sp.get("close", 0.0))),
                        "low": float(sp.get("low", sp.get("close", 0.0))),
                        "source": "RUNTIME_SNAPSHOT",
                        "timestamp": snap.get("timestamp", now_iso),
                        "is_stale": False,
                    }
        except Exception as snap_err:
            log.debug("Snapshot price resolution skipped: %s", snap_err)

    # 3. Resolve missing symbols (and stale ones when the caller asks for a
    #    live refresh) via bounded async lookups. Refreshing is opt-in because
    #    it costs network latency and must not slow down the overview endpoint.
    refresh_syms = [
        t.symbol
        for t in open_trades
        if (
            t.symbol not in quotes
            or quotes[t.symbol]["close"] <= 0
            or (refresh_stale and quotes[t.symbol].get("is_stale"))
        )
    ][:MAX_STALE_REFETCH]
    if refresh_syms:
        async def _guarded_quote(symbol: str):
            try:
                return await asyncio.wait_for(_fetch_single_quote(symbol), timeout=3.0)
            except Exception as exc:
                log.debug("Quote refresh failed for %s: %s", symbol, exc)
                return {}

        results = await asyncio.gather(
            *[_guarded_quote(sym) for sym in refresh_syms],
            return_exceptions=True,
        )
        for sym, res in zip(refresh_syms, results):
            if isinstance(res, dict) and res.get("close", 0.0) > 0:
                quotes[sym] = _assess_quote(
                    sym,
                    float(res["close"]),
                    float(res.get("high", res["close"])),
                    float(res.get("low", res["close"])),
                    str(res.get("source") or "LIVE_FETCH"),
                    res.get("timestamp"),
                    now=now_utc,
                )

    # 4. Perform valuation and enrichment across all open trades
    fx_svc = FXRateService.get_instance()
    enriched_trades = []
    total_unrealized_inr = 0.0
    total_deployed_margin_inr = 0.0
    missing_quotes_count = 0

    for t in open_trades:
        sym = t.symbol
        asset_class, quote_curr, base_curr = resolve_instrument_identity(sym)
        is_long = t.direction in ("LONG", "BUY")
        entry = float(t.entry_price)
        qty = float(t.quantity)

        # Quote resolution check
        q = quotes.get(sym)
        if q and q.get("close", 0.0) > 0:
            ltp = float(q["close"])
            quote_source = q.get("source", "UNKNOWN")
            quote_ts = q.get("timestamp", now_iso)
            quote_age = q.get("age_seconds")
            is_stale = bool(q.get("is_stale", False))
            is_entry_fallback = False
            quote_status = "STALE" if is_stale else ("LIVE" if q.get("is_verified", True) else "UNVERIFIED")
        else:
            # Explicit entry fallback with transparency
            ltp = entry
            quote_source = "ENTRY_FALLBACK"
            quote_ts = now_iso
            quote_age = None
            is_stale = True
            is_entry_fallback = True
            quote_status = "UNPRICED"
            missing_quotes_count += 1

        # Native PnL
        pnl_per_unit_native = (ltp - entry) if is_long else (entry - ltp)
        unrealized_native = pnl_per_unit_native * qty
        unrealized_pct = ((ltp - entry) / entry * 100.0) if entry > 0 else 0.0
        if not is_long:
            unrealized_pct = -unrealized_pct

        # FX Conversion
        try:
            fx_rec = fx_svc.get_fx_rate(quote_curr, base_curr)
            fx_rate = float(fx_rec.rate)
            fx_source = fx_rec.source
        except Exception:
            fx_rate = 1.0 if quote_curr == "INR" else 85.0
            fx_source = "EMERGENCY_FALLBACK"

        unrealized_inr = float(quantize_inr(to_decimal(unrealized_native) * to_decimal(fx_rate)))
        margin_inr = float(quantize_inr(to_decimal(entry * qty) * to_decimal(fx_rate)))

        total_unrealized_inr += unrealized_inr
        total_deployed_margin_inr += margin_inr

        # Trade dict representation
        t_dict = {
            "id": t.id,
            "symbol": sym,
            "direction": "LONG" if is_long else "SHORT",
            "entry_price": entry,
            "stop_loss": float(t.stop_loss),
            "target": float(t.target),
            "entry_time": t.entry_time,
            "quantity": qty,
            "status": "OPEN",
            "exit_price": 0.0,
            "exit_time": "",
            "current_price": ltp,
            "unrealized_pnl": unrealized_inr,
            "unrealized_pnl_native": round(unrealized_native, 6),
            "unrealized_pnl_pct": round(unrealized_pct, 2),
            "asset_class": asset_class,
            "quote_currency": quote_curr,
            "base_currency": base_curr,
            "fx_rate": fx_rate,
            "fx_source": fx_source,
            "quote_source": quote_source,
            "quote_timestamp": quote_ts,
            "is_stale_quote": is_stale,
            "is_entry_fallback": is_entry_fallback,
            "quote_status": quote_status,
            "quote_age_seconds": quote_age,
            "priced": quote_status != "UNPRICED",
            "pnl_status": (
                "UNPRICED" if quote_status == "UNPRICED"
                else ("STALE" if quote_status == "STALE" else "MARKED")
            ),
            "initial_quantity": float(getattr(t, "initial_quantity", qty) or qty),
            "original_target": float(getattr(t, "original_target", t.target) or t.target),
            "target_multiplier": float(getattr(t, "target_multiplier", 1.0) or 1.0),
            "partial_target": float(getattr(t, "partial_target", 0.0) or 0.0),
            "partial_taken": bool(getattr(t, "partial_taken", False)),
            "realized_pnl": float(getattr(t, "realized_pnl", 0.0) or 0.0),
            "signal_type": getattr(t, "signal_type", "SWING"),
            "regime": getattr(t, "regime", "UNKNOWN"),
            "confidence": float(getattr(t, "confidence", 0.0) or 0.0),
        }
        enriched_trades.append(t_dict)

    # Reconciled Summary
    cash = float(engine.capital) if engine else 100000.0
    init_cap = float(engine.initial_capital) if engine else 100000.0
    total_equity = cash + total_unrealized_inr
    closed_trades = [t for t in engine.trades if t.status != "OPEN"] if engine else []
    realized_total = sum(engine._trade_total_pnl(t) for t in closed_trades) if engine else 0.0

    stale_quotes_count = sum(1 for t in enriched_trades if t["quote_status"] == "STALE")
    unpriced_quotes_count = sum(1 for t in enriched_trades if t["quote_status"] == "UNPRICED")

    summary = {
        "initial_capital": round(init_cap, 2),
        "cash_capital": round(cash, 2),
        "total_equity": round(total_equity, 2),
        "unrealized_pnl": round(total_unrealized_inr, 2),
        "realized_pnl": round(realized_total, 2),
        "deployed_margin": round(total_deployed_margin_inr, 2),
        "free_cash": round(max(0.0, cash - total_deployed_margin_inr), 2),
        "open_positions": len(enriched_trades),
        "missing_quotes_count": missing_quotes_count,
        "stale_quotes_count": stale_quotes_count,
        "unpriced_quotes_count": unpriced_quotes_count,
        "valuation_status": (
            "PARTIAL" if missing_quotes_count
            else ("STALE" if stale_quotes_count else "COMPLETE")
        ),
        "valued_at": now_iso,
    }

    return {"open_trades": enriched_trades, "summary": summary}


async def _fetch_single_quote(symbol: str) -> Dict[str, Any]:
    """Helper to fetch a single quote with venue mapping, timeout, and fail-safe handling."""
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        if "/" in symbol:
            if any(fx in symbol for fx in ("EUR", "GBP", "JPY", "AUD", "CAD", "USD", "INR")) and not symbol.endswith("USDT"):
                # Forex
                from agents.data_ingestion.forex_fetcher import get_forex_rate
                rate = await asyncio.to_thread(get_forex_rate, symbol)
                if rate > 0:
                    return {"close": rate, "high": rate, "low": rate, "source": "FOREX_FEED", "timestamp": now_iso, "is_stale": False}
            else:
                # Crypto
                from agents.data_ingestion.binance_fetcher import fetch_historical
                df = await asyncio.to_thread(fetch_historical, symbol, "1h", 2)
                if df is not None and not df.empty:
                    lr = df.iloc[-1]
                    return {
                        "close": float(lr["close"]),
                        "high": float(lr.get("high", lr["close"])),
                        "low": float(lr.get("low", lr["close"])),
                        "source": "BINANCE_FEED",
                        "timestamp": str(lr.get("timestamp", now_iso)),
                        "is_stale": False,
                    }
        else:
            # NSE Equity
            from agents.market_intelligence.nse_fetcher import fetch_ohlcv
            df = await asyncio.to_thread(fetch_ohlcv, symbol, "5d", "1d")
            if df is not None and not df.empty:
                lr = df.iloc[-1]
                return {
                    "close": float(lr["close"]),
                    "high": float(lr.get("high", lr["close"])),
                    "low": float(lr.get("low", lr["close"])),
                    "source": "NSE_YFINANCE_FEED",
                    "timestamp": str(lr.get("timestamp", now_iso)),
                    "is_stale": False,
                }
    except Exception as e:
        log.debug("Quote fetch helper failed for %s: %s", symbol, e)
    return {}
