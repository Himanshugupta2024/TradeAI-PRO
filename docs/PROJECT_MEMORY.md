# TradeAI-PRO — Project Memory & System Architecture

**Last Updated**: 2026-11-02  
**Status**: Active Production-Grade Research & Paper-Trading Platform  
**Target Markets**: Indian Equities (NSE Nifty 50 / Nifty 500), Crypto Majors (Binance/CCXT), Forex (Twelve Data, paper-only)

---

## 1. High-Level Architecture Overview

TradeAI-PRO is a high-performance quantitative research, autonomous paper trading, and market intelligence platform. It operates under strict low-memory constraints (Render Free Tier 512MB RAM cap) with zero-cost data ingestion.

```text
Market Ingestion (yFinance / CCXT / Twelve Data)
        ↓
Data Validation & Cleaning (Data Brain V3)
        ↓
Market Data Router & In-Memory TTL Cache
        ↓
Technical Indicator & Feature Calculation (asyncio.to_thread)
        ↓
Two-Axis Market Regime Engine (Trend: Bull/Bear/Range × Vol: Low/Norm/High)
        ↓
AI Ensemble Inference (XGBoost + LightGBM + PyTorch LSTM + Baseline)
        ↓
Meta Ensemble & Dynamic Probability Calibration
        ↓
Quant Screener & Anomaly Risk Gate (QS Score ≥ 60, AI Conf ≥ 50%)
        ↓
Autonomous Execution Engine (PaperTradingEngine — Max 5 Multi-Asset Positions)
        ↓
Trade Manager (Breakeven Trail at 1R, 50% Partial at 1.5R, EOD 15:15 IST Square-off)
        ↓
TradeOutcomeLogger (SQLite trade_outcomes)
        ↓
Self-Learning Feedback Loop (FeedbackAnalyser + Bayesian WeightAdapter)
```

---

## 2. Directory Structure & Key Components

- **`api/`**:
  - `app.py`: FastAPI app initialization, lifespan management, warning filters, CORS, rate-limiting, and graceful startup/shutdown.
  - `routes.py`: HTTP/WebSocket endpoints (Public reads, protected admin/paper trade execution, SSE/WS price broadcast, dashboard overview).
  - `service.py`: Core background service layer, market data refresh loop, single-flight semaphore orchestration, cache manager, and signal snapshot persistence.
- **`agents/`**:
  - `data_ingestion/`: Market fetchers (`nse_fetcher.py`, `crypto_fetcher.py`, `forex_fetcher.py`, `context_fetcher.py`, `vix_fetcher.py`, `fii_dii_fetcher.py`, `delivery_breadth.py`, `derivatives_oi.py`).
  - `ai_engine/`: Model inference (`ensemble.py`, `xgboost_model.py`, `lstm_model.py`, `calibrator.py`, `meta_learner.py`, `anomaly_detector.py`).
  - `signal_engine/`: Quant signal generation (`signal_generator.py`) incorporating self-learned Bayesian weights (`w_rsi`, `w_ema`, `w_vol`, `w_macd`, `w_div`).
  - `market_intelligence/`: Setup scanners (`quant_screener.py`, `swing_scanner.py`, `intraday_scanner.py`, `breakout_scanner.py`, `morning_scan.py`).
  - `risk_execution/` & `execution/`: `paper_trading.py` (PaperTradingEngine, portfolio heat, circuit breaker, trailing stop manager).
  - `analytics/`: `feedback_loop.py` (TradeOutcomeLogger, FeedbackAnalyser, WeightAdapter, Bayesian shrinkage).
- **`data/`**: `router.py` (MarketDataRouter for vectorized batch downloads and circuit breakers).
- **`db/`**: `database.py` (PostgreSQL/Supabase & SQLite fallback engine), `models.py` (SQLAlchemy ORM models), `runtime_state.py` (Cold-boot runtime state store).
- **`dashboard/`**: Single-page frontend (`index.html`, `app.js`, `styles.css`, `crypto.html`).

---

## 3. Autonomous Trading & Self-Learning Architecture

1. **Autonomous Zero-Click Execution**:
   - Evaluates on every 5-minute candle close.
   - Triggers when `QS Score >= 60`, `AI Confidence >= 50%`, `Regime Aligned`, and `Risk Gate == SAFE`.
   - Max 5 concurrent multi-asset open positions (NSE Equities + Top Crypto).
2. **Smart Exits & Trailing**:
   - `+1R`: SL moved to breakeven (entry price) to secure risk-free status.
   - `+1.5R`: 50% partial profit booked.
   - `15:15 IST`: Equities auto-squared off to eliminate overnight gap risks.
   - `Crypto`: 24/7 dynamic ATR trailing stop.
3. **Continuous Self-Learning**:
   - Every closed trade logs execution telemetry to `trade_outcomes`.
   - Event-driven re-calibration every 10 closed trades + Daily 15:45 IST Post-Market Review.
   - Adapts weights in `config/learned_weights.json` via Bayesian shrinkage.

---

## 4. Deployment Constraints (Render Free Tier)

- **Memory Limit**: Strictly bounded to ≤ 400 MB RSS.
- **CPU**: Single core; heavy pandas/ML calculations offloaded to `asyncio.to_thread`.
- **Thread Count**: Thread pools pinned (`OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`, etc.).
- **Storage**: Ephemeral local disk on free tier web services; runtime state backed by SQLite/PostgreSQL.
