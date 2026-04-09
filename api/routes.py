"""HTTP and WebSocket route handlers for the trading API."""
import asyncio
import json
import logging
import os
from datetime import datetime
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from config import settings
from db.connection import SessionLocal
from db.models import MorningScan
from agents.feature_engine.indicators import calculate_indicators
from agents.feature_engine.trend import classify_trend, propagate_trend_to_ltf
from agents.feature_engine.regime_detector import detect_regime
from agents.signal_engine.signal_generator import generate_signals
from agents.backtester.engine import Backtester
from agents.market_intelligence.market_data import fetch_fii_dii_flow
from agents.market_intelligence.swing_scanner import scan_all_symbols as swing_scan_all
from agents.market_intelligence.index_mover import calculate_index_movers
from agents.market_intelligence.breakout_scanner import scan_all_symbols as breakout_scan_all
from agents.market_intelligence.nse_fetcher import (
    fetch_ohlcv,
    fetch_nse_gainers_losers,
    fetch_sector_performance,
    search_symbols,
    fetch_arbitrary_symbol,
    validate_nse_symbol,
)
from agents.stock_screener import opportunity_stocks
from agents.data_ingestion.context_fetcher import build_context_fetcher
from agents.analytics.trade_journal import get_journal

from api import service as svc
from api.paths import STATIC_DIR

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/api/health")
async def health_check():
    """Health check endpoint — shows loading progress and server status."""
    return {
        "status": "ok",
        "loading": svc._loading_status,
        "feed": svc._get_feed_status(),
        "symbols_loaded": len(svc._market_data),
        "signals_cached": len(svc._signal_cache),
        "last_refresh": svc._last_refresh.isoformat() if svc._last_refresh != datetime.min else None,
        "uptime": datetime.now().isoformat(),
    }


@router.get("/api/signals/all")
async def get_all_signals():
    """Get the latest signal for every tracked symbol."""
    # Lazy bootstrap: if startup is still warming up and cache is empty,
    # trigger one background refresh so the dashboard does not stay blank.
    if not svc._signal_cache and not svc._lazy_bootstrap_started:
        lock = svc._get_refresh_lock()
        if not lock.locked():
            svc._lazy_bootstrap_started = True

            async def _bootstrap_once():
                try:
                    await svc._refresh_market_data()
                except Exception as e:
                    logger.warning("Lazy signal bootstrap failed: %s", e)
                finally:
                    svc._lazy_bootstrap_started = False

            asyncio.create_task(_bootstrap_once())

    results = []
    for symbol in svc._signal_cache:
        sig = svc._get_signal_for_symbol(symbol)
        if sig:
            results.append(sig)
    return {"signals": results, "updated_at": datetime.now().isoformat()}


@router.get("/api/symbols")
async def get_symbols():
    """List all available symbols."""
    return {"symbols": list(svc._market_data.keys())}


@router.get("/api/market/{symbol}")
async def get_market_data(
    symbol: str,
    limit: int = Query(100, le=500),
    interval: str = Query("5m"),
):
    """
    Get OHLCV + indicator data for a symbol.
    Supports ?interval=5m|15m|1h|1d for the chart timeframe selector.
    Non-5m intervals are fetched on-demand and indicators are calculated fresh.
    """
    symbol = symbol.replace("-", "/")
    if interval not in ("5m", "15m", "1h", "1d"):
        interval = "5m"

    if interval == "5m" and symbol in svc._market_data:
        df = svc._market_data[symbol].tail(limit)
        return {"symbol": symbol, "data": svc._build_market_records(df), "interval": interval}

    # On-demand fetch for non-default intervals (or symbol not yet in cache)
    period_map = {"5m": "5d", "15m": "10d", "1h": "30d", "1d": "90d"}
    period = period_map.get(interval, "5d")

    try:
        df = await asyncio.to_thread(fetch_ohlcv, symbol, period, interval)
        if df is None or df.empty:
            df = await asyncio.to_thread(fetch_arbitrary_symbol, symbol, period, interval)
        if df is None or df.empty:
            return {"error": f"Symbol {symbol} not found"}
        df = calculate_indicators(df)
    except Exception as e:
        logger.error("On-demand market fetch error for %s@%s: %s", symbol, interval, e)
        return {"error": str(e)}

    df = df.tail(limit)
    return {"symbol": symbol, "data": svc._build_market_records(df), "interval": interval}


@router.get("/api/signal/{symbol}")
async def get_current_signal(symbol: str):
    """Get the latest signal for a symbol."""
    symbol = symbol.replace("-", "/")
    result = svc._get_signal_for_symbol(symbol)
    if result is None:
        return {"error": f"Symbol {symbol} not found"}
    return result


@router.get("/api/trades")
async def get_trades(limit: int = Query(20, le=100)):
    """Get recent trade history from real paper trading engine."""
    engine = svc._get_engine()
    return {"trades": engine.get_history(limit=limit)}


@router.get("/api/risk/state")
async def get_risk_state():
    """Get current execution risk state (heat, drawdown, circuit breaker)."""
    engine = svc._get_engine()
    return engine.get_risk_state()


@router.get("/api/ai/weights")
async def get_ai_weight_state():
    """Expose dynamic ensemble weight state for monitoring and debugging."""
    return get_dynamic_weight_state()


@router.post("/api/risk/reset-circuit")
async def reset_risk_circuit(rebase_capital: bool = Query(False)):
    """Manually reset circuit breaker and consecutive-loss pause.

    Use rebase_capital=true to set current capital as new drawdown baseline.
    """
    engine = svc._get_engine()
    state = engine.reset_circuit_breaker(rebase_capital=rebase_capital)
    return {"ok": True, "risk": state}


# ── Self-Learning Feedback Loop endpoints ─────────────────────────────────────

@router.get("/api/learning/stats")
async def get_learning_stats_endpoint():
    """
    Return AI self-learning insights + current learned weights.

    Analyses last 500 closed paper trades from trade_outcomes SQLite table.
    Returns:
      - win rate by signal type (RSI_BOUNCE, MACD_CROSS, BREAKOUT, ...)
      - win rate by market regime (TRENDING / RANGING / HIGH_VOLATILITY)
      - win rate by RSI zone at entry
      - volume impact analysis
      - model accuracy (confidence ≥ 50% trades that won)
      - current learned weights for each signal component
    """
    try:
        from agents.analytics.feedback_loop import get_learning_stats
        return get_learning_stats()
    except Exception as exc:
        logger.error("Learning stats error: %s", exc)
        return {
            "insights": {
                "signal_type_stats": {},
                "regime_stats": {},
                "rsi_zone_stats": {},
                "volume_stats": {},
                "overall": {"total_trades": 0, "win_rate": 0, "avg_pnl_pct": 0, "model_accuracy": 0},
                "best_signal_type": "—",
                "worst_signal_type": "—",
                "best_regime": "—",
                "optimal_rsi_zone": "—",
            },
            "weights": {},
            "error": str(exc),
        }


@router.get("/api/learning/outcomes")
async def get_learning_outcomes(limit: int = Query(100, le=500)):
    """
    Return recent raw trade outcomes logged to the feedback loop.
    Used by the dashboard to display the Learning Stats trade history table.
    """
    try:
        from agents.analytics.feedback_loop import get_recent_outcomes
        return {"outcomes": get_recent_outcomes(limit=limit)}
    except Exception as exc:
        logger.error("Learning outcomes error: %s", exc)
        return {"outcomes": [], "error": str(exc)}



@router.get("/api/portfolio")
async def get_portfolio():
    """Get open positions and unrealized P&L from real trading engine."""
    engine = svc._get_engine()
    open_trades = engine.get_open_trades()

    total_unrealized = 0
    for t in open_trades:
        symbol = t["symbol"]
        if symbol in svc._market_data:
            current_close = float(svc._market_data[symbol].iloc[-1]["close"])
            if t["direction"] == "BUY":
                unrealized = (current_close - t["entry_price"]) * t.get("quantity", 1)
            else:
                unrealized = (t["entry_price"] - current_close) * t.get("quantity", 1)
            t["current_price"] = round(current_close, 2)
            t["unrealized_pnl"] = round(unrealized, 2)
            total_unrealized += unrealized
        else:
            t["current_price"] = t["entry_price"]
            t["unrealized_pnl"] = 0

    closed_count = engine.get_stats().get("total_trades", 0)

    return {
        "open_positions": open_trades,
        "total_open": len(open_trades),
        "total_unrealized_pnl": round(total_unrealized, 2),
        "total_closed": closed_count,
    }


@router.get("/api/performance")
async def get_performance():
    """Get detailed performance metrics and equity curve from real trading engine."""
    from collections import defaultdict, Counter

    engine = svc._get_engine()
    stats = engine.get_stats()
    equity_curve = engine.get_equity_curve()

    closed_trades = engine.get_history(limit=1000)
    wins   = [t for t in closed_trades if t.get("pnl", 0) > 0]
    losses = [t for t in closed_trades if t.get("pnl", 0) <= 0]

    # ── Monthly P&L breakdown ──────────────────────────
    monthly: dict[str, float] = defaultdict(float)
    for t in closed_trades:
        key = (t.get("exit_time") or "")[:7]   # "2026-03"
        if key:
            monthly[key] += t.get("pnl", 0) * t.get("quantity", 1)
    monthly_pnl = [{"month": k, "pnl": round(v, 2)} for k, v in sorted(monthly.items())]

    # ── Exit reason breakdown ──────────────────────────
    exit_counts = Counter(t.get("status", "UNKNOWN") for t in closed_trades)
    exit_reasons = [{"reason": r, "count": c} for r, c in exit_counts.most_common()]

    # ── P&L by symbol (top 10) ─────────────────────────
    sym_pnl:    dict[str, float] = defaultdict(float)
    sym_trades: dict[str, int]   = defaultdict(int)
    for t in closed_trades:
        sym = t.get("symbol", "?")
        sym_pnl[sym]    += t.get("pnl", 0) * t.get("quantity", 1)
        sym_trades[sym] += 1
    symbol_breakdown = sorted(
        [{"symbol": k, "pnl": round(v, 2), "trades": sym_trades[k]} for k, v in sym_pnl.items()],
        key=lambda x: abs(x["pnl"]), reverse=True,
    )[:10]

    # ── Return % ───────────────────────────────────────
    initial = engine.initial_capital
    current = stats.get("capital", initial)
    total_return_pct = round((current - initial) / initial * 100, 2) if initial else 0

    return {
        "metrics": {
            "total_trades":     stats.get("total_trades", 0),
            "winning_trades":   stats.get("winning_trades", 0),
            "losing_trades":    stats.get("losing_trades", 0),
            "win_rate":         stats.get("win_rate", 0),
            "total_pnl":        stats.get("total_pnl", 0),
            "total_return_pct": total_return_pct,
            "avg_pnl":          stats.get("avg_pnl", 0),
            "avg_win":          stats.get("avg_win", 0),
            "avg_loss":         stats.get("avg_loss", 0),
            "best_trade":       stats.get("best_trade", 0),
            "worst_trade":      stats.get("worst_trade", 0),
            "sharpe_ratio":     stats.get("sharpe_ratio", 0),
            "max_drawdown":     stats.get("max_drawdown", 0),
            "max_drawdown_pct": stats.get("max_drawdown_pct", 0),
            "profit_factor":    stats.get("profit_factor", 0),
            "capital":          current,
            "initial_capital":  initial,
            "daily_pnl":        stats.get("daily_pnl", 0),
        },
        "status": {
            "feed": svc._get_feed_status(),
            "symbols_loaded": len(svc._market_data),
            "signals_cached": len(svc._signal_cache),
        },
        "equity_curve":       equity_curve,
        "trade_distribution": {"wins": len(wins), "losses": len(losses)},
        "monthly_pnl":        monthly_pnl,
        "exit_reasons":       exit_reasons,
        "symbol_breakdown":   symbol_breakdown,
        "recent_trades":      list(reversed(closed_trades[-50:])),
    }


@router.get("/api/regime/{symbol}")
async def get_regime(symbol: str):
    """Get current market regime for a symbol."""
    symbol = symbol.replace("-", "/")
    if symbol not in svc._regime_cache:
        return {"error": f"Symbol {symbol} not found"}

    return {
        "symbol": symbol,
        "regime": svc._regime_cache[symbol],
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/api/stats")
async def get_stats():
    """Get dashboard summary statistics from real trading engine."""
    engine = svc._get_engine()
    stats = engine.get_stats()

    return {
        "total_trades": stats.get("total_trades", 0),
        "winning_trades": stats.get("winning_trades", 0),
        "win_rate": stats.get("win_rate", 0),
        "total_pnl": stats.get("total_pnl", 0),
        "avg_pnl": stats.get("avg_pnl", 0),
        "best_trade": stats.get("best_trade", 0),
        "worst_trade": stats.get("worst_trade", 0),
        "capital": stats.get("capital", 100000),
    }


@router.get("/api/backtest/{symbol}")
async def run_backtest(symbol: str):
    """Run backtest on cached data for a symbol."""
    symbol = symbol.replace("-", "/")
    if symbol not in svc._signal_cache:
        return {"error": f"Symbol {symbol} not found"}

    df = svc._signal_cache[symbol]
    bt = Backtester(initial_capital=100000, slippage_pct=0.05)
    result = bt.run(df)

    return {
        "symbol": symbol,
        "total_trades": result.total_trades,
        "win_rate": round(result.win_rate, 1),
        "total_pnl": round(result.total_pnl, 2),
        "max_drawdown": round(result.max_drawdown, 2),
        "max_drawdown_pct": round(result.max_drawdown_pct, 1),
        "profit_factor": round(result.profit_factor, 2),
        "sharpe_ratio": round(result.sharpe_ratio, 2),
    }


# ── Market Intelligence Endpoints ───────────────────────
@router.get("/api/screener/gainers")
async def screener_gainers():
    """
    Top NSE gainers with X-Factor multiplier.
    Primary: fetch from NSE screener API.
    Fallback: compute from svc._market_data cache if API fails/times out.
    """
    # Try precomputed cache first for instant response
    if svc._movers_cache.get("gainers"):
        data = svc._movers_cache.get("gainers")
        if data:
            logger.info("Screener gainers from precomputed cache: %d stocks", len(data))
            return {
                "data": data,
                "real_data": True,
                "source": "cache",
                "updated_at": svc._movers_cache.get("updated_at"),
            }

    # Fallback to direct cache computation
    if svc._market_data:
        data = svc._compute_gainers_losers_from_cache("gainers")
        if data:
            logger.info("Screener gainers from cache: %d stocks", len(data))
            return {"data": data, "real_data": True, "source": "cache", "updated_at": datetime.now().isoformat()}

    # Cache empty — try yFinance batch (with short timeout)
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(fetch_nse_gainers_losers, "gainers"),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Screener gainers timed out (5s) — no cache available")
        data = []
    except Exception as e:
        logger.error("Screener gainers fetch failed: %s", e)
        data = []

    return {"data": data or [], "real_data": bool(data), "updated_at": datetime.now().isoformat()}


@router.get("/api/screener/losers")
async def screener_losers():
    """
    Top NSE losers with X-Factor multiplier.
    Primary: fetch from NSE screener API.
    Fallback: compute from svc._market_data cache if API fails/times out.
    """
    # Try precomputed cache first for instant response
    if svc._movers_cache.get("losers"):
        data = svc._movers_cache.get("losers")
        if data:
            logger.info("Screener losers from precomputed cache: %d stocks", len(data))
            return {
                "data": data,
                "real_data": True,
                "source": "cache",
                "updated_at": svc._movers_cache.get("updated_at"),
            }

    # Fallback to direct cache computation
    if svc._market_data:
        data = svc._compute_gainers_losers_from_cache("losers")
        if data:
            logger.info("Screener losers from cache: %d stocks", len(data))
            return {"data": data, "real_data": True, "source": "cache", "updated_at": datetime.now().isoformat()}

    # Cache empty — try yFinance batch (with short timeout)
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(fetch_nse_gainers_losers, "losers"),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Screener losers timed out (5s) — no cache available")
        data = []
    except Exception as e:
        logger.error("Screener losers fetch failed: %s", e)
        data = []

    return {"data": data or [], "real_data": bool(data), "updated_at": datetime.now().isoformat()}


@router.get("/api/sectors")
async def sector_heatmap():
    """
    Sectoral performance heatmap data.
    Cache-first: compute from svc._market_data for instant response.
    Fallback: fetch from yFinance if cache is empty.
    """
    # Try cache first for instant response
    if svc._market_data:
        sectors = svc._compute_sectors_from_cache()
        if sectors:
            logger.info("Sector heatmap from cache: %d sectors", len(sectors))
            return {"sectors": sectors, "real_data": True, "source": "cache", "updated_at": datetime.now().isoformat()}

    # Cache empty — try yFinance (with short timeout)
    try:
        sectors = await asyncio.wait_for(
            asyncio.to_thread(fetch_sector_performance),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Sector fetch timed out (8s)")
        sectors = []
    except Exception as e:
        logger.error("Sector fetch failed: %s", e)
        sectors = []
    return {"sectors": sectors or [], "real_data": bool(sectors), "updated_at": datetime.now().isoformat()}


@router.get("/api/analytics/scorecard")
async def get_scorecard():
    """
    Multi-dimensional win-rate scorecard from the trade journal.
    Dimensions: regime, symbol, hour, confidence bucket, quality grade.
    Also lists false signal reason distribution.
    """
    scorecard = await asyncio.to_thread(get_journal().get_scorecard)
    return scorecard


@router.get("/api/analytics/signals")
async def get_journal_signals(limit: int = Query(50, le=500)):
    """Most recent N recorded signals with their outcomes."""
    signals = await asyncio.to_thread(get_journal().get_recent_signals, limit)
    return {"signals": signals, "total": get_journal().total_count()}


@router.get("/api/tradeflow")
async def get_trade_flow():
    """FII/DII institutional trade flow — real NSE data with rolling history.
    Server-side 8s timeout so the frontend's 10s AbortController never fires first."""
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(fetch_fii_dii_flow),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Trade flow fetch timed out (8s)")
        data = {
            "flows": [],
            "date": datetime.now().strftime("%d-%b-%Y"),
            "sentiment": "UNAVAILABLE",
            "combined_net": None,
            "history": [],
            "source": "timeout",
            "real_data": False,
            "stale": True,
        }
    except Exception as e:
        logger.error("Trade flow fetch failed: %s", e)
        data = {
            "flows": [],
            "date": datetime.now().strftime("%d-%b-%Y"),
            "sentiment": "UNAVAILABLE",
            "combined_net": None,
            "history": [],
            "source": "error",
            "real_data": False,
            "stale": True,
        }
    data["updated_at"] = datetime.now().isoformat()
    return data


@router.get("/api/market/mood")
async def get_market_mood():
    """Get computed market mood from breadth, signals, regimes, and sectors."""
    return svc._compute_market_mood()


@router.get("/api/nlp/summary")
async def get_ai_summary(symbol: str | None = Query(default=None)):
    """Get a concise AI-style summary for market or a specific symbol."""
    if symbol:
        clean = symbol.replace("-", "/")
        sig = svc._get_signal_for_symbol(clean)
        if not sig:
            return {"error": f"Symbol {clean} not found"}

        direction = sig.get("signal", "HOLD")
        conf = float(sig.get("confidence", 0.0))
        trend = str(sig.get("trend", "NEUTRAL"))
        regime = str(sig.get("regime", "RANGING"))
        lines = [
            f"{clean} signal is {direction} with confidence {conf:.0f}%.",
            f"Trend is {trend} and regime is {regime}.",
            f"Risk frame: entry {sig.get('entry')}, stop {sig.get('stop_loss')}, target {sig.get('target')}.",
        ]
        return {
            "symbol": clean,
            "lines": lines,
            "summary": " ".join(lines),
            "generated_at": datetime.now().isoformat(),
        }

    mood = svc._compute_market_mood()
    return svc._build_ai_market_summary(mood)


@router.get("/api/overview/intelligence")
async def get_overview_intelligence():
    """
    Aggregated decision-cockpit payload for the Overview tab.

    Returns top ranked setups, high-quality-but-blocked setups, market guardrails,
    system/risk state, and an advisory learning overlay — all in one fast call.
    """
    try:
        return svc._compute_overview_intelligence()
    except Exception as exc:
        logger.error("Overview intelligence failed: %s", exc)
        return {
            "top_setups": [],
            "avoid_list": [],
            "ranking_meta": {"total_tracked": 0, "actionable": 0, "blocked_high_quality": 0},
            "market_guardrails": {},
            "system_state": {},
            "learning_overlay": None,
            "error": str(exc),
            "updated_at": datetime.now().isoformat(),
        }


@router.get("/api/nse/dashboard")
async def get_nse_dashboard_snapshot():
    """Aggregate NSE dashboard snapshot for faster UI bootstrap."""
    if not svc._movers_cache.get("gainers") and svc._market_data:
        svc._refresh_movers_cache()

    mood = svc._compute_market_mood()
    summary = svc._build_ai_market_summary(mood)
    movers_updated_at = svc._movers_cache.get("updated_at") or datetime.now().isoformat()

    gainers = list((svc._movers_cache.get("gainers") or [])[:7])
    losers = list((svc._movers_cache.get("losers") or [])[:7])

    return {
        "timestamp": datetime.now().isoformat(),
        "last_refresh": svc._last_refresh.isoformat() if svc._last_refresh != datetime.min else None,
        "loading": svc._loading_status,
        "mood": mood,
        "ai_summary": summary,
        "movers": {
            "gainers": gainers,
            "losers": losers,
            "updated_at": movers_updated_at,
        },
    }


# ── Swing Scanner Endpoint ────────────────────────────────────

@router.get("/api/swing-scanner")
async def get_swing_setups():
    """
    SwingSpectrum — serves pre-computed swing trade setups from cache.
    Cache is populated during the background refresh cycle (every few minutes).
    The API never blocks — it always returns immediately from the last known result.
    """
    if svc._swing_cache["symbols_scanned"] == 0 and svc._daily_data_cache:
        await svc._recompute_swing_cache(timeout_sec=30.0)

    # Fallback path: if background refresh did not populate daily cache yet,
    # fetch a small daily subset on demand so Swing tab doesn't stay empty.
    if not svc._swing_cache["real_data"] and not svc._daily_data_cache:
        try:
            daily_sem = asyncio.Semaphore(4)

            async def _fetch_daily_bootstrap(name: str, sem: asyncio.Semaphore):
                async with sem:
                    try:
                        df = await asyncio.wait_for(
                            asyncio.to_thread(fetch_ohlcv, name, "1y", "1d"),
                            timeout=20.0,
                        )
                        if df is not None and not df.empty and len(df) >= 30:
                            svc._daily_data_cache[name] = calculate_indicators(df)
                    except Exception:
                        return

            bootstrap_symbols = svc.TOP_NSE_SYMBOLS[:20]
            await asyncio.gather(*[_fetch_daily_bootstrap(sym, daily_sem) for sym in bootstrap_symbols])
            if svc._daily_data_cache:
                await svc._recompute_swing_cache(timeout_sec=45.0)
        except Exception as exc:
            logger.warning("Swing on-demand bootstrap failed: %s", exc)

    if not svc._swing_cache["real_data"]:
        return {
            "setups": [],
            "total_found": 0,
            "symbols_scanned": len(svc._daily_data_cache),
            "message": "Swing data is still warming up. Daily candles are being fetched now; retry in 20-40 seconds.",
            "real_data": False,
            "updated_at": datetime.now().isoformat(),
            "market_context": svc._swing_cache.get("market_context"),
        }

    return {
        "setups": svc._swing_cache["setups"],
        "total_found": svc._swing_cache["total_found"],
        "symbols_scanned": svc._swing_cache["symbols_scanned"],
        "real_data": True,
        "updated_at": svc._swing_cache["updated_at"],
        "market_context": svc._swing_cache.get("market_context"),
    }


# ── Swing Scanner Debug Endpoint ──────────────────────────────

@router.get("/api/swing-debug")
async def get_swing_debug(symbol: str = None):
    """
    Debug endpoint — shows per-symbol scan results before/after filtering.
    Use ?symbol=RELIANCE to debug one stock, or omit for a summary of all.
    """
    from agents.market_intelligence.swing_scanner import scan_swing_setups
    from agents.feature_engine.indicators import calculate_indicators
    ctx = svc._build_swing_market_context()
    results = []

    data = dict(svc._daily_data_cache)
    if symbol:
        sym_upper = symbol.upper()
        data = {k: v for k, v in data.items() if k.upper() == sym_upper}
        if not data:
            # Try fetching it on-demand
            try:
                df = await asyncio.wait_for(
                    asyncio.to_thread(fetch_ohlcv, sym_upper, "1y", "1d"), timeout=15.0
                )
                if df is not None and not df.empty:
                    data = {sym_upper: calculate_indicators(df)}
            except Exception as exc:
                return {"error": str(exc)}

    for sym, df in data.items():
        if df is None or df.empty:
            results.append({"symbol": sym, "status": "empty_df", "bars": 0})
            continue

        last = df.iloc[-1]
        atr = float(last.get("ATR_14", 0) or 0)
        close = float(last.get("close", 0) or 0)
        rsi = float(last.get("RSI_14", 0) or 0)
        ema20 = float(last.get("EMA_20", 0) or 0)
        ema50 = float(last.get("EMA_50", 0) or 0)

        # Run scan WITHOUT regime gate so we see raw results
        raw = scan_swing_setups(df, sym, sector_momentum=0.0, sector="Other", market_context=None)
        # Run scan WITH market context
        gated = scan_swing_setups(df, sym, sector_momentum=0.0, sector="Other", market_context=ctx)

        results.append({
            "symbol": sym,
            "bars": len(df),
            "close": round(close, 2),
            "atr": round(atr, 2),
            "rsi": round(rsi, 1),
            "ema20": round(ema20, 2),
            "ema50": round(ema50, 2),
            "raw_setups_before_gate": len(raw),
            "setups_after_regime_gate": len(gated),
            "raw_patterns": [{"pattern": s.pattern, "direction": s.direction, "rr": s.risk_reward, "strength": s.strength} for s in raw],
            "gated_patterns": [{"pattern": s.pattern, "direction": s.direction, "rr": s.risk_reward} for s in gated],
            "dropped_by_regime_gate": len(raw) - len(gated),
        })

    results.sort(key=lambda x: x.get("raw_setups_before_gate", 0), reverse=True)
    return {
        "market_context": ctx,
        "symbols_checked": len(results),
        "total_raw_setups": sum(r.get("raw_setups_before_gate", 0) for r in results),
        "total_after_gate": sum(r.get("setups_after_regime_gate", 0) for r in results),
        "settings": {
            "SWING_MIN_RISK_REWARD": settings.SWING_MIN_RISK_REWARD,
            "SWING_REGIME_GATE_ENABLED": settings.SWING_REGIME_GATE_ENABLED,
            "SWING_BLOCK_LONG_IN_BEARISH_TREND": settings.SWING_BLOCK_LONG_IN_BEARISH_TREND,
            "daily_cache_size": len(svc._daily_data_cache),
        },
        "results": results[:20] if not symbol else results,
    }


# ── Index Mover Endpoint ─────────────────────────────────────

@router.get("/api/index-mover/{index_name}")
async def get_index_movers(index_name: str):
    """
    IndexMover — shows which stocks are contributing the most to
    Nifty 50 / BankNifty movement (up or down).
    """
    # Normalize input
    index_map = {
        "nifty": "NIFTY 50", "nifty50": "NIFTY 50", "nifty-50": "NIFTY 50",
        "nifty_50": "NIFTY 50", "NIFTY 50": "NIFTY 50",
        "banknifty": "BANKNIFTY", "bank-nifty": "BANKNIFTY",
        "bank_nifty": "BANKNIFTY", "BANKNIFTY": "BANKNIFTY",
    }
    clean_name = index_map.get(index_name.replace(" ", "").lower(), index_name)
    if clean_name not in ("NIFTY 50", "BANKNIFTY"):
        return {"error": f"Unknown index: {index_name}. Use 'nifty50' or 'banknifty'."}

    try:
        data = await asyncio.to_thread(
            calculate_index_movers, clean_name, svc._market_data
        )
        # Remove internal cache key before sending to client
        data.pop("_fetched_at", None)
    except Exception as e:
        logger.error("Index mover failed for %s: %s", clean_name, e)
        data = {"error": str(e)}

    return data


# ── Breakout Scanner Endpoint ────────────────────────────────

@router.get("/api/breakouts")
async def get_breakouts(
    min_strength: int = Query(30, ge=0, le=100),
):
    """
    BreakoutScanner — scans all tracked symbols for volume-confirmed
    10-day, 50-day, and 52-week breakouts.
    """
    if not svc._daily_data_cache:
        return {"all": [], "message": "Daily data not yet loaded.",
                "real_data": False, "updated_at": datetime.now().isoformat()}

    try:
        data = await asyncio.to_thread(
            breakout_scan_all, svc._daily_data_cache, min_strength
        )
    except Exception as e:
        logger.error("Breakout scanner failed: %s", e)
        data = {"all": [], "total_breakouts": 0, "real_data": False}

    data["updated_at"] = datetime.now().isoformat()
    return data


# ── WebSocket Endpoint for Real-time Updates ──────────────

@router.websocket("/ws/prices")
async def websocket_prices(websocket: WebSocket):
    """
    WebSocket endpoint for real-time price and signal updates.

    Fix 1: Sends payload under key "prices" (was "data") to match
           the frontend handler: if (data.prices) { updateLivePrices(data.prices) }

    Pushes updates every 5 seconds with latest prices and signals
    for all tracked symbols.
    """
    await websocket.accept()
    svc._ws_clients.append(websocket)
    logger.info("WebSocket client connected (%d total)", len(svc._ws_clients))

    try:
        while True:
            # Fix 1: Use "prices" key to match frontend expectation
            update = {
                "type": "price_update",
                "timestamp": datetime.now().isoformat(),
                "prices": {},   # ← was "data", now "prices"
            }

            for symbol in svc._market_data:
                df = svc._market_data[symbol]
                if df.empty:
                    continue
                last = df.iloc[-1]
                update["prices"][symbol] = {
                    "close": round(float(last["close"]), 2),
                    "high": round(float(last["high"]), 2),
                    "low": round(float(last["low"]), 2),
                    "rsi": round(float(last["RSI_14"]), 1) if pd.notna(last.get("RSI_14")) else None,
                    "signal": str(last.get("signal", "HOLD")),
                    "regime": svc._regime_cache.get(symbol, "RANGING"),
                }

            # Add live trade stats
            engine = svc._get_engine()
            stats = engine.get_stats()
            update["stats"] = {
                "capital": stats.get("capital", 100000),
                "total_pnl": stats.get("total_pnl", 0),
                "open_trades": len(engine.get_open_trades()),
            }

            update["market_mood"] = svc._compute_market_mood()
            update["feed_status"] = svc._get_feed_status()

            await websocket.send_json(update)
            await asyncio.sleep(5)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WebSocket error: %s", e)
    finally:
        if websocket in svc._ws_clients:
            svc._ws_clients.remove(websocket)
        logger.info("WebSocket client disconnected (%d remaining)", len(svc._ws_clients))


@router.post("/api/refresh")
async def refresh_data():
    """Force refresh of all market data."""
    await svc._refresh_market_data()
    return {"status": "ok", "symbols": len(svc._market_data), "updated_at": datetime.now().isoformat()}


# ── Search Endpoints ─────────────────────────────────────

@router.get("/api/search/symbols")
async def search_symbol_list(q: str = Query("", min_length=0, max_length=30)):
    """
    Search available symbols by name/ticker (NSE + Crypto).

    Phase 1: Instant match against pre-loaded NSE_SYMBOLS + crypto list.
    Phase 2: If fewer than 3 pre-loaded results, dynamically validate
             '{QUERY}.NS' via yFinance fast_info (~0.3s) for any NSE stock
             not in the pre-loaded map (e.g. PRECWIRE, NAUKRI, etc.).
    """
    results = search_symbols(q)
    for r in results:
        sym = r["symbol"]
        r["tracked"] = sym in svc._market_data
        if not r["tracked"] and r.get("type") == "CRYPTO":
            r["tracked"] = sym in svc._market_data or sym.replace("/", "-") in svc._market_data

    # ── Dynamic NSE validation ────────────────────────────────────────────
    # Only run if the query looks like a stock ticker (not crypto) and we
    # have fewer than 3 pre-loaded matches — avoids unnecessary yf calls.
    q_clean = q.upper().strip()
    already_found = {r["ticker"].replace(".NS", "") for r in results if r.get("type") == "NSE"}
    is_likely_nse = (
        len(results) < 3
        and "/" not in q_clean
        and len(q_clean) >= 3
        and not any(c in q_clean for c in ["BTC", "ETH", "SOL", "BNB", "USDT"])
    )

    if is_likely_nse and q_clean not in already_found:
        nse_ticker = f"{q_clean}.NS"
        try:
            info = await asyncio.to_thread(validate_nse_symbol, nse_ticker)
            if info:
                # Symbol is valid — add as dynamic suggestion (not pre-tracked)
                results.insert(0, {
                    "symbol": q_clean,
                    "ticker": nse_ticker,
                    "type": "NSE",
                    "tracked": q_clean in svc._market_data,
                    "dynamic": True,
                    "price": info["price"],
                    "exchange": info.get("exchange", "NSE"),
                })
        except Exception as e:
            logger.debug("Dynamic NSE validate failed for %s: %s", nse_ticker, e)

    # ── Inject Opportunity Stocks ──────────────────────────────────────
    if q_clean:
        # Prepend any Opportunity Stocks that match the query
        for opp in reversed(opportunity_stocks):
            if q_clean in opp["symbol"].upper():
                # Avoid duplicates
                if not any(r["symbol"] == opp["symbol"] for r in results):
                    results.insert(0, {
                        "symbol": opp["symbol"],
                        "ticker": f"{opp['symbol']}.NS",
                        "type": "NSE",
                        "tracked": opp["symbol"] in svc._market_data,
                        "dynamic": True,
                        "opportunity": True,
                        "signal": opp["signal"]
                    })

    return {"results": results[:50], "query": q}


@router.get("/api/screener/opportunities")
async def get_screener_opportunities():
    """Return top 5 stock opportunities found by the screener."""
    return {"opportunities": opportunity_stocks}


@router.get("/api/morning-scan")
async def get_morning_scan():
    """Return the latest persisted morning scan results (max 10)."""
    session = SessionLocal()
    try:
        latest = (
            session.query(MorningScan.scan_time)
            .order_by(MorningScan.scan_time.desc())
            .first()
        )

        if not latest:
            return {
                "scan_time": None,
                "count": 0,
                "results": [],
            }

        latest_scan_time = latest[0]
        rows = (
            session.query(MorningScan)
            .filter(MorningScan.scan_time == latest_scan_time)
            .order_by(MorningScan.confidence_score.desc())
            .limit(10)
            .all()
        )

        results = []
        for row in rows:
            results.append(
                {
                    "symbol": row.symbol,
                    "entry_price": float(row.entry_price),
                    "stop_loss": float(row.stop_loss),
                    "target": float(row.target_price),
                    "confidence_score": float(row.confidence_score),
                    "reason": row.reason,
                    "close": float(row.close),
                    "rsi": float(row.rsi),
                    "macd": float(row.macd),
                    "macd_signal": float(row.macd_signal),
                    "ema_20": float(row.ema_20),
                    "ema_50": float(row.ema_50),
                    "ema_200": float(row.ema_200),
                    "volume": float(row.volume),
                    "avg_volume_20": float(row.avg_volume_20),
                    "volume_ratio": float(row.volume_ratio),
                    "scan_time": row.scan_time.isoformat(),
                    "scan_date": row.scan_date.isoformat(),
                }
            )

        return {
            "scan_time": latest_scan_time.isoformat() if latest_scan_time else None,
            "count": len(results),
            "results": results,
        }
    except Exception as e:
        logger.error("Morning scan API error: %s", e)
        return JSONResponse(
            status_code=500,
            content={
                "scan_time": None,
                "count": 0,
                "results": [],
                "error": "Failed to fetch morning scan results",
            },
        )
    finally:
        session.close()


@router.get("/api/search/quote/{symbol}")
async def search_quote(symbol: str):
    """
    Get full quote (OHLCV + indicators + signal) for any symbol.
    Supports NSE stocks (yFinance), crypto pairs (CCXT/Binance),
    and any global ticker.

    Auto-appends ".NS" suffix for unrecognized Indian stock symbols.
    """
    clean = symbol.replace("-", "/").replace("_", " ")
    # Strip any .NS suffix the caller may have included (normalize to display name format)
    clean_base = clean.upper().replace(".NS", "").strip()

    # Check if already in cache (try both the clean name and the original symbol)
    if clean in svc._market_data:
        df = svc._market_data[clean]
    elif clean_base in svc._market_data:
        df = svc._market_data[clean_base]
        clean = clean_base
    else:
        df = None

        # ── Try crypto via CCXT first ─────────────────
        is_crypto = "/" in clean and any(
            clean.upper().endswith(f"/{q}") for q in ("USDT", "BTC", "ETH", "BUSD", "USDC")
        )
        if is_crypto:
            try:
                from agents.data_ingestion.binance_fetcher import fetch_historical
                df = fetch_historical(clean.upper(), "1h", since_days=7)
                if df is None or df.empty:
                    df = None
                else:
                    htf_df = fetch_historical(clean.upper(), "1d", since_days=60)
                    df = calculate_indicators(df)
                    if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:
                        htf_df = calculate_indicators(htf_df)
                        htf_df = classify_trend(htf_df)
                        df = propagate_trend_to_ltf(df, htf_df)
                    else:
                        df = classify_trend(df)
                    regime = detect_regime(df)
                    svc._regime_cache[clean] = regime.value
                    df = generate_signals(df, regime=regime)
                    svc._market_data[clean] = df
                    svc._signal_cache[clean] = df
            except Exception as e:
                logger.warning("Crypto fetch failed for %s: %s", clean, e)
                df = None

        # ── Fallback to yFinance (NSE + global) ──────
        if df is None:
            # 1st attempt: raw symbol (handles known tickers, AAPL, etc.)
            df = await asyncio.to_thread(fetch_arbitrary_symbol, clean)

            # 2nd attempt: try as NSE stock with .NS suffix (e.g. PRECWIRE → PRECWIRE.NS)
            if (df is None or df.empty) and "/" not in clean:
                nse_ticker = f"{clean_base}.NS"
                logger.info("Trying NSE suffix: %s", nse_ticker)
                df = await asyncio.to_thread(fetch_arbitrary_symbol, nse_ticker)
                if df is not None and not df.empty:
                    clean = clean_base  # Use clean display name for cache key

            if df is None or df.empty:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"Symbol '{symbol}' not found on NSE or yFinance. "
                                      f"Tried '{clean_base}.NS'. Check the symbol and try again."},
                )
            df = calculate_indicators(df)
            htf_df = await asyncio.to_thread(fetch_arbitrary_symbol, clean, "60d", "1d")
            if htf_df is None or htf_df.empty:
                # For dynamic NSE symbols, also try with .NS for HTF
                nse_htf = f"{clean_base}.NS"
                htf_df = await asyncio.to_thread(fetch_arbitrary_symbol, nse_htf, "60d", "1d")
            if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:
                htf_df = calculate_indicators(htf_df)
                htf_df = classify_trend(htf_df)
                df = propagate_trend_to_ltf(df, htf_df)
            else:
                df = classify_trend(df)
            regime = detect_regime(df)
            svc._regime_cache[clean] = regime.value
            df = generate_signals(df, regime=regime)
            svc._market_data[clean] = df
            svc._signal_cache[clean] = df

    last = df.iloc[-1]

    # Build quote response
    sig = svc._get_signal_for_symbol(clean)

    # Recent OHLCV data (last 100 bars)
    tail = df.tail(100)
    chart_data = []
    for _, row in tail.iterrows():
        chart_data.append({
            "timestamp": row["timestamp"].isoformat(),
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
            "volume": round(float(row["volume"]), 0),
            "rsi": round(float(row["RSI_14"]), 1) if pd.notna(row.get("RSI_14")) else None,
            "ema_20": round(float(row["EMA_20"]), 2) if pd.notna(row.get("EMA_20")) else None,
            "ema_50": round(float(row["EMA_50"]), 2) if pd.notna(row.get("EMA_50")) else None,
            "vwap": round(float(row["VWAP"]), 2) if pd.notna(row.get("VWAP")) else None,
            "macd_hist": round(float(row["MACD_hist"]), 4) if pd.notna(row.get("MACD_hist")) else None,
            "signal": str(row.get("signal", "HOLD")) if "signal" in row.index else "HOLD",
        })

    # Compute MACD histogram for current bar
    macd_hist = round(float(last["MACD_hist"]), 4) if pd.notna(last.get("MACD_hist")) else None

    return {
        "symbol": clean,
        "close": round(float(last["close"]), 2),
        "high": round(float(last["high"]), 2),
        "low": round(float(last["low"]), 2),
        "rsi": round(float(last["RSI_14"]), 1) if pd.notna(last.get("RSI_14")) else None,
        "macd_hist": macd_hist,
        "regime": svc._regime_cache.get(clean, "RANGING"),
        "signal": sig,
        "chart": chart_data,
    }


# ── AI / Ensemble Endpoints ──────────────────────────────

@router.get("/api/ensemble/validate")
async def ensemble_validate(
    symbol: str = Query(default="BTC/USDT"),
    timeframe: str = Query(default="4h"),
    days: int = Query(default=365),
    n_folds: int = Query(default=5),
):
    """
    Run walk-forward validation of the ensemble predictor.

    Tests prediction accuracy on historical data without lookahead bias.
    Each fold trains on older data and tests on newer data.

    Returns per-fold accuracy, precision, high-confidence accuracy,
    estimated profit factor, and calibration score.

    Note: Takes 2-5 minutes depending on dataset size.
    """
    from concurrent.futures import ThreadPoolExecutor
    import asyncio

    async def _run():
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=1) as ex:
            from agents.backtester.ensemble_validator import (
                run_walk_forward_validation, result_to_dict
            )
            result = await loop.run_in_executor(
                ex,
                lambda: run_walk_forward_validation(
                    symbol=symbol, timeframe=timeframe,
                    days=days, n_folds=n_folds,
                )
            )
        return result_to_dict(result)

    try:
        return await _run()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/api/ensemble/status")
async def ensemble_status():
    """
    Return status of all AI models in the ensemble.
    Shows which models are active, their last training time, and current
    Fear & Greed sentiment reading.
    """
    status = {
        "statistical": {"active": True, "note": "Always active — no training required"},
        "xgboost": {"active": False, "model_path": "models/xgb_model.pkl"},
        "lstm": {"active": False, "model_path": "models/best_model.pt"},
        "sentiment": {"active": False, "fear_greed": None},
    }

    # XGBoost status
    try:
        import os, pickle
        xgb_path = "models/xgb_model.pkl"
        if os.path.exists(xgb_path):
            mtime = os.path.getmtime(xgb_path)
            size_kb = round(os.path.getsize(xgb_path) / 1024, 1)
            with open(xgb_path, "rb") as f:
                meta = pickle.load(f)
            status["xgboost"] = {
                "active": True,
                "model_path": xgb_path,
                "n_samples": meta.get("n_samples"),
                "symbols": meta.get("symbols", ["BTC/USDT"]),
                "timeframe": meta.get("timeframe", "4h"),
                "size_kb": size_kb,
                "trained_at": datetime.fromtimestamp(mtime).isoformat(),
            }
    except Exception as e:
        status["xgboost"]["error"] = str(e)

    # LSTM status
    try:
        import os, json as _json
        lstm_path = "models/best_model.pt"
        meta_path = "models/model_meta.json"
        if os.path.exists(lstm_path) and os.path.exists(meta_path):
            with open(meta_path) as f:
                lstm_meta = _json.load(f)
            status["lstm"] = {
                "active": True,
                "model_path": lstm_path,
                "val_accuracy": lstm_meta.get("val_accuracy"),
                "input_size": lstm_meta.get("input_size"),
                "trained_at": lstm_meta.get("trained_at"),
            }
    except Exception as e:
        status["lstm"]["error"] = str(e)

    # Sentiment (Fear & Greed)
    try:
        from agents.ai_engine.crypto_sentiment import get_fear_greed_index
        fg = get_fear_greed_index(timeout=3)
        status["sentiment"] = {
            "active": True,
            "fear_greed_value": fg["value"],
            "classification": fg["classification"],
            "sentiment_bias": fg["sentiment_bias"],
            "contrarian_conf": fg["contrarian_conf"],
            "source": fg.get("source", "api"),
        }
    except Exception as e:
        status["sentiment"] = {"active": False, "error": str(e)}

    active_models = sum(1 for k, v in status.items() if isinstance(v, dict) and v.get("active"))
    return {
        "ensemble_active": True,
        "active_model_count": active_models,
        "models": status,
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.post("/api/retrain")
async def retrain_xgboost(
    symbols: str = Query(default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT",
                         description="Comma-separated crypto symbols to train on"),
    timeframe: str = Query(default="4h", description="Candle timeframe: 1h, 4h, 1d"),
    days: int = Query(default=730, description="Days of history to use"),
    forward_bars: int = Query(default=12, description="Forward bars for label generation"),
    min_return_pct: float = Query(default=0.5, description="Minimum % move to count as signal"),
):
    """
    Retrain the XGBoost model on fresh crypto data.

    Runs asynchronously in a background thread.
    Returns a job_id that can be polled at /api/retrain/status.

    Usage from dashboard:
        POST /api/retrain
        POST /api/retrain?symbols=BTC/USDT,ETH/USDT&timeframe=4h&days=730
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]

    async def _run_training():
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = await loop.run_in_executor(
                executor,
                svc._train_xgboost_sync,
                symbol_list, timeframe, days, forward_bars, min_return_pct,
            )
        return result

    # Store job status
    job_id = f"retrain_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    svc._retrain_jobs[job_id] = {"status": "running", "started_at": datetime.utcnow().isoformat()}

    async def _background():
        try:
            result = await _run_training()
            svc._retrain_jobs[job_id] = {
                "status": "completed",
                "result": result,
                "completed_at": datetime.utcnow().isoformat(),
            }
            logger.info("XGBoost retrain complete: %s", result)
        except Exception as e:
            svc._retrain_jobs[job_id] = {
                "status": "failed",
                "error": str(e),
                "completed_at": datetime.utcnow().isoformat(),
            }
            logger.error("XGBoost retrain failed: %s", e)

    asyncio.create_task(_background())
    return {"job_id": job_id, "status": "started", "symbols": symbol_list, "timeframe": timeframe}


@router.get("/api/retrain/status")
async def retrain_status():
    """Return status of all retrain jobs."""
    return {"jobs": svc._retrain_jobs}

@router.get("/")
async def serve_dashboard():
    """Serve the main dashboard page."""
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "Dashboard not found. Place files in /dashboard/"}


@router.get("/landing")
async def serve_landing():
    """Serve the marketing landing page."""
    landing = os.path.join(STATIC_DIR, "landing.html")
    if os.path.exists(landing):
        return FileResponse(landing)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@router.get("/crypto")
@router.get("/crypto/")
async def serve_crypto():
    """Serve the live crypto dashboard page."""
    crypto = os.path.join(STATIC_DIR, "crypto.html")
    if os.path.exists(crypto):
        return FileResponse(crypto)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@router.get("/api/crypto/news")
async def get_crypto_news(limit: int = Query(12, ge=3, le=40)):
    """Fetch latest crypto headlines from multiple free/keyless RSS sources."""
    feeds = [
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("Decrypt", "https://decrypt.co/feed"),
        ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ]

    def _norm_text(node: ET.Element | None) -> str:
        if node is None:
            return ""
        txt = (node.text or "").strip()
        return " ".join(txt.split())

    def _fetch_feed(source: str, url: str) -> list[dict]:
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        with urlopen(req, timeout=10) as resp:
            payload = resp.read()

        root = ET.fromstring(payload)
        items = root.findall(".//item")
        out: list[dict] = []
        for it in items[:15]:
            title = _norm_text(it.find("title"))
            link = _norm_text(it.find("link"))
            pub = _norm_text(it.find("pubDate")) or _norm_text(it.find("published"))
            if not title or not link:
                continue
            out.append(
                {
                    "title": title,
                    "url": link,
                    "source": source,
                    "published": pub,
                }
            )
        return out

    all_news: list[dict] = []
    sources_ok: list[str] = []
    failed_sources: list[str] = []

    for source, url in feeds:
        try:
            all_news.extend(await asyncio.to_thread(_fetch_feed, source, url))
            sources_ok.append(source)
        except Exception as exc:
            logger.warning("Crypto news feed failed (%s): %s", source, exc)
            failed_sources.append(source)

    # Deduplicate by URL while preserving source diversity.
    seen_urls: set[str] = set()
    unique_news: list[dict] = []
    for row in all_news:
        u = row.get("url") or ""
        if not u or u in seen_urls:
            continue
        seen_urls.add(u)
        unique_news.append(row)

    return {
        "items": unique_news[:limit],
        "sources_ok": sources_ok,
        "failed_sources": failed_sources,
        "updated_at": datetime.utcnow().isoformat(),
    }


@router.get("/api/crypto/intelligence")
async def get_crypto_intelligence():
    """
    Phase 6 – Aggregated crypto cockpit intelligence payload.
    Mirrors /api/overview/intelligence contract for frontend reuse.
    Includes: top_setups, avoid_list, market_guardrails, futures_context, theme_leadership.
    """
    try:
        payload = await asyncio.to_thread(svc._compute_crypto_intelligence)
        return payload
    except Exception as exc:
        logger.error("Crypto intelligence failed: %s", exc)
        return {
            "top_setups": [],
            "avoid_list": [],
            "market_guardrails": {"mood": "UNKNOWN", "mood_score": 50, "why_market_hint": "Intelligence fetch failed."},
            "futures_context": {"per_symbol": [], "updated_at": None, "source": "unavailable"},
            "theme_leadership": [],
            "ranking_meta": {"total_tracked": 0, "actionable": 0, "blocked_high_quality": 0},
            "updated_at": datetime.utcnow().isoformat(),
            "error": str(exc),
        }


@router.get("/api/crypto/prediction")
async def get_crypto_prediction():
    """Calculated crypto prediction from live market, news sentiment, and macro context."""

    def _fetch_json(url: str) -> object:
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def _calc_rsi(closes: list[float], period: int = 14) -> float | None:
        if len(closes) < period + 1:
            return None
        gains = 0.0
        losses = 0.0
        for i in range(1, period + 1):
            d = closes[i] - closes[i - 1]
            if d > 0:
                gains += d
            else:
                losses -= d
        avg_g = gains / period
        avg_l = losses / period
        for i in range(period + 1, len(closes)):
            d = closes[i] - closes[i - 1]
            if d > 0:
                avg_g = (avg_g * (period - 1) + d) / period
                avg_l = (avg_l * (period - 1)) / period
            else:
                avg_l = (avg_l * (period - 1) - d) / period
                avg_g = (avg_g * (period - 1)) / period
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - (100.0 / (1.0 + rs))

    def _vol_spike(volumes: list[float]) -> float:
        if len(volumes) < 21:
            return 1.0
        recent = volumes[-1]
        base = float(np.mean(volumes[-21:-1]))
        return recent / base if base > 0 else 1.0

    def _sentiment_from_titles(items: list[dict]) -> tuple[float, int, int]:
        pos_words = {
            "surge", "rally", "breakout", "adoption", "approval", "launch", "partnership", "bull", "bullish",
            "upgrade", "rebound", "inflow", "record", "gain", "recovery", "accumulate"
        }
        neg_words = {
            "crash", "dump", "selloff", "hack", "ban", "lawsuit", "liquidation", "bear", "bearish",
            "outflow", "downgrade", "risk", "warning", "fraud", "decline", "drop"
        }
        score = 0.0
        hits_pos = 0
        hits_neg = 0
        for row in items:
            title = str(row.get("title") or "").lower()
            if not title:
                continue
            pw = sum(1 for w in pos_words if w in title)
            nw = sum(1 for w in neg_words if w in title)
            if pw > nw:
                hits_pos += 1
                score += 1.0
            elif nw > pw:
                hits_neg += 1
                score -= 1.0
        denom = max(len(items), 1)
        return score / denom, hits_pos, hits_neg

    def _hero_signal(sym: str, ticker: dict, closes: list[float], volumes: list[float]) -> dict:
        pct = float(ticker.get("priceChangePercent") or 0.0)
        rsi = _calc_rsi(closes)
        vs = _vol_spike(volumes)
        mom = 0.0
        if len(closes) >= 8 and closes[-8] != 0:
            mom = ((closes[-1] - closes[-8]) / closes[-8]) * 100.0

        score = 0.0
        reasons: list[str] = []
        if pct >= 2.5:
            score += 1.2
            reasons.append(f"24h momentum +{pct:.2f}%")
        elif pct <= -2.5:
            score -= 1.2
            reasons.append(f"24h momentum {pct:.2f}%")

        if rsi is not None:
            if rsi < 33:
                score += 1.4
                reasons.append(f"RSI {rsi:.1f} oversold")
            elif rsi > 68:
                score -= 1.4
                reasons.append(f"RSI {rsi:.1f} overbought")

        if mom >= 0.8:
            score += 0.8
            reasons.append("short trend up")
        elif mom <= -0.8:
            score -= 0.8
            reasons.append("short trend down")

        if vs >= 1.8 and pct > 0:
            score += 0.6
            reasons.append(f"volume spike x{vs:.1f}")
        elif vs >= 1.8 and pct < 0:
            score -= 0.6
            reasons.append(f"sell volume x{vs:.1f}")

        action = "HOLD"
        if score >= 1.9:
            action = "BUY"
        elif score <= -1.9:
            action = "SELL"

        conf = int(max(35, min(95, round(40 + abs(score) * 16 + (6 if rsi is not None else 0)))))
        return {
            "symbol": sym,
            "action": action,
            "confidence": conf,
            "score": round(score, 3),
            "rsi": round(rsi, 2) if rsi is not None else None,
            "volume_spike": round(vs, 2),
            "momentum_8h_pct": round(mom, 2),
            "reasons": reasons[:4],
        }

    try:
        # 1) Live market snapshot from Binance
        raw_tickers = await asyncio.to_thread(_fetch_json, "https://api.binance.com/api/v3/ticker/24hr")
        if not isinstance(raw_tickers, list):
            raise ValueError("Unexpected ticker payload")

        usdt = [
            t for t in raw_tickers
            if isinstance(t, dict)
            and str(t.get("symbol", "")).endswith("USDT")
            and float(t.get("quoteVolume") or 0.0) > 1_000_000
            and "DOWN" not in str(t.get("symbol"))
            and "UP" not in str(t.get("symbol"))
            and "BEAR" not in str(t.get("symbol"))
            and "BULL" not in str(t.get("symbol"))
        ]
        ticker_map = {str(t.get("symbol")): t for t in usdt}

        btc_pct = float((ticker_map.get("BTCUSDT") or {}).get("priceChangePercent") or 0.0)
        eth_pct = float((ticker_map.get("ETHUSDT") or {}).get("priceChangePercent") or 0.0)

        gainers_n = sum(1 for t in usdt if float(t.get("priceChangePercent") or 0.0) > 0)
        losers_n = sum(1 for t in usdt if float(t.get("priceChangePercent") or 0.0) < 0)
        breadth = (gainers_n - losers_n) / max(gainers_n + losers_n, 1)

        # 2) Fear & Greed
        fg_value: int | None = None
        try:
            fg_raw = await asyncio.to_thread(_fetch_json, "https://api.alternative.me/fng/?limit=1")
            if isinstance(fg_raw, dict):
                fg_s = (fg_raw.get("data") or [{}])[0].get("value")
                fg_value = int(fg_s) if fg_s is not None else None
        except Exception as exc:
            logger.warning("Fear&Greed fetch failed: %s", exc)

        # 3) Context mood from internal fetcher
        ctx_payload = await get_market_mood_api()
        ctx_mood = str(ctx_payload.get("crypto_mood") or "NEUTRAL").upper()
        ctx_event_risk = bool(ctx_payload.get("active_event_risk"))
        ctx_news_sent = float(ctx_payload.get("avg_news_sentiment") or 0.0)

        # 4) Multi-source news sentiment
        news_payload = await get_crypto_news(limit=24)
        news_items = list(news_payload.get("items") or [])
        news_score, pos_hits, neg_hits = _sentiment_from_titles(news_items)

        # 5) Hero signals from live klines
        hero_syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
        hero_signals: list[dict] = []
        for sym in hero_syms:
            ticker = ticker_map.get(sym)
            if not ticker:
                continue
            try:
                kline_url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=50"
                kraw = await asyncio.to_thread(_fetch_json, kline_url)
                if not isinstance(kraw, list):
                    continue
                closes = [float(r[4]) for r in kraw if isinstance(r, list) and len(r) > 5]
                vols = [float(r[5]) for r in kraw if isinstance(r, list) and len(r) > 5]
                hero_signals.append(_hero_signal(sym, ticker, closes, vols))
            except Exception as exc:
                logger.warning("Hero signal calc failed for %s: %s", sym, exc)

        hero_signals.sort(key=lambda r: r.get("confidence", 0), reverse=True)

        # 6) Combined prediction score
        score = 0.0
        reasons: list[str] = []

        if btc_pct >= 2.0:
            score += 1.2
            reasons.append(f"BTC +{btc_pct:.2f}%")
        elif btc_pct <= -2.0:
            score -= 1.2
            reasons.append(f"BTC {btc_pct:.2f}%")

        if eth_pct >= 2.0:
            score += 0.8
            reasons.append(f"ETH +{eth_pct:.2f}%")
        elif eth_pct <= -2.0:
            score -= 0.8
            reasons.append(f"ETH {eth_pct:.2f}%")

        score += breadth * 2.0
        reasons.append(f"breadth {(breadth * 100):.0f}%")

        if fg_value is not None:
            if fg_value < 30:
                score += 0.7
                reasons.append(f"Fear&Greed {fg_value} (contrarian +)")
            elif fg_value > 70:
                score -= 0.7
                reasons.append(f"Fear&Greed {fg_value} (greed risk)")

        if ctx_mood == "RISK_ON":
            score += 0.8
            reasons.append("context mood risk-on")
        elif ctx_mood == "RISK_OFF":
            score -= 0.8
            reasons.append("context mood risk-off")

        if ctx_event_risk:
            score -= 0.5
            reasons.append("event risk active")

        if ctx_news_sent > 0.15:
            score += 0.5
            reasons.append("internal news sentiment +")
        elif ctx_news_sent < -0.10:
            score -= 0.5
            reasons.append("internal news sentiment -")

        if news_score > 0.10:
            score += 0.6
            reasons.append(f"external news positive ({pos_hits}/{len(news_items)})")
        elif news_score < -0.10:
            score -= 0.6
            reasons.append(f"external news negative ({neg_hits}/{len(news_items)})")

        direction = "NEUTRAL"
        if score >= 1.0:
            direction = "BULLISH_BIAS"
        elif score <= -1.0:
            direction = "BEARISH_BIAS"

        confidence = int(max(35, min(93, round(38 + abs(score) * 18))))

        return {
            "prediction": {
                "direction": direction,
                "confidence": confidence,
                "score": round(score, 3),
                "horizon": "6-24h",
                "reasons": reasons[:6],
                "inputs": {
                    "btc_24h_pct": round(btc_pct, 3),
                    "eth_24h_pct": round(eth_pct, 3),
                    "breadth": round(breadth, 4),
                    "fear_greed": fg_value,
                    "context_mood": ctx_mood,
                    "context_news_sentiment": round(ctx_news_sent, 4),
                    "external_news_sentiment": round(news_score, 4),
                },
            },
            "signals": hero_signals[:5],
            "sources": {
                "market": ["Binance Spot /api/v3"],
                "sentiment": ["Alternative.me Fear&Greed", "Internal context mood", "Crypto RSS headlines"],
                "news_ok": news_payload.get("sources_ok") or [],
                "news_failed": news_payload.get("failed_sources") or [],
            },
            "calculated_at": datetime.utcnow().isoformat(),
            "real_data": True,
        }
    except Exception as exc:
        logger.error("Crypto prediction failed: %s", exc)
        return {
            "prediction": {
                "direction": "NEUTRAL",
                "confidence": 20,
                "score": 0.0,
                "horizon": "6-24h",
                "reasons": ["Prediction unavailable due to upstream market/news fetch failure."],
                "inputs": {},
            },
            "signals": [],
            "sources": {
                "market": ["Binance Spot /api/v3"],
                "sentiment": ["Alternative.me", "Internal context", "Crypto RSS"],
                "news_ok": [],
                "news_failed": [],
            },
            "calculated_at": datetime.utcnow().isoformat(),
            "real_data": False,
            "error": str(exc),
        }



@router.get("/api/context/events")
async def get_context_events():
    """Return upcoming high-impact events in next 7 days."""
    try:
        fetcher = build_context_fetcher()
        events = await asyncio.to_thread(fetcher.get_upcoming_events)
        return {"events": events}
    except Exception as exc:
        logger.error("Context events fetch failed: %s", exc)
        return {"events": [], "error": str(exc)}


@router.get("/api/context/market-mood")
async def get_market_mood_api():
    """Return composite market mood payload for dashboard context strip."""
    try:
        fetcher = build_context_fetcher()
        btc_snapshot = await asyncio.to_thread(fetcher.get_snapshot_sync, "BTCUSDT")

        # Nifty from cached 5m stream if available.
        nifty_change = 0.0
        if "NIFTY 50" in svc._market_data and len(svc._market_data["NIFTY 50"]) >= 2:
            nifty_df = svc._market_data["NIFTY 50"]
            latest = float(nifty_df.iloc[-1]["close"])
            first = float(nifty_df.iloc[0]["open"]) if float(nifty_df.iloc[0]["open"]) != 0 else latest
            nifty_change = ((latest - first) / first) * 100.0 if first else 0.0

        # Average context sentiment over active symbols (cache-first, cheap after warmup).
        sentiments: list[float] = []
        active_event_risk = bool(btc_snapshot.event_risk_active)
        tracked = list(svc._signal_cache.keys())[:8]
        for sym in tracked:
            try:
                snap = await asyncio.to_thread(fetcher.get_snapshot_sync, sym)
                sentiments.append(float(snap.news_sentiment_24hr or 0.0))
                active_event_risk = active_event_risk or bool(snap.event_risk_active)
            except Exception:
                continue

        avg_sent = float(np.mean(sentiments)) if sentiments else float(btc_snapshot.news_sentiment_24hr or 0.0)
        events = await asyncio.to_thread(fetcher.get_upcoming_events)
        next_event = events[0] if events else None

        if active_event_risk or btc_snapshot.price_change_24hr_pct <= -1.0:
            crypto_mood = "RISK_OFF"
        elif btc_snapshot.price_change_24hr_pct >= 1.0 and avg_sent > 0.1:
            crypto_mood = "RISK_ON"
        else:
            crypto_mood = "NEUTRAL"

        # Read BTC dominance from context fetcher global cache if available.
        btc_dominance = float(
            fetcher._global_cache.get("coingecko_global", {}).get("payload", {}).get("btc_dominance", 0.0)  # type: ignore[attr-defined]
        )

        return {
            "crypto_mood": crypto_mood,
            "btc_dominance": round(btc_dominance, 2),
            "btc_24hr_change": round(float(btc_snapshot.price_change_24hr_pct or 0.0), 2),
            "nifty_24hr_change": round(float(nifty_change), 2),
            "avg_news_sentiment": round(avg_sent, 3),
            "active_event_risk": active_event_risk,
            "next_event": next_event,
        }
    except Exception as e:
        logger.error("Error fetching market mood: %s", e)
        return {
            "crypto_mood": "NEUTRAL",
            "btc_dominance": 0.0,
            "btc_24hr_change": 0.0,
            "nifty_24hr_change": 0.0,
            "avg_news_sentiment": 0.0,
            "active_event_risk": False,
            "next_event": None,
        }


@router.get("/api/context/{symbol}")
async def get_context_snapshot(symbol: str):
    """Return latest context snapshot for a symbol."""
    try:
        fetcher = build_context_fetcher()
        snapshot = await asyncio.to_thread(fetcher.get_snapshot_sync, symbol)
        return {
            "symbol": snapshot.symbol,
            "timestamp_utc": snapshot.timestamp_utc.isoformat(),
            "price_change_24hr_pct": snapshot.price_change_24hr_pct,
            "volume_change_24hr_pct": snapshot.volume_change_24hr_pct,
            "news_sentiment_24hr": snapshot.news_sentiment_24hr,
            "news_sentiment_7d": snapshot.news_sentiment_7d,
            "news_volume_spike": snapshot.news_volume_spike,
            "weekly_trend_bias": snapshot.weekly_trend_bias,
            "oi_change_7d_pct": snapshot.oi_change_7d_pct,
            "btc_dominance_7d_trend": snapshot.btc_dominance_7d_trend,
            "days_to_next_event": snapshot.days_to_next_event,
            "event_risk_active": snapshot.event_risk_active,
            "context_score": snapshot.context_score,
        }
    except Exception as exc:
        logger.error("Context snapshot failed for %s: %s", symbol, exc)
        return {"error": str(exc), "symbol": symbol}
