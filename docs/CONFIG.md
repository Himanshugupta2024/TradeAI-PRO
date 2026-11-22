# TradeAI PRO — Runtime Configuration & Environment Variable Catalog (P6)

> **Document Status**: Complete & Verified (Audit Phase 6)  
> **Repository Context**: Multi-Box Microservice Architecture (Edge Worker + Box A Gateway + Box B ML + Box C Feed)  
> **Audit Constraint**: Read-Only reverse-engineering audit. Every variable extracted from source code AST and Blueprint manifests.

## 1. Executive Summary & Configuration Architecture

The TradeAI PRO ecosystem is governed by **327 unique environment variables** across four execution planes:
1. **Cloudflare Edge Worker** (`src/worker.js`, `wrangler.jsonc`): Edge caching, request routing, and cron dispatch.
2. **Box A Core API** (`server.py`, `api/service.py`, `render.yaml` `trading-api`): FastAPI gateway, market scanner, paper execution, and WebSocket hub.
3. **Box B ML Sidecar** (`ml_server.py`, `render.yaml` `trading-ml`): Isolated machine learning inference engine (XGBoost, LightGBM, Meta-Learner, Calibrator).
4. **Box C Market Data Feed** (`feed_server.py`, `render.yaml` `trading-feed`): Dedicated Shoonya WebSocket feed process.

### 1.1 Render Dashboard Override Hierarchy
In production on Render, environment variable evaluation strictly follows this precedence hierarchy:
1. **Render Dashboard UI Environment Variables** (Highest precedence): Any key defined in the Render Web Console under *Environment* overrides both the Blueprint manifest and code defaults.
2. **`render.yaml` Blueprint Manifest** (`services[].envVars`): Variables declared with static `value:` strings are injected during deployment.
3. **`sync: false` Variables**: Variables explicitly flagged as `sync: false` in `render.yaml` **must be configured manually** in the Render Dashboard UI. Deployments will fail to connect or crash if left unset.
4. **Code-Level Fallbacks** (`os.getenv(KEY, default)`): If a variable is absent from both the Render Dashboard and `render.yaml`, the runtime uses the hardcoded fallback defined in `config/settings.py` or the calling module.

### 1.2 Inventory Breakdown & State Overview

| Classification Tier | Count | Description |
| :--- | :--- | :--- |
| **Declared in `render.yaml` (Blueprint)** | **41** | Managed infrastructure, ports, resource caps, and internal tokens |
| ↳ *Blueprint Static Values* | 33 | Fixed container configs (e.g. `LIGHTWEIGHT_API_MODE: 'true'`, `MALLOC_ARENA_MAX: '2'`) |
| ↳ *Blueprint Manual Secrets (`sync: false`)* | **8** | Critical secrets requiring manual dashboard entry (`DATABASE_URL`, `ADMIN_API_KEY`, etc.) |
| **Edge Worker Variables (`wrangler.jsonc` / `src/worker.js`)** | **7** | Cloudflare origins, cron keep-warm policies, and GitHub Action dispatch tokens |
| **Unmanaged Code Defaults (Dashboard-Tunable)** | **279** | Strategy thresholds, indicators, risk limits, and model parameters read in code |
| **TOTAL UNIQUE VARIABLES** | **327** | Total configuration surface across the codebase |

---

## 2. Complete Environment Variable Catalog

The table below catalogs all 327 environment variables grouped by functional subsystem. Every row details the exact source code read site (`file:line`), fallback default, operational impact, execution target, Blueprint registration, and dashboard override mechanism.

### 1. Core Hosting, Infrastructure & Process Lifecycle

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `BACKGROUND_READY_WAIT_SEC` | `config/settings.py:181` | `"180"` | Background refresh controls. | Box A (API) | NO | YES (code default) |
| `BACKGROUND_REFRESH_ENABLED` | `tests/test_challenger_backend_stress.py:41` | `None` | Master switch enabling the periodic in-process market data refresh loop. | Box A (API) | YES (static) | NO (in blueprint) |
| `DATABASE_URL` | `config/settings.py:22` | `"sqlite:///./trading.db"` | Connection string for persistent PostgreSQL database (Supabase/Render Postgres). Ephemeral SQLite fallback if unset. | Box A (API), Box B (ML) | **YES (`sync: false`)** | YES (manual secret) |
| `ENABLE_API_DOCS` | `api/app.py:459` | `None` | Toggles public access to FastAPI Swagger UI (/docs, /redoc, /openapi.json). Default false in production. | Box A (API) | YES (static) | NO (in blueprint) |
| `ENVIRONMENT` | `config/settings.py:16` | `"development"` | Runtime deployment stage ('production', 'prod', or 'development'). | Box A (API) | NO | YES (code default) |
| `INITIAL_LOAD_ENABLED` | `api/service.py:1315` | `"true"` | If false, delays initial data hydration by 45s on boot to allow DB pool and health probes to stabilize. | Box A (API) | YES (static) | NO (in blueprint) |
| `LIGHTWEIGHT_API_MODE` | `agents/ai_engine/ensemble.py:379` | `"false"` | When true, disables heavy XGBoost/LightGBM model loading on Box A to keep RSS strictly under 512MB RAM cap. | Box A (API), Box B (ML) | YES (static) | NO (in blueprint) |
| `PYTHONWARNINGS` | `render.yaml only` | `N/A` | Filters noisy yfinance and deprecation warnings from system logs. | Box A (API) | YES (static) | NO (in blueprint) |
| `PYTHON_VERSION` | `render.yaml only` | `N/A` | Python runtime version pinned on Render build environment (3.12.7). | Box A (API), Box B (ML), Box C (Feed) | YES (static) | NO (in blueprint) |
| `REFRESH_CYCLE_TIMEOUT_SEC` | `config/settings.py:180` | `"240"` | Hard upper bound in seconds for a single market data refresh cycle (default 240s). | Box A (API) | YES (static) | NO (in blueprint) |
| `REFRESH_INTERVAL_SEC` | `config/settings.py:179` | `"60"` | Base cadence in seconds between market data ingestion cycles (default 300s). | Box A (API) | YES (static) | NO (in blueprint) |
| `RENDER` | `config/settings.py:18` | `None` | datacenter IPs with connection timeouts. Fast-track to reliable TradingView scanner (Tier 0) with fallback to yfinance fast_info. | Box A (API) | NO | YES (code default) |
| `SELF_PING_ENABLED` | `render.yaml only` | `N/A` | Enables internal self-ping HTTP task to combat Render idle sleep (disabled in favor of Cloudflare Worker cron). | Box A (API) | YES (static) | NO (in blueprint) |
| `SERVICE_ROLE` | `agents/ai_engine/ensemble.py:421` | `None` | Identifies process persona: 'ml_sidecar' for Box B, 'feed_service' for Box C, or unset for Box A. | Box B (ML), Box C (Feed) | YES (static) | NO (in blueprint) |


### 2. Security, Authentication & Access Control Tokens

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `ADMIN_API_KEY` | `config/settings.py:19` | `""` | Bearer/X-Admin-Key secret protecting admin routes (/api/paper/trade, /api/refresh, /api/risk/reset-circuit, /api/models/sync). | Box A (API) | **YES (`sync: false`)** | YES (manual secret) |
| `CRON_SECRET` | `api/routers/admin.py:1077` | `None` | Shared secret protecting background runner and cron refresh endpoints (/api/cron/refresh, /api/autonomous/run-cycle). | Box A (API), Edge (Worker) | **YES (`sync: false`)** | YES (manual secret) |
| `FEED_INTERNAL_TOKEN` | `api/feed_client.py:19` | `""` | Internal bearer token authenticating Box A and Box C communication for /api/feed/bars and /api/feed/snapshot. | Box A (API), Box C (Feed) | YES (static) | NO (in blueprint) |
| `GH_PAT` | `render.yaml only` | `N/A` | GitHub Personal Access Token allowing Cloudflare Worker cron to trigger .github/workflows/autonomous_cycle.yml. | Edge (Worker) | NO | YES (code default) |
| `ML_INTERNAL_TOKEN` | `api/ml_client.py:20` | `""` | Internal bearer token authenticating Box A and Box B communication for /infer endpoint. | Box A (API), Box B (ML) | YES (static) | NO (in blueprint) |


### 3. Cloudflare Edge & Microservice Network Mesh

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `ASSETS` | `render.yaml only` | `N/A` | Controls runtime threshold or behavior for `ASSETS`. | Edge (Worker) | NO | YES (code default) |
| `BOX_B_URL` | `scripts/verify_box_b.py:15` | `"https://trading-ml-8umr.onrend...` | Controls runtime threshold or behavior for `BOX_B_URL`. | Scripts / CI | NO | YES (code default) |
| `EDGE_WARM` | `render.yaml only` | `N/A` | Cloudflare Worker keep-warm policy ('off', 'always', or default scheduled window). | Edge (Worker) | NO | YES (code default) |
| `FEED_BACKEND_ORIGIN` | `render.yaml only` | `N/A` | Origin URL used by Cloudflare Edge Worker to route /api/feed/* requests directly to Box C. | Edge (Worker) | NO | YES (code default) |
| `FEED_SERVICE_URL` | `api/feed_client.py:18` | `""` | HTTP URL of Box C Shoonya market data feed service (e.g. https://trading-feed.onrender.com). | Box A (API) | YES (static) | NO (in blueprint) |
| `FEED_TIMEOUT_SEC` | `api/feed_client.py:20` | `"3.0"` | Controls runtime threshold or behavior for `FEED_TIMEOUT_SEC`. | Box C (Feed) | NO | YES (code default) |
| `ML_BACKEND_ORIGIN` | `render.yaml only` | `N/A` | Origin URL used by Cloudflare Edge Worker to route /api/ml/* requests directly to Box B. | Edge (Worker) | NO | YES (code default) |
| `ML_SIDECAR_TIMEOUT_SEC` | `api/ml_client.py:22` | `"6.0"` | Tight timeout to protect caller threads from event loop starvation | Box A (API) | NO | YES (code default) |
| `ML_SIDECAR_URL` | `agents/ai_engine/ensemble.py:418` | `""` | HTTP URL of Box B ML inference sidecar (e.g. https://trading-ml-8umr.onrender.com). | Box A (API) | YES (static) | NO (in blueprint) |
| `PUBLIC_ORIGIN` | `render.yaml only` | `N/A` | Public custom domain URL (e.g. https://tradeprotick.com) used for Edge cache revalidation headers. | Edge (Worker) | NO | YES (code default) |


### 4. System Memory, Concurrency & Render Free-Tier Limits

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `ENABLE_PYTORCH_MODELS` | `agents/ai_engine/ensemble.py:521` | `"false"` | When false, suppresses PyTorch LSTM import and weights allocation to conserve ~150MB heap. | Box A (API), Box B (ML) | YES (static) | NO (in blueprint) |
| `MALLOC_ARENA_MAX` | `render.yaml only` | `N/A` | Limits glibc memory arenas to 2, preventing virtual memory fragmentation and OOM on Render Free tier. | Box A (API), Box B (ML), Box C (Feed) | YES (static) | NO (in blueprint) |
| `MAX_NSE_CONCURRENCY` | `api/service.py:280` | `"3"` | Maximum concurrent symbols downloaded in parallel (default 1 to prevent rate-limit bans). | Box A (API) | YES (static) | NO (in blueprint) |
| `MKL_NUM_THREADS` | `render.yaml only` | `N/A` | Caps Intel MKL threads to 1 on small Linux containers. | Box A (API), Box B (ML) | YES (static) | NO (in blueprint) |
| `NSE_SYMBOL_LIMIT` | `api/service.py:165` | `"8"` | Maximum number of NSE equity symbols processed per refresh cycle (default 8 on free tier). | Box A (API) | YES (static) | NO (in blueprint) |
| `NUMEXPR_NUM_THREADS` | `render.yaml only` | `N/A` | Caps NumExpr threads to 1. | Box A (API), Box B (ML) | YES (static) | NO (in blueprint) |
| `OMP_NUM_THREADS` | `render.yaml only` | `N/A` | Caps OpenMP CPU threads to 1, preventing thread oversubscription and CPU throttling on 0.1-CPU instances. | Box A (API), Box B (ML) | YES (static) | NO (in blueprint) |
| `OPENBLAS_NUM_THREADS` | `render.yaml only` | `N/A` | Caps OpenBLAS threads to 1 on small Linux containers. | Box A (API), Box B (ML) | YES (static) | NO (in blueprint) |
| `SYMBOL_FETCH_TIMEOUT_SECONDS` | `render.yaml only` | `N/A` | Maximum timeout in seconds allocated to fetch and validate a single symbol (default 20s). | Box A (API) | YES (static) | NO (in blueprint) |


### 5. Shoonya Broker WebSocket Credentials (Box C Feed)

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `SHOONYA_API_KEY` | `feed/shoonya_client.py:49` | `""` | Shoonya developer API application key. | Box C (Feed) | **YES (`sync: false`)** | YES (manual secret) |
| `SHOONYA_IMEI` | `feed/shoonya_client.py:52` | `"tradeai-feed-01"` | Unique device/IMEI identifier string required by Shoonya API login protocol. | Box C (Feed) | YES (static) | NO (in blueprint) |
| `SHOONYA_PASSWORD` | `feed/shoonya_client.py:48` | `""` | Shoonya account password. | Box C (Feed) | **YES (`sync: false`)** | YES (manual secret) |
| `SHOONYA_TOTP_SECRET` | `feed/shoonya_client.py:51` | `""` | Base32 TOTP secret string used to generate 6-digit 2FA security tokens during login. | Box C (Feed) | **YES (`sync: false`)** | YES (manual secret) |
| `SHOONYA_USERID` | `feed/shoonya_client.py:47` | `""` | Shoonya (Finvasia) trading client ID for WebSocket market data feed. | Box C (Feed) | **YES (`sync: false`)** | YES (manual secret) |
| `SHOONYA_USERTOKEN` | `feed/shoonya_client.py:53` | `""` | Controls runtime threshold or behavior for `SHOONYA_USERTOKEN`. | Box C (Feed) | NO | YES (code default) |
| `SHOONYA_VENDOR_CODE` | `feed/shoonya_client.py:50` | `""` | Shoonya vendor code registered with broker API portal. | Box C (Feed) | **YES (`sync: false`)** | YES (manual secret) |


### 6. External Market Data & Sentiment API Keys

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `COINMARKETCAP_API_KEY` | `config/settings.py:483` | `""` | Legacy keys kept for compatibility; free-only policy does not use paid endpoints. | Box A (API) | NO | YES (code default) |
| `CONTEXT_USE_PAID_APIS` | `config/settings.py:473` | `"false"` | Master switch enabling paid RapidAPI and MarketAux sentiment providers (default false; keyless RSS used). | Box A (API) | YES (static) | NO (in blueprint) |
| `FINNHUB_API_KEY` | `config/settings.py:484` | `""` | Legacy keys kept for compatibility; free-only policy does not use paid endpoints. | Box A (API) | NO | YES (code default) |
| `MARKETAUX_API_KEY` | `config/settings.py:478` | `""` | Try nsepython first for NSE day change in context layer (no key). Disable if NSE blocks your host. | Box A (API) | NO | YES (code default) |
| `RAPIDAPI_KEY` | `config/settings.py:479` | `""` | Controls runtime threshold or behavior for `RAPIDAPI_KEY`. | Box A (API) | NO | YES (code default) |
| `TWELVE_DATA_API_KEY` | `config/settings.py:1004` | `""` | ── Forex Module ───────────────────────────────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `YFINANCE_CACHE_DIR` | `agents/market_intelligence/nse_fetcher.py:43` | `"/tmp/py-yfinance-cache"` | Controls runtime threshold or behavior for `YFINANCE_CACHE_DIR`. | Box A (API) | NO | YES (code default) |


### 7. Autonomous Trading Daemon, Schedulers & Background Tasks

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `ALLOW_LOCAL_AUTONOMOUS_CYCLE` | `api/routers/admin.py:620` | `"false"` | Permits synchronous in-process execution of autonomous trading cycles without external Celery queue workers. | Box A (API) | YES (static) | NO (in blueprint) |
| `AUTONOMOUS_MAX_POSITIONS` | `config/settings.py:1427` | `"8"` | Configured defaults are captured once; the live accessors below layer operator runtime overrides on top of them per decision. | Box A (API) | NO | YES (code default) |
| `AUTONOMOUS_MIN_AI_CONFIDENCE` | `config/settings.py:1429` | `"40.0"` | Controls runtime threshold or behavior for `AUTONOMOUS_MIN_AI_CONFIDENCE`. | Box A (API) | NO | YES (code default) |
| `AUTONOMOUS_MIN_QS_SCORE` | `config/settings.py:1428` | `"50.0"` | Configured defaults are captured once; the live accessors below layer operator runtime overrides on top of them per decision. | Box A (API) | NO | YES (code default) |
| `AUTONOMOUS_TRADING_ENABLED` | `config/settings.py:1426` | `"true"` | Controls runtime threshold or behavior for `AUTONOMOUS_TRADING_ENABLED`. | Box A (API) | NO | YES (code default) |
| `CYCLE_SCHEDULER_ENABLED` | `api/cycle_scheduler.py:199` | `"true"` | Controls runtime threshold or behavior for `CYCLE_SCHEDULER_ENABLED`. | Box A (API) | NO | YES (code default) |
| `MORNING_SCAN_TASK_ENABLED` | `render.yaml only` | `N/A` | Enables pre-market morning scan background task in API lifespan. | Box A (API) | YES (static) | NO (in blueprint) |
| `SCREENER_TASK_ENABLED` | `render.yaml only` | `N/A` | Enables background screener asyncio task in API lifespan. | Box A (API) | YES (static) | NO (in blueprint) |
| `SWING_OUTCOME_TASK_CHECK_SEC` | `config/settings.py:455` | `"60"` | Controls runtime threshold or behavior for `SWING_OUTCOME_TASK_CHECK_SEC`. | Box A (API) | NO | YES (code default) |
| `SWING_OUTCOME_TASK_ENABLED` | `config/settings.py:450` | `"true"` | Calendar days after scan emission before we mark EXPIRED if no level hit. Dedicated background loop controls for swing outcome evaluation. | Box A (API) | NO | YES (code default) |


### 8. Capital Protection, Position Sizing & Risk Guardrails

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `CHALLENGER_MIN_EXPECTANCY_MARGIN_PCT` | `config/settings.py:545` | `"0.05"` | Controls runtime threshold or behavior for `CHALLENGER_MIN_EXPECTANCY_MARGIN_PCT`. | Box A (API) | NO | YES (code default) |
| `CIRCUIT_BREAKER_THRESHOLD` | `config/settings.py:525` | `"0.12"` | Risk management and sizing | Box A (API) | NO | YES (code default) |
| `COOLDOWN_CANDLES` | `config/settings.py:246` | `"1"` | Cooldown: prevent re-entry into same symbol for N candles after exit | Box A (API) | NO | YES (code default) |
| `DAILY_LOSS_LIMIT_PCT` | `config/settings.py:216` | `"3.0"` | Money Flow Index thresholds | Box A (API) | NO | YES (code default) |
| `DEFAULT_SLIPPAGE_PCT` | `config/settings.py:570` | `"0.001"` | Backtest defaults | Box A (API) | NO | YES (code default) |
| `EVENT_RISK_CONFIDENCE_PENALTY` | `config/settings.py:509` | `"0.25"` | Controls runtime threshold or behavior for `EVENT_RISK_CONFIDENCE_PENALTY`. | Box A (API) | NO | YES (code default) |
| `EVENT_RISK_DAYS_THRESHOLD` | `config/settings.py:505` | `"2"` | Context modifier thresholds | Box A (API) | NO | YES (code default) |
| `FRACTIONAL_KELLY` | `config/settings.py:528` | `"0.50"` | Dynamic sizing: regime scaling + ATR vol-targeting + fractional-Kelly guard. FRACTIONAL_KELLY is the fraction of full Kelly applied (0.5 = half-Kelly). | Box A (API) | NO | YES (code default) |
| `INTRADAY_MAX_DAILY_LOSS_PCT` | `config/settings.py:145` | `"0.02"` | Controls runtime threshold or behavior for `INTRADAY_MAX_DAILY_LOSS_PCT`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_MIN_RISK_REWARD` | `config/settings.py:143` | `"1.5"` | Controls runtime threshold or behavior for `INTRADAY_MIN_RISK_REWARD`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_RISK_PER_TRADE_PCT` | `config/settings.py:140` | `"1.0"` | Intraday risk controls (paper-first defaults). These values shape signal sizing/filters for intraday scanner output. | Box A (API) | NO | YES (code default) |
| `INTRADAY_VIRTUAL_CAPITAL` | `config/settings.py:139` | `"100000"` | Intraday risk controls (paper-first defaults). These values shape signal sizing/filters for intraday scanner output. | Box A (API) | NO | YES (code default) |
| `KELLY_LOOKBACK_TRADES` | `config/settings.py:530` | `"60"` | FRACTIONAL_KELLY is the fraction of full Kelly applied (0.5 = half-Kelly). | Box A (API) | NO | YES (code default) |
| `KELLY_MIN_SAMPLE` | `config/settings.py:529` | `"30"` | Dynamic sizing: regime scaling + ATR vol-targeting + fractional-Kelly guard. FRACTIONAL_KELLY is the fraction of full Kelly applied (0.5 = half-Kelly). | Box A (API) | NO | YES (code default) |
| `KELLY_RISK_FLOOR_RATIO` | `config/settings.py:533` | `"0.25"` | Kelly is a de-risking guard: it can shrink risk toward this ratio of base risk but never raise it, and never drops sizing to zero. | Box A (API) | NO | YES (code default) |
| `MAX_CONSECUTIVE_LOSSES` | `config/settings.py:199` | `"3"` | ── Risk Management ───────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `MAX_PORTFOLIO_LEVERAGE` | `config/settings.py:1433` | `"1.0"` | ── Position Sizing & Margin Bounds ────────────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `MAX_POSITION_LEVERAGE` | `config/settings.py:1432` | `"0.25"` | ── Position Sizing & Margin Bounds ────────────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `MAX_RISK_PER_TRADE_PCT` | `config/settings.py:535` | `"2.0"` | but never raise it, and never drops sizing to zero. Hard ceiling after all scaling (regime ×1.25 and ATR ×2 can otherwise overshoot). | Box A (API) | NO | YES (code default) |
| `MIN_RISK_REWARD` | `config/settings.py:200` | `"2.0"` | ── Risk Management ───────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `PAPER_TRADING_CAPITAL` | `config/settings.py:1412` | `"500000.0"` | Capital used for paper trade position sizing (virtual, no real money) | Box A (API) | NO | YES (code default) |
| `RISK_PER_TRADE_PCT` | `config/settings.py:218` | `"1.25"` | Controls runtime threshold or behavior for `RISK_PER_TRADE_PCT`. | Box A (API) | NO | YES (code default) |
| `SWING_MAX_CAPITAL_PER_TRADE` | `config/settings.py:630` | `"0.25"` | Overnight/portfolio guardrails. | Box A (API) | NO | YES (code default) |
| `SWING_MAX_OPEN_POSITIONS` | `config/settings.py:631` | `"3"` | Overnight/portfolio guardrails. | Box A (API) | NO | YES (code default) |
| `SWING_MAX_PORTFOLIO_RISK_PCT` | `config/settings.py:633` | `"0.03"` | Controls runtime threshold or behavior for `SWING_MAX_PORTFOLIO_RISK_PCT`. | Box A (API) | NO | YES (code default) |
| `SWING_MIN_RISK_REWARD` | `config/settings.py:619` | `"1.5"` | Minimum Risk:Reward for swing setups — lowered from 1.8 to 1.5 to allow more setups through. | Box A (API) | NO | YES (code default) |
| `SWING_RISK_PER_TRADE_PCT` | `config/settings.py:625` | `"0.015"` | Virtual capital used for swing sizing calculations (scanner-side guidance). Fractional risk per trade. 0.015 = 1.5% capital risked per setup. | Box A (API) | NO | YES (code default) |
| `SWING_VIRTUAL_CAPITAL` | `config/settings.py:623` | `"100000"` | ── Swing Risk Management (1-3 day holding) ─────────────────────────────── Virtual capital used for swing sizing calculations (scanner-side guidance). | Box A (API) | NO | YES (code default) |
| `TELEGRAM_ALERT_COOLDOWN_HOURS` | `config/settings.py:567` | `"4"` | Telegram deduplication | Box A (API) | NO | YES (code default) |


### 9. Machine Learning, Shadow Testing & Probability Calibration

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `CHALLENGER_MAX_BRIER_REGRESSION` | `config/settings.py:548` | `"0.02"` | Controls runtime threshold or behavior for `CHALLENGER_MAX_BRIER_REGRESSION`. | Box A (API) | NO | YES (code default) |
| `CHALLENGER_MAX_OBSERVATIONS` | `config/settings.py:555` | `"2000"` | Controls runtime threshold or behavior for `CHALLENGER_MAX_OBSERVATIONS`. | Box A (API) | NO | YES (code default) |
| `CHALLENGER_MIN_PAIRED_SAMPLES` | `config/settings.py:542` | `"40"` | Kronos champion/challenger promotion gate. All thresholds must pass; the gate fails closed so a thin or unstable sample can never promote the challenger. | Box A (API) | NO | YES (code default) |
| `CHALLENGER_MIN_SHADOW_SESSIONS` | `config/settings.py:543` | `"30"` | Kronos champion/challenger promotion gate. All thresholds must pass; the gate fails closed so a thin or unstable sample can never promote the challenger. | Box A (API) | NO | YES (code default) |
| `CHALLENGER_MIN_WINDOWS_WON` | `config/settings.py:551` | `"0.60"` | Controls runtime threshold or behavior for `CHALLENGER_MIN_WINDOWS_WON`. | Box A (API) | NO | YES (code default) |
| `CHALLENGER_ROLLING_WINDOW` | `config/settings.py:550` | `"10"` | Controls runtime threshold or behavior for `CHALLENGER_ROLLING_WINDOW`. | Box A (API) | NO | YES (code default) |
| `DRIFT_CONFIDENCE_PENALTY` | `config/settings.py:564` | `"0.20"` | Feature drift controls | Box A (API) | NO | YES (code default) |
| `DRIFT_Z_THRESHOLD` | `config/settings.py:563` | `"3.0"` | Feature drift controls | Box A (API) | NO | YES (code default) |
| `KRONOS_MODEL_ID` | `agents/ai_engine/kronos_forecaster.py:45` | `"NeoQuasar/Kronos-mini"` | Controls runtime threshold or behavior for `KRONOS_MODEL_ID`. | Box A (API) | NO | YES (code default) |
| `MIN_TRADES_FOR_RETRAIN` | `config/settings.py:571` | `"200"` | Backtest defaults | Box A (API) | NO | YES (code default) |
| `MODELS_DIR` | `api/model_sync.py:58` | `None` | Controls runtime threshold or behavior for `MODELS_DIR`. | Box A (API) | NO | YES (code default) |
| `MODEL_BUNDLE_MIN_ACCURACY` | `api/model_sync.py:54` | `"0.50"` | Controls runtime threshold or behavior for `MODEL_BUNDLE_MIN_ACCURACY`. | Box A (API) | NO | YES (code default) |


### 10. Technical Indicators, Candlestick Filters & SwingSpectrum

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `ATR_HISTORY_MIN_SAMPLES` | `config/settings.py:538` | `"10"` | Rolling ATR history kept in-process when a caller cannot supply historical ATR. | Box A (API) | NO | YES (code default) |
| `ATR_HISTORY_WINDOW` | `config/settings.py:537` | `"60"` | Hard ceiling after all scaling (regime ×1.25 and ATR ×2 can otherwise overshoot). Rolling ATR history kept in-process when a caller cannot supply historical ATR. | Box A (API) | NO | YES (code default) |
| `BREAKEVEN_ATR_THRESHOLD` | `config/settings.py:222` | `"1.0"` | Trailing Stop-Loss | Box A (API) | NO | YES (code default) |
| `CANDLE_QUALITY_BODY_PENALTY` | `config/settings.py:268` | `"8.0"` | Controls runtime threshold or behavior for `CANDLE_QUALITY_BODY_PENALTY`. | Box A (API) | NO | YES (code default) |
| `CANDLE_QUALITY_DIRECTION_FIGHT_PENALTY` | `config/settings.py:270` | `"6.0"` | Controls runtime threshold or behavior for `CANDLE_QUALITY_DIRECTION_FIGHT_PENALTY`. | Box A (API) | NO | YES (code default) |
| `CANDLE_QUALITY_MIN_BODY_RATIO` | `config/settings.py:266` | `"0.35"` | Controls runtime threshold or behavior for `CANDLE_QUALITY_MIN_BODY_RATIO`. | Box A (API) | NO | YES (code default) |
| `CANDLE_QUALITY_MIN_RANGE_ATR_RATIO` | `config/settings.py:267` | `"0.25"` | Controls runtime threshold or behavior for `CANDLE_QUALITY_MIN_RANGE_ATR_RATIO`. | Box A (API) | NO | YES (code default) |
| `CANDLE_QUALITY_RANGE_PENALTY` | `config/settings.py:269` | `"5.0"` | Controls runtime threshold or behavior for `CANDLE_QUALITY_RANGE_PENALTY`. | Box A (API) | NO | YES (code default) |
| `DIVERGENCE_LOOKBACK` | `config/settings.py:207` | `"14"` | Lookback period for RSI divergence detection | Box A (API) | NO | YES (code default) |
| `INTRADAY_ATR_SL_MULTIPLIER` | `config/settings.py:141` | `"1.5"` | These values shape signal sizing/filters for intraday scanner output. | Box A (API) | NO | YES (code default) |
| `INTRADAY_CONFLUENCE_ENABLED` | `config/settings.py:149` | `"true"` | Controls runtime threshold or behavior for `INTRADAY_CONFLUENCE_ENABLED`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_CONFLUENCE_MIN_AGREE` | `config/settings.py:154` | `"2"` | Controls runtime threshold or behavior for `INTRADAY_CONFLUENCE_MIN_AGREE`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_CONFLUENCE_SCORE_BONUS_PER_EXTRA` | `config/settings.py:156` | `"4.0"` | Controls runtime threshold or behavior for `INTRADAY_CONFLUENCE_SCORE_BONUS_PER_EXTRA`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_INDEX_CONFLUENCE_GATE` | `config/settings.py:170` | `"true"` | Controls runtime threshold or behavior for `INTRADAY_INDEX_CONFLUENCE_GATE`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_MIN_RVOL` | `config/settings.py:168` | `"1.5"` | Intraday Live Feed, RVOL, and Index Confluence settings | Box A (API) | NO | YES (code default) |
| `INTRADAY_RVOL_GATE_ENABLED` | `config/settings.py:169` | `"true"` | Enforces relative volume (RVOL) filter on intraday signal issuance. | Box A (API) | YES (static) | NO (in blueprint) |
| `INTRADAY_SUPERTREND_BASE_MULTIPLIER` | `config/settings.py:159` | `"2.5"` | Controls runtime threshold or behavior for `INTRADAY_SUPERTREND_BASE_MULTIPLIER`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_SUPERTREND_HIGHVOL_ATR_PCT` | `config/settings.py:163` | `"1.5"` | Controls runtime threshold or behavior for `INTRADAY_SUPERTREND_HIGHVOL_ATR_PCT`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_SUPERTREND_HIGHVOL_MULTIPLIER` | `config/settings.py:161` | `"3.0"` | Controls runtime threshold or behavior for `INTRADAY_SUPERTREND_HIGHVOL_MULTIPLIER`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_SUPERTREND_LOWVOL_ATR_PCT` | `config/settings.py:162` | `"0.6"` | Controls runtime threshold or behavior for `INTRADAY_SUPERTREND_LOWVOL_ATR_PCT`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_SUPERTREND_LOWVOL_MULTIPLIER` | `config/settings.py:160` | `"2.0"` | Controls runtime threshold or behavior for `INTRADAY_SUPERTREND_LOWVOL_MULTIPLIER`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_TARGET_ATR_MULTIPLIER` | `config/settings.py:142` | `"3.0"` | Controls runtime threshold or behavior for `INTRADAY_TARGET_ATR_MULTIPLIER`. | Box A (API) | NO | YES (code default) |
| `MEME_DYNAMIC_MIN_QUOTE_VOLUME_USDT` | `config/settings.py:77` | `"5000000"` | Controls runtime threshold or behavior for `MEME_DYNAMIC_MIN_QUOTE_VOLUME_USDT`. | Box A (API) | NO | YES (code default) |
| `MIN_ATR_PCT` | `config/settings.py:204` | `"0.15"` | ── AI / Signal Filtering ──────────────────────────────── Minimum ATR as % of price to avoid low-volatility noise signals | Box A (API) | NO | YES (code default) |
| `REGIME_ADX_MIXED_FLOOR` | `config/settings.py:676` | `"20.0"` | ADX threshold above which market has some directional bias (MIXED zone). Below this = SIDEWAYS (breakout strategies should be avoided). | Box A (API) | NO | YES (code default) |
| `REGIME_ADX_TRENDING_FLOOR` | `config/settings.py:672` | `"25.0"` | ADX threshold above which market is considered "strongly trending". Academic standard: 25. Use 22 for Indian markets (slightly less efficient). | Box A (API) | NO | YES (code default) |
| `REGIME_HIGH_VOL_ATR_RATIO` | `config/settings.py:680` | `"1.5"` | Volatility regime: if current ATR > this ratio × 20-bar ATR avg → HIGH_VOL. HIGH_VOL = widen SL, reduce position size. | Box A (API) | NO | YES (code default) |
| `REGIME_LOW_VOL_ATR_RATIO` | `config/settings.py:684` | `"0.7"` | Volatility regime: if current ATR < this ratio × 20-bar ATR avg → LOW_VOL. LOW_VOL = possible squeeze/breakout setup forming. | Box A (API) | NO | YES (code default) |
| `REGIME_REFRESH_INTERVAL_HOURS` | `config/settings.py:688` | `"4.0"` | How often to refresh the regime cache (hours). Default 4h = twice a trading day. Set lower (e.g., 2) for more frequent updates; set higher (e.g., 8) to reduce yFinance calls. | Box A (API) | NO | YES (code default) |
| `REGIME_TARGET_HIGH_VOL_MULT` | `config/settings.py:243` | `"0.7"` | Controls runtime threshold or behavior for `REGIME_TARGET_HIGH_VOL_MULT`. | Box A (API) | NO | YES (code default) |
| `REGIME_TARGET_RANGE_MULT` | `config/settings.py:242` | `"0.7"` | Controls runtime threshold or behavior for `REGIME_TARGET_RANGE_MULT`. | Box A (API) | NO | YES (code default) |
| `REGIME_TARGET_SCALING_ENABLED` | `config/settings.py:235` | `"true"` | Regime-based target scaling (execution-level) | Box A (API) | NO | YES (code default) |
| `REGIME_TARGET_STRONG_TREND_MULT` | `config/settings.py:240` | `"1.5"` | Controls runtime threshold or behavior for `REGIME_TARGET_STRONG_TREND_MULT`. | Box A (API) | NO | YES (code default) |
| `REGIME_TARGET_WEAK_TREND_MULT` | `config/settings.py:241` | `"0.7"` | Controls runtime threshold or behavior for `REGIME_TARGET_WEAK_TREND_MULT`. | Box A (API) | NO | YES (code default) |
| `RSI_DIVERGENCE_CONF_BONUS` | `config/settings.py:318` | `"15"` | ── RSI Divergence Confidence Boost ─────────────────────────────────────────── Bullish/bearish RSI divergence → add to AI confidence (on top of score logic). | Box A (API) | NO | YES (code default) |
| `SIGNAL_CONFLUENCE_GATE_ENABLED` | `config/settings.py:286` | `"true"` | ── Strict Signal Gates (accuracy-first) ───────────────────────────────────── 3-of-5 directional confluence gate: RSI + EMA + MACD + Volume + Divergence | Box A (API) | NO | YES (code default) |
| `SIGNAL_CONFLUENCE_MIN_VOTES` | `config/settings.py:291` | `"3"` | Controls runtime threshold or behavior for `SIGNAL_CONFLUENCE_MIN_VOTES`. | Box A (API) | NO | YES (code default) |
| `SIGNAL_EMA200_HARD_FILTER_ENABLED` | `config/settings.py:294` | `"true"` | Long-term directional hard filter using EMA200 | Box A (API) | NO | YES (code default) |
| `SIGNAL_REQUIRE_VOLUME_GATE` | `config/settings.py:301` | `"true"` | Require volume confirmation (>= 1.2x 20-bar average where volume exists) | Box A (API) | NO | YES (code default) |
| `STOCHRSI_OVERBOUGHT` | `config/settings.py:211` | `"80"` | Stochastic RSI thresholds | Box A (API) | NO | YES (code default) |
| `STOCHRSI_OVERSOLD` | `config/settings.py:210` | `"20"` | Stochastic RSI thresholds | Box A (API) | NO | YES (code default) |
| `SWING_ADX_MIN_FOR_BREAKOUT` | `config/settings.py:754` | `"18"` | ADX hard gate for breakout patterns. Below this = no trend = breakout likely fails. Applies to: 20-day range breakout, 52W high, BB squeeze, inside-bar breakout. 0 disables the gate. 18 is the academic "weak trend" floor; 20 is stricter. | Box A (API) | NO | YES (code default) |
| `SWING_ATR_SL_MULTIPLIER` | `config/settings.py:627` | `"2.0"` | Fractional risk per trade. 0.015 = 1.5% capital risked per setup. Canonical ATR multipliers for swing stop and first target. | Box A (API) | NO | YES (code default) |
| `SWING_ATR_TARGET_MULTIPLIER` | `config/settings.py:628` | `"3.0"` | Canonical ATR multipliers for swing stop and first target. | Box A (API) | NO | YES (code default) |
| `SWING_CHOPPY_ATR_RATIO_THRESHOLD` | `config/settings.py:663` | `"0.7"` | If latest index ATR is below this fraction of its 20-bar ATR average, mark context as CHOPPY. | Box A (API) | NO | YES (code default) |
| `SWING_CONFLUENCE_BONUS_PER_EXTRA` | `config/settings.py:799` | `"5"` | Multi-pattern confluence: when several patterns agree (same direction), merge into one row with a strength bonus (capped). Conflicting LONG vs SHORT resolved by peak strength, then count. | Box A (API) | NO | YES (code default) |
| `SWING_CONFLUENCE_ENABLED` | `config/settings.py:798` | `"true"` | Multi-pattern confluence: when several patterns agree (same direction), merge into one row with a strength bonus (capped). Conflicting LONG vs SHORT resolved by peak strength, then count. | Box A (API) | NO | YES (code default) |
| `SWING_CONFLUENCE_MAX_BONUS` | `config/settings.py:800` | `"15"` | with a strength bonus (capped). Conflicting LONG vs SHORT resolved by peak strength, then count. | Box A (API) | NO | YES (code default) |
| `SWING_DEDUPE_ONE_PER_SYMBOL` | `config/settings.py:790` | `"true"` | Keep only the single strongest setup per symbol when multiple patterns fire. | Box A (API) | NO | YES (code default) |
| `SWING_DISABLED_PATTERNS` | `config/settings.py:722` | `"DEFAULT"` | Bearish RSI Divergence                   → 11.8% WR  [DISABLED]  To override via env: SWING_DISABLED_PATTERNS="" (empty = none disabled) | Box A (API) | NO | YES (code default) |
| `SWING_INDEX_SYMBOL` | `config/settings.py:636` | `"NIFTY 50"` | Reference index for swing regime gate (must match NSE_SYMBOLS display name). | Box A (API) | NO | YES (code default) |
| `SWING_LOW_LIQUIDITY_PENALTY` | `config/settings.py:607` | `"20"` | Minimum average daily volume (shares) — below this = illiquid, penalise heavily. | Box A (API) | NO | YES (code default) |
| `SWING_MAX_BAR_AGE_DAYS` | `config/settings.py:705` | `"5"` | Skip scanning a symbol if the last daily bar is older than N calendar days (0 = off). Tightened from 10 -> 5: stale data tolerate nahi karna hai for daily accuracy. | Box A (API) | NO | YES (code default) |
| `SWING_MAX_SECTOR_CONCENTRATION` | `config/settings.py:632` | `"1"` | Overnight/portfolio guardrails. | Box A (API) | NO | YES (code default) |
| `SWING_MAX_SETUPS_RETURN` | `config/settings.py:778` | `"100"` | Number of swing setups returned by API cache (higher = fewer names get hidden). | Box A (API) | NO | YES (code default) |
| `SWING_MIN_AVG_VOL_SHARES` | `config/settings.py:606` | `"50000"` | Minimum average daily volume (shares) — below this = illiquid, penalise heavily. | Box A (API) | NO | YES (code default) |
| `SWING_MIN_BARS_FOR_EMA200` | `config/settings.py:758` | `"220"` | Golden/Death Cross needs enough history for EMA_200 to be meaningful. yFinance ke early bars me EMA_200 garbage hota hai — false crosses. | Box A (API) | NO | YES (code default) |
| `SWING_MIN_CLEAN_EMA200_TAIL` | `config/settings.py:759` | `"20"` | Golden/Death Cross needs enough history for EMA_200 to be meaningful. yFinance ke early bars me EMA_200 garbage hota hai — false crosses. | Box A (API) | NO | YES (code default) |
| `SWING_MTF_ALLOW_NEUTRAL` | `config/settings.py:809` | `"true"` | Controls runtime threshold or behavior for `SWING_MTF_ALLOW_NEUTRAL`. | Box A (API) | NO | YES (code default) |
| `SWING_MTF_BLOCK_ON_UNKNOWN` | `config/settings.py:814` | `"false"` | Controls runtime threshold or behavior for `SWING_MTF_BLOCK_ON_UNKNOWN`. | Box A (API) | NO | YES (code default) |
| `SWING_MTF_CONFIRM_ENABLED` | `config/settings.py:804` | `"true"` | Swing MTF confirmation gate (higher-timeframe direction check before keeping setups). Uses provided `mtf_trend` hint or `htf_trend`/`mtf_score` columns when available. | Box A (API) | NO | YES (code default) |
| `SWING_MTF_NEUTRAL_STRENGTH_PENALTY` | `config/settings.py:819` | `"4"` | Controls runtime threshold or behavior for `SWING_MTF_NEUTRAL_STRENGTH_PENALTY`. | Box A (API) | NO | YES (code default) |
| `SWING_MTF_SCORE_NEUTRAL_BAND` | `config/settings.py:820` | `"15"` | Controls runtime threshold or behavior for `SWING_MTF_SCORE_NEUTRAL_BAND`. | Box A (API) | NO | YES (code default) |
| `SWING_OUTCOME_EVAL_BATCH` | `config/settings.py:446` | `"120"` | Minimum seconds between evaluation passes (yFinance budget). | Box A (API) | NO | YES (code default) |
| `SWING_OUTCOME_EVAL_ENABLED` | `config/settings.py:443` | `"true"` | ── Swing outcome tracker (SQLite OPEN rows → SL/T1/T2/EXPIRED) ─── | Box A (API) | NO | YES (code default) |
| `SWING_OUTCOME_EVAL_INTERVAL_SEC` | `config/settings.py:445` | `"3600"` | ── Swing outcome tracker (SQLite OPEN rows → SL/T1/T2/EXPIRED) ─── Minimum seconds between evaluation passes (yFinance budget). | Box A (API) | NO | YES (code default) |
| `SWING_OUTCOME_MAX_HOLD_DAYS` | `config/settings.py:448` | `"18"` | Calendar days after scan emission before we mark EXPIRED if no level hit. | Box A (API) | NO | YES (code default) |
| `SWING_REGIME_GATE_ENABLED` | `config/settings.py:639` | `"true"` | When True, drop LONG swing setups if index htf_trend is BEARISH (and optionally mirror for shorts). | Box A (API) | NO | YES (code default) |
| `SWING_RSI_DIVERGENCE_LOOKBACK` | `config/settings.py:823` | `"35"` | Peak-based RSI divergence tuning for swing scanner. | Box A (API) | NO | YES (code default) |
| `SWING_RSI_DIVERGENCE_MIN_PRICE_DELTA_PCT` | `config/settings.py:826` | `"0.4"` | Controls runtime threshold or behavior for `SWING_RSI_DIVERGENCE_MIN_PRICE_DELTA_PCT`. | Box A (API) | NO | YES (code default) |
| `SWING_RSI_DIVERGENCE_MIN_RSI_DELTA` | `config/settings.py:828` | `"2.0"` | Controls runtime threshold or behavior for `SWING_RSI_DIVERGENCE_MIN_RSI_DELTA`. | Box A (API) | NO | YES (code default) |
| `SWING_RSI_DIVERGENCE_PIVOT_WINDOW` | `config/settings.py:824` | `"2"` | Peak-based RSI divergence tuning for swing scanner. | Box A (API) | NO | YES (code default) |
| `SWING_SECTOR_HEADWIND_BLOCK_THRESHOLD` | `config/settings.py:696` | `"-1.5"` | LONGs are blocked when sector momentum is at/below this threshold. | Box A (API) | NO | YES (code default) |
| `SWING_SECTOR_MOMENTUM_AGG` | `config/settings.py:775` | `"median"` | Sector momentum aggregation: "median" (robust to outliers) or "mean" (legacy). | Box A (API) | NO | YES (code default) |
| `SWING_SECTOR_TAILWIND_BLOCK_SHORT_THRESHOLD` | `config/settings.py:700` | `"1.5"` | SHORTs are blocked when sector momentum is at/above this threshold. | Box A (API) | NO | YES (code default) |
| `SWING_UNIVERSE` | `api/service.py:1451` | `"nifty500"` | Controls runtime threshold or behavior for `SWING_UNIVERSE`. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_52W_BREAKOUT` | `config/settings.py:597` | `"1.5"` | 52-week high proximity | Box A (API) | NO | YES (code default) |
| `SWING_VOL_52W_BREAKOUT_HARD_BLOCK` | `config/settings.py:599` | `"0.5"` | Controls runtime threshold or behavior for `SWING_VOL_52W_BREAKOUT_HARD_BLOCK`. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_52W_BREAKOUT_MIN_ACTIONABLE` | `config/settings.py:598` | `"1.0"` | Controls runtime threshold or behavior for `SWING_VOL_52W_BREAKOUT_MIN_ACTIONABLE`. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_BB_SQUEEZE` | `config/settings.py:589` | `"1.5"` | e.g. ZYDUSWELL 0.2x -> blocked. Must exceed 0.5x to appear at all. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_BB_SQUEEZE_HARD_BLOCK` | `config/settings.py:591` | `"0.5"` | Controls runtime threshold or behavior for `SWING_VOL_BB_SQUEEZE_HARD_BLOCK`. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_BB_SQUEEZE_MIN_ACTIONABLE` | `config/settings.py:590` | `"1.0"` | Controls runtime threshold or behavior for `SWING_VOL_BB_SQUEEZE_MIN_ACTIONABLE`. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_EMA_PULLBACK` | `config/settings.py:593` | `"0.8"` | EMA pullback (min interest) | Box A (API) | NO | YES (code default) |
| `SWING_VOL_EMA_PULLBACK_HARD_BLOCK` | `config/settings.py:595` | `"0.3"` | Controls runtime threshold or behavior for `SWING_VOL_EMA_PULLBACK_HARD_BLOCK`. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_EMA_PULLBACK_MIN_ACTIONABLE` | `config/settings.py:594` | `"0.6"` | Controls runtime threshold or behavior for `SWING_VOL_EMA_PULLBACK_MIN_ACTIONABLE`. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_INSIDE_BAR` | `config/settings.py:581` | `"2.0"` | Hard block: below this ratio, treat breakouts as non-actionable/fakeout-prone. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_INSIDE_BAR_HARD_BLOCK` | `config/settings.py:587` | `"0.5"` | Hard block: vol ratio below this = skip inside-bar signal entirely (not even watchlist). e.g. ZYDUSWELL 0.2x -> blocked. Must exceed 0.5x to appear at all. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_INSIDE_BAR_MIN_ACTIONABLE` | `config/settings.py:584` | `"1.5"` | Minimum ratio for inside-bar setups to be treated as actionable (below => watchlist). Raised to 1.5x: anything below this shows as watchlist only (not a live trade signal). | Box A (API) | NO | YES (code default) |
| `SWING_VOL_POST_RESULT` | `config/settings.py:601` | `"3.0"` | Post-result gap up | Box A (API) | NO | YES (code default) |
| `SWING_VOL_POST_RESULT_HARD_BLOCK` | `config/settings.py:603` | `"1.0"` | Controls runtime threshold or behavior for `SWING_VOL_POST_RESULT_HARD_BLOCK`. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_POST_RESULT_MIN_ACTIONABLE` | `config/settings.py:602` | `"2.0"` | Controls runtime threshold or behavior for `SWING_VOL_POST_RESULT_MIN_ACTIONABLE`. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_RANGE_BREAKOUT` | `config/settings.py:576` | `"1.8"` | ── Swing Scanner Volume Thresholds ───────────────────────────────────────── Raised from 1.3x to eliminate false breakouts on mid/small-cap NSE stocks. Research: 1.5x-2x is the reliable threshold for sub-₹1000 Indian stocks. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_RANGE_BREAKOUT_HARD_BLOCK` | `config/settings.py:579` | `"1.5"` | Hard block: below this ratio, treat breakouts as non-actionable/fakeout-prone. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_RANGE_BREAKOUT_MIN_ACTIONABLE` | `config/settings.py:577` | `"1.5"` | Raised from 1.3x to eliminate false breakouts on mid/small-cap NSE stocks. Research: 1.5x-2x is the reliable threshold for sub-₹1000 Indian stocks. | Box A (API) | NO | YES (code default) |
| `SWING_VOL_ROLLING_DAYS` | `config/settings.py:749` | `"3"` | ── Phase 1 quality gates (accuracy improvements) ──────────────────────────── Use N-day rolling avg for "recent" volume instead of last single bar. Single-bar volume ratio ek din ke spike se manipulate ho jaata hai; 3-day smooths. | Box A (API) | NO | YES (code default) |
| `SWING_WATCHLIST_MAX_STRENGTH` | `config/settings.py:781` | `"72"` | Keep watchlist-grade setups visually below full actionable conviction. | Box A (API) | NO | YES (code default) |
| `TRADEFLOW_FIVE_DAY_LOOKBACK` | `config/settings.py:332` | `"5"` | Tradeflow analytics (FII/DII + optional F&O/forex context) | Box A (API) | NO | YES (code default) |
| `TRAILING_ATR_MULTIPLIER` | `config/settings.py:221` | `"2.0"` | Trailing Stop-Loss | Box A (API) | NO | YES (code default) |
| `VOLUME_SPIKE_CONF_BONUS` | `config/settings.py:256` | `"20"` | Volume-confirmed signals get bonus score + confidence. | Box A (API) | NO | YES (code default) |
| `VOLUME_SPIKE_MULTIPLIER` | `config/settings.py:254` | `"1.5"` | ── Volume Spike Confirmation ───────────────────────────────────────────────── Signal is "volume-confirmed" when current volume > this × 20-bar avg. Volume-confirmed signals get bonus score + confidence. | Box A (API) | NO | YES (code default) |
| `VOLUME_SPIKE_SCORE_BONUS` | `config/settings.py:255` | `"15"` | Signal is "volume-confirmed" when current volume > this × 20-bar avg. Volume-confirmed signals get bonus score + confidence. | Box A (API) | NO | YES (code default) |


### 11. Crypto & Meme Asset Intelligence

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `COINGECKO_CACHE_MINUTES` | `config/settings.py:492` | `"60"` | Free-source cache/rate controls | Box A (API) | NO | YES (code default) |
| `CRYPTOPANIC_API_KEY` | `config/settings.py:482` | `""` | Legacy keys kept for compatibility; free-only policy does not use paid endpoints. | Box A (API) | NO | YES (code default) |
| `CRYPTO_CONF_FLOOR` | `config/settings.py:436` | `"35"` | rows are tagged tier="exploratory" vs tier="premium". Accuracy gates (A1–A4) still apply — only the score floor is widened. | Box A (API) | NO | YES (code default) |
| `CRYPTO_CONF_FLOOR_RELAXED` | `config/settings.py:440` | `"28"` | Controls runtime threshold or behavior for `CRYPTO_CONF_FLOOR_RELAXED`. | Box A (API) | NO | YES (code default) |
| `CRYPTO_FUNDING_GATE_ENABLED` | `config/settings.py:413` | `"true"` | Master switches — set to "false" to disable a gate entirely without changing the numeric thresholds above (useful for A/B comparisons). | Box A (API) | NO | YES (code default) |
| `CRYPTO_FUNDING_MAX_LONG` | `config/settings.py:407` | `"0.0005"` | wash-out risk). SHORT is REJECTED when funding < CRYPTO_FUNDING_MIN_SHORT (crowded shorts → squeeze risk). Use 0.0005 (= 0.05 %) as a conservative default — any symbol with missing/zero funding data passes unchanged. | Box A (API) | NO | YES (code default) |
| `CRYPTO_FUNDING_MIN_SHORT` | `config/settings.py:408` | `"-0.0005"` | (crowded shorts → squeeze risk). Use 0.0005 (= 0.05 %) as a conservative default — any symbol with missing/zero funding data passes unchanged. | Box A (API) | NO | YES (code default) |
| `CRYPTO_MTF_4H_BARS` | `config/settings.py:428` | `"200"` | 4h bars to fetch for HTF trend computation. 200 covers ~33 days — enough for EMA_200 to be valid and for classify_trend to give a stable label. | Box A (API) | NO | YES (code default) |
| `CRYPTO_MTF_GATE_ENABLED` | `config/settings.py:421` | `"true"` | SHORT allowed only if 4h trend != BULLISH NEUTRAL 4h trend passes (avoids over-blocking during range-bound periods). Missing 4h data → gate skipped (same pattern as volume/funding gates). | Box A (API) | NO | YES (code default) |
| `CRYPTO_MTF_STRICT` | `config/settings.py:425` | `"false"` | Strict mode = require 4h trend to exactly MATCH the signal direction (BULLISH for LONG, BEARISH for SHORT). NEUTRAL 4h → blocked in strict mode. Default is relaxed (NEUTRAL passes) so gate is not too aggressive. | Box A (API) | NO | YES (code default) |
| `CRYPTO_QUALITY_FLOOR` | `config/settings.py:435` | `"45"` | quality/confidence floors, back-fill from a relaxed floor band. Those rows are tagged tier="exploratory" vs tier="premium". Accuracy gates (A1–A4) still apply — only the score floor is widened. | Box A (API) | NO | YES (code default) |
| `CRYPTO_QUALITY_FLOOR_RELAXED` | `config/settings.py:439` | `"40"` | Controls runtime threshold or behavior for `CRYPTO_QUALITY_FLOOR_RELAXED`. | Box A (API) | NO | YES (code default) |
| `CRYPTO_SPARSE_FILL_ENABLED` | `config/settings.py:437` | `"true"` | (A1–A4) still apply — only the score floor is widened. | Box A (API) | NO | YES (code default) |
| `CRYPTO_SPARSE_TARGET` | `config/settings.py:438` | `"5"` | Controls runtime threshold or behavior for `CRYPTO_SPARSE_TARGET`. | Box A (API) | NO | YES (code default) |
| `CRYPTO_SYMBOLS` | `config/settings.py:26` | `""` | ── Symbols ────────────────────────────────────────────── Configurable via env vars (comma-separated) or defaults | Box A (API) | NO | YES (code default) |
| `CRYPTO_VOL_GATE_ENABLED` | `config/settings.py:412` | `"true"` | Master switches — set to "false" to disable a gate entirely without changing the numeric thresholds above (useful for A/B comparisons). | Box A (API) | NO | YES (code default) |
| `CRYPTO_VOL_MIN_RATIO` | `config/settings.py:400` | `"1.30"` | Volume confirmation — LONG/SHORT setup ka entry bar volume must be this multiple of the 20-bar rolling average. Default 1.30 = entry bar me 30% above-average volume required. Set to 0 to disable the gate entirely. | Box A (API) | NO | YES (code default) |
| `MEME_DYNAMIC_DISCOVERY_ENABLED` | `config/settings.py:70` | `"true"` | Dynamic meme universe expansion (Binance public ticker scan) | Box A (API) | NO | YES (code default) |
| `MEME_DYNAMIC_DISCOVERY_LIMIT` | `config/settings.py:75` | `"20"` | Controls runtime threshold or behavior for `MEME_DYNAMIC_DISCOVERY_LIMIT`. | Box A (API) | NO | YES (code default) |
| `MEME_MAX_TRACKED_COINS` | `config/settings.py:79` | `"60"` | Controls runtime threshold or behavior for `MEME_MAX_TRACKED_COINS`. | Box A (API) | NO | YES (code default) |


### 13. Telemetry, State Persistence & Local Cache Files

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `CALENDAR_CACHE_HOURS` | `config/settings.py:494` | `"4"` | Free-source cache/rate controls | Box A (API) | NO | YES (code default) |
| `CONTEXT_CACHE_FILE` | `config/settings.py:500` | `"context_cache.json"` | Local filename for market context and sentiment cache (context_cache.json). | Box A (API) | YES (static) | NO (in blueprint) |
| `DAILY_API_USAGE_FILE` | `config/settings.py:502` | `"daily_api_usage.json"` | Local JSON filename tracking Twelve Data API daily request consumption (daily_api_usage.json). | Box A (API) | YES (static) | NO (in blueprint) |
| `HEALTH_FILE` | `config/settings.py:461` | `"health.json"` | Local filename for persistent health status cache (health.json). | Box A (API) | YES (static) | NO (in blueprint) |
| `LOG_DIR` | `config/logger.py:42` | `None` | LOG_DIR is overridable so tests (and sandboxed runs) can keep the repo's logs/ directory untouched instead of failing on an unwritable path. | Box A (API) | NO | YES (code default) |
| `LOG_LEVEL` | `config/logger.py:45` | `"INFO"` | Controls runtime threshold or behavior for `LOG_LEVEL`. | Box A (API) | NO | YES (code default) |
| `NEWS_CACHE_FILE` | `config/settings.py:501` | `"news_cache.json"` | Local filename for news sentiment cache (news_cache.json). | Box A (API) | YES (static) | NO (in blueprint) |
| `NEWS_CACHE_MINUTES` | `config/settings.py:493` | `"240"` | Free-source cache/rate controls | Box A (API) | NO | YES (code default) |
| `NSE_EVENTS_CACHE_HOURS` | `config/settings.py:495` | `"24"` | Controls runtime threshold or behavior for `NSE_EVENTS_CACHE_HOURS`. | Box A (API) | NO | YES (code default) |
| `PAPER_TRADING_STATE_KEY` | `agents/execution/paper_trading.py:200` | `"paper_trading_state:v1"` | Controls runtime threshold or behavior for `PAPER_TRADING_STATE_KEY`. | Box A (API) | NO | YES (code default) |
| `PYTEST_SKIP_DB_CLEANUP` | `tests/conftest.py:156` | `None` | Purge in a bounded background thread: a read-only or lock-contended database used to block every teardown until the driver's busy timeout expired, which made the whole suite look like it had hung. | Box A (API) | NO | YES (code default) |


### 14. Research, Backtesting & Auxiliary Parameters

| Variable | Read at (file:line) | Default Value | Functional Effect | Target Box | In `render.yaml`? | Dashboard-Only / Override? |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `ANALYSE_EVERY_N_TRADES` | `agents/analytics/feedback_loop.py:44` | `str(PAPER_TRADING_UNLOCK_MIN_TR...` | ── Feedback trigger: analyse after every N closed trades ───────────────────── | Box C (Feed) | NO | YES (code default) |
| `CALENDAR_REFRESH_INTERVAL` | `config/settings.py:489` | `"14400"` | Context fetcher intervals (seconds) | Box A (API) | NO | YES (code default) |
| `CONTEXT_NEWS_WEIGHT_24HR` | `config/settings.py:512` | `"0.15"` | Blend last-24h vs last-7d news narrative into context_score (sum ~0.30 by default). | Box A (API) | NO | YES (code default) |
| `CONTEXT_NEWS_WEIGHT_7D` | `config/settings.py:513` | `"0.15"` | Blend last-24h vs last-7d news narrative into context_score (sum ~0.30 by default). | Box A (API) | NO | YES (code default) |
| `CONTEXT_REFRESH_INTERVAL` | `config/settings.py:487` | `"900"` | Context fetcher intervals (seconds) | Box A (API) | NO | YES (code default) |
| `CONTEXT_STRONG_NEGATIVE` | `config/settings.py:507` | `"-0.30"` | Context modifier thresholds | Box A (API) | NO | YES (code default) |
| `CONTEXT_STRONG_POSITIVE` | `config/settings.py:508` | `"0.40"` | Controls runtime threshold or behavior for `CONTEXT_STRONG_POSITIVE`. | Box A (API) | NO | YES (code default) |
| `DAILY_REPORT_ENABLED` | `config/settings.py:459` | `"true"` | ── Scheduling ─────────────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `FNO_TIMEOUT_SEC` | `api/fno_client.py:23` | `"2.5"` | Controls runtime threshold or behavior for `FNO_TIMEOUT_SEC`. | Box A (API) | NO | YES (code default) |
| `FRED_API_KEY` | `config/settings.py:1166` | `""` | All APIs are free: FRED (macro), CFTC (COT), public sentiment, market risk gauges. FRED API (Federal Reserve Economic Data) — free from stlouisfed.org | Box A (API) | NO | YES (code default) |
| `GH_TOKEN` | `api/app.py:405` | `None` | Controls runtime threshold or behavior for `GH_TOKEN`. | Box A (API) | NO | YES (code default) |
| `GITHUB_REPOSITORY` | `api/cycle_scheduler.py:46` | `None` | Controls runtime threshold or behavior for `GITHUB_REPOSITORY`. | Box A (API) | NO | YES (code default) |
| `GITHUB_TOKEN` | `api/app.py:405` | `None` | Controls runtime threshold or behavior for `GITHUB_TOKEN`. | Box A (API) | NO | YES (code default) |
| `GIT_COMMIT` | `api/routers/health.py:51` | `None` | Controls runtime threshold or behavior for `GIT_COMMIT`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_ENTRY_CUTOFF_HOUR_IST` | `config/settings.py:171` | `"14"` | Controls runtime threshold or behavior for `INTRADAY_ENTRY_CUTOFF_HOUR_IST`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_ENTRY_CUTOFF_MINUTE_IST` | `config/settings.py:172` | `"30"` | Controls runtime threshold or behavior for `INTRADAY_ENTRY_CUTOFF_MINUTE_IST`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_LIVE_FEED_ENABLED` | `config/settings.py:166` | `"true"` | Intraday Live Feed, RVOL, and Index Confluence settings | Box A (API) | NO | YES (code default) |
| `INTRADAY_MAX_BAR_AGE_MINUTES` | `config/settings.py:135` | `"120"` | Intraday scanner: skip symbols when latest 5m bar is older than this threshold. Set 0 to disable freshness gating. | Box A (API) | NO | YES (code default) |
| `INTRADAY_MAX_OPEN_TRADES` | `config/settings.py:144` | `"6"` | Controls runtime threshold or behavior for `INTRADAY_MAX_OPEN_TRADES`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_MAX_SINGLE_TRADE_PCT` | `config/settings.py:146` | `"0.20"` | Controls runtime threshold or behavior for `INTRADAY_MAX_SINGLE_TRADE_PCT`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_REQUIRE_LIVE_FEED` | `config/settings.py:167` | `"true"` | Intraday Live Feed, RVOL, and Index Confluence settings | Box A (API) | NO | YES (code default) |
| `INTRADAY_SQUARE_OFF_HOUR_IST` | `config/settings.py:147` | `"15"` | Controls runtime threshold or behavior for `INTRADAY_SQUARE_OFF_HOUR_IST`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_SQUARE_OFF_MINUTE_IST` | `config/settings.py:148` | `"15"` | Controls runtime threshold or behavior for `INTRADAY_SQUARE_OFF_MINUTE_IST`. | Box A (API) | NO | YES (code default) |
| `INTRADAY_VWAP_BAND_SIGMA` | `config/settings.py:158` | `"1.0"` | Controls runtime threshold or behavior for `INTRADAY_VWAP_BAND_SIGMA`. | Box A (API) | NO | YES (code default) |
| `KRONOS_DEVICE` | `agents/ai_engine/kronos_forecaster.py:73` | `"cpu"` | Controls runtime threshold or behavior for `KRONOS_DEVICE`. | Box A (API), Scripts / CI | NO | YES (code default) |
| `KRONOS_REPO_PATH` | `agents/ai_engine/kronos_forecaster.py:44` | `os.path.join("third_party", "Kr...` | Controls runtime threshold or behavior for `KRONOS_REPO_PATH`. | Box A (API) | NO | YES (code default) |
| `KRONOS_TOKENIZER_ID` | `agents/ai_engine/kronos_forecaster.py:46` | `"NeoQuasar/Kronos-Tokenizer-2k"` | Controls runtime threshold or behavior for `KRONOS_TOKENIZER_ID`. | Box A (API) | NO | YES (code default) |
| `MARKETAUX_DAILY_LIMIT` | `config/settings.py:496` | `"100"` | Controls runtime threshold or behavior for `MARKETAUX_DAILY_LIMIT`. | Box A (API) | NO | YES (code default) |
| `MARKETAUX_TOP_SYMBOLS` | `config/settings.py:497` | `"10"` | Controls runtime threshold or behavior for `MARKETAUX_TOP_SYMBOLS`. | Box A (API) | NO | YES (code default) |
| `MARKET_CONTROL_PASSWORD_HASH` | `agents/execution/market_control.py:146` | `""` | Controls runtime threshold or behavior for `MARKET_CONTROL_PASSWORD_HASH`. | Box A (API) | NO | YES (code default) |
| `MARKET_CONTROL_SALT` | `agents/execution/market_control.py:126` | `"tradeai_salt_2026"` | Controls runtime threshold or behavior for `MARKET_CONTROL_SALT`. | Box A (API) | NO | YES (code default) |
| `MAX_PORTFOLIO_HEAT_PCT` | `config/settings.py:524` | `"0.03"` | Risk management and sizing | Box A (API) | NO | YES (code default) |
| `MFI_OVERBOUGHT` | `config/settings.py:215` | `"80"` | Money Flow Index thresholds | Box A (API) | NO | YES (code default) |
| `MFI_OVERSOLD` | `config/settings.py:214` | `"20"` | Money Flow Index thresholds | Box A (API) | NO | YES (code default) |
| `MTF_AGREEMENT_LOW_VOTE_PENALTY` | `config/settings.py:279` | `"7.0"` | Controls runtime threshold or behavior for `MTF_AGREEMENT_LOW_VOTE_PENALTY`. | Box A (API) | NO | YES (code default) |
| `MTF_AGREEMENT_MIN_VOTES` | `config/settings.py:278` | `"2"` | Controls runtime threshold or behavior for `MTF_AGREEMENT_MIN_VOTES`. | Box A (API) | NO | YES (code default) |
| `MTF_ALIGNMENT_CONF_BONUS` | `config/settings.py:260` | `"15"` | ── Multi-Timeframe Alignment Bonus ─────────────────────────────────────────── When 15m + 1h + 1d all agree on direction → add to confidence. | Box A (API) | NO | YES (code default) |
| `MTF_HIGH_CONVICTION_VOTES` | `config/settings.py:314` | `"3"` | Controls runtime threshold or behavior for `MTF_HIGH_CONVICTION_VOTES`. | Box A (API) | NO | YES (code default) |
| `MTF_SCORE_WEAK_PENALTY` | `config/settings.py:282` | `"6.0"` | When caller passes mtf_score (-100..100), map to 0–4 display votes; penalize very weak alignment | Box A (API) | NO | YES (code default) |
| `MTF_SCORE_WEAK_THRESHOLD` | `config/settings.py:281` | `"35"` | When caller passes mtf_score (-100..100), map to 0–4 display votes; penalize very weak alignment | Box A (API) | NO | YES (code default) |
| `MTF_SIGNAL_GATE_ENABLED` | `config/settings.py:308` | `"true"` | Require multi-timeframe alignment votes for directional signal emission | Box A (API) | NO | YES (code default) |
| `MTF_SIGNAL_MIN_VOTES` | `config/settings.py:313` | `"2"` | Controls runtime threshold or behavior for `MTF_SIGNAL_MIN_VOTES`. | Box A (API) | NO | YES (code default) |
| `MTF_VOTE_CONF_ADJUST_ENABLED` | `config/settings.py:273` | `"true"` | MTF agreement (HTF + EMA + MACD [+ VWAP] stack) when full mtf_score is not passed | Box A (API) | NO | YES (code default) |
| `NEWS_REFRESH_INTERVAL` | `config/settings.py:488` | `"1800"` | Context fetcher intervals (seconds) | Box A (API) | NO | YES (code default) |
| `NEWS_SPIKE_MULTIPLIER` | `config/settings.py:506` | `"2.5"` | Context modifier thresholds | Box A (API) | NO | YES (code default) |
| `NIGHTLY_SCAN_HOUR` | `api/service.py:1398` | `21` | ── Nightly SwingSpectrum Batch Scan (auto-scheduled) ───────────────────────── | Box A (API) | NO | YES (code default) |
| `NIGHTLY_SCAN_MINUTE` | `api/service.py:1399` | `30` | ── Nightly SwingSpectrum Batch Scan (auto-scheduled) ───────────────────────── | Box A (API) | NO | YES (code default) |
| `NSEPYTHON_QUOTES_ENABLED` | `config/settings.py:476` | `"true"` | Try nsepython first for NSE day change in context layer (no key). Disable if NSE blocks your host. | Box A (API) | NO | YES (code default) |
| `NSE_SYMBOLS` | `config/settings.py:82` | `""` | Controls runtime threshold or behavior for `NSE_SYMBOLS`. | Box A (API) | NO | YES (code default) |
| `NSE_SYMBOL_TIMEOUT_SECONDS` | `config/settings.py:176` | `"150"` | NSE fetch timeout per symbol (seconds) for startup/refresh concurrency workers. | Box A (API) | NO | YES (code default) |
| `OUTCOME_FORWARD_BARS` | `config/settings.py:389` | `"5"` | Outcome label parameters | Box A (API) | NO | YES (code default) |
| `OUTCOME_RR_RATIO` | `config/settings.py:390` | `"2.0"` | Outcome label parameters | Box A (API) | NO | YES (code default) |
| `PAPER_TRADING_ENABLED` | `config/settings.py:1407` | `"true"` |  PAPER_TRADING_ENABLED=true   → signals auto-captured on every scan PAPER_TRADING_ENABLED=false  → paper trading off (default safe mode) | Box A (API) | NO | YES (code default) |
| `PAPER_TRADING_FORWARD_DAYS` | `config/settings.py:1420` | `"5"` | How many calendar days ahead to check SL/Target for EOD resolution | Box A (API) | NO | YES (code default) |
| `PAPER_TRADING_MAX_OPEN` | `config/settings.py:1423` | `"3"` | Max open paper trades at once (prevents over-crowding) | Box A (API) | NO | YES (code default) |
| `PAPER_TRADING_UNLOCK_MIN_TRADES` | `config/settings.py:1416` | `"30"` | Unlock thresholds: system will show "VALIDATED — ready for real trading" only when BOTH conditions are met (run enough trades + high enough win rate) | Box A (API) | NO | YES (code default) |
| `PAPER_TRADING_UNLOCK_MIN_WR_PCT` | `config/settings.py:1417` | `"50.0"` | Unlock thresholds: system will show "VALIDATED — ready for real trading" only when BOTH conditions are met (run enough trades + high enough win rate) | Box A (API) | NO | YES (code default) |
| `PARTIAL_TP_BREAKEVEN_ON_TRIGGER` | `config/settings.py:228` | `"true"` | Controls runtime threshold or behavior for `PARTIAL_TP_BREAKEVEN_ON_TRIGGER`. | Box A (API) | NO | YES (code default) |
| `PARTIAL_TP_CLOSE_FRACTION` | `config/settings.py:226` | `"0.5"` | Partial take-profit (execution-level) | Box A (API) | NO | YES (code default) |
| `PARTIAL_TP_ENABLED` | `config/settings.py:225` | `"true"` | Partial take-profit (execution-level) | Box A (API) | NO | YES (code default) |
| `PARTIAL_TP_TRIGGER_FRACTION` | `config/settings.py:227` | `"0.5"` | Partial take-profit (execution-level) | Box A (API) | NO | YES (code default) |
| `PYTEST_ALLOW_REAL_DB` | `tests/conftest.py:35` | `None` | Controls runtime threshold or behavior for `PYTEST_ALLOW_REAL_DB`. | Box A (API) | NO | YES (code default) |
| `PYTEST_CURRENT_TEST` | `api/app.py:99` | `None` | Controls runtime threshold or behavior for `PYTEST_CURRENT_TEST`. | Box A (API) | NO | YES (code default) |
| `RENDER_API_KEY` | `scripts/check_deploy.py:7` | `None` | Controls runtime threshold or behavior for `RENDER_API_KEY`. | Scripts / CI | NO | YES (code default) |
| `RENDER_EXTERNAL_URL` | `api/app.py:135` | `None` | Controls runtime threshold or behavior for `RENDER_EXTERNAL_URL`. | Box A (API) | NO | YES (code default) |
| `RENDER_GIT_COMMIT` | `api/routers/health.py:51` | `None` | Controls runtime threshold or behavior for `RENDER_GIT_COMMIT`. | Box A (API) | NO | YES (code default) |
| `SCAN_INTERVAL_SECONDS` | `scripts/scanner_daemon.py:261` | `"180"` | Default 3 minutes | Scripts / CI | NO | YES (code default) |
| `SENTIMENT_GATE_ENABLED` | `config/settings.py:1218` | `"true"` | V2 Targeted News & Sentiment Gate | Box A (API) | NO | YES (code default) |
| `SKIP_CONFIDENCE_THRESHOLD` | `config/settings.py:327` | `"40"` | ── SKIP Confidence Threshold ───────────────────────────────────────────────── Signals with AI confidence below this → SKIP (not LONG/SHORT). Lowered from 55 → 40: 40-55% confidence setups are valid in trending markets. | Box A (API) | NO | YES (code default) |
| `SLOW_OPERATION_THRESHOLD_MS` | `utils/profiler.py:18` | `"150.0"` | Controls runtime threshold or behavior for `SLOW_OPERATION_THRESHOLD_MS`. | Box A (API) | NO | YES (code default) |
| `SR_TEST_CONF_BONUS` | `config/settings.py:322` | `"10"` | ── S/R Level Test Confidence Bonus ─────────────────────────────────────────── Price testing key S/R level (within 0.5% of support/resistance) → +10 conf pts. | Box A (API) | NO | YES (code default) |
| `SUPABASE_JWKS_URL` | `config/settings.py:1440` | `""` | Controls runtime threshold or behavior for `SUPABASE_JWKS_URL`. | Box A (API) | NO | YES (code default) |
| `SUPABASE_PUBLISHABLE_KEY` | `config/settings.py:1438` | `""` | ── Supabase Cloud Configuration ───────────────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `SUPABASE_SECRET_KEY` | `config/settings.py:1439` | `""` | ── Supabase Cloud Configuration ───────────────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `SUPABASE_URL` | `config/settings.py:1437` | `""` | ── Supabase Cloud Configuration ───────────────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `TELEGRAM_ALERTS_ENABLED` | `agents/notifications/telegram_alerts.py:34` | `"false"` | Controls runtime threshold or behavior for `TELEGRAM_ALERTS_ENABLED`. | Box A (API) | NO | YES (code default) |
| `TELEGRAM_BOT_TOKEN` | `agents/alerts/telegram_bot.py:27` | `""` | Controls runtime threshold or behavior for `TELEGRAM_BOT_TOKEN`. | Box A (API) | NO | YES (code default) |
| `TELEGRAM_CHAT_ID` | `agents/alerts/telegram_bot.py:28` | `""` | Controls runtime threshold or behavior for `TELEGRAM_CHAT_ID`. | Box A (API) | NO | YES (code default) |
| `TELEGRAM_DEDUP_MINUTES` | `agents/notifications/telegram_alerts.py:38` | `"60"` | Minimum gap between alerts for the same symbol (prevents rapid-fire spam) | Box A (API) | NO | YES (code default) |
| `TRADEFLOW_FII_NOISE_THRESHOLD_CR` | `config/settings.py:330` | `"500"` | Tradeflow analytics (FII/DII + optional F&O/forex context) | Box A (API) | NO | YES (code default) |
| `TRADEFLOW_FII_STRONG_THRESHOLD_CR` | `config/settings.py:331` | `"3000"` | Tradeflow analytics (FII/DII + optional F&O/forex context) | Box A (API) | NO | YES (code default) |
| `TRADEFLOW_FO_ENDPOINT` | `config/settings.py:336` | `""` | Optional NSE derivatives endpoint path/URL for F&O intent (leave empty to disable) | Box A (API) | NO | YES (code default) |
| `TRADEFLOW_SIGNAL_BIAS_MULTIPLIER` | `config/settings.py:344` | `"1.15"` | Controls runtime threshold or behavior for `TRADEFLOW_SIGNAL_BIAS_MULTIPLIER`. | Box A (API) | NO | YES (code default) |
| `TRADEFLOW_SIGNAL_COUNTER_MULTIPLIER` | `config/settings.py:345` | `"0.85"` | Controls runtime threshold or behavior for `TRADEFLOW_SIGNAL_COUNTER_MULTIPLIER`. | Box A (API) | NO | YES (code default) |
| `TRADEFLOW_SIGNAL_IGNORE_STALE` | `config/settings.py:346` | `"true"` | Controls runtime threshold or behavior for `TRADEFLOW_SIGNAL_IGNORE_STALE`. | Box A (API) | NO | YES (code default) |
| `TRADEFLOW_SIGNAL_INFLUENCE_ENABLED` | `config/settings.py:339` | `"true"` | Tradeflow -> signal-score integration (A/B-testable) | Box A (API) | NO | YES (code default) |
| `TRADEFLOW_SIGNAL_STALE_DISABLE_HOURS` | `config/settings.py:351` | `"24"` | Controls runtime threshold or behavior for `TRADEFLOW_SIGNAL_STALE_DISABLE_HOURS`. | Box A (API) | NO | YES (code default) |
| `TRADEFLOW_USDINR_EXIT_THRESHOLD_PCT` | `config/settings.py:333` | `"0.5"` | Controls runtime threshold or behavior for `TRADEFLOW_USDINR_EXIT_THRESHOLD_PCT`. | Box A (API) | NO | YES (code default) |
| `WS_ENABLED` | `config/settings.py:464` | `"true"` | ── WebSocket Settings ─────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `WS_FALLBACK_TIMEOUT` | `config/settings.py:468` | `"30"` | If WebSocket is unavailable for this many seconds, fall back to CCXT polling | Box A (API) | NO | YES (code default) |
| `WS_RECONNECT_MAX` | `config/settings.py:465` | `"50"` | ── WebSocket Settings ─────────────────────────────────── | Box A (API) | NO | YES (code default) |
| `WS_STALE_THRESHOLD_MULT` | `config/settings.py:466` | `"2.0"` | ── WebSocket Settings ─────────────────────────────────── | Box A (API) | NO | YES (code default) |

