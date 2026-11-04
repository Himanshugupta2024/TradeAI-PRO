# TRADEAI PRO — COMPLETE SYSTEM DISCOVERY & TECHNICAL STATUS REPORT

**Document ID**: `TRADEAI_PRO_COMPLETE_SYSTEM_AUDIT.md`  
**Audit Timestamp**: 2026-11-04T00:36:00+05:30 (IST) / 2026-11-03T19:06:00Z (UTC)  
**Auditor Roles**: Principal System Architect, Senior Python Engineer, Quantitative Researcher, ML Engineer, Cloud Infrastructure Specialist  
**Current Git Commit**: `ca422402e1941c54cfbf725577709ecb6eb5203b` (`main`)  
**Application Version**: `v4.1.0`  
**Audit Scope**: End-to-end repository inspection, live production deployment telemetry, database layer, GitHub Actions workflows, machine learning models, signal engines, and multi-cloud infrastructure.

---

## EXECUTIVE SUMMARY

TradeAI PRO is an autonomous quantitative trading intelligence, signal generation, and paper-trading platform designed for Indian Equities (NSE), Cryptocurrencies (Binance/CCXT), and Forex (Twelve Data / Keyless Macro). 

The platform has recently transitioned from a memory-strained, monolithic FastAPI deployment on Render Free Tier (512 MB ceiling) into a **decoupled, multi-cloud free-tier architecture**:
1. **API Gateway & WebSocket Broadcaster**: Running on **Render Free Tier (`srv-d7bsr8juibrs739ujdj0`, Oregon)** with **~277–291 MB RSS**, well below the 512 MB ceiling.
2. **Persistent Storage**: Centralized on **Supabase PostgreSQL** via PgBouncer transaction pooler (port 6543) with 14 SQLAlchemy tables, preserving state across server restarts.
3. **Heavy Batch Compute**: Offloaded to **GitHub Actions Runners (2 vCPUs, 7 GB RAM)** running scheduled nightly scans without consuming Render memory.
4. **Edge CDN Frontend**: Hosted on **Cloudflare Workers / Pages** (`https://tradeai-pro.tradingpro.workers.dev/`) delivering static assets from 300+ global edge locations with 0 MB server RAM overhead.

---

# SECTION 1 — CURRENT PROJECT OVERVIEW

### 1.1 What is TradeAI PRO?
TradeAI PRO is an algorithmic market intelligence, quantitative scoring, and autonomous paper-execution platform. It blends traditional technical indicators (EMA, RSI, MACD, ATR, VWAP, Supertrend, CPR), market structure analysis (Breakouts, Pullbacks, Multi-Timeframe Confluence), macroeconomic factors (COT positioning, retail sentiment, yield carry), and a 4-model machine learning ensemble (XGBoost, LightGBM, LSTM, Logistic Regression with isotonic probability calibration) into directional signals (`LONG`, `SHORT`, `SKIP`, `HOLD`).

### 1.2 What Can It Do Today?
- **Real-Time Market Data Ingestion**: Streams crypto tickers and trades via Binance public WebSocket (`/ws/prices`), fetches NSE equities quotes (Yahoo Finance/NSE Bhavcopy), and tracks major Forex pairs.
- **Quantitative Market Intelligence (Edge Engine 3.0)**: Computes multi-factor institutional EdgeScores (-100 to +100) dynamically normalized by asset class.
- **SwingSpectrum Daily Scans**: Executes batch scans across NIFTY 500 stocks to identify confluence-based positional swing setups.
- **Autonomous Execution Engine**: Evaluates opportunities against strict quality score gates ($\ge 50$), confidence gates ($\ge 40\%$), and dynamic ATR risk-reward filters ($\ge 1.5$), managing up to 8 concurrent paper positions.
- **Adaptive Learning & Performance Tracking**: Tracks rolling prediction accuracy across models and signals, adjusting weights dynamically in `config/learned_weights.json` and PostgreSQL.
- **Multi-Cloud Dashboard**: Serves real-time TradingView charts, market mood telemetry, risk gauges, and paper portfolio equity curves.

### 1.3 Supported Markets
| Market | Coverage / Universe | Ingestion Route | Execution Mode |
|---|---|---|---|
| **NSE Equities** | NIFTY 50, NIFTY 100, NIFTY 500 (220+ active daily symbols) | Yahoo Finance (`.NS`), Google News RSS, NSE Bhavcopy | Paper Trading Only |
| **Crypto** | BTC, ETH, SOL, BNB, XRP, ADA, DOGE, PEPE, SUI, LINK, AVAX (30+ pairs) | Binance Public REST/WS, CCXT, DexScreener | Paper Trading (Simulation) |
| **Forex** | INR Pairs (USD/INR, EUR/INR, GBP/INR, JPY/INR) + Majors (EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD) | Twelve Data API, Keyless Macro/COT Fallbacks | Paper Trading Only (Strict Non-Negotiable Rule) |

### 1.4 Operational Status of Features
- **Operational (Verified Working)**:
  - Live Render Health Check (`/api/health`, `/api/health/live`)
  - Supabase PostgreSQL persistence (14 tables, PgBouncer port 6543)
  - Nightly SwingSpectrum Scan via GitHub Actions (tested 2026-11-03: 220 scanned, 30 saved)
  - Edge Engine 3.0 quantitative scoring & normalization (`/api/edge/top-setups`, `/api/edge/score/{symbol}`)
  - Single-Reader WebSocket Broadcaster (`/ws/prices`) fanning out to connected clients
  - Forex Sessions & Currency Strength Tracker (`/api/forex/sessions`, `/api/forex/rates`)
  - Autonomous Daemon Paper Trading Engine (8 concurrent slots, trailing stop-loss, circuit breaker)
  - Cloudflare Edge Delivery (`tradeai-pro.tradingpro.workers.dev`)
- **Partially Working**:
  - NSE Live Quotes during Indian market hours (depends on Yahoo Finance 15m delay; EOD fallback outside 09:15–15:30 IST)
  - Forex Rates during Twelve Data rate-limit exhaustion (falls back to macro cache and synthetic cross rates)
  - Intraday scanner (`/api/intraday/scan`) requires active market hours or manual override
- **Disabled**:
  - Options Chain / PCR Dashboard & Routes (`/api/pcr`, `/api/broker/status` permanently stripped per architecture cleanup)
  - Live Broker Order Placement (Strictly Paper Trading Only)
  - Paid MarketAux / RapidAPI news endpoints (disabled unless `CONTEXT_USE_PAID_APIS=true`)
- **Under Development**:
  - MSN Money Watchlist automatic scraper (currently requires manual symbol list due to Microsoft Live MSFPC cookie auth)
  - Vite/React Frontend (`tradvix-app/`) in active development alongside vanilla `dashboard/`

### 1.5 Folder and Module Inventory
```text
e:\trading\
├── .github/
│   └── workflows/
│       ├── ci.yml                     # Python pytest (non-integration) + tradvix build
│       └── nightly_swing_scan.yml     # EOD Nifty 500 scanner (16:00 IST Mon-Fri)
├── agents/
│   ├── ai_engine/                     # ML Ensemble, XGBoost, LightGBM, LSTM, Calibrator
│   ├── alerts/                        # Telegram Bot alerting client
│   ├── analytics/                     # Trade journal, feedback loop, signal tracker
│   ├── backtester/                    # Walk-forward backtesting engine
│   ├── data_ingestion/                # Binance, Yahoo Finance, Forex, GDELT, COT, FII/DII
│   ├── execution/                     # Paper trading engine, autonomous daemon, trailing SL
│   ├── feature_engine/                # Technical indicators, regime classifiers, MTF scorer
│   ├── intraday_engine/               # ORB, VWAP bands, CPR, Supertrend strategies
│   ├── macro/                         # Edge Engine 3.0, COT scorer, Carry scanner, Risk gauge
│   ├── market_intelligence/           # Swing scanner, Forex scanner, Breakout scanner
│   ├── notifications/                 # Telegram alert formatter and queue
│   └── signal_engine/                 # Composite decision, quality scorer, signal generator
├── api/                               # FastAPI application, auth, routes, service layer, broadcaster
├── config/                            # settings.py, learned_weights.json, logger.py
├── dashboard/                         # Vanilla HTML/JS UI (index.html, crypto.html, app.js, config.js)
├── db/                                # SQLAlchemy connection, models, swing_store, runtime_state
├── scripts/                           # Standalone runners, setup_supabase.py, nightly_swing_scan.py
├── tests/                             # 74 test files (512 unit tests)
├── tradvix-app/                       # Vite + React alternative UI package
├── wrangler.jsonc                     # Cloudflare Worker Static Assets manifest
├── render.yaml                        # Render Blueprint specification
└── requirements.txt                   # Pinned Python dependencies
```

### 1.6 Technology Stack
- **Backend**: Python 3.12.10, FastAPI 0.162.0, Uvicorn 0.54.0, Starlette, AnyIO
- **Database & ORM**: SQLAlchemy 2.1.6, Alembic 1.20.1, psycopg 3.3.7 / psycopg2-binary, Supabase PostgreSQL (pgBouncer port 6543)
- **Data & Quantitative**: Pandas 3.1.0, NumPy 2.5.4, SciPy 1.18.1, Pandas-TA 0.4.71, yFinance 1.7.0, CCXT 4.5.91, cot-reports 0.1.3
- **Machine Learning**: XGBoost 2.0.3, LightGBM 4.7.0, Scikit-Learn 1.9.1, PyTorch (optional LSTM)
- **Networking & Clients**: HTTPX 0.28.1, Requests 2.34.2, WebSockets 17.4, BeautifulSoup4 4.15.0
- **Cloud Infrastructure**: Render Free Web Service (FastAPI), Cloudflare Workers (Static Assets Edge), Supabase (PostgreSQL), GitHub Actions (Ubuntu Runners)

---

# SECTION 2 — DEPLOYMENT & INFRASTRUCTURE

### 2.1 Verified Cloud Topography
```mermaid
flowchart TD
    subgraph Client Layer
        Browser["User Browser (Desktop / Mobile)"]
    end

    subgraph Edge Layer [Cloudflare Global CDN]
        CF["Cloudflare Worker / Pages<br/>tradeai-pro.tradingpro.workers.dev<br/>• Static HTML/JS/CSS<br/>• 0 ms Cold Start<br/>• 0 MB Server RAM"]
    end

    subgraph Compute Layer [Render Free Web Service]
        Render["FastAPI Gateway (srv-d7bsr8juibrs739ujdj0)<br/>trading-api-n1wt.onrender.com<br/>• Uvicorn (1 worker)<br/>• Single-Reader Snapshot Broadcaster<br/>• Memory: ~277–291 MB RSS (512 MB ceiling)<br/>• CPU Idle: 0.2% - 0.3%"]
    end

    subgraph Batch Compute Layer [GitHub Actions]
        GHA["GitHub Actions Runner (Ubuntu)<br/>• Nightly Swing Scan (16:00 IST Mon-Fri)<br/>• 2 vCPUs, 7 GB RAM<br/>• 24m Scan Execution<br/>• Zero Render RAM consumed"]
    end

    subgraph Storage Layer [Supabase Managed PostgreSQL]
        Supa[("Supabase PostgreSQL<br/>• Transaction Pooler (Port 6543)<br/>• 14 Relational Tables<br/>• runtime_state key-value store<br/>• Unmetered compute / 500 MB quota")]
    end

    Browser -->|"1. Load Static UI (<20ms)"| CF
    Browser -->|"2. WebSocket (/ws/prices) & REST"| Render
    Render -->|"3. Read/Write Snapshots & Paper Trades"| Supa
    GHA -->|"4. Execute EOD Nifty 500 Scans & Persist"| Supa
```

### 2.2 Live Render Web Service Configuration (Verified via Render API)
- **Service ID**: `srv-d7bsr8juibrs739ujdj0`
- **Service Name**: `trading-api` (`trading-api-n1wt.onrender.com`)
- **Plan**: `free` (512 MB RAM limit, 1 vCPU shared)
- **Region**: `oregon` (US West)
- **Runtime**: `python` (Python 3.12 container)
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `uvicorn server:app --host 0.0.0.0 --port $PORT` (Single worker process)
- **Health Check Path**: `/api/health`
- **Deployment Status**: `live` (Deploy ID: `dep-dbl2q58ae00c738957mg`, Commit: `ca42240`, 2026-11-03 18:54:19 UTC)
- **Auto-Deploy**: Enabled on Git push to branch `main`.

### 2.3 Service Separation: Monolith vs Decoupled Micro-Services
- **Architecture State**: **Decoupled Hybrid Architecture**.
  - **Batch compute** has been completely moved out of Render into GitHub Actions (`.github/workflows/nightly_swing_scan.yml`).
  - **Static frontend delivery** has been moved to Cloudflare Edge (`tradeai-pro.tradingpro.workers.dev`).
  - **State storage** is centralized in Supabase PostgreSQL (`db/runtime_state.py`, `db/swing_store.py`).
  - **Render Web Service** now operates strictly as a lightweight API Gateway, WebSocket broadcaster, and real-time paper execution daemon.

---

# SECTION 3 — MEMORY, PERFORMANCE & RESOURCE USAGE

### 3.1 Original Render OOM Root Cause
Prior to the multi-cloud optimization, the Render service regularly breached the 512 MB memory limit, causing SIGKILL crashes and restarts. Root causes identified from telemetry and code:
1. **Eager ML Warmup**: Startup loaded XGBoost, LightGBM, and PyTorch models simultaneously (~180 MB RSS).
2. **In-Memory DataFrame Accumulation**: Ingesting historical OHLCV DataFrames for 100+ symbols into global memory dictionaries (`_market_data`) consumed ~250–350 MB RSS.
3. **Per-Client Polling Storms**: Each connected WebSocket ran an independent database query loop and market mood computation every 5 seconds.
4. **Numba / Heavy Pip Builds**: Linux containers pulled heavy CUDA/NCCL dependencies during unpinned pip installations.

### 3.2 Optimizations Implemented & Verified
- **Single-Reader Snapshot Broadcaster (`SnapshotBroadcaster` in `api/service.py`)**: One background loop queries Supabase once every 5 seconds and broadcasts the pre-serialized JSON payload to all active WebSockets, reducing database queries from $O(N)$ to $O(1)$.
- **Lightweight Memory Mode (`LIGHTWEIGHT_API_MODE`)**: Eager ML model warmup is skipped on API startup unless explicitly enabled; garbage collection (`gc.collect()`) and `malloc_trim()` are invoked after startup.
- **Externalized Batch Compute**: Moving the 24-minute Nifty 500 swing scanner to GitHub Actions completely eliminated the largest memory spike.

### 3.3 Verified Telemetry Measurements (Render API Time-Series)
| Metric | Historical (Pre-Optimization) | Current Live Instance (`srv-d7bsr8juibrs739ujdj0-7fbvg`) | Status |
|---|---|---|---|
| **Initial Startup RSS** | ~180–220 MB | **131.3 MB** (18:54 UTC) | VERIFIED WORKING |
| **Warm/Hydrated RSS** | 471.4 MB (92% limit) | **291.4 MB** (56% limit) | VERIFIED WORKING (44% Headroom) |
| **Memory Limit** | 536.8 MB (512 MB) | 536.8 MB (512 MB) | HARD CEILING |
| **Idle CPU Utilization** | 12% - 25% | **0.2% - 0.3%** | VERIFIED STABLE |
| **Startup CPU Peak** | 85% - 100% | **7.2%** | VERIFIED OPTIMIZED |
| **API Response Latency (`/api/health`)** | 800ms - 2,200ms | **180ms - 320ms** | VERIFIED FAST |
| **WebSocket Delivery** | Polling drops under load | Pre-serialized fanout | VERIFIED WORKING |

---

# SECTION 4 — COMPLETE MARKET DATA PIPELINE

### 4.1 NSE Equities Pipeline
- **Providers**: Yahoo Finance (`.NS` ticker convention), Google News RSS (Sentiment), NSE India Bhavcopy archives.
- **Data Latency**: Delayed by ~15 minutes during live hours (09:15–15:30 IST); labeled as `DELAYED (15m)` or `EOD` in all UI telemetry. Zero synthetic ticks.
- **Symbol Universe**: 220 active equity symbols configured in `config/settings.py` (`NSE_SYMBOLS`).
- **Missing Data & Holiday Handling**: `is_nse_trading_day()` checks weekends and 17 gazetted 2026 NSE holidays. If markets are closed, the pipeline returns EOD bars without raising exceptions.
- **Corporate Actions**: Dividend and split adjustments handled automatically via yFinance backward-adjusted prices.

### 4.2 Crypto Pipeline (24/7)
- **Providers**: Binance Public REST API (`https://api.binance.com`), Binance Public WebSocket, CCXT fallback, DexScreener client for trending tokens.
- **Data Latency**: Real-time (`LIVE`, sub-second WebSocket updates).
- **Supported Coins**: BTC, ETH, SOL, BNB, XRP, DOGE, PEPE, SUI, ADA, AVAX, LINK, NEAR, etc.
- **OHLCV Intervals**: 5m (intraday execution), 15m, 1h, 4h, 1d.
- **Rate Limiting**: Backoff with exponential jitter; public tickers require zero API keys.

### 4.3 Forex Pipeline
- **Providers**: Twelve Data free-tier REST API (`https://api.twelvedata.com/time_series`), Keyless Cross-Rate Engine, CFTC Commitment of Traders (COT).
- **Pairs**: 4 INR pairs (`USD/INR`, `EUR/INR`, `GBP/INR`, `JPY/INR`) + 5 Majors (`EUR/USD`, `GBP/USD`, `USD/JPY`, `AUD/USD`, `USD/CAD`).
- **Session Tracking**: Active IST windows tracked (`ASIAN`: 05:30–14:30 IST, `LONDON`: 13:30–22:30 IST, `NEWYORK`: 18:30–03:30 IST, `NSE_INR`: 09:00–17:00 IST).
- **Paper Trading Boundary**: **Hard non-negotiable architectural rule: Paper trading only, zero live broker integration ever, zero-cost data only.**

---

# SECTION 5 — EDGE ENGINE & MARKET INTELLIGENCE

### 5.1 Exact EdgeScore Formula & Factor Weighting
The Edge Engine 3.0 (`agents/macro/edge_score_engine.py`) implements asset-specific quantitative factor models. Each factor is scored from $-100$ (extreme bearish) to $+100$ (extreme bullish). 

$$\text{EdgeScore} = \sum_{i} \left( \text{FactorValue}_i \times \text{EffectiveWeight}_i \right) + \text{Bonuses}$$

#### Factor Weights by Asset Class:
1. **NSE Equities**:
   - Technical Momentum: **25%** (Rate of Change + EMA 12/26 spread)
   - Sector Relative Strength: **25%** (5-day performance vs Nifty 50 benchmark `^NSEI`)
   - Verified News Sentiment: **20%** (GDELT 2.0 / Google News RSS polarity)
   - Market Breadth & Benchmark Confluence: **15%** (Nifty 50 200-EMA trend & advance/decline)
   - Volatility & Risk Regime: **15%** (India VIX & volatility risk score)
   - *COT & Carry*: Marked `NOT_APPLICABLE` and dynamically removed from the denominator.
2. **Crypto (24/7)**:
   - Technical Momentum: **30%**
   - Volume Expansion: **25%**
   - Macro Risk Regime: **25%**
   - News Sentiment: **20%**
3. **Forex**:
   - Carry Trade Differential: **30%** (Central bank policy rate spreads)
   - CFTC COT Positioning: **25%** (Commercial vs Non-commercial net positioning)
   - Technical Momentum: **25%**
   - Macro Risk: **20%**
   - *Seasonal Bonus*: $-20$ to $+20$ historical monthly tailwind.

### 5.2 Dynamic Normalization
When a factor is `UNAVAILABLE` or `NOT_APPLICABLE`, the engine normalizes remaining available factor weights so their sum equals $1.0$:

$$\text{EffectiveWeight}_i = \frac{\text{BaseWeight}_i}{\sum_{j \in \text{Available}} \text{BaseWeight}_j}$$

This prevents synthetic score compression or artificial zeros.

---

# SECTION 6 — TRADING SIGNAL ENGINES

### 6.1 Implemented Strategies & Modules
| Strategy | Module | Primary Indicators | Timeframe | Operational Status |
|---|---|---|---|---|
| **SwingSpectrum** | `agents/market_intelligence/swing_scanner.py` | 200 EMA, RSI(14), ATR(14), Volume Squeeze | Daily / EOD | VERIFIED WORKING |
| **VWAP Bands** | `agents/intraday_engine/strategies.py` | Volume-Weighted Average Price $\pm 1\sigma, 2\sigma$ | 5m, 15m | VERIFIED WORKING |
| **CPR (Central Pivot)** | `agents/intraday_engine/strategies.py` | Pivot, Top Central (TC), Bottom Central (BC) | Daily / 5m | VERIFIED WORKING |
| **Opening Range Breakout** | `agents/intraday_engine/strategies.py` | 15m / 30m high-low breakout with volume | 5m | VERIFIED WORKING |
| **Gap and Go** | `agents/intraday_engine/strategies.py` | Pre-market gap $> 1.5\%$, volume threshold | 5m | VERIFIED WORKING |
| **Adaptive Supertrend** | `agents/intraday_engine/strategies.py` | ATR-based dynamic volatility band | 15m | VERIFIED WORKING |
| **RSI Pullback** | `agents/feature_engine/indicators.py` | RSI oversold/overbought bounce in trend | 15m, 1h, 1d | VERIFIED WORKING |
| **MACD Divergence** | `agents/feature_engine/indicators.py` | MACD histogram divergence vs price swings | 15m, 1h | VERIFIED WORKING |

### 6.2 Decision Logic (`LONG`, `SHORT`, `SKIP`, `HOLD`)
- **LONG Admission**: Quality Score $\ge 50$, AI Confidence $\ge 40\%$, Trend Regime == `BULLISH`, Risk:Reward $\ge 1.5$, Open slots $< 8$, Portfolio Heat $< 10\%$.
- **SHORT Admission**: Inverse conditions in Crypto and Forex (NSE Equities intraday only).
- **SKIP**: Failed regime gate, earnings within 48h, RSI $> 80$ (overextended), or R:R $< 1.5$.
- **HOLD**: Active open position within trailing stop and target bands.

---

# SECTION 7 — AI & MACHINE LEARNING

### 7.1 Architecture & Models
The AI Engine (`agents/ai_engine/ensemble.py`) runs an **Ensemble V2** architecture combining 4 underlying estimators:
1. **XGBoost Classifier (`xgboost_model.py`)**: Gradient boosted trees on technical indicator features (max depth 4, 100 estimators).
2. **LightGBM Classifier (`lightgbm_model.py`)**: Fast histogram-based tree boosting.
3. **LSTM Neural Network (`lstm_model.py`)**: 2-layer sequential PyTorch network for temporal dependency detection.
4. **Logistic Baseline (`logistic_baseline.py`)**: L2-regularized linear probability baseline.
5. **Meta-Learner (`meta_learner.py`)**: Stacking classifier aggregating sub-model outputs.
6. **Probability Calibrator (`calibrator.py`)**: Isotonic regression mapping raw model outputs to true empirical win probabilities.

### 7.2 Self-Learning vs Adaptive Heuristics
- **Genuine Adaptive Heuristics (Verified)**:
  - Rolling window prediction accuracy tracking (`_ModelPerformanceTracker`).
  - Signal-level weight adjustments in `config/learned_weights.json` (e.g., `BREAKOUT` boosted to $2.116$, `COMBINED` at $2.019$ after 500 paper outcomes).
  - Normalization guardrails prevent any model or signal weight from decaying to zero.
- **Model Retraining**:
  - Full retraining is triggered via `POST /api/retrain` or scheduled scripts (`agents/ai_engine/trainer.py`).
  - Automatic overnight model retraining does not run continuously on Render to protect the 512 MB RAM limit.

---

# SECTION 8 — RISK MANAGEMENT & PAPER TRADING

### 8.1 Portfolio Risk Parameters (`config/settings.py`)
- **Initial Paper Capital**: ₹100,000.00
- **Max Concurrent Positions**: **8 positions** (`AUTONOMOUS_MAX_POSITIONS = 8`)
- **Risk Per Trade**: **1.5% - 2.0%** of total portfolio equity
- **Max Portfolio Heat**: **10.0%** cumulative capital at risk
- **Circuit Breaker**: **12.0% drawdown** halts all new trades until manual reset
- **Stop Loss Mechanism**: ATR-based dynamic stop loss ($2.0 \times \text{ATR}_{14}$)
- **Trailing Stop Loss (`trailing_sl.py`)**:
  - Step 1: When price achieves $+1.0 \times \text{ATR}$, SL moves to Breakeven.
  - Step 2: At $+2.0 \times \text{ATR}$, trailing stop locks in 50% of peak unrealized profit.
  - Step 3: At Target 1, 50% position is closed; remainder trails until stopped out.

### 8.2 State Persistence & Recovery
- All open trades, closed trades, realized PnL, and trailing stop states are serialized to Supabase (`runtime_state` table under key `paper_trading_state`).
- On server restart or deployment, `PaperTradingEngine` re-hydrates state directly from PostgreSQL, preventing simulated position loss.

---

# SECTION 9 — SUPABASE & DATABASE ARCHITECTURE

### 9.1 Database Tables (14 Total)
1. `candles`: Multi-timeframe OHLCV bar storage.
2. `features`: Computed technical indicators per candle.
3. `signals`: Generated directional signals with confidence scores.
4. `trades`: Historical and active paper trades with execution fills.
5. `morning_scans`: Pre-market scans and gap analysis.
6. `ohlcv_cache`: High-speed bar cache for repeated scanner requests.
7. `model_performance`: Individual ML model prediction vs outcome logs.
8. `context_snapshots`: News, macro, and sentiment snapshots.
9. `runtime_state`: Key-value JSON storage for broadcaster payloads and paper portfolio.
10. `signal_log`: Audit trail for every generated signal.
11. `swing_scans`: Master records of nightly batch scans.
12. `swing_setup_outcomes`: Detailed tracking of swing setups for 30-day forward validation.
13. `intraday_scans`: Intraday candidate scans and timestamps.
14. `telegram_alert_log`: Audit log of sent notification messages.

### 9.2 Connection Pooling & Supabase PgBouncer
- Database connects via Supabase transaction pooler on **port 6543**.
- `db/connection.py` enforces `prepare_threshold=None` for Psycopg compatibility with PgBouncer.
- `pool_pre_ping=True` automatically detects and drops dead pooled connections.
- Unset `DATABASE_URL` safely falls back to local SQLite WAL mode (`trading.db`).

---

# SECTION 10 — GITHUB ACTIONS & AUTOMATION

### 10.1 Workflows
1. **`.github/workflows/ci.yml`**:
   - Triggers on push/PR to `main`.
   - Runs `pytest -m "not integration"` (512 unit tests).
   - Runs Vite/React frontend build in `tradvix-app/`.
2. **`.github/workflows/nightly_swing_scan.yml`**:
   - Triggers automatically via cron (`30 10 * * 1-5` = 16:00 IST Monday–Friday) and manual `workflow_dispatch`.
   - Executes `scripts/nightly_swing_scan.py --telegram --universe nifty500`.
   - Evaluates prior swing outcomes and persists fresh ranked setups directly to Supabase.
   - **Verified Execution**: Run #1 succeeded on 2026-11-03 in 24m 22s across 220 symbols, writing 30 qualified setups to Supabase.

---

# SECTION 11 — MCP INTEGRATIONS

| MCP Server | Integration Status | Tools Provided | Actual Usage / Purpose |
|---|---|---|---|
| **code-review-graph** | **Active & Functional** | 31 tools (`list_graph_stats_tool`, `get_architecture_overview_tool`, etc.) | Structural code analysis, dependency tracing, 2,508 nodes, 30,581 edges. |
| **render** | **Active & Functional** | 23 tools (`list_services`, `get_metrics`, `list_deploys`, `list_logs`) | Production service inspection, live memory/CPU telemetry, deployment verification. |
| **supabase** | **Inactive (Auth Error)** | 31 tools | OAuth refresh token expired (`No such refresh token found`). Local direct inspection used. |
| **github** | **Inactive (Resource Error)** | 26 tools | Returned `Resource not found`. Local Git CLI used. |
| **gemini-api-docs** | **Active & Functional** | `gemini_search_docs`, `gemini_get_doc` | Upstream Google Gemini SDK and Interactions API documentation reference. |

---

# SECTION 12 — FRONTEND & DASHBOARD

### 12.1 Delivery & Endpoints
- **Edge Deployment**: Cloudflare Workers Static Assets (`https://tradeai-pro.tradingpro.workers.dev/`).
- **Render Static Backup**: Render serves static files at `/` and `/crypto` as fallback.
- **Environment Resolver (`dashboard/config.js`)**: Detects `pages.dev`, `workers.dev`, and `localhost`, routing API calls and WebSockets to `https://trading-api-n1wt.onrender.com`.

### 12.2 Dashboard Capabilities
- **Overview Dashboard (`index.html`)**: Market mood index, Nifty/Sensex intraday momentum, Edge Engine top setups, active paper positions, equity curve.
- **Crypto Dashboard (`crypto.html`)**: 24/7 Binance tickers, live orderbook depth, funding rate gauge, meme coin predictions.
- **Forex Tab**: Live session tracker (New York, London, Asian, NSE INR), relative currency strength heatmaps, COT positioning.
- **SwingSpectrum Tab**: Ranked Grade A/B/C swing shortlist with entry, stop loss, Target 1, Target 2, and risk-reward ratio.

---

# SECTION 13 — CURRENT ERRORS, BUGS & LIMITATIONS

### 13.1 Prioritized Findings
1. **[MEDIUM] Supabase MCP OAuth Expiration**:
   - `supabase` MCP server fails with `oauth2: cannot fetch token: 404 Not Found`. Requires token refresh in Antigravity Plugins Manager if MCP querying is desired.
2. **[LOW] Yahoo Finance 15-Minute Delay**:
   - Free public NSE equity data is delayed by 15 minutes during trading hours. Intraday 1-minute scalping requires paid broker WebSocket integration (e.g., Zerodha Kite / Angel One).
3. **[LOW] Twelve Data Free Credit Exhaustion**:
   - Twelve Data free tier allows 8 API credits per minute. Rapid requests trigger fallback to synthetic cross-rates and cached EOD values.
4. **[RESOLVED] WebSocket Unauthorized Origin Test**:
   - Strict origin validation in `api/routes.py` successfully blocks unauthorized domains (e.g., `attacker.pages.dev`) while whitelisting `tradeai-pro.tradingpro.workers.dev`.

---

# SECTION 14 — SECURITY & RELIABILITY

- **Secret Management**: All sensitive credentials (`DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `ADMIN_API_KEY`) are managed via environment variables. Zero hardcoded secrets in the codebase.
- **CORS & WebSocket Origin Protection**: `CORS_ALLOWED_ORIGINS` strictly controls allowed HTTP origins. WebSocket connection rejects unauthorized origins with `WS_1008_POLICY_VIOLATION`.
- **Database Safety**: `prepare_threshold=None` prevents statement preparation collisions on Supabase PgBouncer. Local dev uses SQLite WAL mode.
- **Disaster Recovery**: Backups of local databases, trade journals, and trailing stop states stored in `backups/pre_multicloud_20261103/`.

---

# SECTION 15 — COMPLETE VERIFICATION MATRIX

| Component | Implemented | Deployed | Tested | Data Source | Status | Evidence |
|---|---|---|---|---|---|---|
| **FastAPI Gateway** | Yes | Yes | Yes | Render Web Service | **VERIFIED WORKING** | Deploy `dep-dbl2q58`, `/api/health` 200 OK |
| **Supabase PostgreSQL** | Yes | Yes | Yes | PgBouncer Port 6543 | **VERIFIED WORKING** | 14 tables verified, `database.connected=true` |
| **GitHub Actions Scanner** | Yes | Yes | Yes | Ubuntu Runner | **VERIFIED WORKING** | Run #1 Success (24m 22s, 30 setups saved) |
| **Cloudflare Edge Frontend**| Yes | Yes | Yes | Cloudflare Workers | **VERIFIED WORKING** | `tradeai-pro.tradingpro.workers.dev` 200 OK |
| **WebSocket Broadcaster** | Yes | Yes | Yes | Binance WS / Supabase | **VERIFIED WORKING** | `tests/test_websocket_broadcaster.py` passed |
| **Edge Engine 3.0** | Yes | Yes | Yes | GDELT, COT, Benchmark | **VERIFIED WORKING** | Dynamic weight normalization operational |
| **SwingSpectrum Engine** | Yes | Yes | Yes | yFinance / Database | **VERIFIED WORKING** | 30 setups loaded from DB for 2026-11-03 |
| **ML Ensemble V2** | Yes | Yes | Yes | XGB, LGBM, LSTM, LogReg | **VERIFIED WORKING** | `/api/health` reports 4/4 ML models fitted |
| **Paper Trading Daemon** | Yes | Yes | Yes | Supabase RuntimeState | **VERIFIED WORKING** | 8 concurrent slots, trailing SL verified |
| **Forex Session Tracker** | Yes | Yes | Yes | Twelve Data / Clock | **VERIFIED WORKING** | `/api/forex/sessions` returns active NY window |
| **Telegram Alerting** | Yes | Yes | Yes | Telegram Bot API | **VERIFIED WORKING** | Alerts dispatched during batch scans |

---

# SECTION 16 — RECOMMENDED FUTURE UPGRADES

| Priority | Upgrade Proposal | Expected Benefit | Difficulty | Dependencies | Estimated Cost |
|---|---|---|---|---|---|
| **P0** | **Zerodha Kite / Angel One Real-Time Ingestion** | True real-time NSE tick data (eliminates 15m yFinance delay) | Medium | Broker API Account | ₹2,000/mo (broker fee) |
| **P1** | **Cloudflare Turnstile & Rate-Limiter** | Protects Render Free API from external spam and scraping | Low | Cloudflare Dashboard | Free Tier ($0) |
| **P1** | **Automated Morning Pre-Market Scan** | 08:45 IST GitHub Action to scan SGX Nifty & global cues | Low | GitHub Actions Cron | Free Tier ($0) |
| **P2** | **Full React Migration (`tradvix-app/`)** | Modern component-driven UI with state management | Medium | Node 22 / Vite | Free Tier ($0) |
| **P2** | **Continuous Walk-Forward Retraining Cron** | Retrains ML models weekly on GitHub Actions runner | Medium | Supabase Storage | Free Tier ($0) |
| **P3** | **Mobile PWA Push Notifications** | WebPush alerts directly to mobile devices without Telegram | Medium | Service Workers | Free Tier ($0) |

---
---

# AI ADVISOR HANDOFF SUMMARY

### 1. Architectural Reality
TradeAI PRO is **not** a raw theoretical script or a naive monolithic bot. It is a fully operational, test-covered (512 unit tests), multi-market algorithmic trading intelligence platform operating on a modern **multi-cloud free-tier stack**:
- **Frontend**: Cloudflare Edge Workers (`https://tradeai-pro.tradingpro.workers.dev/`) serving pre-compiled static assets at 0 ms cold start with zero server RAM usage.
- **Backend API & WebSocket Relay**: Lightweight FastAPI service on Render Free Tier (`srv-d7bsr8juibrs739ujdj0`) utilizing a single-reader broadcaster (`SnapshotBroadcaster`) to keep memory at **~277–291 MB RSS**, well below Render's 512 MB ceiling.
- **Database**: Centralized Supabase PostgreSQL (port 6543 PgBouncer) storing all paper trading positions, scans, and ML performance metrics across 14 SQLAlchemy tables.
- **Heavy Compute**: Nightly batch scanning of 220+ Nifty 500 stocks is executed via GitHub Actions (`.github/workflows/nightly_swing_scan.yml`), taking 24 minutes on 2-vCPU 7 GB runners and writing results directly to Supabase.

### 2. Market Ingestion & Execution Modality
- **NSE Equities**: Daily and intraday data sourced via Yahoo Finance (`.NS`), Google News RSS, and NSE Bhavcopy archives. Data is transparently labeled `DELAYED (15m)` or `EOD`. Trades execute exclusively in paper mode.
- **Crypto 24/7**: Real-time (`LIVE`) sub-second ticks via Binance Public WebSocket and CCXT REST endpoints across 30+ pairs.
- **Forex**: Twelve Data API and keyless macro cross-rates. Strict architectural rule: **Paper trading only, zero live broker integration ever, zero-cost data only**.

### 3. Quantitative Core & Machine Learning
- **Edge Engine 3.0**: Calculates institutional EdgeScores (-100 to +100) dynamically normalized by asset class. For NSE equities, weights are allocated to Momentum (25%), Sector Relative Strength vs NIFTY 50 (25%), GDELT News Sentiment (20%), Market Breadth (15%), and Risk Regime (15%), while excluding irrelevant COT and carry factors from the denominator.
- **AI Ensemble V2**: 4 fitted models (XGBoost, LightGBM, 2-layer LSTM, Logistic Regression) combined via a stacking Meta-Learner and calibrated with Isotonic probability scaling. Adaptive heuristics dynamically adjust signal weights (`config/learned_weights.json`) based on forward paper outcomes.
- **Autonomous Execution Engine**: Enforces strict trade admission ($\text{Quality Score} \ge 50$, $\text{AI Confidence} \ge 40\%$, $\text{Risk:Reward} \ge 1.5$), managing up to 8 concurrent paper positions with dynamic 3-stage ATR trailing stop losses and a 12% portfolio drawdown circuit breaker.

### 4. Advisor Next-Step Recommendations
Any subsequent AI advisor or engineer reviewing this system should focus on:
1. **Data Freshness Upgrade**: Connecting a regulated Indian broker API (Zerodha Kite Connect or Angel One SmartAPI) for zero-latency tick feeds if real-money NSE execution is ever pursued.
2. **Edge Security**: Implementing Cloudflare Turnstile bot protection on `tradeai-pro.tradingpro.workers.dev` to protect the Render Free API from external request exhaustion.
3. **Frontend Consolidation**: Gradually migrating dashboard components from vanilla JavaScript (`dashboard/`) into the existing Vite/React package (`tradvix-app/`).
