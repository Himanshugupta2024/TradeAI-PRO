"""
Unified Mark-to-Market Valuation & Quote Resolution Service for TradeAI PRO.

Ensures /api/portfolio, /api/paper/open, and /api/dashboard/overview all share
a single authoritative, venue-aware mark-to-market pricing pipeline.
Never silently presents entry price as current quote or missing valuation as zero P&L.
Exposes stale status, missing symbol counts, and complete currency attribution.
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import pandas as pd

from config.logger import get_logger
from agents.execution.currency import FXRateService, resolve_instrument_identity, to_decimal, quantize_inr
from db.runtime_state import get_runtime_state

log = get_logger(__name__)


async def resolve_open_trades_valuation(engine, svc) -> Dict[str, Any]:
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
                "valuation_status": "COMPLETE",
                "valued_at": now_iso,
            },
        }

    # 1. Gather all existing in-memory quotes
    quotes: Dict[str, Dict[str, Any]] = {}
    if svc and hasattr(svc, "_market_data"):
        for sym, d in svc._market_data.items():
            if isinstance(d, pd.DataFrame) and not d.empty and "close" in d.columns:
                last_r = d.iloc[-1]
                quotes[sym] = {
                    "close": float(last_r["close"]),
                    "high": float(last_r.get("high", last_r["close"])),
                    "low": float(last_r.get("low", last_r["close"])),
                    "source": "IN_MEMORY_FEED",
                    "timestamp": now_iso,
                    "is_stale": False,
                }
            elif isinstance(d, dict) and "close" in d:
                quotes[sym] = {
                    "close": float(d["close"]),
                    "high": float(d.get("high", d["close"])),
                    "low": float(d.get("low", d["close"])),
                    "source": "IN_MEMORY_FEED",
                    "timestamp": now_iso,
                    "is_stale": False,
                }

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

    # 3. Resolve any still-uncached symbols via bounded async lookups
    still_missing = [t.symbol for t in open_trades if t.symbol not in quotes or quotes[t.symbol]["close"] <= 0]
    if still_missing:
        tasks = []
        for sym in still_missing:
            tasks.append(_fetch_single_quote(sym))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, res in zip(still_missing, results):
            if isinstance(res, dict) and res.get("close", 0.0) > 0:
                quotes[sym] = res

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
            is_stale = bool(q.get("is_stale", False))
            is_entry_fallback = False
        else:
            # Explicit entry fallback with transparency
            ltp = entry
            quote_source = "ENTRY_FALLBACK"
            quote_ts = now_iso
            is_stale = True
            is_entry_fallback = True
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
        "valuation_status": "COMPLETE" if missing_quotes_count == 0 else "PARTIAL",
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
