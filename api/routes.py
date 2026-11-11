"""
TradeAI PRO — Unified API Route Aggregator & Compatibility Barrel.
=================================================================
This module serves as the central router aggregator for the FastAPI application.
All route endpoints are organized by functional domain under `api.routers`:
  - `api.routers.health`: Health probes, readiness, memory, and telemetry.
  - `api.routers.admin`: Operator market control, autonomous worker, model sync, retraining.
  - `api.routers.research`: Next-day picks, edge scores, macro radar.
  - `api.routers.crypto`: Binance proxy, crypto meme radar, intelligence.
  - `api.routers.forex`: Twelve Data forex scanner, session tracking, pairs.
  - `api.routers.swing`: SwingSpectrum, intraday scanner, breakouts, morning scan.
  - `api.routers.portfolio`: Paper trading engine, portfolio management, PnL.
  - `api.routers.market`: Market data, quotes, regimes, overview, screener, search, websocket.

Backwards compatibility: All route handlers, internal caches, and helpers
are re-exported below so existing tests and consumers continue to work seamlessly.
"""

from __future__ import annotations

import logging
from fastapi import APIRouter

from api.security import require_admin, get_authenticated_account

# Import domain sub-routers
from api.routers.health import router as health_router
from api.routers.admin import router as admin_router
from api.routers.research import router as research_router
from api.routers.crypto import router as crypto_router
from api.routers.forex import router as forex_router
from api.routers.swing import router as swing_router
from api.routers.portfolio import router as portfolio_router
from api.routers.market import router as market_router
from api.routers.prediction import router as prediction_router

logger = logging.getLogger(__name__)

# Primary master router
router = APIRouter()

# Mount all domain routers in explicit dependency & precedence order
router.include_router(health_router)
router.include_router(admin_router)
router.include_router(research_router)
router.include_router(crypto_router)
router.include_router(forex_router)
router.include_router(swing_router)
router.include_router(portfolio_router)
router.include_router(market_router)
router.include_router(prediction_router)

# ══════════════════════════════════════════════════════════════════════════════
# RE-EXPORTS FOR BACKWARDS COMPATIBILITY & TEST SUITE INTEGRITY
# ══════════════════════════════════════════════════════════════════════════════

# Health & Telemetry
from api.routers.health import (
    favicon,
    health_check,
    readiness_check,
    get_system_memory_telemetry,
    get_system_data_health,
    liveness_check,
    health_ready_check,
    health_providers_check,
    _check_db_connectivity_with_retry,
)

# Admin & Autonomous Control
from api.routers.admin import (
    MarketControlToggleRequest,
    get_market_control_status_route,
    toggle_market_control_route,
    telegram_status,
    telegram_test_alert,
    invalidate_autonomous_status_cache,
    get_autonomous_status,
    get_autonomous_feed,
    trigger_autonomous_cycle,
    toggle_autonomous,
    trigger_eod_review_now,
    get_model_bundle_status,
    sync_model_bundle,
    get_learning_stats_endpoint,
    get_learning_outcomes,
    ensemble_validate,
    ensemble_status,
    retrain_xgboost,
    retrain_status,
    refresh_data,
    serve_admin,
    _AUTONOMOUS_STATUS_CACHE,
    _AUTONOMOUS_STATUS_CACHE_TS,
    _last_manual_cycle_dispatch_time,
    _ensemble_validation_job,
)

# Research & Next-Day Picks
from api.routers.research import (
    NextDayGenerateRequest,
    NextDayResolveRequest,
    get_next_day_picks_route,
    generate_next_day_picks_route,
    resolve_next_day_picks_route,
    get_edge_score,
    get_edge_top_setups,
    get_edge_macro_heatmap,
    get_edge_risk_gauge,
    get_macro_intelligence,
    get_macro_sector_impact,
)

# Crypto & Binance Proxies
from api.routers.crypto import (
    serve_crypto,
    get_meme_coin_signals,
    get_crypto_news,
    get_crypto_intelligence,
    proxy_binance_24hr,
    proxy_binance_klines,
    get_crypto_prediction,
    _binance_symbol_cache,
    _binance_klines_cache,
    _binance_proxy_cache,
)

# Forex Scanner & Rates
from api.routers.forex import (
    get_forex_rates,
    get_forex_signals,
    get_forex_intelligence,
    get_forex_signal,
    get_forex_sessions,
    get_forex_ohlcv_endpoint,
)

# Swing Spectrum & Intraday Scanners
from api.routers.swing import (
    get_swing_shortlist,
    get_swing_history,
    get_swing_setups,
    swing_scan_history,
    swing_scan_history_detail,
    swing_outcome_stats,
    swing_scan_now,
    intraday_run_scan,
    intraday_latest,
    intraday_symbol_detail,
    intraday_live_quote_endpoint,
    intraday_stats_endpoint,
    get_swing_for_symbol,
    get_swing_debug,
    get_index_movers,
    get_breakouts,
    get_morning_scan,
)

# Portfolio & Paper Trading
from api.routers.portfolio import (
    get_trades,
    get_track_record,
    get_risk_state,
    get_ai_weight_state,
    reset_risk_circuit,
    get_paper_scorecard,
    resolve_paper_trades,
    place_paper_trade_endpoint,
    get_open_paper_trades_endpoint,
    close_paper_trade_endpoint,
    close_paper_trades_bulk,
    set_paper_capital_endpoint,
    reset_paper_trading_endpoint,
    get_portfolio,
    get_performance,
)

# Market Data, Screener, Quotes & Websocket
from api.routers.market import (
    get_data_brain_v3_context,
    ai_ensemble_status,
    get_dashboard_overview,
    get_all_regimes,
    get_all_signals,
    get_signal_winrate,
    get_signal_leaderboard,
    get_symbols,
    _build_market_response,
    get_market_data,
    get_watchlist_quotes,
    get_current_signal,
    get_regime,
    get_stats,
    run_backtest,
    get_symbol_news,
    screener_gainers,
    screener_losers,
    sector_heatmap,
    get_scorecard,
    get_journal_signals,
    get_trade_flow,
    get_market_mood,
    get_ai_summary,
    get_overview_intelligence,
    websocket_prices,
    search_symbol_list,
    get_screener_opportunities,
    get_search_latency_stats,
    search_quote,
    serve_dashboard,
    serve_landing,
    serve_legal,
    get_nse_news,
    get_nse_prediction,
    get_context_events,
    get_market_mood_api,
    get_context_snapshot,
    fetch_ohlcv,
    fetch_arbitrary_symbol,
    _quote_latencies_ms,
    _NEGATIVE_SYMBOL_CACHE,
    _backtest_locks,
    _backtest_cache,
    _symbol_news_cache,
    _CONTEXT_MOOD_CACHE,
    _CRYPTO_PREDICTION_HISTORY,
    _NSE_PREDICTION_HISTORY,
)
