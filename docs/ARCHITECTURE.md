# System Architecture & Topology Map

> **Audit State**: READ-ONLY reverse-engineering audit.  
> **Source Evidence Date**: 2026-11-22  
> **All assertions cite verified `path/to/file:LINE` references.**

---

## 1. Process & Entry Point Inventory

The system consists of one edge router/proxy and three dedicated backend processes configured across Render and Cloudflare:

| Process / Target | Entry File:Line | Start Command | Port | Health Check Path | Host / Service | State Classification | Evidence Citation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Edge Router & Reverse Proxy** | `src/worker.js:552` (`fetch`) | Cloudflare Edge Runtime | 443 (HTTPS) | `/api/health/live` (proxied to Box A) | Cloudflare Worker (`tradeai-pro`) | **DEPLOYED** | `wrangler.jsonc:4`, `src/worker.js:552` |
| **Box A: Core Trading API & Gateway** | `server.py:17` (`create_app`) | `uvicorn server:app --host 0.0.0.0 --port $PORT` | Dynamic `$PORT` (Render) | `/api/health/live` | Render Service `trading-api` | **DEPLOYED** | `render.yaml:6,15,20`, `server.py:17` |
| **Box B: Dedicated ML Sidecar** | `ml_server.py:16` (`FastAPI`) | `uvicorn ml_server:app --host 0.0.0.0 --port $PORT` | Dynamic `$PORT` (Render) | `/health/live` | Render Service `trading-ml` | **DEPLOYED** | `render.yaml:105,110,111`, `ml_server.py:16` |
| **Box C: Market Data Feed Service** | `feed_server.py:28` (`FastAPI`) | `uvicorn feed_server:app --host 0.0.0.0 --port $PORT` | Dynamic `$PORT` (Render) | `/health/live` | Render Service `trading-feed` | **DEPLOYED** | `render.yaml:138,143,144`, `feed_server.py:28` |

### State Classification Definitions:
* **DEFINED**: Present in codebase repository tree.
* **WIRED**: Imported, instantiated, or actively consumed by reachable execution paths.
* **DEPLOYED**: Declared in production deployment configuration (`render.yaml`, `wrangler.jsonc`) and bound to runtime networking.

---

## 2. End-to-End System ASCII Architecture

```text
[ Browser / Public Client ]
           │
           │ HTTPS / WSS
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Cloudflare Edge Worker (src/worker.js)                                      │
│ - Origin: https://tradeai-pro.tradingpro.workers.dev                        │
│ - Shared Cache: caches.default (RFC 5861 Stale-While-Revalidate)            │
│ - Cache Rules: PUBLIC_CACHEABLE_PATHS (src/worker.js:71-115)                │
│ - Security Headers: CSP, HSTS, X-Content-Type-Options (src/worker.js:39-47) │
└──────┬──────────────────────────────────────────────────────────────┬───────┘
       │                                                              │
       │ HTTP / WebSocket Reverse Proxy                               │ Scheduled Edge Cron
       │ Headers: createCleanOriginHeaders() (src/worker.js:166)      │ (src/worker.js:933)
       │ Host: trading-api-n1wt.onrender.com                          │ Keep-Alive Pings
       ▼                                                              ▼
┌──────────────────────────────────────────────────────────┐      ┌───────────┐
│ Box A: Core Trading API (Render: trading-api)            │◄─────┤ Cloudflare│
│ - Process: uvicorn server:app (server.py:17)             │      │ Edge Cron │
│ - In-Memory State: _signal_cache, _market_data,          │      └─────┬─────┘
│   _persisted_signals_snapshot (api/service.py:100-115)   │            │
│ - Background Worker: _background_refresh()               │            │
│   Cadence: REFRESH_INTERVAL_SEC=300 (render.yaml:62)     │            │
└──────┬───────────────────┬───────────────────┬───────────┘            │
       │                   │                   │                        │
       │ HTTP POST (3.0s)  │ HTTP GET (3.0s)   │ PostgreSQL TLS         │
       │ Token Auth:       │ Token Auth:       │ (SQLAlchemy)           │
       │ X-Internal-Token  │ X-Internal-Token  │ DATABASE_URL           │
       │ (render.yaml:95)  │ (render.yaml:100) │ (render.yaml:36)       │
       ▼                   ▼                   ▼                        │
┌──────────────┐    ┌──────────────┐    ┌─────────────────┐             │
│ Box B: ML    │    │ Box C: Feed  │    │ External DB     │             │
│ Sidecar      │    │ Service      │    │ (Supabase /     │             │
│ (Render:     │    │ (Render:     │    │ PostgreSQL)     │             │
│ trading-ml)  │    │ trading-feed)│    │ - runtime_state │             │
│ ml_server.py │    │ feed_server  │    │ - candles       │             │
│ :app         │    │ .py:app      │    │ - trades        │             │
└──────────────┘    └──────┬───────┘    └─────────────────┘             │
                           │                                            │
                           │ WSS / HTTPS                                │ Keep-Alive
                           │ (NorenApi)                                 │ Pings
                           ▼                                            │
                    ┌──────────────┐                                    │
                    │ Shoonya /    │                                    │
                    │ Finvasia     │                                    │
                    │ NSE Tick Feed│                                    │
                    └──────────────┘                                    │
                                                                        │
Box B & Box C Keep-Alive:                                               │
- Box B Ping: GET https://trading-ml-8umr.onrender.com/health/live ─────┤ (src/worker.js:1088)
- Box C Ping: GET https://trading-feed.onrender.com/health/live ────────┘ (src/worker.js:1103)
```

### Protocol & Authentication Hop Specifications:
1. **Browser $\rightarrow$ Worker**: Public HTTPS/WSS. No edge auth for public endpoints; private API endpoints require session cookie or `X-Admin-Key` header (`api/security.py:45`).
2. **Worker $\rightarrow$ Box A**: HTTPS with sanitized headers. `createCleanOriginHeaders()` (`src/worker.js:166`) explicitly strips `cf-*`, `x-forwarded-*`, and `x-real-*` to prevent Render's Cloudflare layer from rejecting proxy requests with HTTP 403/429. Authenticated cron dispatches inject `X-Cron-Secret: env.CRON_SECRET` (`src/worker.js:1123`).
3. **Box A $\rightarrow$ Box B**: HTTP POST with timeout `3.0s` (`api/ml_sidecar_client.py:50`). Header: `X-Internal-Token: ML_INTERNAL_TOKEN` (`api/ml_sidecar_client.py:48`, `render.yaml:95,134`). Fail-closed: 403 if token mismatch (`ml_server.py:65`).
4. **Box A $\rightarrow$ Box C**: HTTP GET with timeout `3.0s` (`api/feed_client.py:44`). Header: `X-Internal-Token: FEED_INTERNAL_TOKEN` (`api/feed_client.py:43`, `render.yaml:100,152`). Fail-closed: 403 if token mismatch (`feed_server.py:114`).
5. **Box C $\rightarrow$ Shoonya**: WSS (`wss://api.shoonya.com/NorenWSAPI/`) and HTTPS (`https://api.shoonya.com/NorenWClientAPI/`) (`feed/shoonya_client.py:23,24`). Authenticated via SHA256 hashed password, dynamic TOTP, and `GenAcsTok` token exchange (`feed/shoonya_client.py:81-97`).
6. **Box A $\rightarrow$ NSE Public Archive CDN**: HTTPS GET (`https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv`) (`agents/data_ingestion/nse_bhavcopy.py:27`). Keyless, authenticated via standard browser User-Agent headers, timeout `10s`.
7. **Box A / Box B $\rightarrow$ PostgreSQL**: Async TCP over TLS via SQLAlchemy `DATABASE_URL` (`render.yaml:36,131`).

---

## 3. End-to-End Request Trace: `GET /api/nse/dashboard`

The complete execution path for a request to `GET /api/nse/dashboard`:

```text
[Browser]
  │
  ▼
1. src/worker.js:552 ➔ async fetch(request, env, ctx)
  │
  ├─► src/worker.js:601: Checks url.pathname.startsWith("/api/") ➔ True
  │
  ├─► src/worker.js:604: isPathCacheable("/api/nse/dashboard") ➔ Evaluates true
  │   Evidence: PUBLIC_CACHEABLE_PATHS.has("/api/nse/dashboard") is present at src/worker.js:74
  │
  ├─► src/worker.js:610: cache.match(cacheKey)
  │   ├── Case A (HIT, Age <= 15s): Returns fresh cached response directly with "CF-Cache-Status: HIT" (src/worker.js:623)
  │   ├── Case B (STALE, Age 16s..600s): Returns cached response with "CF-Cache-Status: STALE" (src/worker.js:655)
  │   │   └─► Spawns background revalidation task via ctx.waitUntil(revalTask) (src/worker.js:649)
  │   └── Case C (MISS / Revalidation): Forwards request to Box A origin (src/worker.js:664)
  │
  ├─► src/worker.js:665: Calls createCleanOriginHeaders(request.headers)
  │   (Strips cf-* headers, injects Host: trading-api-n1wt.onrender.com) (src/worker.js:166-187)
  │
  └─► src/worker.js:681: originRes = await fetch(targetUrl, { headers: originHeaders })
        │
        ▼
[Box A Origin: trading-api]
  │
  ├─► server.py:17: Application bootstrap imports app from api.app
  │
  ├─► api/app.py:155: Router registration dispatches path to api/routers/research.py
  │
  ├─► api/routers/research.py:649: async def get_nse_dashboard_snapshot()
  │     │
  │     ├─► Line 653: Checks if not svc._movers_cache.get("gainers") and svc._market_data:
  │     │   └─► If empty: Calls svc._refresh_movers_cache() (api/service.py:2909)
  │     │         ├─► Calls _compute_gainers_losers_from_cache("gainers") (api/service.py:2875)
  │     │         └─► Calls _compute_gainers_losers_from_cache("losers") (api/service.py:2875)
  │     │
  │     ├─► Line 657: Calls svc._compute_market_mood() (api/service.py:2918)
  │     │     ├─► Reads gainers/losers counts to compute market breadth (api/service.py:2927-2929)
  │     │     ├─► Iterates _signal_cache to count LONG vs SHORT vs HOLD (api/service.py:2934-2950)
  │     │     ├─► Iterates _regime_cache to evaluate trending vs ranging regime distribution (api/service.py:2951-2959)
  │     │     └─► Computes composite mood_score (0-100) and label (BULLISH/BEARISH/NEUTRAL) (api/service.py:2963-3004)
  │     │
  │     ├─► Line 659: Calls svc._build_ai_market_summary(mood) (api/service.py:3153)
  │     │     └─► Formats natural language synthesis of market breadth, dominant regime, and risk posture (api/service.py:3154-3200)
  │     │
  │     ├─► Lines 667-669: Slices top 7 gainers and top 7 losers from svc._movers_cache
  │     │
  │     └─► Lines 671-697: Constructs payload dictionary:
  │           {
  │             "timestamp": ...,
  │             "last_refresh": ...,
  │             "loading": ...,
  │             "mood": mood,
  │             "ai_summary": summary,
  │             "movers": { "gainers": [...], "losers": [...], "updated_at": ... }
  │           }
  │
  ▼
[Edge Worker Return & Cache Insertion]
  │
  ├─► src/worker.js:688: Receives response from Box A
  ├─► src/worker.js:698: Stores copy in edge cache via storeInEdgeCache(cache, cacheKey, originRes)
  ├─► src/worker.js:705: Applies security and cache headers (UNIFIED_CACHE_CONTROL: max-age=5, s-maxage=15, swr=30)
  └─► Browser receives 200 OK JSON response
```

---

## 4. End-to-End Signal Generation Trace

The complete trace of how a raw OHLCV bar becomes an actionable signal and renders on the client dashboard:

```text
1. RAW BAR INGESTION
   - File: api/service.py:1543 (`_fetch_nse_symbol`)
   - Fetch: Calls `fetch_ohlcv(display_name, "5d", "5m")` (api/service.py:1554) via data/router.py:46.
   - Provider Router: If `FEED_SERVICE_URL` is set, calls Box C `ShoonyaFeedProvider.get_batch_ohlcv()` (data/providers/shoonya_feed.py:42). Otherwise falls back to Yahoo Finance (data/providers/yahoo.py:130).

2. VALIDATION GATE
   - File: api/service.py:1560
   - Execution: `validate_ohlcv(df, display_name, "5m")` in agents/data_ingestion/validator.py:130.
   - Guard: Calculates quality score (`quality_pct`). If `not report.is_safe`, symbol is dropped immediately with log warning (api/service.py:1563-1568).

3. FEATURE & INDICATOR COMPUTATION
   - File: api/service.py:1570
   - Execution: `calculate_indicators(df)` in agents/feature_engine/indicators.py:38.
   - Generated Indicators:
     * Momentum: RSI (14-period), MACD (12, 26, 9), MACD Signal, MACD Histogram.
     * Moving Averages: EMA 20, EMA 50, EMA 200.
     * Volatility: ATR 14, Bollinger Bands (Upper, Lower, Middle, Bandwidth).
     * Volume: Volume SMA 20, Relative Volume (RVOL).

4. AI ENSEMBLE PREDICTION
   - File: api/service.py:1573
   - Execution: `predict_batch(df)` in agents/ai_engine/ensemble.py:452.
   - Inference Routing:
     * If `ML_SIDECAR_URL` is set (render.yaml:93), dispatches features to Box B (`trading-ml`) HTTP POST `/predict/batch` (api/ml_sidecar_client.py:75).
     * Otherwise evaluates local tree models (LightGBM / XGBoost) or falls back to statistical RSI-calibrated conviction when `LIGHTWEIGHT_API_MODE=true` (agents/ai_engine/ensemble.py:475).
   - Output: `ai_confidence` pandas Series (values normalized 0.0 to 100.0).

5. REGIME & MULTI-TIMEFRAME (MTF) CONFLUENCE
   - File: api/service.py:1578-1615
   - HTF Trend: Fetches 60-day daily bars (`fetch_ohlcv(display_name, "60d", "1d")`), classifies HTF trend via `classify_trend(htf_df)` (agents/market_intelligence/market_regime.py:80), and propagates to 5m LTF bars (`propagate_trend_to_ltf`, api/service.py:1582).
   - MTF Score: Resamples 5m bars to 15m, builds composite MTF dictionary (`5m`, `15m`, `1d`), and computes alignment via `calculate_mtf_score(mtf_data)` (agents/signal_engine/mtf_scoring.py:35).
   - Market Regime: Identifies regime (`TRENDING`, `RANGING`, `HIGH_VOLATILITY`) via `detect_regime(df)` (agents/market_intelligence/market_regime.py:30).

6. SIGNAL & RISK LEVEL DERIVATION
   - File: api/service.py:1643-1652
   - Execution: `generate_signals(df, ai_confidence, regime, symbol, market_trend, mtf_score, news_sentiment)` in agents/signal_engine/signal_generator.py:378.
   - Direction Decision: Evaluates EMA alignment, RSI bounds, and MACD crossovers. Sets raw direction: `LONG`, `SHORT`, or `HOLD` (agents/signal_engine/signal_generator.py:1120-1145).
   - Stop-Loss & Target Geometry:
     * SL Distance: `sl_distance = atr * atr_sl_mult` (agents/signal_engine/signal_generator.py:1153).
     * TP Distance: `tp_distance = sl_distance * settings.MIN_RISK_REWARD` (agents/signal_engine/signal_generator.py:1154).
     * For LONG: `stop_loss = close - sl_distance`, `target = close + tp_distance` (agents/signal_engine/signal_generator.py:1355-1356).
     * For SHORT: `stop_loss = close + sl_distance`, `target = close - tp_distance` (agents/signal_engine/signal_generator.py:1357-1358).
   - Quality Scoring & Filtering:
     * Assigns `quality_score` (0-100) and `quality_grade` (A, B, C, D) (agents/signal_engine/signal_generator.py:1210).
     * If grade is C or D, downgrades actionable signal to `SKIP` (agents/signal_engine/signal_generator.py:1225).

7. IN-MEMORY & DB PERSISTENCE
   - File: api/service.py:1655-1660
   - In-Memory Cache: Stores enriched DataFrame in `_market_data[display_name] = df` and `_signal_cache[display_name] = df`.
   - Alert Trigger: `_fire_telegram_signal(display_name, df)` executes non-blocking notification and paper journal logging if signal transitioned to new actionable state (api/service.py:1485).
   - Durable Snapshot: Persisted to Supabase `runtime_state` table under key `latest_signals:v1` during refresh cycle completion (api/service.py:1837).

8. API SERIALIZATION
   - File: api/routers/market.py:14 & api/service.py:2224 (`_get_signal_for_symbol`)
   - Route: Client queries `GET /api/signals/all` or `GET /api/dashboard/overview`.
   - Extraction: Reads latest row from `_signal_cache[symbol]`, builds dictionary containing:
     `{ "symbol": symbol, "signal": "LONG", "entry": 1378.4, "stop_loss": 1370.61, "target": 1393.98, "confidence": 72.0, "risk_reward": 2.0, "quality_score": 85, "source": "authoritative_engine" }`.

9. DASHBOARD CARD RENDERING
   - File: dashboard/app.js:285
   - Client Consumption: Frontend polls `/api/signals/all` or `/api/dashboard/overview`.
   - DOM Update: Slices signal record, attaches CSS status badge (`signal-long` 🟢 / `signal-short` 🔴), formats entry zone, target, and stop loss into responsive trade cards in `#signalsContainer`.
```

---

## 5. Deployment Topology & Operational Orchestration

### Service Deployment Modes:
* **Box A (`trading-api`)**: Render Web Service. `autoDeploy: false` (`render.yaml:13`). Deployed manually or programmatically via `scripts/trigger_deploy.py` using `RENDER_API_KEY`.
* **Box B (`trading-ml`)**: Render Web Service. `autoDeploy: false` (`render.yaml:108`). Dedicated ML sidecar.
* **Box C (`trading-feed`)**: Render Web Service. `autoDeploy: false` (`render.yaml:141`). Market data ingest microservice.
* **Cloudflare Edge Worker (`tradeai-pro`)**: Cloudflare Workers platform. Deployed via `npx wrangler deploy` (`wrangler.jsonc:1-26`).

### Cloudflare Worker Cron Matrix:
Defined in `wrangler.jsonc:7-10`:
1. `*/2 3-10 * * 1-5`: Fires every 2 minutes Monday–Friday between 03:00 and 10:58 UTC (08:30 to 16:28 IST) covering the entire NSE pre-open, trading, and post-market window.
2. `*/10 * * * *`: Fires every 10 minutes 24/7/365 to maintain anti-sleep keep-warm pings on Render free-tier containers.

### Cron Execution Actions (`src/worker.js:933-1161`):
1. **Box A Liveness Keep-Warm**: Pings `GET https://trading-api-n1wt.onrender.com/api/health/live` (`src/worker.js:1075`).
2. **Box B Liveness Keep-Warm**: Pings `GET https://trading-ml-8umr.onrender.com/health/live` (`src/worker.js:1088`).
3. **Box C Liveness Keep-Warm**: Pings `GET https://trading-feed.onrender.com/health/live` (`src/worker.js:1103`).
4. **Market Refresh Trigger**: Sends authenticated `POST https://trading-api-n1wt.onrender.com/api/cron/refresh` with header `X-Cron-Secret: env.CRON_SECRET` (`src/worker.js:1125`).
5. **Edge Cache Pre-Warming**: Pre-fills shared cache across 8 primary dashboard endpoints (`EDGE_WARM_PATHS`, `src/worker.js:1005-1014`) during the IST pre-open window (08:30 to 09:45 IST, `src/worker.js:1019-1020`).
6. **Autonomous Cycle Dispatch**: Calls GitHub API `POST /repos/Himanshugupta2024/TradeAI-PRO/actions/workflows/autonomous_cycle.yml/dispatches` every 20 minutes during live market hours (`src/worker.js:940, 974`).
